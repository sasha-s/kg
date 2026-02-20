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
- (gotcha) `memory_show` clears a node's token budget (marks it reviewed). Use it intentionally when you've examined a node and decided it's healthy.
- (fact) `kg review` lists nodes ordered by accumulated credits — nodes that have been served heavily without review. Examine and update them with `kg show <slug>`.
- (fact) File sources indexed via `[[sources]]` in kg.toml appear in `kg context` output alongside curated nodes — same FTS index, no distinction needed.
