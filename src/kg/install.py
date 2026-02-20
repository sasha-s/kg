"""Setup helpers: MCP registration, hook installation, health checks.

Used by `kg start`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kg.config import KGConfig

_HOOK_COMMAND = "python -m kg.hooks.session_context"
_STOP_HOOK_COMMAND = "python -m kg.hooks.stop"
_MCP_SERVER_NAME = "kg"
_KG_HOOK_COMMANDS = {_HOOK_COMMAND, _STOP_HOOK_COMMAND}


# ---------------------------------------------------------------------------
# MCP registration via `claude mcp add`
# ---------------------------------------------------------------------------


def ensure_mcp_registered(scope: str = "user") -> tuple[bool, str]:
    """Register `kg serve` as an MCP server. Idempotent.

    Returns (success, message).
    """
    if not shutil.which("claude"):
        return False, "`claude` CLI not found — install Claude Code to register MCP server"

    # Check if already registered
    result = subprocess.run(
        ["claude", "mcp", "list"],
        capture_output=True,
        text=True,
        check=False,
    )
    if _MCP_SERVER_NAME in result.stdout:
        return True, f"MCP server '{_MCP_SERVER_NAME}' already registered"

    # Register
    kg_bin = shutil.which("kg")
    if kg_bin is None:
        return (
            False,
            "`kg` not found on PATH — install with `pip install kg` or `uv tool install kg`",
        )

    result = subprocess.run(
        ["claude", "mcp", "add", "--scope", scope, _MCP_SERVER_NAME, "--", kg_bin, "serve"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return True, f"MCP server '{_MCP_SERVER_NAME}' registered (scope={scope})"
    return False, f"Failed to register MCP: {result.stderr.strip()}"


# ---------------------------------------------------------------------------
# Hook installation into ~/.claude/settings.json
# ---------------------------------------------------------------------------


def _claude_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _load_settings(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def _save_settings(path: Path, settings: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")


def ensure_hook_installed() -> tuple[bool, str]:
    """Merge session_context hook into ~/.claude/settings.json. Idempotent."""
    path = _claude_settings_path()
    settings = _load_settings(path)

    hooks_section = settings.setdefault("hooks", {})
    ups_list = hooks_section.setdefault("UserPromptSubmit", [])

    # Check if already present
    for entry in ups_list:
        for h in entry.get("hooks", []):
            if h.get("command") == _HOOK_COMMAND:
                return True, "session_context hook already installed"

    # Append
    ups_list.append({"hooks": [{"type": "command", "command": _HOOK_COMMAND}]})
    _save_settings(path, settings)
    return True, f"session_context hook installed in {path}"


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def mcp_health(_cfg: KGConfig) -> str:
    """Quick health string."""
    if not shutil.which("claude"):
        return "claude CLI not found"
    result = subprocess.run(
        ["claude", "mcp", "list"],
        capture_output=True,
        text=True,
        check=False,
    )
    if _MCP_SERVER_NAME in result.stdout:
        return f"registered ('{_MCP_SERVER_NAME}')"
    return "not registered — run `kg start` to register"


def hook_status() -> str:
    """Return hook installation status string."""
    path = _claude_settings_path()
    settings = _load_settings(path)
    for entry in settings.get("hooks", {}).get("UserPromptSubmit", []):
        for h in entry.get("hooks", []):
            if h.get("command") == _HOOK_COMMAND:
                return "installed"
    return "not installed — run `kg start` to install"


def ensure_stop_hook_installed() -> tuple[bool, str]:
    """Merge stop hook into ~/.claude/settings.json under Stop event. Idempotent."""
    path = _claude_settings_path()
    settings = _load_settings(path)

    hooks_section = settings.setdefault("hooks", {})
    stop_list = hooks_section.setdefault("Stop", [])

    for entry in stop_list:
        for h in entry.get("hooks", []):
            if h.get("command") == _STOP_HOOK_COMMAND:
                return True, "stop hook already installed"

    stop_list.append({"hooks": [{"type": "command", "command": _STOP_HOOK_COMMAND}]})
    _save_settings(path, settings)
    return True, f"stop hook installed in {path}"


def list_all_hooks(settings_path: Path | None = None) -> list[dict[str, Any]]:
    """Return all hooks from ~/.claude/settings.json as a flat list.

    Each entry: {"event": str, "type": str, "command": str, "kg": bool}
    """
    path = settings_path or _claude_settings_path()
    settings = _load_settings(path)
    result = []
    for event, entries in settings.get("hooks", {}).items():
        for entry in entries:
            for h in entry.get("hooks", []):
                cmd = h.get("command", "")
                result.append(
                    {
                        "event": event,
                        "type": h.get("type", "command"),
                        "command": cmd,
                        "kg": cmd in _KG_HOOK_COMMANDS,
                    }
                )
    return result
