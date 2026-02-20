"""KGConfig: project-local config for the file-based knowledge graph.

Default layout (all relative to the project root):

    kg.toml               # project config (git-tracked)
    .env                  # optional: TURSO_URL, TURSO_TOKEN, GEMINI_API_KEY (gitignore this)
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

    [[sources]]
    name = "workspace"
    path = "."
    include = ["**/*.py", "**/*.md", "**/*.toml"]
    exclude = [".kg/**", "**/__pycache__/**"]
    use_git = true          # use git ls-files (respects .gitignore)
    max_size_kb = 512

    [embeddings]
    model = "gemini:gemini-embedding-001"

    [database]
    url = ""    # libsql://... for Turso, empty = local SQLite
    token = ""  # JWT auth token (or set TURSO_URL / TURSO_TOKEN in .env)

    [server]
    port = 7343
    vector_port = 7344

    [search]
    fts_weight = 0.5
    vector_weight = 0.5
    dual_match_bonus = 0.1
    use_reranker = true
    reranker_model = "Xenova/ms-marco-MiniLM-L-6-v2"
    auto_calibrate_threshold = 0.05
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

# Default file patterns for [[sources]]
_DEFAULT_INCLUDE = [
    "**/*.py", "**/*.md", "**/*.txt", "**/*.rst",
    "**/*.toml", "**/*.yaml", "**/*.yml",
    "**/*.js", "**/*.ts", "**/*.jsx", "**/*.tsx",
    "**/*.go", "**/*.rs", "**/*.java",
    "**/*.c", "**/*.h", "**/*.cpp", "**/*.hpp",
    "**/*.sql", "**/*.sh",
    "**/Dockerfile", "**/Makefile",
]
_DEFAULT_EXCLUDE = [
    ".kg/**", "**/.git/**", "**/__pycache__/**",
    "**/*.lock", "**/node_modules/**", "**/dist/**", "**/build/**",
    "**/*.min.js", "**/*.min.css",
]


@dataclass
class SourceConfig:
    """A [[sources]] entry in kg.toml."""
    path: str                               # relative to kg root
    name: str = ""
    include: list[str] = field(default_factory=lambda: list(_DEFAULT_INCLUDE))
    exclude: list[str] = field(default_factory=lambda: list(_DEFAULT_EXCLUDE))
    use_git: bool = True                    # prefer git ls-files (respects .gitignore)
    max_size_kb: int = 512

    @property
    def abs_path(self) -> Path:
        """Caller must set _root first via resolve()."""
        return self._root / self.path  # type: ignore[attr-defined]

    def resolve(self, root: Path) -> SourceConfig:
        """Attach the project root for abs_path resolution."""
        self._root = root  # type: ignore[attr-defined]
        return self


@dataclass
class ReviewConfig:
    budget_threshold: float = 3000.0   # chars served before flagging a node for review


@dataclass
class EmbeddingsConfig:
    model: str = "gemini:gemini-embedding-001"


@dataclass
class DatabaseConfig:
    url: str = ""    # libsql://... for Turso, empty = local SQLite
    token: str = ""  # JWT auth token


@dataclass
class ServerConfig:
    port: int = 7343
    vector_port: int = 7344


@dataclass
class SearchConfig:
    fts_weight: float = 0.5
    vector_weight: float = 0.5
    dual_match_bonus: float = 0.1
    use_reranker: bool = True
    reranker_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    auto_calibrate_threshold: float = 0.05   # fraction of bullets changed to trigger recal


@dataclass
class KGConfig:
    """Resolved configuration for a knowledge graph project."""

    root: Path                      # directory that contains kg.toml
    name: str = ""
    nodes_dir: Path = field(default_factory=Path)
    index_dir: Path = field(default_factory=Path)
    sources: list[SourceConfig] = field(default_factory=list)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    embeddings: EmbeddingsConfig = field(default_factory=EmbeddingsConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    search: SearchConfig = field(default_factory=SearchConfig)

    @property
    def db_path(self) -> Path:
        return self.index_dir / "graph.db"

    @property
    def use_turso(self) -> bool:
        return bool(self.database.url)

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


def _load_env(root: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file (no external dependency)."""
    env_file = root / ".env"
    if not env_file.exists():
        return {}
    env: dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def load_config(root: Path | str | None = None) -> KGConfig:
    """Load kg.toml from root (or search upward from cwd if root is None)."""
    root_path = _find_root(Path(root) if root else Path.cwd())
    config_path = root_path / _CONFIG_FILENAME

    raw: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open("rb") as f:
            raw = tomllib.load(f)

    # Load .env for secrets (TURSO_URL, TURSO_TOKEN, GEMINI_API_KEY)
    env = _load_env(root_path)
    # Inject API keys into os.environ so embedder/clients can find them
    import os as _os
    for _key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY"):
        if _key in env:
            _os.environ.setdefault(_key, env[_key])

    kg_section = raw.get("kg", {})
    name = kg_section.get("name", root_path.name)

    nodes_rel = kg_section.get("nodes_dir", _DEFAULT_NODES_DIR)
    index_rel = kg_section.get("index_dir", _DEFAULT_INDEX_DIR)

    rev_section = raw.get("review", {})
    emb_section = raw.get("embeddings", {})
    db_section = raw.get("database", {})
    srv_section = raw.get("server", {})
    srch_section = raw.get("search", {})

    sources: list[SourceConfig] = []
    for s in raw.get("sources", []):
        src = SourceConfig(
            path=s.get("path", "."),
            name=s.get("name", ""),
            include=s.get("include", list(_DEFAULT_INCLUDE)),
            exclude=s.get("exclude", list(_DEFAULT_EXCLUDE)),
            use_git=bool(s.get("use_git", True)),
            max_size_kb=int(s.get("max_size_kb", 512)),
        )
        src.resolve(root_path)
        sources.append(src)

    # Database: .env overrides kg.toml for secrets
    db_url: str = env.get("TURSO_URL") or str(db_section.get("url", ""))
    db_token: str = env.get("TURSO_TOKEN") or str(db_section.get("token", ""))

    return KGConfig(
        root=root_path,
        name=name,
        nodes_dir=root_path / nodes_rel,
        index_dir=root_path / index_rel,
        sources=sources,
        review=ReviewConfig(
            budget_threshold=float(rev_section.get("budget_threshold", 3000.0)),
        ),
        embeddings=EmbeddingsConfig(
            model=emb_section.get("model", "gemini:gemini-embedding-001"),
        ),
        database=DatabaseConfig(
            url=db_url,
            token=db_token,
        ),
        server=ServerConfig(
            port=int(srv_section.get("port", 7343)),
            vector_port=int(srv_section.get("vector_port", 7344)),
        ),
        search=SearchConfig(
            fts_weight=float(srch_section.get("fts_weight", 0.5)),
            vector_weight=float(srch_section.get("vector_weight", 0.5)),
            dual_match_bonus=float(srch_section.get("dual_match_bonus", 0.1)),
            use_reranker=bool(srch_section.get("use_reranker", True)),
            reranker_model=srch_section.get("reranker_model", "Xenova/ms-marco-MiniLM-L-6-v2"),
            auto_calibrate_threshold=float(srch_section.get("auto_calibrate_threshold", 0.05)),
        ),
    )


