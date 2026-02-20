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

## Quickstart

```bash
pip install kg
kg init             # write kg.toml + create .kg/
kg add my-note "discovered: async context managers don't propagate cancellation"
kg search "async"
kg context "cancellation"
```

## Commands

```
kg init            create kg.toml and .kg/ directories
kg reindex         rebuild SQLite from all node.jsonl files
kg upgrade         reindex + schema migrations (safe to run anytime)
kg add <slug> <text> [--type fact|gotcha|decision|task]
kg show <slug>     dump a node's bullets
kg search <query>  FTS5 search
kg context <query> [-c] [--session ID] [--max-tokens N]
kg serve           start stdio MCP server
```

## MCP Server (Claude Code)

Add to `~/.claude/settings.json`:

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

MCP tools exposed (compatible with memory_graph):

| Tool | Description |
|------|-------------|
| `memory_context(query, session_id?)` | Ranked context for LLM injection |
| `memory_search(query, limit?)` | FTS search results |
| `memory_show(slug)` | Show node bullets |
| `memory_add_bullet(node_slug, text, bullet_type?)` | Add bullet |

## Session ID Hook

Auto-inject `session_id` into every Claude prompt — add to `~/.claude/settings.json`:

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
# Via MCP
memory_add_bullet(
    node_slug=f"_fleeting-{session_id[:12]}",
    text="discovered: X",
    bullet_type="fact"
)
```
