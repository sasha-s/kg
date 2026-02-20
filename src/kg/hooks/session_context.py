"""Claude Code hook: inject session_id into every prompt via additionalContext.

Install in ~/.claude/settings.json:
    {
      "hooks": {
        "UserPromptSubmit": [
          {
            "hooks": [
              {
                "type": "command",
                "command": "python -m kg.hooks.session_context"
              }
            ]
          }
        ]
      }
    }

Or alongside memory_graph — they both emit the same session_id key so either one works.
"""

from __future__ import annotations

import json
import sys


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    if data.get("hook_event_name") != "UserPromptSubmit":
        sys.exit(0)

    session_id = data.get("session_id", "")
    context: dict[str, str] = {"session_id": session_id}

    # Optionally forward cwd so kg can locate kg.toml
    cwd = data.get("cwd", "")
    if cwd:
        context["cwd"] = cwd

    print(json.dumps({  # noqa: T201
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": json.dumps(context),
        },
    }))


if __name__ == "__main__":
    main()
