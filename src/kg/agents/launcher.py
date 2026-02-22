"""kg agent launcher — per-node process supervisor for Claude Code agents.

Reads .kg/agents/<name>.toml (git-tracked) and manages agent processes for
agents allocated to this node (KG_NODE_NAME env var).

Agent TOML format (.kg/agents/<name>.toml):
    name = "alice"
    node = "local"              # node that runs this agent
    auto_start = true           # start on launcher boot
    restart = "always"          # always | on-failure | never
    wake_on_message = true      # start when idle + pending messages
    model = ""                  # empty = claude default
    working_dir = ""            # empty = kg_root

Lifecycle:
  - Agents on this node: start (if auto_start), monitor, restart (per policy)
  - Agents on OTHER nodes that are running here: kill (reallocated)
  - New .toml files: picked up on next git pull cycle
  - Deleted .toml: agent stopped
  - Changed node: old node kills, new node starts
"""

from __future__ import annotations

import json
import logging
import os
import random
import signal
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kg.config import KGConfig

log = logging.getLogger("kg.launcher")


# ─── Agent definition ─────────────────────────────────────────────────────────


@dataclass
class AgentDef:
    """Parsed from .kg/agents/<name>.toml."""
    name: str
    node: str
    auto_start: bool = True
    restart: str = "always"        # always | on-failure | never
    wake_on_message: bool = True
    model: str = ""
    working_dir: str = ""
    status: str = "running"        # running | paused | draining

    @classmethod
    def from_toml(cls, path: Path) -> AgentDef:
        with path.open("rb") as f:
            data = tomllib.load(f)
        return cls(
            name=str(data.get("name", path.stem)),
            node=str(data.get("node", "")),
            auto_start=bool(data.get("auto_start", True)),
            restart=str(data.get("restart", "always")),
            wake_on_message=bool(data.get("wake_on_message", True)),
            model=str(data.get("model", "")),
            working_dir=str(data.get("working_dir", "")),
            status=str(data.get("status", "running")),
        )

    def toml_str(self) -> str:
        lines = [
            f'name = "{self.name}"',
            f'node = "{self.node}"',
            f'auto_start = {str(self.auto_start).lower()}',
            f'restart = "{self.restart}"',
            f'wake_on_message = {str(self.wake_on_message).lower()}',
        ]
        if self.model:
            lines.append(f'model = "{self.model}"')
        if self.working_dir:
            lines.append(f'working_dir = "{self.working_dir}"')
        if self.status != "running":
            lines.append(f'status = "{self.status}"')
        return "\n".join(lines) + "\n"


# ─── Running agent state ──────────────────────────────────────────────────────


@dataclass
class ManagedAgent:
    defn: AgentDef
    proc: subprocess.Popen | None = None
    restart_count: int = 0
    next_start_after: float = 0.0   # epoch seconds, for backoff
    last_exit_code: int | None = None


# ─── Backoff ──────────────────────────────────────────────────────────────────


def _backoff_delay(restart_count: int, max_delay: float = 60.0) -> float:
    """Exponential backoff with ±20% jitter."""
    base = min(2.0 ** restart_count, max_delay)
    jitter = random.uniform(-base * 0.2, base * 0.2)
    return max(1.0, base + jitter)


# ─── Mux helpers ──────────────────────────────────────────────────────────────


def _has_pending_messages(mux_url: str, agent_name: str) -> bool:
    """Check if agent has pending messages without consuming them."""
    import urllib.request
    try:
        url = f"{mux_url}/agent/{agent_name}/pending"
        with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310
            data = json.loads(resp.read())
            return bool(data.get("messages"))
    except Exception:
        return False


# ─── Launcher ─────────────────────────────────────────────────────────────────


