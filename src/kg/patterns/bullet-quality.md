---
slug: bullet-quality
title: Bullet Quality Guidelines
type: concept
---

- (decision) Each bullet should be a single, standalone fact. A reader with no context should understand it.
- (fact) Use bullet types to signal intent: `fact` (verified), `gotcha` (trap or surprise), `decision` (with rationale), `task` (actionable), `success` (validated approach), `failure` (what not to do).
- (gotcha) Vague bullets ("it works", "fixed") decay quickly. Always include: what, why, and any conditions or caveats.
- (fact) Good bullet formula: [context] + [observation/decision] + [rationale/consequence]. Example: "In async SQLite (aiosqlite), nested transactions silently succeed but don't actually nest — use savepoints instead."
- (decision) Prefer specific over general. "ruff check --fix rewrites imports in-place" beats "ruff can fix some issues automatically."
- (fact) Cross-reference related nodes with [slug] in bullet text. The indexer extracts these as backlinks and `kg context` surfaces them as explore hints.
- (gotcha) Don't add bullets that duplicate the node title or are obvious from context. Every bullet should add information that isn't already in the node header.
