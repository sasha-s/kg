"""Read and write node.jsonl / meta.jsonl files.

FileStore is the public API:
    store = FileStore("/path/to/nodes")
    node = store.get("asyncpg-patterns")
    store.add_bullet("asyncpg-patterns", type="gotcha", text="LIKE is case-sensitive")
    store.vote("b-abc12345", useful=True)
"""

from __future__ import annotations

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
        """Load meta.jsonl and attach vote counts to bullets."""
        path = self._meta_path(node.slug)
        if not path.exists():
            return

        votes: dict[str, dict[str, Any]] = {}
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "id" in obj:
                    # Last write wins (meta.jsonl is append-only, latest entry is truth)
                    votes[obj["id"]] = obj

        for b in node.bullets:
            if b.id in votes:
                v = votes[b.id]
                b.useful = int(v.get("useful", 0))
                b.harmful = int(v.get("harmful", 0))
                b.used = int(v.get("used", 0))

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
        """Append a bullet to node.jsonl. Auto-creates node if missing."""
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
    # Write — votes (append to meta.jsonl)
    # ------------------------------------------------------------------

    def vote(self, slug: str, bullet_id: str, *, useful: bool) -> None:
        """Append a vote entry to meta.jsonl."""
        path = self._meta_path(slug)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Read current vote state to increment
        votes: dict[str, Any] = {}
        if path.exists():
            with path.open() as f:
                for line in f:
                    try:
                        obj = json.loads(line.strip())
                        if "id" in obj:
                            votes[obj["id"]] = obj
                    except json.JSONDecodeError:
                        continue

        current = votes.get(bullet_id, {"id": bullet_id, "useful": 0, "harmful": 0, "used": 0})
        if useful:
            current["useful"] = int(current.get("useful", 0)) + 1
        else:
            current["harmful"] = int(current.get("harmful", 0)) + 1
        current["updated_at"] = datetime.now(UTC).isoformat()

        line = json.dumps(current) + "\n"
        with path.open("a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(line)

    def record_use(self, slug: str, bullet_id: str) -> None:
        """Increment used counter in meta.jsonl."""
        path = self._meta_path(slug)
        path.parent.mkdir(parents=True, exist_ok=True)

        votes: dict[str, Any] = {}
        if path.exists():
            with path.open() as f:
                for line in f:
                    try:
                        obj = json.loads(line.strip())
                        if "id" in obj:
                            votes[obj["id"]] = obj
                    except json.JSONDecodeError:
                        continue

        current = votes.get(bullet_id, {"id": bullet_id, "useful": 0, "harmful": 0, "used": 0})
        current["used"] = int(current.get("used", 0)) + 1
        current["updated_at"] = datetime.now(UTC).isoformat()

        line = json.dumps(current) + "\n"
        with path.open("a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(line)

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
