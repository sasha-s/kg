"""Bootstrap: load bundled pattern nodes into a new graph on `kg init`.

Pattern files live in src/kg/patterns/*.md with frontmatter:
    ---
    slug: fleeting-notes
    title: Fleeting Notes Workflow
    type: concept
    ---
    - (fact) bullet text
    - (gotcha) bullet text
    - plain bullet text
"""

from __future__ import annotations

import re
import shutil
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from kg.indexer import index_node
from kg.reader import FileStore

if TYPE_CHECKING:
    from kg.config import KGConfig

_BULLET_PREFIX_RE = re.compile(r"^\s*-\s+(?:\((\w+)\)\s+)?(.+)$")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_FM_KEY_RE = re.compile(r"^(\w+):\s*(.+)$", re.MULTILINE)


def _parse_pattern(text: str) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Parse markdown pattern file → (frontmatter dict, [(type, text)] bullets)."""
    fm: dict[str, str] = {}
    bullets: list[tuple[str, str]] = []

    m = _FRONTMATTER_RE.match(text)
    if m:
        for key, val in _FM_KEY_RE.findall(m.group(1)):
            fm[key] = val.strip()
        body = text[m.end():]
    else:
        body = text

    for line in body.splitlines():
        bm = _BULLET_PREFIX_RE.match(line)
        if bm:
            btype = bm.group(1) or "fact"
            btext = bm.group(2).strip()
            if btext:
                bullets.append((btype, btext))

    return fm, bullets


def _patterns_dir() -> Path:
    """Return path to bundled patterns directory."""
    # Works both installed and from source
    try:
        ref = resources.files("kg") / "patterns"
        return Path(str(ref))
    except Exception:
        return Path(__file__).parent / "patterns"


def _skills_dir() -> Path:
    """Return path to bundled skills directory."""
    try:
        ref = resources.files("kg") / "skills"
        return Path(str(ref))
    except Exception:
        return Path(__file__).parent / "skills"


def bootstrap_patterns(cfg: KGConfig, *, overwrite: bool = False) -> list[str]:
    """Load bundled patterns into the graph. Returns list of bootstrapped slugs."""
    store = FileStore(cfg.nodes_dir)
    bootstrapped: list[str] = []

    patterns_dir = _patterns_dir()
    if not patterns_dir.exists():
        return []

    for md_file in sorted(patterns_dir.glob("*.md")):
        text = md_file.read_text()
        fm, bullets = _parse_pattern(text)

        slug = fm.get("slug") or md_file.stem
        title = fm.get("title") or slug
        node_type = fm.get("type", "concept")

        if not overwrite and store.exists(slug):
            continue

        # Create node (or recreate if overwrite)
        if overwrite and store.exists(slug):
            # Delete and recreate by removing the dir
            shutil.rmtree(cfg.nodes_dir / slug)

        store.create(slug, title, node_type)
        for btype, btext in bullets:
            store.add_bullet(slug, text=btext, bullet_type=btype)

        index_node(slug, nodes_dir=cfg.nodes_dir, db_path=cfg.db_path)
        bootstrapped.append(slug)

    return bootstrapped


def bootstrap_skills(cfg: KGConfig, *, overwrite: bool = False) -> list[str]:
    """Copy bundled skills into .kg/skills/. Returns list of installed skill names."""
    skills_src = _skills_dir()
    if not skills_src.exists():
        return []

    skills_dst = cfg.index_dir.parent / "skills"
    skills_dst.mkdir(parents=True, exist_ok=True)

    installed: list[str] = []
    for skill_dir in sorted(skills_src.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue
        dest_dir = skills_dst / skill_dir.name
        dest_file = dest_dir / "SKILL.md"
        if not overwrite and dest_file.exists():
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(skill_file, dest_file)
        installed.append(skill_dir.name)

    return installed
