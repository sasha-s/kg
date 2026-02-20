"""Transcript resolution and fingerprinting utilities.

Used by the HTTP server and CLI for session-based deduplication.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TranscriptFingerprint:
    """Content extracted from a transcript for dedup."""

    ids: set[str] = field(default_factory=set)  # bullet IDs, chunk IDs, node slugs
    text: str = ""  # full text blob for substring matching


def resolve_session_transcript(session_id: str) -> str | None:
    """Resolve a session ID (full UUID or prefix) to a transcript path.

    Returns most recently modified match. Warns to stderr if ambiguous.
    """
    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.exists():
        return None

    matches: list[Path] = []
    for project_dir in claude_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for f in project_dir.iterdir():
            if f.suffix == ".jsonl" and f.stem.startswith(session_id):
                matches.append(f)

    if not matches:
        return None
    if len(matches) > 1:
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        print(
            f"warning: '{session_id}' matches {len(matches)} sessions, using most recent",
            file=sys.stderr,
        )
    return str(matches[0])


def fingerprint_transcript(transcript_path: str) -> TranscriptFingerprint:
    """Extract IDs and full text from a Claude Code transcript.

    IDs: bullet-xxx, _chunk-xxx, [node-slug] references.
    Text: all text content concatenated for substring matching.
    """
    path = Path(transcript_path)
    if not path.exists():
        return TranscriptFingerprint()

    # Load full text (needed for substring dedup)
    text = path.read_text(errors="replace")

    # Extract IDs — try rg first (fast), fall back to regex
    # Slug pattern: min 2 chars before hyphen, min 2 chars after.
    # Filters Rich markup [bold], regex classes [a-f0-9], [a-z0-9-], etc.
    _slug_re = r"\[\[([a-z_][a-z0-9_]+-[a-z][a-z0-9_-]*[a-z0-9])\]\]"
    ids: set[str] = set()
    try:
        result = subprocess.run(
            [
                "rg",
                "-o",
                r"bullet-[a-f0-9]{12}|_?chunk-[a-f0-9]{12}|" + _slug_re,
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        for line in result.stdout.splitlines():
            line = line.strip().strip("[]")
            if line:
                ids.add(line)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # Fallback: Python regex
        ids.update(re.findall(r"bullet-[a-f0-9]{12}", text))
        ids.update(re.findall(r"_?chunk-[a-f0-9]{12}", text))
        ids.update(re.findall(_slug_re, text))

    return TranscriptFingerprint(ids=ids, text=text)


def extract_last_user_prompt(transcript_path: str) -> str:
    """Extract the last user prompt text from a Claude Code transcript.

    Skips tool_result-only messages. Returns empty string if not found.
    """
    path = Path(transcript_path)
    if not path.exists():
        return ""

    # Read lines in reverse to find last user message with text
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""

    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        if obj.get("type") != "user":
            continue

        msg = obj.get("message")
        if not msg:
            continue

        content = msg.get("content", "")

        # String content — direct user text
        if isinstance(content, str) and content.strip():
            return content.strip()

        # List content — look for text blocks (skip tool_result-only)
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if text.strip():
                        return text.strip()

    return ""
