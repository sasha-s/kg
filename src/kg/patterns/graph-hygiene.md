---
slug: graph-hygiene
title: Graph Hygiene (Zettelkasten)
type: concept
---

- (decision) One concept per node. If you find yourself writing "also, separately..." in a bullet, that's a new node. Split and cross-reference with [slug].
- (fact) Nodes are claims, not documents. A node titled "asyncpg" with 40 bullets is a problem. Break it into "asyncpg-connection-pooling", "asyncpg-transactions", "asyncpg-gotchas".
- (gotcha) Big nodes (high bullet count + high token budget) are graph debt. `kg review` / `memory_review()` surfaces them. Don't let nodes grow past ~15 bullets without pruning.
- (decision) When a bullet is wrong or outdated, delete it — don't add a correction alongside it. The graph is truth, not a changelog.
- (gotcha) Don't create nodes defensively ("just in case"). Every node is maintenance debt. Create when you have at least 2 concrete bullets to put in it.
- (fact) Good slug hygiene: kebab-case, specific over general. "python-async-cancellation" beats "python". Slugs appear in cross-references — they should be guessable from context.
- (decision) Merge nodes that cover the same concept with different slugs. Move bullets to the canonical slug, delete the orphan.

## Review Workflow

When `kg show` / `memory_show` shows a ⚠ NEEDS REVIEW banner:

- (fact) Step 1 — read every bullet: delete anything stale, wrong, or already captured elsewhere. If a bullet starts with "tried: X failed" and the approach now works, update or delete it.
- (fact) Step 2 — check size: if the node has >15 bullets, it probably covers multiple concepts. Identify natural splits. Create new nodes for sub-concepts with `kg add` / `memory_add_bullet`, move bullets, then delete from the source node.
- (fact) Step 3 — push insights outward: if a bullet belongs in a related node, add it there too (or instead). Follow ↳ Explore hints to find candidate nodes. This is how the graph self-organizes over time.
- (fact) Step 4 — mark reviewed: call `kg review <slug>` or `memory_mark_reviewed(slug)` when done. This clears the token budget. Do not call it before doing the work — the flag is a promise, not a checkbox.
- (gotcha) Viewing a node does NOT count as reviewing it. `kg show` and `memory_show` are read-only. Only the explicit mark-reviewed step clears the budget.
- (decision) If you find yourself marking reviewed without changing anything, the node is well-maintained. That's the goal: a node that needs no changes after heavy use.
