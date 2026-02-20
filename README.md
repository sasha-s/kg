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
kg add <slug> <text> [--type fact|gotcha|decision|task]
kg search <query>  FTS5 search
kg context <query> ranked context for LLM injection
kg show <slug>     dump a node's bullets
```
