"""Stdio MCP server for kg.

Tools (named for compatibility with memory_graph MCP):
    memory_context(query, session_id?)   → compact context text
    memory_search(query, limit?)         → list of search results
    memory_show(slug)                    → node text
    memory_add_bullet(node_slug, text, bullet_type?, status?) → bullet_id

Session ID:
    Auto-injected by hooks/session_context.py via Claude's additionalContext.
    Passed as `session_id` parameter in memory_context calls.

Protocol: JSON-RPC 2.0 over stdin/stdout (MCP spec).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

_VERSION = "0.1.0"


def _tool_defs() -> list[dict[str, Any]]:
    return [
        {
            "name": "memory_context",
            "description": (
                "Search the knowledge graph and return ranked context for LLM injection. "
                "Use session_id (auto-injected) for differential context."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "session_id": {"type": "string", "description": "Session ID (auto-provided)"},
                    "max_tokens": {"type": "integer", "default": 1000},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["query"],
            },
        },
        {
            "name": "memory_search",
            "description": "FTS search over bullets. Returns ranked list of matching bullets.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["query"],
            },
        },
        {
            "name": "memory_show",
            "description": "Show all bullets for a node by slug.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                },
                "required": ["slug"],
            },
        },
        {
            "name": "memory_review",
            "description": "List nodes ordered by accumulated token budget — these need examination and maintenance.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "threshold": {"type": "number", "default": 500, "description": "Min credits to list"},
                    "limit": {"type": "integer", "default": 20},
                },
            },
        },
        {
            "name": "memory_mark_reviewed",
            "description": (
                "Mark a node as reviewed after you have examined it, updated stale bullets, "
                "and pushed relevant info to other nodes. Clears the token budget."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                },
                "required": ["slug"],
            },
        },
        {
            "name": "memory_add_bullet",
            "description": "Add a bullet to a node. Auto-creates node if it doesn't exist.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "node_slug": {"type": "string", "description": "Node slug (use _fleeting-<session_id[:12]> for session notes)"},
                    "text": {"type": "string"},
                    "bullet_type": {
                        "type": "string",
                        "enum": ["fact", "gotcha", "decision", "task", "note", "success", "failure"],
                        "default": "fact",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["pending", "completed", "archived"],
                        "description": "For task bullets",
                    },
                },
                "required": ["node_slug", "text"],
            },
        },
    ]


class KGServer:
    def __init__(self, config_root: Path | None = None) -> None:
        from kg.config import load_config
        self._cfg = load_config(config_root)
        self._cfg.ensure_dirs()

    def _call_memory_context(self, args: dict[str, Any]) -> str:
        from kg.context import build_context
        result = build_context(
            args["query"],
            db_path=self._cfg.db_path,
            nodes_dir=self._cfg.nodes_dir,
            max_tokens=int(args.get("max_tokens", 1000)),
            limit=int(args.get("limit", 20)),
        )
        if not result.nodes:
            return "(no results)"
        return result.format_compact()

    def _call_memory_search(self, args: dict[str, Any]) -> str:
        from kg.indexer import search_fts
        rows = search_fts(args["query"], self._cfg.db_path, limit=int(args.get("limit", 20)))
        if not rows:
            return "(no results)"
        lines = []
        for r in rows:
            lines.append(f"[{r['slug']}] {r['text'][:120]} ←{r['bullet_id']}")
        return "\n".join(lines)

    def _call_memory_show(self, args: dict[str, Any]) -> str:
        from kg.reader import FileStore
        store = FileStore(self._cfg.nodes_dir)
        slug = args["slug"]
        node = store.get(slug)
        if node is None:
            return f"Node not found: {slug}"
        live = node.live_bullets
        budget_info = f"  ↑{int(node.token_budget)} credits" if node.token_budget >= 100 else ""
        hint = node.review_hint(bullet_count=len(live))
        lines = [f"# {node.title} [{node.slug}]  type={node.type}  ●{len(live)} bullets{budget_info}"]
        if hint:
            bar = "─" * 60
            lines += [
                bar,
                f"⚠ NEEDS REVIEW: {int(node.token_budget)} credits, {len(live)} bullets  see [node-review]",
                bar,
            ]
        for b in live:
            prefix = f"({b.type}) " if b.type != "fact" else ""
            lines.append(f"- {prefix}{b.text}  ←{b.id}")
        return "\n".join(lines)

    def _call_memory_mark_reviewed(self, args: dict[str, Any]) -> str:
        from kg.indexer import index_node
        from kg.reader import FileStore
        slug = args["slug"]
        store = FileStore(self._cfg.nodes_dir)
        if not store.exists(slug):
            return f"Node not found: {slug}"
        store.clear_node_budget(slug)
        index_node(slug, nodes_dir=self._cfg.nodes_dir, db_path=self._cfg.db_path)
        return f"Marked reviewed: {slug}"

    def _call_memory_review(self, args: dict[str, Any]) -> str:
        import sqlite3
        if not self._cfg.db_path.exists():
            return "No index — run kg reindex first"
        threshold = float(args.get("threshold", 500))
        limit = int(args.get("limit", 20))
        conn = sqlite3.connect(str(self._cfg.db_path))
        rows = conn.execute(
            """SELECT slug, title, bullet_count, token_budget
               FROM nodes
               WHERE token_budget >= ? AND type NOT LIKE '_%'
               ORDER BY token_budget DESC LIMIT ?""",
            (threshold, limit),
        ).fetchall()
        conn.close()
        if not rows:
            return "No nodes need review — graph looks healthy."
        lines = [f"{'Credits':>8}  {'Bullets':>7}  Node", "-" * 50]
        for slug, title, bc, budget in rows:
            lines.append(f"{int(budget):>8}  {bc or 0:>7}  [{slug}] {title}")
        return "\n".join(lines)

    def _call_memory_add_bullet(self, args: dict[str, Any]) -> str:
        from kg.reader import FileStore
        store = FileStore(self._cfg.nodes_dir)
        bullet = store.add_bullet(
            args["node_slug"],
            text=args["text"],
            bullet_type=args.get("bullet_type", "fact"),
            status=args.get("status"),
        )
        from kg.indexer import index_node
        index_node(args["node_slug"], nodes_dir=self._cfg.nodes_dir, db_path=self._cfg.db_path)
        return bullet.id

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        dispatch = {
            "memory_context": self._call_memory_context,
            "memory_search": self._call_memory_search,
            "memory_show": self._call_memory_show,
            "memory_add_bullet": self._call_memory_add_bullet,
            "memory_mark_reviewed": self._call_memory_mark_reviewed,
            "memory_review": self._call_memory_review,
        }
        if name not in dispatch:
            msg = f"Unknown tool: {name}"
            raise ValueError(msg)
        return dispatch[name](arguments)


async def _run_server(config_root: Path | None = None) -> None:
    server = KGServer(config_root)
    reader = asyncio.StreamReader()
    loop = asyncio.get_event_loop()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)

    writer_transport, writer_protocol = await loop.connect_write_pipe(
        asyncio.BaseProtocol, sys.stdout.buffer
    )

    def write_json(obj: Any) -> None:
        line = json.dumps(obj) + "\n"
        writer_transport.write(line.encode())

    while True:
        try:
            line = await reader.readline()
        except (asyncio.IncompleteReadError, EOFError):
            break
        if not line:
            break
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = msg.get("method", "")
        msg_id = msg.get("id")

        if method == "initialize":
            write_json({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "kg", "version": _VERSION},
                },
            })

        elif method == "notifications/initialized":
            pass  # no response for notifications

        elif method == "tools/list":
            write_json({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"tools": _tool_defs()},
            })

        elif method == "tools/call":
            params = msg.get("params", {})
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            try:
                result_text = server.call_tool(tool_name, arguments)
                write_json({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": result_text}],
                        "isError": False,
                    },
                })
            except Exception as exc:
                write_json({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Error: {exc}"}],
                        "isError": True,
                    },
                })

        elif msg_id is not None:
            write_json({
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            })


def run_server(config_root: Path | None = None) -> None:
    """Entry point for `kg serve`."""
    asyncio.run(_run_server(config_root))