class Launcher:
    def __init__(self, cfg: KGConfig, node_name: str) -> None:
        self.cfg = cfg
        self.node_name = node_name
        self.agents_dir = cfg.root / ".kg" / "agents"
        self.managed: dict[str, ManagedAgent] = {}   # name → ManagedAgent
        self._stop = False

    # ── public ────────────────────────────────────────────────────────────────

    def run(self, poll_interval: float = 30.0) -> None:
        log.info("launcher starting on node=%s", self.node_name)
        signal.signal(signal.SIGTERM, self._on_sigterm)
        signal.signal(signal.SIGINT, self._on_sigterm)

        try:
            self._run_inotify(poll_interval)
        except ImportError:
            log.info("inotify_simple unavailable — using pure poll mode")
            self._run_poll(poll_interval)

        self._shutdown()

    def _run_inotify(self, poll_interval: float) -> None:
        """Event-driven loop: inotify on agents_dir + periodic git-pull cycle."""
        import inotify_simple  # type: ignore[import]  # raises ImportError on macOS/Docker

        inotify = inotify_simple.INotify()
        flags = inotify_simple.flags  # type: ignore[attr-defined]

        self.agents_dir.mkdir(parents=True, exist_ok=True)
        inotify.add_watch(
            str(self.agents_dir),
            flags.CLOSE_WRITE | flags.MOVED_TO | flags.MOVED_FROM | flags.DELETE | flags.CREATE,
        )
        log.info("inotify watching %s (poll every %.0fs)", self.agents_dir, poll_interval)

        # Force immediate first poll cycle
        last_poll = time.monotonic() - poll_interval

        try:
            while not self._stop:
                now = time.monotonic()
                time_until_poll = max(0.0, last_poll + poll_interval - now)
                timeout_ms = int(min(time_until_poll, 1.0) * 1000)

                events = inotify.read(timeout=timeout_ms)

                # Inotify: re-sync immediately on any .toml change
                if any(e.name.endswith(".toml") for e in events):
                    log.info("agents dir changed — resyncing definitions")
                    try:
                        self._sync_definitions()
                        self._reap_and_restart()
                        self._wake_on_message()
                    except Exception as exc:
                        log.warning("sync after inotify event failed: %s", exc)

                # Periodic cycle: git pull + full sync
                now = time.monotonic()
                if now >= last_poll + poll_interval:
                    try:
                        self._git_pull()
                        self._sync_definitions()
                        self._reap_and_restart()
                        self._wake_on_message()
                    except Exception as exc:
                        log.warning("launcher poll cycle error: %s", exc)
                    last_poll = now
        finally:
            inotify.close()

    def _run_poll(self, poll_interval: float) -> None:
        """Pure polling fallback (macOS, Docker, no inotify_simple)."""
        log.info("polling agents_dir every %.0fs", poll_interval)
        while not self._stop:
            try:
                self._git_pull()
                self._sync_definitions()
                self._reap_and_restart()
                self._wake_on_message()
            except Exception as exc:
                log.warning("launcher cycle error: %s", exc)
            self._sleep_interruptible(poll_interval)

    def start_agent(self, name: str) -> bool:
        """Manually start a specific agent. Returns True if started."""
        ma = self.managed.get(name)
        if ma is None:
            log.warning("agent %s not managed by this node", name)
            return False
        if ma.proc and ma.proc.poll() is None:
            log.info("agent %s already running (pid %d)", name, ma.proc.pid)
            return False
        self._launch(ma)
        return True

    def stop_agent(self, name: str, timeout: float = 5.0) -> bool:
        ma = self.managed.get(name)
        if ma is None or ma.proc is None:
            return False
        self._terminate(ma, timeout)
        return True

    def status(self) -> list[dict]:
        rows = []
        for name, ma in self.managed.items():
            pid = ma.proc.pid if ma.proc else None
            running = ma.proc is not None and ma.proc.poll() is None
            rows.append({
                "name": name,
                "node": ma.defn.node,
                "running": running,
                "pid": pid if running else None,
                "restart_count": ma.restart_count,
                "last_exit_code": ma.last_exit_code,
            })
        return sorted(rows, key=lambda r: r["name"])

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def _sync_definitions(self) -> None:
        """Load/reload .toml files; kill agents reallocated away from this node."""
        current_names: set[str] = set()

        for path in sorted(self.agents_dir.glob("*.toml")):
            try:
                defn = AgentDef.from_toml(path)
            except Exception as exc:
                log.warning("failed to parse %s: %s", path, exc)
                continue

            if defn.node != self.node_name:
                # Not ours — kill if we somehow started it
                if defn.name in self.managed:
                    log.info("agent %s reallocated to %s — stopping", defn.name, defn.node)
                    self._terminate(self.managed.pop(defn.name))
                continue

            current_names.add(defn.name)

            if defn.name not in self.managed:
                # New agent for this node
                ma = ManagedAgent(defn=defn)
                self.managed[defn.name] = ma
                if defn.auto_start and defn.status == "running":
                    self._launch(ma)
            else:
                ma = self.managed[defn.name]
                ma.defn = defn
                # Paused: terminate immediately if running
                if defn.status == "paused" and ma.proc and ma.proc.poll() is None:
                    log.info("agent %s paused — terminating", defn.name)
                    self._terminate(ma)

        # Remove agents whose .toml was deleted
        for name in list(self.managed):
            if name not in current_names:
                log.info("agent %s toml deleted — stopping", name)
                self._terminate(self.managed.pop(name))

    def _reap_and_restart(self) -> None:
        """Check for exited processes and restart per policy."""
        now = time.monotonic()
        for name, ma in list(self.managed.items()):
            if ma.proc is None:
                continue
            rc = ma.proc.poll()
            if rc is None:
                continue  # still running

            ma.last_exit_code = rc
            ma.proc = None
            log.info("agent %s exited (rc=%d, restarts=%d)", name, rc, ma.restart_count)

            # Paused or draining: do not restart
            if ma.defn.status in ("paused", "draining"):
                log.info("agent %s status=%s — not restarting", name, ma.defn.status)
                continue

            should_restart = (
                ma.defn.restart == "always"
                or (ma.defn.restart == "on-failure" and rc != 0)
            )
            if should_restart and now >= ma.next_start_after:
                delay = _backoff_delay(ma.restart_count)
                ma.next_start_after = now + delay
                log.info("agent %s restart in %.1fs", name, delay)
            elif should_restart:
                pass  # waiting for backoff
            # "never" or backoff in progress → leave idle

    def _wake_on_message(self) -> None:
        """Start idle agents that have pending messages (wake_on_message=true)."""
        now = time.monotonic()
        for name, ma in self.managed.items():
            if ma.defn.status != "running":
                continue  # paused or draining — don't wake
            if not ma.defn.wake_on_message:
                continue
            if ma.proc and ma.proc.poll() is None:
                continue  # already running
            if now < ma.next_start_after:
                continue  # in backoff
            if _has_pending_messages(self.cfg.agents.mux_url, name):
                log.info("agent %s has pending messages — waking", name)
                self._launch(ma)

    def _launch(self, ma: ManagedAgent) -> None:
        if ma.defn.status != "running":
            log.debug("agent %s status=%s — skipping launch", ma.defn.name, ma.defn.status)
            return
        defn = ma.defn
        env = os.environ.copy()
        env["KG_AGENT_NAME"] = defn.name
        if defn.model:
            env["ANTHROPIC_MODEL"] = defn.model

        cmd = ["claude", "--dangerously-skip-permissions"]
        cwd = defn.working_dir or str(self.cfg.root)

        try:
            proc = subprocess.Popen(
                cmd, env=env, cwd=cwd,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            ma.proc = proc
            ma.restart_count += 1
            ma.next_start_after = 0.0
            log.info("launched agent %s (pid %d)", defn.name, proc.pid)
        except Exception as exc:
            log.error("failed to launch agent %s: %s", defn.name, exc)

    def _terminate(self, ma: ManagedAgent, timeout: float = 5.0) -> None:
        if ma.proc is None:
            return
        try:
            ma.proc.terminate()
            try:
                ma.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                ma.proc.kill()
                ma.proc.wait()
        except Exception:
            pass
        ma.proc = None

    def _shutdown(self) -> None:
        log.info("launcher shutting down — stopping all agents")
        for ma in self.managed.values():
            self._terminate(ma)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _git_pull(self) -> None:
        try:
            subprocess.run(
                ["git", "pull", "--rebase", "--autostash"],
                cwd=str(self.cfg.root),
                capture_output=True,
                timeout=30,
                check=False,
            )
        except Exception:
            pass  # offline / no remote — continue with local files

    def _sleep_interruptible(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._stop and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))

    def _on_sigterm(self, *_: object) -> None:
        self._stop = True


