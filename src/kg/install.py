"""Setup helpers: MCP registration, hook installation, health checks.

Used by `kg start`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kg.config import KGConfig

# Use sys.executable so the installed Python (with kg) runs the hook.
# Matches mg's pattern: f"{sys.executable} -m memory_graph.hooks.<name>"
_HOOK_COMMAND = f"{sys.executable} -m kg.hooks.session_context"
_STOP_HOOK_COMMAND = f"{sys.executable} -m kg.hooks.stop"
_MCP_SERVER_NAME = "kg"
# Module-name fragments used to recognize kg hooks regardless of Python path
_KG_HOOK_MODULES = {"kg.hooks.session_context", "kg.hooks.stop", "kg.agents.hooks"}

# Agent hook commands (installed when agents.enabled = true)
_AGENT_SESSION_START_CMD = f"{sys.executable} -m kg.agents.hooks session_start"
_AGENT_USER_PROMPT_CMD = f"{sys.executable} -m kg.agents.hooks user_prompt_submit"
_AGENT_POST_TOOL_CMD = f"{sys.executable} -m kg.agents.hooks post_tool_use"
_AGENT_STOP_CMD = f"{sys.executable} -m kg.agents.hooks stop"
_AGENT_SESSION_END_CMD = f"{sys.executable} -m kg.agents.hooks session_end"


def _is_kg_hook(command: str) -> bool:
    """True if command is a kg hook (regardless of which Python binary)."""
    return any(m in command for m in _KG_HOOK_MODULES)


# ---------------------------------------------------------------------------
# MCP registration via `claude mcp add`
# ---------------------------------------------------------------------------


def ensure_mcp_registered(scope: str = "user", root: Path | None = None) -> tuple[bool, str]:
    """Register `kg serve --root <root>` as an MCP server. Idempotent.

    Passes --root so the server works regardless of invocation cwd.
    Returns (success, message).
    """
    if not shutil.which("claude"):
        return False, "`claude` CLI not found — install Claude Code to register MCP server"

    root_path = str(root.resolve()) if root else str(Path.cwd().resolve())
    kg_bin = shutil.which("kg")
    if kg_bin is None:
        return (
            False,
            "`kg` not found on PATH — install with `pip install kg` or `uv tool install kg`",
        )

    expected_args = [kg_bin, "serve", "--root", root_path]

    # Check if already registered with the correct args
    result = subprocess.run(
        ["claude", "mcp", "list"],
        capture_output=True,
        text=True,
        check=False,
    )
    if _MCP_SERVER_NAME in result.stdout:
        # Check ~/.claude.json for correct root arg
        claude_json = Path.home() / ".claude.json"
        if claude_json.exists():
            try:
                data = json.loads(claude_json.read_text())
                existing = data.get("mcpServers", {}).get(_MCP_SERVER_NAME, {})
                if existing.get("args") == ["serve", "--root", root_path]:
                    return True, f"MCP server '{_MCP_SERVER_NAME}' already registered"
            except Exception:  # noqa: S110
                pass
        # Re-register with updated root — remove first
        subprocess.run(
            ["claude", "mcp", "remove", _MCP_SERVER_NAME],
            capture_output=True,
            check=False,
        )

    # Register with --root
    result = subprocess.run(
        ["claude", "mcp", "add", "--scope", scope, _MCP_SERVER_NAME, "--", *expected_args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return True, f"MCP server '{_MCP_SERVER_NAME}' registered --root {root_path} (scope={scope})"
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

    # Check if already present (any Python path)
    for entry in ups_list:
        for h in entry.get("hooks", []):
            if "kg.hooks.session_context" in h.get("command", ""):
                # Update stale command to use current sys.executable
                if h["command"] != _HOOK_COMMAND:
                    h["command"] = _HOOK_COMMAND
                    _save_settings(path, settings)
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
            if "kg.hooks.session_context" in h.get("command", ""):
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
            if "kg.hooks.stop" in h.get("command", ""):
                # Update stale command to use current sys.executable
                if h["command"] != _STOP_HOOK_COMMAND:
                    h["command"] = _STOP_HOOK_COMMAND
                    _save_settings(path, settings)
                return True, "stop hook already installed"

    stop_list.append({"hooks": [{"type": "command", "command": _STOP_HOOK_COMMAND}]})
    _save_settings(path, settings)
    return True, f"stop hook installed in {path}"


def ensure_dot_claude_symlink(cfg: KGConfig) -> tuple[bool, str]:
    """Create <project_root>/.claude → .kg symlink. Idempotent.

    This lets Claude Code find local settings/commands in .kg/ when running
    inside the project directory, same pattern as mg's .claude → .memory_graph.
    """
    dot_claude = cfg.root / ".claude"
    target_name = cfg.index_dir.parent.name  # ".kg" (relative)

    if dot_claude.is_symlink():
        if dot_claude.resolve() == cfg.kg_dir.resolve():
            return True, f".claude symlink already points to {target_name}"
        return False, f".claude symlink exists but points elsewhere ({dot_claude.readlink()}), skipping"
    if dot_claude.exists():
        return False, ".claude exists as a real directory — skipping symlink creation"

    dot_claude.symlink_to(target_name)
    return True, f"Created .claude → {target_name}"


def ensure_agent_hooks_installed(cfg: Any) -> list[tuple[bool, str]]:
    """Install agent hooks into ~/.claude/settings.json. Idempotent.

    Requires cfg.agents.enabled=True and cfg.agents.name set.
    Installs: SessionStart, UserPromptSubmit, PostToolUse (async), Stop, SessionEnd.
    """
    if not cfg.agents.enabled or not cfg.agents.name:
        return [(False, "agents.enabled=false or agents.name not set — skipping")]

    path = _claude_settings_path()
    settings = _load_settings(path)
    hooks_section = settings.setdefault("hooks", {})
    results: list[tuple[bool, str]] = []
    changed = False

    def _ensure(event: str, cmd: str, marker: str, *, async_hook: bool = False, matcher: str = "") -> None:
        nonlocal changed
        entry_list = hooks_section.setdefault(event, [])
        already = any(
            marker in h.get("command", "")
            for entry in entry_list
            for h in entry.get("hooks", [])
        )
        if already:
            results.append((True, f"{event}: already installed"))
            return
        hook: dict[str, Any] = {"type": "command", "command": cmd}
        if async_hook:
            hook["async"] = True
        item: dict[str, Any] = {"hooks": [hook]}
        if matcher:
            item["matcher"] = matcher
        entry_list.append(item)
        results.append((True, f"{event}: installed"))
        changed = True

    _ensure("SessionStart", _AGENT_SESSION_START_CMD, "session_start")
    _ensure("UserPromptSubmit", _AGENT_USER_PROMPT_CMD, "user_prompt_submit")
    _ensure("PostToolUse", _AGENT_POST_TOOL_CMD, "post_tool_use", async_hook=True, matcher=".*")
    _ensure("Stop", _AGENT_STOP_CMD, "agents.hooks stop")
    _ensure("SessionEnd", _AGENT_SESSION_END_CMD, "session_end")

    if changed:
        _save_settings(path, settings)
    return results


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
                        "kg": _is_kg_hook(cmd),
                    }
                )
    return result
