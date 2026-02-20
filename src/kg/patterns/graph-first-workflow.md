---
slug: graph-first-workflow
title: Graph-First Workflow
type: concept
---

- (decision) Always check the knowledge graph before exploring code or docs. Use `kg context "query"` or `memory_context()` to find existing decisions, gotchas, and prior work before rediscovering them.
- (fact) CLAUDE.md wiring: add "Use `kg context` BEFORE exploring code or files" to project instructions. Ensures all sessions check the graph first.
- (gotcha) Graph search is faster than code exploration for project-specific questions. Only fall back to file reads when the graph doesn't have what you need.
- (decision) Record decisions and gotchas in the graph as they emerge. Don't defer to a "documentation phase" — it never happens.
- (fact) The graph is institutional memory across sessions: what was tried, what worked, what failed, and why decisions were made. More valuable than code comments because it's searchable.
- (fact) Cross-reference nodes with [slug] notation in bullet text. The indexer extracts these into backlinks automatically, and `kg context` surfaces them as "Explore" hints.