# ─── CLI-callable helpers ─────────────────────────────────────────────────────


_LAUNCHER_LOG = Path.home() / ".local" / "share" / "kg" / "launcher.log"
_LAUNCHER_PID = Path.home() / ".local" / "share" / "kg" / ".launcher.pid"


def start_background(cfg: KGConfig, node_name: str) -> tuple[bool, str]:
    if _LAUNCHER_PID.exists():
        try:
            pid = int(_LAUNCHER_PID.read_text().strip())
            os.kill(pid, 0)
            return True, f"launcher already running (pid {pid})"
        except (ProcessLookupError, ValueError):
            _LAUNCHER_PID.unlink(missing_ok=True)

    _LAUNCHER_PID.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-m", "kg.agents.launcher",
         "--root", str(cfg.root), "--node", node_name],
        start_new_session=True,
        stdout=_LAUNCHER_LOG.open("a"),
        stderr=subprocess.STDOUT,
    )
    _LAUNCHER_PID.write_text(str(proc.pid))
    time.sleep(0.3)
    try:
        os.kill(proc.pid, 0)
        return True, f"launcher started (pid {proc.pid}) node={node_name}"
    except ProcessLookupError:
        _LAUNCHER_PID.unlink(missing_ok=True)
        return False, f"launcher failed to start — check {_LAUNCHER_LOG}"


