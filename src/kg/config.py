"""KGConfig: project-local config for the file-based knowledge graph.

Default layout (all relative to the project root):

    kg.toml               # project config (git-tracked)
    .kg/
        nodes/            # node JSONL files (git-tracked)
            <slug>/
                node.jsonl
                meta.jsonl
        index/
            graph.db      # SQLite derived cache (add to .gitignore)
        .gitignore        # auto-written: ignores index/

kg.toml example:

    [kg]
    name = "my-project"
    # nodes_dir = ".kg/nodes"   # default
    # index_dir = ".kg/index"   # default

    [embeddings]
    model = "openai:text-embedding-3-small"

    [server]
    port = 7343
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_CONFIG_FILENAME = "kg.toml"
_DEFAULT_NODES_DIR = ".kg/nodes"
_DEFAULT_INDEX_DIR = ".kg/index"
_GITIGNORE_CONTENT = "index/\n"


@dataclass
class EmbeddingsConfig:
    model: str = "openai:text-embedding-3-small"


@dataclass
class ServerConfig:
    port: int = 7343


@dataclass
class KGConfig:
    """Resolved configuration for a knowledge graph project."""

    root: Path                      # directory that contains kg.toml
    name: str = ""
    nodes_dir: Path = field(default_factory=Path)
    index_dir: Path = field(default_factory=Path)
    embeddings: EmbeddingsConfig = field(default_factory=EmbeddingsConfig)
    server: ServerConfig = field(default_factory=ServerConfig)

    @property
    def db_path(self) -> Path:
        return self.index_dir / "graph.db"

    def ensure_dirs(self) -> None:
        """Create nodes_dir and index_dir if they don't exist."""
        self.nodes_dir.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self._write_gitignore()

    def _write_gitignore(self) -> None:
        """Write .kg/.gitignore to keep index/ out of git."""
        kg_dir = self.index_dir.parent  # .kg/
        gitignore = kg_dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(_GITIGNORE_CONTENT)


def load_config(root: Path | str | None = None) -> KGConfig:
    """Load kg.toml from root (or search upward from cwd if root is None).

    Raises FileNotFoundError if no kg.toml is found.
    """
    root_path = _find_root(Path(root) if root else Path.cwd())
    config_path = root_path / _CONFIG_FILENAME

    raw: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open("rb") as f:
            raw = tomllib.load(f)

    kg_section = raw.get("kg", {})
    name = kg_section.get("name", root_path.name)

    nodes_rel = kg_section.get("nodes_dir", _DEFAULT_NODES_DIR)
    index_rel = kg_section.get("index_dir", _DEFAULT_INDEX_DIR)

    emb_section = raw.get("embeddings", {})
    srv_section = raw.get("server", {})

    return KGConfig(
        root=root_path,
        name=name,
        nodes_dir=root_path / nodes_rel,
        index_dir=root_path / index_rel,
        embeddings=EmbeddingsConfig(
            model=emb_section.get("model", "openai:text-embedding-3-small"),
        ),
        server=ServerConfig(
            port=int(srv_section.get("port", 7343)),
        ),
    )


def _find_root(start: Path) -> Path:
    """Walk upward from start looking for kg.toml. Returns the directory that
    contains it. Falls back to start if not found (allows init without existing file).
    """
    for directory in (start, *start.parents):
        if (directory / _CONFIG_FILENAME).exists():
            return directory
    # No kg.toml found — return start so callers can run `kg init` there
    return start


def init_config(root: Path, name: str | None = None) -> Path:
    """Write a default kg.toml at root. Raises if already exists."""
    config_path = root / _CONFIG_FILENAME
    if config_path.exists():
        msg = f"kg.toml already exists at {config_path}"
        raise FileExistsError(msg)

    project_name = name or root.name
    content = f"""\
[kg]
name = "{project_name}"
# nodes_dir = ".kg/nodes"   # default
# index_dir = ".kg/index"   # default — add to .gitignore (auto-written on first use)

# [embeddings]
# model = "openai:text-embedding-3-small"

# [server]
# port = 7343
"""
    config_path.write_text(content)
    return config_path
