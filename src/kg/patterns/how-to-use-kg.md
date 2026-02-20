---
slug: how-to-use-kg
title: How to Use kg
type: concept
---

- (fact) MCP tools: `memory_context(query)` retrieves ranked context; `memory_add_bullet(node_slug, text, bullet_type)` adds a bullet; `memory_show(slug)` dumps a node. Session ID auto-injected by the hook.
- (fact) CLI: `kg context "query"` for context output; `kg search "query"` for raw FTS results; `kg show <slug>` to inspect a node and clear its review flag.
- (decision) Prefer `memory_context` over `memory_search` for LLM consumption — it packs results into a token-budgeted block with explore hints already formatted.
- (fact) Cross-reference nodes with [slug] in bullet text. The indexer extracts these as backlinks and context output surfaces them as "↳ Explore:" hints.
- (fact) Fleeting notes: `memory_add_bullet(node_slug="_fleeting-<session_id[:12]>", text="discovered: X")` — node auto-creates on first write. Use for in-session captures.
- (gotcha) Viewing a node does NOT clear its review budget. `memory_show` / `kg show` are read-only. Only `memory_mark_reviewed(slug)` / `kg review <slug>` clears the budget — use after actually doing the maintenance work.
- (fact) Review workflow: `memory_review()` → lists hot nodes → `memory_show(slug)` to inspect → update/prune/push info to other nodes → `memory_mark_reviewed(slug)` to clear. Skipping the last step means the node stays flagged.
- (fact) File sources indexed via `[[sources]]` in kg.toml appear in `kg context` output alongside curated nodes — same FTS index, no distinction needed.