def stop_background() -> tuple[bool, str]:
    if not _LAUNCHER_PID.exists():
        return True, "launcher not running"
    try:
        pid = int(_LAUNCHER_PID.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        _LAUNCHER_PID.unlink(missing_ok=True)
        return True, f"launcher stopped (pid {pid})"
    except (ProcessLookupError, ValueError):
        _LAUNCHER_PID.unlink(missing_ok=True)
        return True, "launcher was not running (removed stale pid)"


def launcher_status() -> str:
    if not _LAUNCHER_PID.exists():
        return "stopped"
    try:
        pid = int(_LAUNCHER_PID.read_text().strip())
        os.kill(pid, 0)
        return f"running (pid {pid})"
    except (ProcessLookupError, ValueError):
        return "stopped (stale pid)"


def create_agent_def(
    cfg: KGConfig,
    name: str,
    node: str,
    *,
    auto_start: bool = True,
    restart: str = "always",
    wake_on_message: bool = True,
    model: str = "",
    status: str = "running",
) -> Path:
    """Write .kg/agents/<name>.toml and return its path."""
    agents_dir = cfg.root / ".kg" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    defn = AgentDef(
        name=name, node=node, auto_start=auto_start,
        restart=restart, wake_on_message=wake_on_message, model=model,
        status=status,
    )
    path = agents_dir / f"{name}.toml"
    path.write_text(defn.toml_str())
    return path


def update_agent_def(cfg: KGConfig, name: str, **kwargs: object) -> AgentDef:
    """Read, patch, and rewrite a .kg/agents/<name>.toml. Returns updated AgentDef."""
    agents_dir = cfg.root / ".kg" / "agents"
    path = agents_dir / f"{name}.toml"
    if not path.exists():
        msg = f"No agent TOML found at {path}"
        raise FileNotFoundError(msg)
    defn = AgentDef.from_toml(path)
    for k, v in kwargs.items():
        if hasattr(defn, k):
            setattr(defn, k, v)
    path.write_text(defn.toml_str())
    return defn


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--poll", type=float, default=30.0)
    args = parser.parse_args()

    # KG_LAUNCHER_POLL env var overrides --poll (useful for testing / per-node tuning)
    poll = float(os.environ.get("KG_LAUNCHER_POLL", args.poll))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    from kg.config import load_config
    _cfg = load_config(Path(args.root))
    Launcher(_cfg, args.node).run(poll)
