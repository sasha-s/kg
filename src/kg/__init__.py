"""File-based node store: JSONL files as source of truth, SQLite as derived index.

Layout:
    nodes/
        <slug>/
            node.jsonl    # content — bullets with stable IDs (git-tracked)
            meta.jsonl    # votes/usage (optionally .gitignored)
    .mg-index/
        graph.db          # SQLite: FTS5, backlinks, embeddings (fully reconstructable)

node.jsonl line types:
    {"v":1, "slug":..., "title":..., "type":..., "created_at":...}  # header (line 1)
    {"id":"b-<8hex>", "type":..., "text":..., "created_at":...}      # bullet
    {"id":"b-<8hex>", "deleted":true}                                 # tombstone

meta.jsonl line types:
    {"id":"b-<8hex>", "useful":N, "harmful":N, "used":N, "updated_at":...}

Concurrent writes: O_APPEND atomic for single-line appends < 4096 bytes (Linux).
Read-modify-write (update/delete) requires flock(LOCK_EX) on node.jsonl.
"""

from kg.config import KGConfig, init_config, load_config
from kg.models import FileBullet, FileNode
from kg.reader import FileStore

__all__ = ["FileBullet", "FileNode", "FileStore", "KGConfig", "init_config", "load_config"]
