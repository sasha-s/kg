---
slug: node-review
title: How to Review a Node
type: concept
---

- (fact) Step 1 — read every bullet: delete anything stale, wrong, or superseded. If a bullet says "tried: X failed" and X now works, update or delete it. The graph is truth, not a changelog.
- (fact) Step 2 — check size: if the node has >15 bullets, it likely covers multiple concepts. Identify natural splits. Create sub-nodes with `kg add` / `memory_add_bullet`, move bullets, delete from the source.
- (fact) Step 3 — push insights outward: follow the ↳ Explore hints. If a bullet belongs in a related node too (or instead), add it there. This is how the graph self-organizes.
- (fact) Step 4 — mark reviewed: call `kg review <slug>` or `memory_mark_reviewed(slug)` when done. Do not call it before doing the work — the flag is a commitment, not a checkbox.
- (gotcha) Viewing a node does NOT count as reviewing it. `kg show` and `memory_show` are read-only. Only the explicit mark-reviewed step clears the budget.
- (decision) If you mark reviewed without changing anything, the node is well-maintained. That's the goal: a node that needs no changes after heavy use.