def _find_root(start: Path) -> Path:
    """Walk upward from start looking for kg.toml."""
    for directory in (start, *start.parents):
        if (directory / _CONFIG_FILENAME).exists():
            return directory
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
# index_dir = ".kg/index"   # default — add index/ to .gitignore

# Index source files for FTS search (no LLM extraction — just chunk + index)
# [[sources]]
# name = "workspace"
# path = "."
# include = ["**/*.py", "**/*.md", "**/*.toml"]
# exclude = [".kg/**", "**/__pycache__/**"]
# use_git = true      # use git ls-files to respect .gitignore
# max_size_kb = 512

# [review]
# budget_threshold = 3000   # chars served before flagging a node for review (default: 3000)

# [embeddings]
# model = "gemini:gemini-embedding-001"

# [database]
# url = ""    # libsql://... for Turso; or set TURSO_URL in .env
# token = ""  # JWT auth token; or set TURSO_TOKEN in .env

# [server]
# port = 7343
# vector_port = 7344

# [search]
# fts_weight = 0.5
# vector_weight = 0.5
# dual_match_bonus = 0.1
# use_reranker = true
# reranker_model = "Xenova/ms-marco-MiniLM-L-6-v2"
# auto_calibrate_threshold = 0.05   # recalibrate when this fraction of bullets changes
"""
    config_path.write_text(content)
    return config_path
