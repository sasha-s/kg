---
slug: fleeting-notes
title: Fleeting Notes Workflow
type: concept
---

- (decision) Capture discoveries into fleeting notes as you work, not after — memory of why something failed fades within a few tool calls.
- (fact) Fleeting notes are low-friction scratch space: no dedup, no formatting required. Just dump observations. Run `kg reindex` at session end to ensure they're in the index.
- (fact) CLI mechanics: `kg add _fleeting-<session_id[:12]> "discovered: X"` — node auto-creates on first bullet. Use first 12 chars of session ID.
- (fact) MCP mechanics: `memory_add_bullet(node_slug="_fleeting-<session_id[:12]>", text="discovered: X", bullet_type="fact")` — session ID is auto-injected into every prompt via the session_context hook as additionalContext.
- (gotcha) Never call `kg init` to create fleeting nodes — always use `kg add _fleeting-<id> "..."` or `memory_add_bullet`. The auto-create path happens on first bullet add.
- (fact) Capture prefixes: `discovered:` (confirmed fact), `tried:` (attempted, with outcome), `hypothesis:` (to verify), `confirmed:` (validated hypothesis), `blocked:` (obstacle hit), `decided:` (in-session choice with rationale).
- (decision) Add a fleeting note at natural breakpoints: after a failed approach, after confirming a hypothesis, when something surprises you. Not every tool call — just inflection points.
- (success) Fleeting notes survive context compaction — they're in the JSONL files and SQLite index, not just in context.
- (gotcha) Don't try to be precise in fleeting notes. Write what you actually observed, including failed attempts. You can clean up later with `kg show _fleeting-<id>` and `kg upgrade`.
