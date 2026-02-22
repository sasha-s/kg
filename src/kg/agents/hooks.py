"""Agent hooks — invoked by Claude Code hooks infrastructure.

Usage (from hook command):
  python -m kg.agents.hooks <event>

Events:
  session_start       — SessionStart hook: inject all pending messages
  user_prompt_submit  — UserPromptSubmit hook: inject new pending messages
  post_tool_use       — PostToolUse hook: async heartbeat + urgent delivery
  stop                — Stop hook: ack messages or block for urgent
  session_end         — SessionEnd hook: mark idle on crash/exit

Each handler reads JSON from stdin (hook data from Claude Code) and writes
JSON to stdout (hook response). Exits 0 on success; silent fail if no config.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kg.config import KGConfig


# ─── Mux HTTP helper ──────────────────────────────────────────────────────────


def _mux(mux_url: str, method: str, path: str, body: dict | None = None) -> dict:
    """HTTP call to mux. Returns parsed JSON or {} on any error."""
    url = mux_url.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(  # noqa: S310
        url, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310
            return json.loads(resp.read())
    except Exception:
        return {}


# ─── Message formatting ───────────────────────────────────────────────────────


def _format_messages(messages: list[dict]) -> str:
    if not messages:
        return ""
    parts = ["=== AGENT MESSAGES ==="]
    for msg in messages:
        urgency = " [URGENT]" if msg.get("urgency") == "urgent" else ""
        ts = msg.get("timestamp", "")[:19]
        parts.append(
            f"\nFrom: {msg.get('from_agent', 'unknown')}{urgency} ({ts})\n{msg.get('body', '')}"
        )
    parts.append("\n=== END MESSAGES ===")
    return "\n".join(parts)


# ─── Hook handlers ────────────────────────────────────────────────────────────


def handle_session_start(hook_data: dict, cfg: KGConfig) -> None:
    """SessionStart: inject all pending messages as context."""
    result = _mux(
        cfg.agents.mux_url, "POST",
        f"/agent/{cfg.agents.name}/session-start",
        {"pid": os.getpid(), "session_id": hook_data.get("session_id", "")},
    )
    msgs = result.get("messages", [])
    if msgs:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": _format_messages(msgs),
            }
        }))


def handle_user_prompt_submit(hook_data: dict, cfg: KGConfig) -> None:  # noqa: ARG001
    """UserPromptSubmit: inject any new pending messages."""
    result = _mux(cfg.agents.mux_url, "GET", f"/agent/{cfg.agents.name}/pending")
    msgs = result.get("messages", [])
    if msgs:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": _format_messages(msgs),
            }
        }))


def handle_post_tool_use(hook_data: dict, cfg: KGConfig) -> None:
    """PostToolUse: heartbeat + urgent message delivery."""
    result = _mux(
        cfg.agents.mux_url, "POST",
        f"/agent/{cfg.agents.name}/heartbeat",
        {"pid": os.getpid(), "session_id": hook_data.get("session_id", "")},
    )
    ctx = result.get("additionalContext")
    if ctx:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": ctx,
            }
        }))


def handle_stop(hook_data: dict, cfg: KGConfig) -> None:
    """Stop: ack delivered messages or block for urgent."""
    result = _mux(
        cfg.agents.mux_url, "POST",
        f"/agent/{cfg.agents.name}/stop",
        {
            "stop_hook_active": bool(hook_data.get("stop_hook_active", False)),
            "session_id": hook_data.get("session_id", ""),
            "transcript_path": hook_data.get("transcript_path", ""),
        },
    )
    if result.get("decision") == "block":
        print(json.dumps({
            "decision": "block",
            "reason": result.get("reason", "Urgent message pending"),
        }))


def handle_session_end(hook_data: dict, cfg: KGConfig) -> None:  # noqa: ARG001
    """SessionEnd: mark agent idle on crash/exit (no ack — re-deliver next run)."""
    _mux(
        cfg.agents.mux_url, "POST",
        f"/agent/{cfg.agents.name}/session-end",
        {},
    )


# ─── Dispatch ─────────────────────────────────────────────────────────────────


def dispatch(event: str) -> None:
    """Load config and dispatch to the right handler. Silent on errors."""
    from kg.config import load_config

    try:
        cfg = load_config()
    except Exception:
        return  # no kg project — skip silently

    if not cfg.agents.enabled or not cfg.agents.name:
        return

    try:
        hook_data = json.load(sys.stdin)
    except Exception:
        hook_data = {}

    handlers = {
        "session_start": handle_session_start,
        "user_prompt_submit": handle_user_prompt_submit,
        "post_tool_use": handle_post_tool_use,
        "stop": handle_stop,
        "session_end": handle_session_end,
    }
    handler = handlers.get(event)
    if handler:
        handler(hook_data, cfg)


if __name__ == "__main__":
    _event = sys.argv[1].replace("-", "_") if len(sys.argv) > 1 else ""
    dispatch(_event)
