"""Read and write node.jsonl / meta.json files.

FileStore is the public API:
    store = FileStore("/path/to/nodes")
    node = store.get("asyncpg-patterns")
    store.add_bullet("asyncpg-patterns", type="gotcha", text="LIKE is case-sensitive")
    store.vote("b-abc12345", useful=True)

meta.json layout (single file, read-modify-write under flock):
    {
      "token_budget": 1234.0,
      "last_reviewed": "2026-...",
      "last_bullet_checkpoint": 0,
      "bullets": {
        "b-abc123": {"useful": 3, "harmful": 0, "used": 1, "updated_at": "..."}
      }
    }
"""

from __future__ import annotations

import contextlib
import fcntl
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from kg.models import FileBullet, FileNode, new_bullet_id

if TYPE_CHECKING:
    from collections.abc import Iterator


class FileStore:
    """JSONL-backed node store."""

    def __init__(self, nodes_dir: Path | str) -> None:
        self.nodes_dir = Path(nodes_dir)
        self.nodes_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    def _node_dir(self, slug: str) -> Path:
        return self.nodes_dir / slug

    def _node_path(self, slug: str) -> Path:
        return self._node_dir(slug) / "node.jsonl"

    def _meta_path(self, slug: str) -> Path:
        return self._node_dir(slug) / "meta.json"

    def _meta_path_legacy(self, slug: str) -> Path:
        return self._node_dir(slug) / "meta.jsonl"

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, slug: str) -> FileNode | None:
        """Load a node and its bullets (with vote state merged in)."""
        path = self._node_path(slug)
        if not path.exists():
            return None

        bullets: list[FileBullet] = []
        header: dict[str, Any] | None = None

        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if "v" in obj and "slug" in obj:
                    header = obj
                elif "id" in obj:
                    if obj.get("deleted"):
                        # Tombstone — mark any previously loaded bullet deleted
                        for b in bullets:
                            if b.id == obj["id"]:
                                b.deleted = True
                    else:
                        bullets.append(FileBullet.from_dict(obj))

        if header is None:
            return None

        node = FileNode(
            slug=header["slug"],
            title=header.get("title", slug),
            type=header.get("type", "concept"),
            created_at=header.get("created_at", ""),
            bullets=bullets,
        )

        # Merge vote state
        self._merge_meta(node)
        return node

    def _merge_meta(self, node: FileNode) -> None:
        """Load meta.json and attach vote counts + node-level budget to node."""
        meta = self._read_meta(node.slug)
        bullet_votes: dict[str, Any] = meta.get("bullets", {})
        for b in node.bullets:
            if b.id in bullet_votes:
                v = bullet_votes[b.id]
                b.useful = int(v.get("useful", 0))
                b.harmful = int(v.get("harmful", 0))
                b.used = int(v.get("used", 0))
        node.token_budget = float(meta.get("token_budget", 0.0))
        node.last_reviewed = meta.get("last_reviewed", "")

    def update_node_budget(self, slug: str, delta_chars: float) -> float:
        """Increment token_budget by delta_chars. Returns new total."""
        meta = self._read_meta(slug)
        new_budget = float(meta.get("token_budget", 0.0)) + delta_chars
        self._write_meta(slug, {**meta, "token_budget": new_budget})
        return new_budget

    def clear_node_budget(self, slug: str) -> None:
        """Mark node as reviewed: zero out budget, reset structural checkpoint."""
        meta = self._read_meta(slug)
        self._write_meta(slug, {
            **meta,
            "token_budget": 0.0,
            "last_bullet_checkpoint": 0,
            "last_reviewed": datetime.now(UTC).isoformat(),
        })

    def _read_meta(self, slug: str) -> dict[str, Any]:
        """Read meta.json. Falls back to migrating legacy meta.jsonl on first access."""
        path = self._meta_path(slug)
        if path.exists():
            try:
                with path.open() as f:
                    fcntl.flock(f, fcntl.LOCK_SH)
                    return json.load(f)  # type: ignore[no-any-return]
            except (json.JSONDecodeError, OSError):
                return {}
        # Migrate from legacy meta.jsonl if present
        legacy = self._meta_path_legacy(slug)
        if legacy.exists():
            return self._migrate_legacy_meta(slug, legacy)
        return {}

    def _write_meta(self, slug: str, data: dict[str, Any]) -> None:
        """Atomically write meta.json under exclusive flock."""
        path = self._meta_path(slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to tmp then rename for atomicity
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            json.dump(data, f, indent=2)
        tmp.replace(path)

    def _migrate_legacy_meta(self, slug: str, legacy: Path) -> dict[str, Any]:
        """Read legacy meta.jsonl and write out meta.json. One-time migration."""
        votes: dict[str, Any] = {}
        node_meta: dict[str, Any] = {}
        try:
            with legacy.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "_node" in obj:
                        node_meta = obj
                    elif "id" in obj:
                        votes[obj["id"]] = obj
        except OSError:
            return {}

        meta: dict[str, Any] = {
            "token_budget": float(node_meta.get("token_budget", 0.0)),
            "last_reviewed": node_meta.get("last_reviewed", ""),
            "last_bullet_checkpoint": int(node_meta.get("last_bullet_checkpoint", 0)),
            "bullets": {
                bid: {k: v for k, v in bv.items() if k != "id"}
                for bid, bv in votes.items()
            },
        }
        self._write_meta(slug, meta)
        # Remove legacy file after successful migration
        with contextlib.suppress(OSError):
            legacy.unlink()
        return meta

    def exists(self, slug: str) -> bool:
        return self._node_path(slug).exists()

    def list_slugs(self) -> list[str]:
        """List all node slugs in the store."""
        return sorted(
            d.name for d in self.nodes_dir.iterdir()
            if d.is_dir() and (d / "node.jsonl").exists()
        )

    def iter_nodes(self) -> Iterator[FileNode]:
        """Iterate all nodes (loads each fully)."""
        for slug in self.list_slugs():
            node = self.get(slug)
            if node is not None:
                yield node

    # ------------------------------------------------------------------
    # Write — node creation
    # ------------------------------------------------------------------

    def create(self, slug: str, title: str, node_type: str = "concept") -> FileNode:
        """Create a new node. Raises if already exists."""
        if self.exists(slug):
            msg = f"Node already exists: {slug}"
            raise FileExistsError(msg)

        node_dir = self._node_dir(slug)
        node_dir.mkdir(parents=True, exist_ok=True)

        node = FileNode(
            slug=slug,
            title=title,
            type=node_type,
            created_at=datetime.now(UTC).isoformat(),
        )
        path = self._node_path(slug)
        with path.open("w") as f:
            f.write(json.dumps(node.header_dict()) + "\n")

        return node

    def get_or_create(self, slug: str, title: str | None = None, node_type: str = "concept") -> FileNode:
        """Get existing node or create it."""
        node = self.get(slug)
        if node is not None:
            return node
        return self.create(slug, title or slug, node_type)

    # ------------------------------------------------------------------
    # Write — bullets (O_APPEND atomic for single lines < 4096 bytes)
    # ------------------------------------------------------------------

    def add_bullet(
        self,
        slug: str,
        *,
        text: str,
        bullet_type: str = "fact",
        status: str | None = None,
        bullet_id: str | None = None,
    ) -> FileBullet:
        """Append a bullet to node.jsonl. Auto-creates node if missing.

        After writing, checks if bullet count crosses a structural checkpoint
        (30, 45, 60, ...).  If so, bombs the node's token_budget high enough
        to guarantee a review flag until the user explicitly reviews.
        """
        from kg.models import _REVIEW_BUDGET_THRESHOLD, structural_checkpoint

        self.get_or_create(slug, title=slug)

        bullet = FileBullet(
            id=bullet_id or new_bullet_id(),
            type=bullet_type,
            text=text,
            status=status,
            created_at=datetime.now(UTC).isoformat(),
        )

        line = json.dumps(bullet.to_dict()) + "\n"
        # O_APPEND on Linux: atomic for writes < PIPE_BUF (4096 bytes)
        # For safety we also flock for writes that might exceed that
        path = self._node_path(slug)
        with path.open("a") as f:
            if len(line) >= 4096:  # PIPE_BUF threshold
                fcntl.flock(f, fcntl.LOCK_EX)
            f.write(line)

        # Structural checkpoint: bomb budget if bullet count crossed a threshold
        with contextlib.suppress(Exception):
            node = self.get(slug)
            if node is not None:
                count = len(node.live_bullets)
                cp = structural_checkpoint(count)
                if cp is not None:
                    meta = self._read_meta(slug)
                    last_cp = int(meta.get("last_bullet_checkpoint", 0))
                    if cp > last_cp:
                        bomb = max(0.0, _REVIEW_BUDGET_THRESHOLD * count - float(meta.get("token_budget", 0.0)))
                        self._write_meta(slug, {
                            **meta,
                            "token_budget": float(meta.get("token_budget", 0.0)) + bomb,
                            "last_bullet_checkpoint": cp,
                        })

        return bullet

    def update_bullet(self, slug: str, bullet_id: str, new_text: str) -> None:
        """Rewrite node.jsonl with the bullet text updated. Uses flock."""
        path = self._node_path(slug)
        self._rewrite_with_lock(path, lambda obj: (
            {**obj, "text": new_text, "updated_at": datetime.now(UTC).isoformat()}
            if obj.get("id") == bullet_id and not obj.get("deleted")
            else obj
        ))

    def delete_bullet(self, slug: str, bullet_id: str) -> None:
        """Append a tombstone line. The bullet is logically deleted."""
        path = self._node_path(slug)
        tombstone = json.dumps({"id": bullet_id, "deleted": True}) + "\n"
        with path.open("a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(tombstone)

    # ------------------------------------------------------------------
    # Write — votes (meta.json)
    # ------------------------------------------------------------------

    def vote(self, slug: str, bullet_id: str, *, useful: bool) -> None:
        """Record a vote in meta.json (read-modify-write under flock)."""
        meta = self._read_meta(slug)
        bullets = meta.setdefault("bullets", {})
        b = bullets.setdefault(bullet_id, {"useful": 0, "harmful": 0, "used": 0})
        if useful:
            b["useful"] = int(b.get("useful", 0)) + 1
        else:
            b["harmful"] = int(b.get("harmful", 0)) + 1
        b["updated_at"] = datetime.now(UTC).isoformat()
        self._write_meta(slug, meta)

    def record_use(self, slug: str, bullet_id: str) -> None:
        """Increment used counter in meta.json."""
        meta = self._read_meta(slug)
        bullets = meta.setdefault("bullets", {})
        b = bullets.setdefault(bullet_id, {"useful": 0, "harmful": 0, "used": 0})
        b["used"] = int(b.get("used", 0)) + 1
        b["updated_at"] = datetime.now(UTC).isoformat()
        self._write_meta(slug, meta)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _rewrite_with_lock(
        self,
        path: Path,
        transform: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        """Read-modify-write node.jsonl under exclusive flock."""
        with path.open("r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            lines = f.readlines()
            new_lines = []
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                    obj = transform(obj)
                except json.JSONDecodeError:
                    obj = None
                if obj is not None:
                    new_lines.append(json.dumps(obj) + "\n")

            f.seek(0)
            f.writelines(new_lines)
            f.truncate()
