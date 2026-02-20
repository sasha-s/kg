"""Claude Code Stop hook: spawn background session for knowledge extraction.

Fires after each Claude response. Spawns a detached `claude -p --resume`
subprocess that reads the full session transcript as context, then:
  1. Fills gaps in fleeting notes
  2. Promotes durable knowledge to concept nodes
  3. Searches relevant topics and votes on existing bullets
  4. Records session anchor (intent + outcome)
  5. Cleans up raw capture bullets

This is the same pattern as mg's sidechain extraction — no turn/agent nodes,
just lightweight graph maintenance.

Enabled by default. Disable in kg.toml:
    [hooks]
    stop = false

Install in ~/.claude/settings.json:
    {
      "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "python -m kg.hooks.stop"}]}]
      }
    }

Or run `kg start` — installs automatically.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_LOG_DIR = Path.home() / ".kg" / "logs"
_KG_NO_STOP_HOOK = "KG_NO_STOP_HOOK"

_SYSTEM_PROMPT = """\
STOP. Do not continue the conversation above — it is CONTEXT ONLY.
You are a knowledge extractor for kg (knowledge graph). Extract durable knowledge \
and maintain the graph using `kg` CLI commands.

The kg graph is a Zettelkasten — atomic concept nodes (JSONL files) linked by \
cross-references like [slug]. Your job: process fleeting notes from the session \
and promote durable knowledge into this network.

---

## Step 0: Check fleeting notes + fill gaps

Look for the session's fleeting node (session ID is given in your prompt):
```
kg show _fleeting-<short_id>
```

If it exists, read all bullets. Then scan the transcript above for discoveries NOT yet captured:
- Confirmed facts, gotchas, decisions made
- Hypotheses validated or invalidated
- Patterns that worked or failed

Add any missing captures:
```
kg add _fleeting-<session_id[:12]> "missed: X causes Y"
kg add _fleeting-<session_id[:12]> "confirmed: Z works because W" --type fact
```

---

## Step 1: Search for relevant context

For each distinct topic from the fleeting node (or transcript if no fleeting node):
```
kg context "topic from the session"
kg context "another topic"
```
Search every topic the session touched. Note `←b-xxxxxxxx` bullet IDs for voting.

---

## Step 2: Promote fleeting → concept nodes

For each fleeting bullet, decide:

**Promote** (add to a concept node) if:
- Reusable beyond this session
- No duplicate already exists on the target node
- Would help someone working on this topic next week

**Discard** (skip) if:
- Purely transient: exact commands run, errors fixed in-session, step-by-step narrative
- Wouldn't help future sessions

**Defer** (leave in fleeting) if:
- Pattern not yet clear — needs more sessions to confirm

Before adding to a concept node, check if context output showed `⚠` on that node.
If so, the node is over-budget — call `kg review <slug>` after examining it to clear the flag.

