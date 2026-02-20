"""Data models for file-based node store."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def new_bullet_id() -> str:
    """Generate a stable, compact bullet ID: b-<8 hex chars>."""
    return "b-" + uuid.uuid4().hex[:8]


@dataclass
class FileBullet:
    """A single bullet line from node.jsonl."""

    id: str
    type: str                          # fact | gotcha | decision | task | note | success | failure
    text: str
    created_at: str = ""
    status: str | None = None          # pending | completed | archived (tasks)
    deleted: bool = False

    # Vote state (loaded from meta.jsonl, not stored in node.jsonl)
    useful: int = 0
    harmful: int = 0
    used: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FileBullet:
        return cls(
            id=d["id"],
            type=d.get("type", "fact"),
            text=d.get("text", ""),
            created_at=d.get("created_at", ""),
            status=d.get("status"),
            deleted=d.get("deleted", False),
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "text": self.text,
        }
        if self.status:
            d["status"] = self.status
        if self.created_at:
            d["created_at"] = self.created_at
        return d

    def to_tombstone(self) -> dict[str, Any]:
        return {"id": self.id, "deleted": True}


@dataclass
class FileNode:
    """A node loaded from nodes/<slug>/node.jsonl."""

    slug: str
    title: str
    type: str                    # concept | task | decision | agent | session | …
    created_at: str = ""
    bullets: list[FileBullet] = field(default_factory=list)

    @property
    def live_bullets(self) -> list[FileBullet]:
        """Bullets that have not been tombstoned."""
        return [b for b in self.bullets if not b.deleted]

    def header_dict(self) -> dict[str, Any]:
        return {
            "v": 1,
            "slug": self.slug,
            "title": self.title,
            "type": self.type,
            "created_at": self.created_at or datetime.now(UTC).isoformat(),
        }
