#!/usr/bin/env python3
"""Release script for kg.

Usage:
    python scripts/release.py 0.3.0
    python scripts/release.py 0.3.0 --dry-run
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"


def run(cmd: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=REPO_ROOT, check=check,
                          capture_output=capture, text=True)


def current_version() -> str:
    text = PYPROJECT.read_text()
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not m:
        sys.exit("Could not find version in pyproject.toml")
    return m.group(1)


def bump_version(new: str) -> None:
    text = PYPROJECT.read_text()
    updated = re.sub(r'^(version\s*=\s*)"[^"]+"', rf'\g<1>"{new}"', text, count=1, flags=re.MULTILINE)
    PYPROJECT.write_text(updated)


def ensure_clean() -> None:
    result = run(["git", "status", "--porcelain"], capture=True)
    if result.stdout.strip():
        sys.exit("Working tree is dirty — commit or stash changes first.")


def next_patch(ver: str) -> str:
    parts = ver.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("version", nargs="?", help="New version, e.g. 0.3.0 (default: bump patch)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    old_ver = current_version()
    new_ver = args.version.lstrip("v") if args.version else next_patch(old_ver)
    dry = args.dry_run

    print(f"Releasing {old_ver} → {new_ver}" + (" (dry run)" if dry else ""))

    if not dry:
        ensure_clean()

    # 1. Bump pyproject.toml
    print("\n1. Bumping version...")
    if not dry:
        bump_version(new_ver)

    # 2. Commit
    print("\n2. Committing...")
    if not dry:
        run(["git", "add", "pyproject.toml"])
        run(["git", "commit", "-m", f"Bump version to {new_ver}"])

    # 3. Tag
    tag = f"v{new_ver}"
    print(f"\n3. Tagging {tag}...")
    if not dry:
        run(["git", "tag", tag])

    # 4. Push
    print("\n4. Pushing...")
    if not dry:
        run(["git", "push"])
        run(["git", "push", "origin", tag])

    # 5. GitHub release
    print("\n5. Creating GitHub release...")
    if not dry:
        run(["gh", "release", "create", tag,
             "--title", f"{tag}",
             "--generate-notes"])

    print(f"\nDone! https://github.com/sasha-s/kg/releases/tag/{tag}")


if __name__ == "__main__":
    main()
