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
- (fact) When `kg show` / `memory_show` shows a ⚠ NEEDS REVIEW banner, follow the steps in [node-review].
