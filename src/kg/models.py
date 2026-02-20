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


# Threshold: flag for review when credits-per-bullet exceeds this value.
# credits-per-bullet ≈ serve_count * avg_chars_per_bullet (~200)
# so threshold=3000 ≈ flagged after ~15 context appearances per bullet.
_REVIEW_BUDGET_THRESHOLD = 3000.0
# Structural threshold: each time bullet count crosses one of these checkpoints
# (30, 45, 60, ...) a budget bomb is added, forcing a review flag.
# Clearing the budget (kg review <slug>) resets the checkpoint tracker so the
# next crossing at 45, 60, etc. fires again.
_REVIEW_BULLET_THRESHOLD = 30
_REVIEW_BULLET_STEP = _REVIEW_BULLET_THRESHOLD // 2  # 15


def structural_checkpoint(count: int) -> int | None:
    """Return the current structural checkpoint for bullet count, or None if below base.

    Checkpoints: 30, 45, 60, 75, ... (base + n * step).
    """
    if count < _REVIEW_BULLET_THRESHOLD:
        return None
    return (count // _REVIEW_BULLET_STEP) * _REVIEW_BULLET_STEP


@dataclass
class FileNode:
    """A node loaded from nodes/<slug>/node.jsonl."""

    slug: str
    title: str
    type: str                    # concept | task | decision | agent | session | …
    created_at: str = ""
    bullets: list[FileBullet] = field(default_factory=list)

    # Loaded from meta.jsonl node-level entry {"_node": slug, ...}
    token_budget: float = 0.0       # cumulative chars served in context
    last_reviewed: str = ""         # ISO timestamp of last explicit review

    @property
    def live_bullets(self) -> list[FileBullet]:
        """Bullets that have not been tombstoned."""
        return [b for b in self.bullets if not b.deleted]

    def credits_per_bullet(self, bullet_count: int | None = None) -> float:
        """Return token_budget normalised by bullet count."""
        count = bullet_count if bullet_count is not None else len(self.live_bullets)
        return self.token_budget / max(1, count)

    def needs_review(self, threshold: float = _REVIEW_BUDGET_THRESHOLD, bullet_count: int | None = None) -> bool:
        """True when credits-per-bullet exceeds threshold."""
        return self.credits_per_bullet(bullet_count) >= threshold

    def review_hint(self, threshold: float = _REVIEW_BUDGET_THRESHOLD, bullet_count: int | None = None) -> str | None:
        """Return inline hint string, or None if not needed."""
        if not self.needs_review(threshold, bullet_count):
            return None
        cpb = int(self.credits_per_bullet(bullet_count))
        return f"⚠ needs review ({int(self.token_budget)} credits, {cpb}/bullet)"

    def header_dict(self) -> dict[str, Any]:
        return {
            "v": 1,
            "slug": self.slug,
            "title": self.title,
            "type": self.type,
            "created_at": self.created_at or datetime.now(UTC).isoformat(),
        }
