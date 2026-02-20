# kg — lightweight knowledge graph

JSONL files as source of truth. SQLite as derived index. inotify watcher for live re-indexing.

```
my-project/
  kg.toml           # project config
  .kg/
    nodes/          # one dir per node (git-tracked)
      <slug>/
        node.jsonl  # bullets with stable IDs
        meta.jsonl  # votes / usage counts
    index/
      graph.db      # SQLite: FTS5, backlinks, embeddings (gitignored)
```

## Why

- `jq`, `rg`, `git log` just work on the files
- No daemon required — SQLite index rebuilds from files in seconds
- Git-friendly: content is plain text, metadata is append-only

## Install

**From git (latest):**
```bash
uv tool install "git+https://github.com/sasha-s/kg.git"
```

**With extras (Gemini embeddings + Linux file watching):**
```bash
uv tool install "kg[embeddings,watch] @ git+https://github.com/sasha-s/kg.git"
```

**Dev / editable:**
```bash
git clone https://github.com/sasha-s/kg.git
uv tool install --editable ./kg
```

**Optional extras:**

| Extra | Packages | Use |
|-------|----------|-----|
| `turso` | `libsql` | Turso remote SQLite (requires cmake to build) |
| `dev` | `ruff`, `basedpyright`, `pytest` | Development tools |

`fastembed`, `google-genai`, `diskcache`, `numpy`, and `inotify-simple` are included in the default dependencies.

## Quickstart

```bash
kg init             # write kg.toml + create .kg/
kg start            # index, calibrate, start watcher + vector server, register MCP
kg add my-note "discovered: async context managers don't propagate cancellation"
kg search "async"
kg context "cancellation"
```

## Commands

```
kg init              create kg.toml and .kg/ directories
kg start             index + calibrate + watcher + vector server + MCP + hook
kg reindex           rebuild SQLite from all node.jsonl files
kg calibrate         calibrate FTS/vector score quantiles (auto-runs on start)
kg upgrade           reindex + schema migrations (safe to run anytime)
kg add <slug> <text> [--type fact|gotcha|decision|task]
kg show <slug>       dump a node's bullets
kg search <query> [-q rerank-query] [-n limit]
kg context <query>  [-q rerank-query] [-s session-id] [--max-tokens N]
kg serve             start stdio MCP server
kg status            show node counts, calibration, watcher, vector server
kg stop              stop watcher and vector server
```

## Search & Context

Context retrieval uses hybrid FTS + vector search with calibrated quantile fusion:

- **FTS (BM25)** — keyword matching, OR + prefix wildcards
- **Vector search** — cosine similarity over Gemini or local fastembed embeddings
- **Calibration** — maps raw scores to percentile quantiles before fusion
- **Reranking** — cross-encoder (`Xenova/ms-marco-MiniLM-L-6-v2`) reranks final results
- **Session dedup** — bullets already shown in the current session are filtered out
- **Session boost** — nodes mentioned in the session get 1.3× score boost

```bash
kg context "query"              # hybrid search + rerank (cross-encoder used automatically)
kg context "query" -q "intent"  # use different query for reranking
kg context "query" -s SESSION_ID  # filter already-seen bullets for this session
```

## Review & Budget

Each node accumulates a `token_budget` counter: every time `kg context` / `memory_context` serves a node, the number of chars in its output is added to the budget. When budget ≥ `budget_threshold` (default 3000), `kg show` and context output flag the node with ⚠.

```bash
kg review              # list nodes ordered by budget (reads files, never stale)
kg review <slug>       # mark as reviewed — clears budget to 0
```

**Budget ideas / possible future work:**

- *Normalize by node size* — a 20-bullet node at 3000 credits is less surprising than a 3-bullet node at 3000. Could use `credits / bullet_count` as the review signal instead of raw credits.
- *Serve-count threshold* — track number of times a node was served rather than chars, so the threshold is model-independent.
- *Graph propagation* — when node A is served and it references [node-B] in its bullets, deposit 50% of A's charge into B's budget. Each hop halves the deposit (bounded total). Currently no propagation: only directly-served nodes accumulate budget.

## Configuration

`kg.toml` (all sections optional):

```toml
[kg]
name = "my-project"

[embeddings]
model = "gemini:gemini-embedding-001"   # or "fastembed:BAAI/bge-small-en-v1.5"

[search]
fts_weight = 0.5
vector_weight = 0.5
dual_match_bonus = 0.1
use_reranker = true
reranker_model = "Xenova/ms-marco-MiniLM-L-6-v2"
auto_calibrate_threshold = 0.05   # recalibrate when 5% of bullets change

[review]
budget_threshold = 3000   # chars served before flagging for review

[server]
port = 7343
vector_port = 7344

# [[sources]]  — index source files for FTS (no LLM extraction)
# name = "workspace"
# path = "."
# include = ["**/*.py", "**/*.md"]
# use_git = true
```

Secrets go in `.env` (gitignored):
```
GEMINI_API_KEY=...
TURSO_URL=libsql://...
TURSO_TOKEN=...
```

## MCP Server (Claude Code)

```bash
kg start   # registers MCP automatically
```

Or manually in `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "kg": {
      "command": "kg",
      "args": ["serve"],
      "cwd": "/path/to/your/project"
    }
  }
}
```

MCP tools (compatible with `memory_graph`):

| Tool | Description |
|------|-------------|
| `memory_context(query, session_id?)` | Ranked context for LLM injection |
| `memory_search(query, limit?)` | FTS search results |
| `memory_show(slug)` | Show node bullets |
| `memory_add_bullet(node_slug, text, bullet_type?)` | Add bullet |

## Session ID Hook

`kg start` installs this automatically, or add manually to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [{
      "hooks": [{
        "type": "command",
        "command": "python -m kg.hooks.session_context"
      }]
    }]
  }
}
```

Capture fleeting notes during a session:

```python
memory_add_bullet(
    node_slug=f"_fleeting-{session_id[:12]}",
    text="discovered: X",
    bullet_type="fact"
)
```