Adding bullets (node auto-creates if it doesn't exist):
```
kg add <slug> "reusable insight" --type TYPE
```

Bullet types:
- `fact`     — How something works, API behavior, config
- `gotcha`   — Traps, surprises, non-obvious behavior
- `decision` — Choices with rationale (include why)
- `success`  — Patterns that worked
- `failure`  — Approaches that failed and why
- `note`     — Observations, context

Cross-link aggressively: `kg add asyncpg-patterns "LIKE is case-sensitive — use ILIKE [postgres-gotchas]"`

**NEVER add bullets about:**
- The extraction process itself ("verified", "completed", "all steps done")
- Exact commands run during the session (ephemeral)
- Transient errors that were fixed in-session

---

## Step 3: Review over-budget nodes

For any node that showed `⚠` in context output — examine it with `kg show <slug>`.
After reviewing (not necessarily changing anything), mark it reviewed to clear the flag:
```
kg review <slug>
```

---

## Step 4: Record session anchor

Add 2-4 summary bullets to `_fleeting-<session_id[:12]>` describing what this session was:
```
kg add _fleeting-<session_id[:12]> "intent: <one-line goal summary>" --type note
kg add _fleeting-<session_id[:12]> "outcome: <what was done/decided/discovered>" --type note
# Optional:
kg add _fleeting-<session_id[:12]> "gotcha: <non-obvious issue>" --type gotcha
kg add _fleeting-<session_id[:12]> "pending: <what still needs doing>" --type task --status pending
```

Rules:
- `intent:` and `outcome:` are always required
- Keep to 2-4 bullets max — this is a navigation entry, not a full log
- These anchor bullets STAY after cleanup
- Do NOT add bullets about the extraction process itself

---

## Checklist

- [ ] Checked _fleeting-<session> for captures; filled any gaps
- [ ] Searched graph for every distinct topic touched
- [ ] Promoted worthy bullets to concept nodes (with [cross-refs])
- [ ] Called `kg review <slug>` on any over-budget nodes (⚠)
- [ ] Added session anchor (intent + outcome required)
"""


def _find_session_cwd(session_id: str) -> str | None:
    """Find the cwd from the session transcript file.

    claude stores transcripts at ~/.claude/projects/<cwd-hash>/<session_id>.jsonl.
    The <cwd-hash> is not trivially reversible, so we search all project dirs
    and read the cwd from the first line of the matching transcript.
    """
    projects_dir = Path.home() / ".claude" / "projects"
    for project_dir in projects_dir.iterdir():
        transcript = project_dir / f"{session_id}.jsonl"
        if transcript.exists():
            try:
                with transcript.open() as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        entry = json.loads(line)
                        cwd = entry.get("cwd")
                        if cwd:
                            return cwd
            except Exception:  # noqa: S110
                pass
    return None


def _log(session_id: str, msg: str) -> None:
    try:
        import datetime

        ts = datetime.datetime.now(tz=datetime.UTC).strftime("%H:%M:%S")
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = _LOG_DIR / f"stop-{session_id[:12]}.log"
        with log_file.open("a") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:  # noqa: S110
        pass


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    if data.get("hook_event_name") != "Stop":
        sys.exit(0)

    # Prevent recursion
    if data.get("stop_hook_active") or os.environ.get(_KG_NO_STOP_HOOK):
        sys.exit(0)

    session_id: str = data.get("session_id", "")
    if not session_id:
        sys.exit(0)

    # Find the session transcript to get the original cwd.
    # claude --resume resolves sessions under ~/.claude/projects/<cwd-hash>/,
    # which is derived from the cwd when the session was CREATED — not the
    # hook event's cwd (which reflects the current working directory).
    hook_cwd = data.get("cwd", "")
    session_cwd = _find_session_cwd(session_id) or hook_cwd

    # Find kg config from cwd
    try:
        from kg.config import load_config

        cfg = load_config(Path(session_cwd) if session_cwd else None)
    except Exception:
        sys.exit(0)

    # Respect opt-out
    if not cfg.hooks.stop:
        sys.exit(0)

    # Throttle: skip if another extraction for this session spawned in the last 120s.
    # Stop fires after every response; we only need one extraction per ~2 min.
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    lock_file = _LOG_DIR / f"stop-{session_id[:12]}.lock"
    import time
    now_ts = time.time()
    try:
        if lock_file.exists():
            last = float(lock_file.read_text().strip())
            if now_ts - last < 120:
                sys.exit(0)
        lock_file.write_text(str(now_ts))
    except Exception:  # noqa: S110
        pass

    prompt_file = _LOG_DIR / f"stop-{session_id[:12]}-prompt.txt"
    log_file = _LOG_DIR / f"stop-{session_id[:12]}.log"

    try:
        prompt_file.write_text(_SYSTEM_PROMPT)
    except Exception as exc:
        _log(session_id, f"failed to write prompt file: {exc}")
        sys.exit(0)

    short_id = session_id[:12]
    # Build claude -p command: resume session as context, restricted kg tools, no persistence
    cmd = [
        "claude",
        "-p",
        (
            "STOP. The conversation above is CONTEXT ONLY — do not continue it. "
            "You are a KNOWLEDGE EXTRACTOR for kg. Read your system prompt for instructions. "
            f"Session ID: {session_id}  (short: {short_id}). "
            f"The fleeting node for this session is: _fleeting-{short_id}. "
            "Search the graph for relevant topics, process fleeting notes, "
            "and promote durable knowledge to concept nodes. Run kg commands now."
        ),
        "--append-system-prompt-file",
        str(prompt_file),
        "--model",
        "sonnet",
        "--allowedTools",
        "Bash(kg add *)",
        "Bash(kg show *)",
        "Bash(kg search *)",
        "Bash(kg context *)",
        "Bash(kg review *)",
        "Bash(kg create *)",
        "--disable-slash-commands",
        "--resume",
        session_id,
        "--no-session-persistence",
    ]

    # Env: strip CLAUDE* vars to avoid nested session conflicts, set guard flag
    env = os.environ.copy()
    for key in [k for k in env if k.startswith("CLAUDE")]:
        del env[key]
    env[_KG_NO_STOP_HOOK] = "1"

    # Must run from the session's original cwd so claude resolves the transcript
    run_cwd = session_cwd if session_cwd and Path(session_cwd).is_dir() else None

    try:
        log_fh = log_file.open("a")
        subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=log_fh,
            close_fds=True,
            start_new_session=True,
            env=env,
            cwd=run_cwd,
        )
        _log(session_id, f"spawned extraction (model=sonnet session={session_id[:12]} cwd={run_cwd})")
    except Exception as exc:
        _log(session_id, f"failed to spawn: {exc}")

    sys.exit(0)


if __name__ == "__main__":
    main()
