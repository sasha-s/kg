---
slug: graph-hygiene
title: Graph Hygiene (Zettelkasten)
type: concept
---

- (decision) One concept per node. If you find yourself writing "also, separately..." in a bullet, that's a new node. Split and cross-reference with [slug].
- (fact) Nodes are not documents — they're claims. A node titled "asyncpg" with 40 bullets is a problem. Break it into "asyncpg-connection-pooling", "asyncpg-transactions", "asyncpg-gotchas".
- (gotcha) Big nodes (high bullet count + high token budget) are the graph's technical debt. `kg review` surfaces them. Don't let them grow past ~15 bullets without pruning.
- (decision) When a bullet is wrong or outdated, delete it — don't add a correction bullet alongside it. The graph is truth, not a changelog.
- (fact) Token budget is a maintenance signal: a node that accumulates 500+ credits has been relied upon heavily. That's when to ask — is it still accurate? Is it too fat? Should it be split?
- (fact) After reviewing a node with `kg show <slug>` or `memory_show`, the budget clears automatically. No need to manually reset.
- (decision) Merge nodes that are essentially the same concept with different slugs. Use `kg add` to move bullets to the canonical node, then delete the orphan.
- (gotcha) Don't create nodes defensively ("just in case"). Every node you create is maintenance debt. Create when you have ≥2 concrete bullets to put in it.
- (fact) Good slug hygiene: kebab-case, specific over general. "python-async-gotchas" beats "python". Slugs appear in cross-references — they should be guessable.
