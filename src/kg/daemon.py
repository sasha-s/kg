"""Watcher daemon management: supervisord-first, PID-file fallback.

supervisord is preferred when available (process restart on crash, log rotation).
Falls back to a background subprocess with a PID file in .kg/.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from kg.config import KGConfig

_SUPERVISORD_PROGRAM = "kg-watcher"


# ---------------------------------------------------------------------------
# supervisord path
# ---------------------------------------------------------------------------

def _supervisord_conf_path(cfg: KGConfig) -> Path:
    return cfg.index_dir / "supervisord.conf"


def _supervisord_pid_path(cfg: KGConfig) -> Path:
    return cfg.index_dir / "supervisord.pid"


def _generate_supervisord_conf(cfg: KGConfig) -> Path:
    conf_path = _supervisord_conf_path(cfg)
    log_dir = cfg.index_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    python = sys.executable
    conf = f"""\
[supervisord]
pidfile={_supervisord_pid_path(cfg)}
logfile={log_dir / "supervisord.log"}
logfile_maxbytes=5MB
logfile_backups=3
loglevel=info
nodaemon=false
silent=true

[supervisorctl]
serverurl=unix://{cfg.index_dir / "supervisor.sock"}

[unix_http_server]
file={cfg.index_dir / "supervisor.sock"}
chmod=0700

[rpcinterface:supervisor]
supervisor.rpcinterface_factory=supervisor.rpcinterface:make_main_rpcinterface

[program:{_SUPERVISORD_PROGRAM}]
command={python} -m kg.watcher {cfg.root}
autostart=true
autorestart=true
startretries=5
stdout_logfile={log_dir / "watcher.log"}
stderr_logfile={log_dir / "watcher.log"}
stdout_logfile_maxbytes=2MB
stdout_logfile_backups=2
"""
    conf_path.write_text(conf)
    return conf_path


def _supervisord_running(cfg: KGConfig) -> bool:
    pid_path = _supervisord_pid_path(cfg)
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False


def _start_supervisord(cfg: KGConfig) -> bool:
    """Generate config and start supervisord. Returns True on success."""
    try:
        import supervisord  # type: ignore[import-untyped]  # noqa: F401, PLC0415 — optional dep check
    except ImportError:
        return False

    conf_path = _generate_supervisord_conf(cfg)
    result = subprocess.run(
        ["supervisord", "-c", str(conf_path)],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# PID file fallback
# ---------------------------------------------------------------------------

def _pid_file(cfg: KGConfig) -> Path:
    return cfg.index_dir / "watcher.pid"


def _pid_running(pid_file: Path) -> bool:
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False


def _start_bg_watcher(cfg: KGConfig) -> int:
    """Start watcher as background subprocess. Returns PID."""
    pid_file = _pid_file(cfg)
    log_file = cfg.index_dir / "logs" / "watcher.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    with log_file.open("a") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "kg.watcher", str(cfg.root)],
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    pid_file.write_text(str(proc.pid))
    return proc.pid


def _stop_bg_watcher(cfg: KGConfig) -> bool:
    pid_file = _pid_file(cfg)
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        pid_file.unlink(missing_ok=True)
        return True
    except (ValueError, ProcessLookupError):
        pid_file.unlink(missing_ok=True)
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ensure_watcher(cfg: KGConfig) -> tuple[str, str]:
    """Ensure watcher is running. Returns (method, status) for display."""
    # Try supervisord first
    if _supervisord_running(cfg):
        return ("supervisord", "already running")

    if _start_supervisord(cfg):
        return ("supervisord", "started")

    # Fall back to PID file
    pid_file = _pid_file(cfg)
    if _pid_running(pid_file):
        pid = int(pid_file.read_text().strip())
        return ("bg-process", f"already running (pid {pid})")

    pid = _start_bg_watcher(cfg)
    return ("bg-process", f"started (pid {pid})")


def watcher_status(cfg: KGConfig) -> str:
    if _supervisord_running(cfg):
        return "running via supervisord"
    pid_file = _pid_file(cfg)
    if _pid_running(pid_file):
        pid = int(pid_file.read_text().strip())
        return f"running (pid {pid})"
    return "stopped"


def stop_watcher(cfg: KGConfig) -> str:
    if _stop_bg_watcher(cfg):
        return "stopped"
    # supervisord: let user handle it
    if _supervisord_running(cfg):
        return "supervisord manages watcher — use supervisorctl to stop"
    return "not running"
