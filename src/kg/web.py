"""Simple HTTP web viewer for the kg knowledge graph.

Routes:
    GET /              → node index (all nodes, alphabetical)
    GET /node/<slug>   → single node with full bullet list + backlinks
    GET /search?q=...  → FTS + vector blended search with reranker
"""

from __future__ import annotations

import html as _html
import json
import re
import socketserver
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from kg.config import KGConfig
    from kg.models import FileNode

# ─── Text rendering ───────────────────────────────────────────────────────────

_SLUG_RE = re.compile(r"\[\[([a-z0-9][a-z0-9\-]*[a-z0-9])\]\]")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_CODE_RE = re.compile(r"`(.+?)`")
_URL_RE = re.compile(r"https?://\S+")
# Matches path-like strings (at least one /) for auto-linkification to _doc-* nodes
_PATH_RE = re.compile(r"(?<![/\w])[a-zA-Z0-9_][a-zA-Z0-9_\-\.]*(?:/[a-zA-Z0-9_\-\.]+)+")
# Sentence boundary: ". " before uppercase, "[", or "(" — used to add visual line breaks
_SENT_RE = re.compile(r"\. (?=[A-Z\[\(])")
# Matches _fleeting-* node slugs in tool output
_FLEETING_RE = re.compile(r"(_fleeting-[a-z0-9_-]+)")


def _break_sentences(text: str) -> str:
    """Insert newlines at sentence boundaries, skipping backtick code spans."""
    parts = re.split(r"(`[^`]*`)", text)
    return "".join(_SENT_RE.sub(".\n", p) if i % 2 == 0 else p for i, p in enumerate(parts))


def _tool_summary(name: str, inp: dict) -> str:
    """Return a brief summary string to show inline in a tool call header."""
    if name in ("Read", "Write", "Edit"):
        p = inp.get("file_path", "")
        return ("…" + p[-55:]) if len(p) > 55 else p
    if name == "Bash":
        cmd = inp.get("command", "")
        first = cmd.split("\n")[0]
        return (first[:70] + "…") if len(first) > 70 else first
    if name == "Glob":
        return inp.get("pattern", "")[:60]
    if name == "Grep":
        pat = inp.get("pattern", "")[:40]
        g = inp.get("glob", "")
        return f"{pat} in {g}" if g else pat
    if name == "Task":
        st = inp.get("subagent_type", "")
        desc = inp.get("description", "")
        return (f"[{st}] {desc[:40]}" if desc else st)[:60]
    if name == "WebFetch":
        return inp.get("url", "")[:70]
    if name == "WebSearch":
        return inp.get("query", "")[:60]
    if name == "TodoWrite":
        todos = inp.get("todos", [])
        if isinstance(todos, list):
            n = len(todos)
            pending = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "in_progress")
            done = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "completed")
            parts = [f"{n} item{'s' if n != 1 else ''}"]
            if pending:
                parts.append(f"{pending} active")
            if done:
                parts.append(f"{done} done")
            return " · ".join(parts)
        return ""
    if name == "AskUserQuestion":
        qs = inp.get("questions", [])
        if isinstance(qs, list) and qs:
            q0 = qs[0]
            if isinstance(q0, dict):
                return q0.get("question", "")[:60]
    if name in ("NotebookEdit",):
        return inp.get("notebook_path", "")[:55]
    if "memory_add_bullet" in name:
        return inp.get("node_slug", "")[:50]
    if "memory_context" in name or "memory_search" in name:
        return inp.get("query", "")[:50]
    if "memory_show" in name:
        return inp.get("slug", "")[:50]
    if "send_message" in name:
        return f"→ {inp.get('to_agent', '')} · {inp.get('body', '')[:30]}"
    if "get_pending_messages" in name:
        return "inbox"
    return ""


def _render_tool_result(text: str, slugs: set[str]) -> str:
    """Escape tool result text and turn _fleeting-* node slugs into links."""
    parts: list[str] = []
    last = 0
    for m in _FLEETING_RE.finditer(text):
        slug = m.group(1)
        parts.append(_html.escape(text[last:m.start()]))
        if slug in slugs:
            parts.append(f'<a href="/node/{slug}" style="color:var(--ac)">{_html.escape(slug)}</a>')
        else:
            parts.append(_html.escape(slug))
        last = m.end()
    parts.append(_html.escape(text[last:]))
    return "".join(parts)


def _get_path_slugs(cfg: KGConfig) -> dict[str, str]:
    """Return {rel_path: doc_slug} for all indexed source files."""
    import contextlib
    result: dict[str, str] = {}
    with contextlib.suppress(Exception):
        from kg.db import get_conn
        conn = get_conn(cfg)
        result = dict(conn.execute("SELECT rel_path, slug FROM file_sources").fetchall())
        conn.close()
    return result


def _render(text: str, slugs: set[str], path_slugs: dict[str, str] | None = None) -> str:
    """Escape text and convert URLs, [[slug]] links, **bold**, `code`, file paths to HTML."""
    text = _break_sentences(text)

    def _link(m: re.Match[str]) -> str:
        s = m.group(1)
        return f'<a href="/node/{s}" data-slug="{s}">[[{s}]]</a>' if s in slugs else f'<span class="dead">[[{s}]]</span>'

    def _inner(seg: str) -> str:
        return _BOLD_RE.sub(
            r"<strong>\1</strong>",
            _SLUG_RE.sub(_link, _CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", _html.escape(seg).replace("\n", "<br>"))),
        )

    def _process_seg(seg: str) -> str:
        """Like _inner but also auto-links known file paths."""
        if not path_slugs:
            return _inner(seg)
        parts: list[str] = []
        last = 0
        for m in _PATH_RE.finditer(seg):
            p = m.group(0)
            slug = path_slugs.get(p)
            if slug:
                parts.append(_inner(seg[last : m.start()]))
                parts.append(
                    f'<a href="/node/{slug}" title="{_html.escape(p)}">'
                    f'<code style="color:var(--lk)">{_html.escape(p)}</code></a>'
                )
                last = m.end()
        parts.append(_inner(seg[last:]))
        return "".join(parts)

    # Extract URLs before HTML-escaping so href values are intact
    parts: list[str] = []
    last = 0
    for m in _URL_RE.finditer(text):
        parts.append(_process_seg(text[last : m.start()]))
        raw = m.group(0).rstrip(".,;:!?)'\"")
        parts.append(
            f'<a href="{_html.escape(raw)}" target="_blank" rel="noopener noreferrer">'
            f'{_html.escape(raw)}</a>'
        )
        trailing = text[m.start() + len(raw) : m.end()]
        if trailing:
            parts.append(_html.escape(trailing))
        last = m.end()
    parts.append(_process_seg(text[last:]))
    return "".join(parts)


# ─── File language detection ──────────────────────────────────────────────────

_EXT_LANG: dict[str, str] = {
    "py": "python", "js": "javascript", "ts": "typescript",
    "jsx": "javascript", "tsx": "typescript", "rs": "rust",
    "go": "go", "java": "java", "c": "c", "cpp": "cpp",
    "h": "c", "hpp": "cpp", "sh": "bash",
    "sql": "sql", "toml": "toml", "yaml": "yaml", "yml": "yaml",
    "json": "json", "html": "xml", "css": "css",
    "md": "markdown", "rst": "plaintext", "txt": "plaintext",
    "dockerfile": "dockerfile", "makefile": "makefile",
}
_MD_EXTS = {"md"}


def _file_lang(path: str) -> tuple[str, bool]:
    """Return (highlight.js language id, is_markdown) for a file path."""
    name = path.rsplit("/", 1)[-1].lower()
    if name in ("dockerfile", "makefile"):
        return name, False
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    return _EXT_LANG.get(ext, "plaintext"), ext in _MD_EXTS


# ─── CSS ──────────────────────────────────────────────────────────────────────

_CSS = """
:root{--bg:#0f1117;--sf:#161b22;--bd:#30363d;--tx:#e6edf3;--mt:#8b949e;--ac:#58a6ff;--lk:#79c0ff}
html.light{--bg:#ffffff;--sf:#f6f8fa;--bd:#d0d7de;--tx:#1f2328;--mt:#656d76;--ac:#0969da;--lk:#0969da}
html.light .bt-concept,.html.light .bt-other{background:#eaeef2;color:#57606a}
html.light .bt-task{background:#dbeafe;color:#1d4ed8}
html.light .bt-decision{background:#ede9fe;color:#6d28d9}
html.light .bt-agent{background:#d1fae5;color:#065f46}
html.light .bt-session{background:#ffedd5;color:#c2410c}
html.light .bt-doc{background:#e0e7ff;color:#4338ca}
html.light .bullet{background:var(--sf)}
html.light .ag-card{background:var(--sf)}
html.light .msg-in{background:var(--sf)}
html.light .msg-out{background:#f0f6fc}
html.light .tool-call{background:var(--sf)}
html.light .turn-assistant{border-left-color:rgba(0,0,0,.12)}
html.light code{background:rgba(175,184,193,.2)}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--tx);font-size:14px;line-height:1.6}
nav{position:sticky;top:0;background:var(--sf);border-bottom:1px solid var(--bd);padding:10px 20px;display:flex;gap:16px;align-items:center;z-index:100}
.brand{font-weight:600;color:var(--ac);text-decoration:none}
nav form{display:flex;flex:1;max-width:420px;gap:6px}
nav input{flex:1;background:var(--bg);border:1px solid var(--bd);border-radius:6px;color:var(--tx);padding:5px 10px;font-size:13px}
nav input:focus{outline:none;border-color:var(--ac)}
nav button{background:var(--ac);color:#0f1117;border:none;border-radius:6px;padding:5px 12px;cursor:pointer;font-size:13px;font-weight:600}
main{max-width:900px;margin:0 auto;padding:24px 20px}
h1{font-size:1.4rem;margin-bottom:10px}
h2{font-size:11px;color:var(--mt);text-transform:uppercase;letter-spacing:.06em;margin:28px 0 10px;padding-bottom:4px;border-bottom:1px solid var(--bd)}
a{color:var(--lk);text-decoration:none}
a:hover{text-decoration:underline}
.dead{color:var(--mt)}
code{font-family:"SF Mono",Consolas,monospace;font-size:12px;background:rgba(255,255,255,.07);padding:1px 5px;border-radius:3px}
.meta{color:var(--mt);font-size:12px;margin-bottom:18px}
.badge{display:inline-block;font-size:10px;font-weight:600;padding:1px 7px;border-radius:10px;text-transform:uppercase;letter-spacing:.04em;vertical-align:middle}
.bt-concept,.bt-other{background:#1f2937;color:#9ca3af}
.bt-task{background:#1e3a5f;color:#60a5fa}
.bt-decision{background:#312e81;color:#a78bfa}
.bt-agent{background:#064e3b;color:#34d399}
.bt-session{background:#451a03;color:#fb923c}
.bt-doc{background:#1c1c3a;color:#a5b4fc}
.node-list{display:flex;flex-direction:column;gap:3px}
.node-row{display:flex;gap:10px;align-items:baseline;padding:6px 10px;border-radius:6px;border:1px solid transparent}
.node-row:hover{background:var(--sf);border-color:var(--bd)}
.node-row .t{flex:1}
.node-row .m{color:var(--mt);font-size:12px;white-space:nowrap}
.bullets{display:flex;flex-direction:column;gap:5px}
.bullet{display:flex;gap:10px;padding:8px 12px;background:var(--sf);border-radius:6px;border-left:3px solid #374151}
.b-gotcha{border-color:#b45309}
.b-decision{border-color:#7c3aed}
.b-task{border-color:#2563eb}
.b-note{border-color:#059669}
.b-success{border-color:#10b981}
.b-failure{border-color:#dc2626}
.btp{flex-shrink:0;font-size:10px;color:var(--mt);text-transform:uppercase;letter-spacing:.05em;padding-top:3px;width:56px}
.btx{flex:1;word-break:break-word}
.bid{flex-shrink:0;font-size:10px;color:var(--mt);font-family:monospace;padding-top:3px}
.vt{font-size:11px;color:var(--mt);padding-top:3px;white-space:nowrap}
.sp{font-size:11px}
.sp-pending{color:#fbbf24}
.sp-completed{color:#34d399}
.sp-archived{color:var(--mt)}
.sg{margin-bottom:24px}
.sg h3{font-size:15px;margin-bottom:8px}
.sg h3 a{color:var(--tx);font-weight:600}
/* chunks */
.hidden{display:none!important}
.chunks-toggle{font-size:13px;cursor:pointer;user-select:none;display:flex;align-items:center;gap:6px;color:var(--mt);padding:3px 10px;border:1px solid var(--bd);border-radius:6px;background:var(--sf)}
.chunks-toggle:hover{border-color:var(--ac);color:var(--tx)}
.chunks-toggle input{cursor:pointer;accent-color:var(--ac)}
.chunks-section{display:flex;flex-direction:column;gap:0;margin-top:20px;border:1px solid var(--bd);border-radius:8px;overflow:hidden}
.chunk{border-bottom:1px solid var(--bd)}
.chunk:last-child{border-bottom:none}
.chunk-hdr{display:flex;align-items:center;justify-content:space-between;padding:4px 12px;background:rgba(255,255,255,.03);font-size:11px;color:var(--mt);font-family:monospace}
.chunk pre{margin:0;overflow-x:auto}
.chunk pre code.hljs{padding:14px 16px;background:transparent!important;font-size:12px;line-height:1.5}
/* agents */
.ag-filter{width:100%;max-width:340px;background:var(--bg);border:1px solid var(--bd);border-radius:6px;color:var(--tx);padding:6px 10px;font-size:13px;margin-bottom:14px}
.ag-filter:focus{outline:none;border-color:var(--ac)}
.ag-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px;margin-top:4px}
.ag-card{background:var(--sf);border:1px solid var(--bd);border-radius:8px;padding:14px 16px}
.ag-card h3{font-size:14px;font-weight:600;margin-bottom:6px}
.ag-card h3 a{color:var(--tx)}
.ag-status{display:inline-block;font-size:11px;padding:2px 8px;border-radius:10px;font-weight:600}
.ag-running{background:#064e3b;color:#34d399}
.ag-idle{background:#1f2937;color:#9ca3af}
.ag-paused{background:#3b2a00;color:#fbbf24}
.ag-draining{background:#1e3a5f;color:#60a5fa}
.ag-meta{font-size:11px;color:var(--mt);margin-top:4px}
.ag-ctrls{display:flex;gap:5px;margin-top:10px;flex-wrap:wrap}
.ag-btn{font-size:11px;padding:2px 8px;border-radius:4px;border:1px solid var(--bd);background:transparent;color:var(--tx);cursor:pointer}
.ag-btn:hover{background:var(--bd)}
.ag-btn-pause{border-color:#fbbf24;color:#fbbf24}
.ag-btn-resume{border-color:#34d399;color:#34d399}
.ag-btn-drain{border-color:#60a5fa;color:#60a5fa}
.ag-btn-archive{border-color:#6b7280;color:#6b7280}
.ag-btn-unarchive{border-color:#34d399;color:#34d399}
.ag-card.ag-archived{opacity:.45;border-style:dashed}
.ag-archived{background:#111827;color:#6b7280}
.ag-create-form{background:var(--sf);border:1px solid var(--bd);border-radius:8px;padding:14px 16px;margin-bottom:12px;display:none}
.ag-create-form input,.ag-create-form select{background:var(--bg);border:1px solid var(--bd);border-radius:6px;color:var(--tx);padding:5px 10px;font-size:13px;width:100%;margin-bottom:8px}
.ag-create-form button{background:var(--ac);color:#0f1117;border:none;border-radius:6px;padding:6px 14px;cursor:pointer;font-size:13px;font-weight:600}
.ag-info{display:flex;gap:12px;align-items:center;margin-bottom:16px;flex-wrap:wrap;padding:10px 14px;background:var(--sf);border:1px solid var(--bd);border-radius:8px}
.ag-info-lbl{font-size:12px;color:var(--mt)}
.ag-info-val{font-size:12px;color:var(--tx);font-family:monospace}
.msg-thread{display:flex;flex-direction:column;gap:8px;margin-top:12px;padding-right:4px}
.msg{padding:10px 14px;border-radius:8px;border-left:3px solid var(--bd)}
.msg-in{background:var(--sf);border-color:var(--ac)}
.msg-in.urgent{border-color:#f87171;background:rgba(248,113,113,.07)}
.msg-out{background:rgba(255,255,255,.03);border-color:#7c3aed}
.msg-hdr{font-size:11px;color:var(--mt);margin-bottom:4px}
.msg-body{font-size:13px;white-space:pre-wrap;word-break:break-word}
.chat-form{margin-top:16px;display:flex;flex-direction:column;gap:8px;border:1px solid var(--bd);border-radius:8px;padding:12px}
.chat-sticky{position:sticky;top:46px;z-index:50;background:var(--bg);padding-bottom:4px;margin-top:8px}
.chat-form textarea{background:var(--bg);border:1px solid var(--bd);border-radius:6px;color:var(--tx);padding:8px 10px;font-size:13px;resize:vertical;min-height:56px;width:100%;box-sizing:border-box}
.chat-form textarea:focus{outline:none;border-color:var(--ac)}
.chat-form-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.chat-send-btn{background:var(--ac);color:#0f1117;border:none;border-radius:6px;padding:7px 16px;cursor:pointer;font-size:13px;font-weight:600}
.chat-send-btn.urgent{background:#f87171;color:#fff}
.chat-urgent-lbl{display:flex;align-items:center;gap:5px;font-size:12px;color:var(--mt);cursor:pointer}
.send-form{margin-top:20px;display:flex;gap:8px}
.send-form textarea{flex:1;background:var(--bg);border:1px solid var(--bd);border-radius:6px;color:var(--tx);padding:8px 10px;font-size:13px;resize:vertical;min-height:60px}
.send-form textarea:focus{outline:none;border-color:var(--ac)}
.send-form button{background:var(--ac);color:#0f1117;border:none;border-radius:6px;padding:8px 14px;cursor:pointer;font-size:13px;font-weight:600;align-self:flex-end}
.session-list{display:flex;flex-direction:column;gap:4px;margin-top:10px}
.session-row{display:flex;gap:10px;align-items:center;padding:6px 10px;border-radius:6px;border:1px solid transparent}
.session-row:hover{background:var(--sf);border-color:var(--bd)}
/* session log */
.turn{margin-bottom:12px}
.turn-user{background:rgba(88,166,255,.08);border-left:3px solid var(--ac);padding:10px 14px;border-radius:0 6px 6px 0}
.turn-assistant{padding:10px 14px;border-left:3px solid rgba(255,255,255,.12);border-radius:0 6px 6px 0}
.turn-label{font-size:10px;color:var(--mt);text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px}
.turn-text{font-size:13px;white-space:pre-wrap;word-break:break-word}
.tool-call{background:var(--sf);border:1px solid var(--bd);border-radius:6px;margin:4px 0;overflow:hidden}
.tool-hdr{display:flex;align-items:center;gap:6px;padding:5px 10px;background:rgba(255,255,255,.04);font-size:11px;font-family:monospace;cursor:pointer;user-select:none}
.tool-hdr:hover{background:rgba(255,255,255,.09)}
.arr{font-size:9px;flex-shrink:0;width:9px;display:inline-block;text-align:center}
.tool-body{padding:10px;font-size:11px;font-family:monospace;white-space:pre-wrap;word-break:break-word;max-height:300px;overflow-y:auto;display:none}
.tool-body.open{display:block}
.tool-group{border:1px solid var(--bd);border-radius:6px;margin:6px 0;overflow:hidden}
.tool-group-hdr{padding:5px 10px;background:rgba(255,255,255,.04);font-size:11px;font-family:monospace;cursor:pointer;color:var(--mt);user-select:none;display:flex;align-items:center;gap:6px}
.tool-group-hdr:hover{background:rgba(255,255,255,.09)}
.tool-group-hdr .tg-count{color:var(--ac);font-weight:600}
.tool-group-body{padding:4px;display:none}
.tool-group-body.open{display:block}
.tool-result{background:rgba(40,180,99,.04);border-top:1px solid rgba(40,180,99,.18);overflow:hidden}
.tool-result-hdr{display:flex;align-items:center;gap:6px;padding:4px 10px;font-size:11px;font-family:monospace;cursor:pointer;color:rgba(40,180,99,.85);user-select:none}
.tool-result-hdr:hover{background:rgba(40,180,99,.10);color:rgba(40,180,99,1)}
.tool-result-body{padding:8px 10px;font-size:11px;font-family:monospace;white-space:pre-wrap;word-break:break-word;max-height:300px;overflow-y:auto;display:none;border-top:1px solid rgba(40,180,99,.12)}
.tool-result-body.open{display:block}
.thinking-block{background:rgba(255,200,0,.04);border:1px solid rgba(255,200,0,.18);border-radius:6px;margin:6px 0;overflow:hidden}
.thinking-hdr{display:flex;align-items:center;gap:8px;padding:4px 10px;font-size:10px;font-family:monospace;cursor:pointer;color:rgba(255,200,0,.7)}
.thinking-hdr:hover{background:rgba(255,200,0,.06)}
.thinking-body{padding:8px 10px;font-size:11px;font-family:monospace;white-space:pre-wrap;word-break:break-word;max-height:300px;overflow-y:auto;display:none;color:rgba(255,200,0,.8)}
.thinking-body.open{display:block}
.send-form{margin-top:20px;display:flex;gap:8px}
.send-form textarea{flex:1;background:var(--bg);border:1px solid var(--bd);border-radius:6px;color:var(--tx);padding:8px 10px;font-size:13px;resize:vertical;min-height:60px}
.send-form textarea:focus{outline:none;border-color:var(--ac)}
.send-form button{background:var(--ac);color:#0f1117;border:none;border-radius:6px;padding:8px 14px;cursor:pointer;font-size:13px;font-weight:600;align-self:flex-end}
/* markdown body */
.md-body{padding:16px;font-size:14px;line-height:1.7}
.md-body h1,.md-body h2,.md-body h3,.md-body h4{margin:1.2em 0 .4em;color:var(--tx);line-height:1.3}
.md-body h1{font-size:1.4em;border-bottom:1px solid var(--bd);padding-bottom:.3em}
.md-body h2{font-size:1.15em;border-bottom:1px solid var(--bd);padding-bottom:.2em}
.md-body h3{font-size:1em}
.md-body p{margin:0 0 .8em}
.md-body code{font-family:"SF Mono",Consolas,monospace;font-size:12px;background:rgba(255,255,255,.07);padding:1px 5px;border-radius:3px}
.md-body pre{background:#161b22;border:1px solid var(--bd);border-radius:6px;overflow-x:auto;margin:0 0 .8em}
.md-body pre code{background:none;padding:12px 14px;display:block;font-size:12px;line-height:1.5}
.md-body ul,.md-body ol{padding-left:1.6em;margin:0 0 .8em}
.md-body li{margin-bottom:.2em}
.md-body blockquote{border-left:3px solid var(--bd);margin:0 0 .8em 0;padding:.2em 0 .2em 1em;color:var(--mt)}
.md-body a{color:var(--lk)}
.md-body table{border-collapse:collapse;width:100%;margin:0 0 .8em;font-size:13px}
.md-body th,.md-body td{border:1px solid var(--bd);padding:6px 10px;text-align:left}
.md-body th{background:var(--sf)}
.md-body hr{border:none;border-top:1px solid var(--bd);margin:.8em 0}
/* docs toggle button */
.docs-toggle-btn{font-size:12px;cursor:pointer;color:var(--mt);background:none;border:1px solid var(--bd);border-radius:6px;padding:3px 10px;line-height:1.4}
.docs-toggle-btn:hover{border-color:var(--ac);color:var(--tx)}
/* hover preview popup */
.kg-pv{position:fixed;z-index:1000;background:var(--sf);border:1px solid var(--ac);border-radius:8px;padding:10px 14px;max-width:360px;box-shadow:0 4px 20px rgba(0,0,0,.5);pointer-events:none;display:none;font-size:12px;line-height:1.5}
.kg-pv.visible{display:block}
.kg-pv-title{font-weight:600;margin-bottom:6px;color:var(--tx);font-size:13px}
.kg-pv-bullet{color:var(--mt);margin-bottom:3px;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;word-break:break-word}
/* settings panel */
.settings-btn{background:none;border:1px solid var(--bd);border-radius:6px;color:var(--mt);cursor:pointer;font-size:13px;padding:4px 9px;line-height:1}
.settings-btn:hover{border-color:var(--ac);color:var(--tx)}
.settings-panel{display:none;position:fixed;top:46px;right:10px;background:var(--sf);border:1px solid var(--bd);border-radius:8px;padding:14px 16px;z-index:200;min-width:220px;box-shadow:0 4px 16px rgba(0,0,0,.4)}
.settings-panel.open{display:block}
.settings-row{display:flex;align-items:center;gap:10px;margin-bottom:10px;font-size:13px;color:var(--mt)}
.settings-row input[type=range]{flex:1;accent-color:var(--ac)}
/* SSE live indicator */
.live-dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:#34d399;margin-right:5px;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
/* Scroll-to-top button */
.scroll-top{position:fixed;bottom:20px;right:20px;background:var(--sf);border:1px solid var(--bd);border-radius:50%;width:36px;height:36px;display:none;align-items:center;justify-content:center;cursor:pointer;font-size:16px;color:var(--mt);z-index:150;box-shadow:0 2px 8px rgba(0,0,0,.3)}
.scroll-top:hover{border-color:var(--ac);color:var(--ac)}
.scroll-top.visible{display:flex}
/* Mobile responsive */
@media(max-width:640px){
  nav{padding:8px 12px;gap:8px;flex-wrap:wrap}
  nav form{order:3;flex:1 1 100%}
  .settings-btn{order:2}
  main{padding:14px 12px}
  h1{font-size:1.2rem}
  .ag-grid{grid-template-columns:1fr}
  .msg-thread{max-height:55vh}
  .bullet{padding:6px 8px}
  .ag-info{gap:8px;padding:8px 10px}
  .chat-form{padding:8px}
  .session-row{flex-wrap:wrap}
  .chunks-toggle{font-size:12px}
  .brand{font-size:15px}
}
"""

_NODE_TYPES = {"concept", "task", "decision", "agent", "session"}
_BULLET_COLORS = {"gotcha", "decision", "task", "note", "success", "failure"}

# CDN resources for doc pages
_HLJS_CSS = '<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.10.0/styles/github-dark.min.css">'
_HLJS_JS = '<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.10.0/highlight.min.js"></script>'
_MARKED_JS = '<script src="https://cdn.jsdelivr.net/npm/marked@9.1.6/marked.min.js"></script>'

# Shared JS for show/hide source-file sections — used on index + search pages.
# Works with any button label as long as it starts with "Show" or "Hide".
_TOGGLE_DOCS_JS = """
(function(){
  var K='kg-docs',sec=document.getElementById('docs-section'),btn=document.getElementById('docs-btn');
  function setDocs(on){
    if(!sec)return;
    if(on){sec.classList.remove('hidden');if(btn)btn.textContent=btn.textContent.replace(/^Show/,'Hide');}
    else{sec.classList.add('hidden');if(btn)btn.textContent=btn.textContent.replace(/^Hide/,'Show');}
  }
  if(localStorage.getItem(K)==='1')setDocs(true);
  window.toggleDocs=function(){
    var on=sec&&sec.classList.contains('hidden');
    setDocs(on);
    if(on)localStorage.setItem(K,'1');else localStorage.removeItem(K);
  };
})();
"""


def _badge(node_type: str) -> str:
    cls = f"bt-{node_type}" if node_type in _NODE_TYPES | {"doc"} else "bt-other"
    return f'<span class="badge {cls}">{_html.escape(node_type)}</span>'


_GLOBAL_JS = """
(function(){
  // Hover preview popup for [[slug]] links
  var popup=document.createElement('div');popup.className='kg-pv';popup.id='kg-pv';
  document.body.appendChild(popup);
  var cache={},hideTimer=0,currentSlug='';
  function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
  function showPv(el,slug){
    currentSlug=slug;clearTimeout(hideTimer);
    if(cache[slug]!==undefined){renderPv(el,cache[slug]);return;}
    cache[slug]=null;
    fetch('/api/preview/'+encodeURIComponent(slug))
      .then(function(r){return r.json();})
      .then(function(d){cache[slug]=d;if(currentSlug===slug)renderPv(el,d);})
      .catch(function(){cache[slug]=false;});
  }
  function renderPv(el,d){
    if(!d)return;
    var html='<div class="kg-pv-title">'+esc(d.title)+'</div>';
    (d.bullets||[]).slice(0,4).forEach(function(b){html+='<div class="kg-pv-bullet">'+esc(b)+'</div>';});
    popup.innerHTML=html;
    var r=el.getBoundingClientRect();
    var left=Math.max(4,Math.min(r.left,window.innerWidth-372));
    popup.style.left=left+'px';popup.style.top=(r.bottom+6)+'px';
    popup.classList.add('visible');
  }
  function hidePv(){hideTimer=setTimeout(function(){popup.classList.remove('visible');currentSlug='';},200);}
  document.addEventListener('mouseover',function(e){
    var a=e.target.closest?e.target.closest('a[data-slug]'):null;
    if(a)showPv(a,a.getAttribute('data-slug'));
  });
  document.addEventListener('mouseout',function(e){
    var a=e.target.closest?e.target.closest('a[data-slug]'):null;
    if(a)hidePv();
  });
  // Settings panel toggle
  var sbtn=document.getElementById('settings-btn');
  var spanel=document.getElementById('settings-panel');
  if(sbtn&&spanel){
    sbtn.addEventListener('click',function(e){e.stopPropagation();spanel.classList.toggle('open');});
    document.addEventListener('click',function(e){
      if(spanel.classList.contains('open')&&!spanel.contains(e.target)&&e.target!==sbtn)
        spanel.classList.remove('open');
    });
  }
  // Font size restore
  var fs=localStorage.getItem('kg-font-size');
  if(fs)document.body.style.fontSize=fs+'px';
  var fsinp=document.getElementById('font-size-input');
  if(fsinp){
    fsinp.value=fs||'14';
    document.getElementById('font-size-val').textContent=(fs||'14')+'px';
    fsinp.addEventListener('input',function(){
      document.body.style.fontSize=this.value+'px';
      localStorage.setItem('kg-font-size',this.value);
      var lbl=document.getElementById('font-size-val');
      if(lbl)lbl.textContent=this.value+'px';
    });
  }
  // Theme toggle
  function applyTheme(light){
    document.documentElement.classList.toggle('light',light);
    var btn=document.getElementById('theme-btn');
    if(btn)btn.textContent=light?'☀':'🌙';
    var chk=document.getElementById('theme-chk');
    if(chk)chk.checked=light;
  }
  var savedTheme=localStorage.getItem('kg-theme');
  applyTheme(savedTheme==='light');
  var tbtn=document.getElementById('theme-btn');
  if(tbtn){
    tbtn.addEventListener('click',function(){
      var isLight=document.documentElement.classList.contains('light');
      applyTheme(!isLight);
      localStorage.setItem('kg-theme',isLight?'dark':'light');
    });
  }
  var tchk=document.getElementById('theme-chk');
  if(tchk){
    tchk.addEventListener('change',function(){
      applyTheme(this.checked);
      localStorage.setItem('kg-theme',this.checked?'light':'dark');
    });
  }
  // Generic expand/collapse toggle — used by tool-hdr, tool-result-hdr, tool-group-hdr, thinking-hdr
  // Toggles .open on next sibling; toggles ▶/▼ on .arr span inside btn.
  window._tog=function(btn){
    var body=btn.nextElementSibling;
    if(!body)return;
    var open=body.classList.toggle('open');
    var arr=btn.querySelector('.arr');
    if(arr)arr.textContent=open?'▼':'▶';
  };
  // Keyboard shortcut: / to focus search (like GitHub/GitLab)
  document.addEventListener('keydown',function(e){
    if(e.key==='/'&&e.target.tagName!=='INPUT'&&e.target.tagName!=='TEXTAREA'&&!e.target.isContentEditable){
      e.preventDefault();
      var inp=document.querySelector('nav input[type=search]');
      if(inp){inp.focus();inp.select();}
    }
  });
  // Scroll-to-top button
  var stBtn=document.getElementById('scroll-top-btn');
  if(stBtn){
    window.addEventListener('scroll',function(){
      stBtn.classList.toggle('visible',window.scrollY>400);
    });
    stBtn.addEventListener('click',function(){window.scrollTo({top:0,behavior:'smooth'});});
  }
})();
"""


def _page(cfg: KGConfig, title: str, body: str, q: str = "", extra_head: str = "", extra_script: str = "") -> str:
    qesc = _html.escape(q)
    name = _html.escape(cfg.name)
    t = _html.escape(title)
    script_tag = f"<script>{extra_script}</script>" if extra_script else ""
    settings_panel = (
        '<div id="settings-panel" class="settings-panel">'
        '<div class="settings-row"><span class="settings-lbl">Font size</span>'
        '<input id="font-size-input" type="range" min="11" max="36" step="1" value="14">'
        '<span id="font-size-val" style="font-size:11px;color:var(--mt);min-width:26px">14px</span>'
        '</div>'
        '<div class="settings-row">'
        '<label style="display:flex;align-items:center;gap:8px;cursor:pointer;color:var(--mt)">'
        '<input type="checkbox" id="theme-chk" style="accent-color:var(--ac);width:15px;height:15px;cursor:pointer">'
        'Light mode'
        '</label>'
        '</div>'
        '</div>'
    )
    return (
        f'<!DOCTYPE html>\n<html lang="en">\n<head>'
        f'<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{t} — {name}</title>'
        f'<style>{_CSS}</style>'
        f'{extra_head}'
        f'</head>\n<body>'
        f'<nav><a class="brand" href="/">{name}</a>'
        f'<a href="/agents" style="color:var(--mt);font-size:13px">agents</a>'
        f'<form action="/search" method="get">'
        f'<input name="q" type="search" placeholder="Search nodes…" value="{qesc}" autocomplete="off">'
        f'<button type="submit">Search</button></form>'
        f'<button class="settings-btn" id="theme-btn" title="Toggle light/dark mode" style="margin-right:4px">🌙</button>'
        f'<button class="settings-btn" id="settings-btn" title="Settings">⚙</button>'
        f'</nav>'
        f'{settings_panel}'
        f'<main>{body}</main>'
        f'<button id="scroll-top-btn" class="scroll-top" title="Back to top" aria-label="Scroll to top">↑</button>'
        f'{script_tag}'
        f'<script>{_GLOBAL_JS}</script>'
        f'</body></html>'
    )


# ─── Page renderers ───────────────────────────────────────────────────────────

def _get_slugs_db(cfg: KGConfig) -> set[str]:
    """Return all node slugs from SQLite — O(1) DB read vs O(N) filesystem syscalls."""
    import contextlib
    with contextlib.suppress(Exception):
        from kg.db import get_conn
        conn = get_conn(cfg)
        rows = conn.execute("SELECT slug FROM nodes").fetchall()
        conn.close()
        return {r[0] for r in rows}
    from kg.reader import FileStore
    return set(FileStore(cfg.nodes_dir).list_slugs())


def _render_index(cfg: KGConfig) -> str:
    """Render the index page using SQLite — avoids reading every node.jsonl."""
    import contextlib
    all_rows: list[tuple[str, str, str, int]] = []
    with contextlib.suppress(Exception):
        from kg.db import get_conn
        conn = get_conn(cfg)
        all_rows = conn.execute(
            "SELECT slug, title, type, bullet_count FROM nodes ORDER BY title COLLATE NOCASE"
        ).fetchall()
        conn.close()

    public = [
        (s, t or s, nt or "concept", bc)
        for s, t, nt, bc in all_rows
        if not s.startswith("_")
    ]
    docs = [
        (s, t or s, bc)
        for s, t, nt, bc in all_rows
        if s.startswith("_doc-")
    ]

    html_rows = []
    for slug, title, node_type, bc in public:
        suffix = "" if bc == 1 else "s"
        html_rows.append(
            f'<div class="node-row">'
            f'<span class="t"><a href="/node/{_html.escape(slug)}">{_html.escape(title)}</a></span>'
            f'<span class="m">{_badge(node_type)}&nbsp;&nbsp;{bc} bullet{suffix}</span>'
            f'</div>'
        )

    doc_rows_html = []
    for slug, title, bc in docs:
        suffix = "" if bc == 1 else "s"
        doc_rows_html.append(
            f'<div class="node-row">'
            f'<span class="t"><a href="/node/{_html.escape(slug)}">'
            f'<code style="font-size:12px">{_html.escape(title)}</code></a></span>'
            f'<span class="m">{bc} chunk{suffix}</span>'
            f'</div>'
        )

    docs_section = ""
    docs_btn = ""
    if docs:
        docs_section = (
            f'<div id="docs-section" class="hidden" style="margin-top:24px">'
            f'<h2>Source files ({len(docs)})</h2>'
            f'<div class="node-list">{"".join(doc_rows_html)}</div>'
            f'</div>'
        )
        docs_btn = (
            f'<button class="docs-toggle-btn" id="docs-btn" onclick="toggleDocs()">'
            f'Show {len(docs)} source files'
            f'</button>'
        )

    body = (
        f'<div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;margin-bottom:10px">'
        f'<h1>{_html.escape(cfg.name)}</h1>'
        f'{docs_btn}'
        f'</div>'
        f'<p class="meta">{len(public)} nodes</p>'
        f'<div class="node-list">{"".join(html_rows)}</div>'
        + docs_section
    )

    return _page(cfg, cfg.name, body, extra_script=_TOGGLE_DOCS_JS)


# ─── Doc node (source file) renderer ─────────────────────────────────────────

def _github_link(source_path: str, rel_path: str) -> str | None:
    """Return GitHub blob URL (with file commit hash) for rel_path in source_path, or None."""
    import subprocess
    try:
        r = subprocess.run(
            ["git", "-C", source_path, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        if r.returncode != 0:
            return None
        remote_url = r.stdout.strip()

        # Normalise to https://github.com/user/repo
        m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", remote_url)
        if not m:
            return None
        base = f"https://github.com/{m.group(1)}"

        # Commit hash of the last change to this file (fall back to HEAD)
        log = subprocess.run(
            ["git", "-C", source_path, "log", "-1", "--format=%H", "--", rel_path],
            capture_output=True, text=True, timeout=3, check=False,
        )
        commit = log.stdout.strip() if log.returncode == 0 else ""
        if not commit:
            head = subprocess.run(
                ["git", "-C", source_path, "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=3, check=False,
            )
            commit = head.stdout.strip() if head.returncode == 0 else ""
        if not commit:
            return None

        return f"{base}/blob/{commit}/{rel_path}"
    except Exception:
        return None


def _get_doc_node(cfg: KGConfig, slug: str) -> dict | None:
    """Fetch a _doc-* source file node from SQLite. Returns None if not found."""
    import contextlib
    with contextlib.suppress(Exception):
        from kg.db import get_conn
        conn = get_conn(cfg)
        row = conn.execute(
            "SELECT title, bullet_count, created_at FROM nodes WHERE slug = ? AND type = 'doc'",
            (slug,),
        ).fetchone()
        if row is None:
            conn.close()
            return None
        title, bullet_count, created_at = row
        # Also fetch source metadata to build a GitHub link
        fs_row = conn.execute(
            "SELECT rel_path, source_name FROM file_sources WHERE slug = ?",
            (slug,),
        ).fetchone()
        chunks = conn.execute(
            "SELECT id, text FROM bullets WHERE node_slug = ? ORDER BY id",
            (slug,),
        ).fetchall()
        conn.close()

        github_url: str | None = None
        if fs_row is not None:
            rel_path, source_name = fs_row
            # Find matching SourceConfig by name (or first source if unnamed)
            for src in cfg.sources:
                if src.name == source_name or (not source_name and not src.name):
                    with contextlib.suppress(Exception):
                        github_url = _github_link(str(src.abs_path), rel_path)
                    break

        return {
            "slug": slug,
            "title": title or slug,
            "bullet_count": bullet_count,
            "created_at": created_at,
            "chunks": [(cid, text) for cid, text in chunks],
            "github_url": github_url,
        }
    return None


def _render_doc_page(cfg: KGConfig, doc: dict, show_chunks: bool) -> str:
    """Render a _doc-* source file node with syntax-highlighted / markdown chunks."""
    slug = doc["slug"]
    title = doc["title"]  # relative path e.g. "src/kg/cli.py"
    lang, is_md = _file_lang(title)
    n = doc["bullet_count"]
    created = f" · {doc['created_at'][:10]}" if doc.get("created_at") else ""

    # Build chunks HTML
    chunk_items: list[str] = []
    for cid, text in doc["chunks"]:
        esc = _html.escape(text)
        chars = len(text)
        hdr = (
            f'<div class="chunk-hdr">'
            f'<span>{_html.escape(cid)}</span>'
            f'<span>{chars:,} chars</span>'
            f'</div>'
        )
        if is_md:
            content = (
                f'<div class="md-body">'
                f'<pre class="md-raw hidden">{esc}</pre>'
                f'</div>'
            )
        else:
            content = f'<pre><code class="language-{lang}">{esc}</code></pre>'
        chunk_items.append(f'<div class="chunk">{hdr}{content}</div>')

    vis = "" if show_chunks else " hidden"
    chunks_section = (
        f'<div id="chunks-section" class="chunks-section{vis}">{"".join(chunk_items)}</div>'
        if chunk_items else ""
    )

    github_url = doc.get("github_url")
    gh_link = (
        f' <a href="{_html.escape(github_url)}" target="_blank" rel="noopener noreferrer"'
        f' style="font-size:0.8rem;opacity:0.7;text-decoration:none" title="View on GitHub">GitHub ↗</a>'
        if github_url else ""
    )
    lbl = f"Show {n} chunk{'s' if n != 1 else ''}"
    body = (
        f'<div style="display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:12px;margin-bottom:8px">'
        f'<h1><code style="font-size:1.1rem;background:none;padding:0">{_html.escape(title)}</code></h1>'
        f'<div style="display:flex;align-items:center;gap:12px">'
        f'{gh_link}'
        f'<label class="chunks-toggle">'
        f'<input type="checkbox" id="toggle-chunks" {"checked" if show_chunks else ""}>'
        f' <span id="toggle-label">{lbl if not show_chunks else lbl.replace("Show","Hide")}</span>'
        f'</label>'
        f'</div>'
        f'</div>'
        f'<p class="meta">{_badge("doc")} [{_html.escape(slug)}] · {n} chunk{"s" if n != 1 else ""} · {lang}{created}</p>'
        + chunks_section
    )

    # CDN scripts
    extra_head = _HLJS_CSS + _HLJS_JS + (_MARKED_JS if is_md else "")

    # JS: toggle + syntax highlight + markdown render + localStorage persist
    restore_js = (
        # Restore from localStorage if ?chunks not in URL
        "var u=new URL(window.location);"
        "if(!u.searchParams.has('chunks')&&localStorage.getItem('kg-chunks')==='0'){"
        "  u.searchParams.set('chunks','0');location.replace(u.toString());}"
    )
    toggle_js = (
        "(function(){"
        + restore_js +
        "  var sec=document.getElementById('chunks-section');"
        "  var cb=document.getElementById('toggle-chunks');"
        "  var lbl=document.getElementById('toggle-label');"
        "  function setLabel(on){"
        "    if(lbl)lbl.textContent=lbl.textContent.replace(on?'Show':'Hide',on?'Hide':'Show');"
        "  }"
        "  if(cb)cb.addEventListener('change',function(){"
        "    var on=this.checked;"
        "    var u=new URL(window.location);"
        "    if(on){u.searchParams.set('chunks','1');localStorage.setItem('kg-chunks','1');}"
        "    else{u.searchParams.delete('chunks');localStorage.removeItem('kg-chunks');}"
        "    history.replaceState(null,'',u.toString());"
        "    if(sec){if(on)sec.classList.remove('hidden');else sec.classList.add('hidden');}"
        "    setLabel(on);"
        "  });"
    )
    if is_md:
        toggle_js += (
            "  document.querySelectorAll('.md-raw').forEach(function(el){"
            "    var raw=el.textContent;"
            "    var div=el.parentElement;"
            "    div.innerHTML=marked.parse(raw);"
            "  });"
        )
    else:
        toggle_js += "  if(typeof hljs!=='undefined')hljs.highlightAll();"
    toggle_js += "})();"

    return _page(cfg, title, body, extra_head=extra_head, extra_script=toggle_js)


def _agent_link_for_node(cfg: KGConfig, slug: str, node_type: str) -> str:
    """Return an HTML link to the agent page if this node is agent-related, else ''."""
    import contextlib

    def _agent_link(name: str, label: str = "") -> str:
        lbl = label or f"→ agent page ({_html.escape(name)})"
        return f'<a href="/agent/{_html.escape(name)}" style="font-size:12px;color:var(--ac);margin-left:10px">{lbl}</a>'

    def _toml_exists(name: str) -> bool:
        with contextlib.suppress(Exception):
            return (cfg.root / ".kg" / "agents" / f"{name}.toml").exists()
        return False

    # Direct: agent-type nodes link to /agent/<slug>
    if node_type == "agent":
        return _agent_link(slug, "→ agent page")
    # Pattern: agent-<name>-mission, agent-<name>-instructions, agent-<name>-knowledge
    # TOML name is <name> (without agent- prefix), so strip both suffixes.
    for suffix in ("-mission", "-instructions", "-knowledge"):
        if slug.endswith(suffix):
            base = slug[: -len(suffix)]  # e.g. "agent-improve-web"
            if _toml_exists(base):
                return _agent_link(base)
            # Try stripping "agent-" prefix too (e.g. TOML is "improve-web.toml")
            if base.startswith("agent-"):
                short = base[len("agent-"):]
                if short and _toml_exists(short):
                    return _agent_link(short)
    # Pattern: agent-<name> (memory/working node) — TOML is <name>.toml
    if slug.startswith("agent-"):
        base = slug[len("agent-"):]
        if base and _toml_exists(base):
            return _agent_link(base)
    return ""


def _render_node_page(cfg: KGConfig, node: FileNode, slugs: set[str]) -> str:
    path_slugs = _get_path_slugs(cfg)
    items = []
    for b in node.live_bullets:
        bc = f"b-{b.type}" if b.type in _BULLET_COLORS else ""
        sp = f' <span class="sp sp-{b.status}">({b.status})</span>' if b.status else ""
        votes = ""
        if b.useful or b.harmful:
            votes = f'<span class="vt">+{b.useful}/-{b.harmful}</span>'
        items.append(
            f'<div class="bullet {bc}">'
            f'<span class="btp">{_html.escape(b.type)}</span>'
            f'<span class="btx">{_render(b.text, slugs, path_slugs)}{sp}</span>'
            f'{votes}'
            f'<span class="bid">{_html.escape(b.id)}</span>'
            f'</div>'
        )
    bc = len(node.live_bullets)
    s = "" if bc == 1 else "s"
    created = f" · created {node.created_at[:10]}" if node.created_at else ""
    agent_link = _agent_link_for_node(cfg, node.slug, node.type)
    back_lnk = f'<a href="/" style="font-size:12px;color:var(--mt);text-decoration:none">← all nodes</a>'
    body = (
        f'<div style="margin-bottom:8px">{back_lnk}</div>'
        f'<h1 style="margin-top:4px">{_html.escape(node.title)}{agent_link}</h1>'
        f'<p class="meta">{_badge(node.type)} '
        f'[{_html.escape(node.slug)}] · {bc} bullet{s}{created}</p>'
        f'<div class="bullets">{"".join(items)}</div>'
    )

    from kg.indexer import get_backlinks
    from_slugs = get_backlinks(node.slug, cfg.db_path, cfg)
    bl = _backlinks_html(cfg, node.slug, from_slugs, slugs, path_slugs)
    if bl:
        body += f"<h2>Referenced by</h2>{bl}"

    # Related is lazy-loaded via JS to avoid blocking page render
    body += _related_placeholder(node.slug)

    return _page(cfg, node.title, body)


def _backlinks_html(cfg: KGConfig, slug: str, from_slugs: list[str], slugs: set[str], path_slugs: dict[str, str] | None = None) -> str:
    """Render nodes that link to *slug*, grouped with their referencing bullets."""
    if not from_slugs:
        return ""
    from kg.reader import FileStore
    store = FileStore(cfg.nodes_dir)
    parts: list[str] = []
    for from_slug in sorted(from_slugs):
        node = store.get(from_slug)
        if node is None:
            continue
        refs = [b for b in node.live_bullets if f"[{slug}]" in b.text]
        if not refs:
            continue
        title = _html.escape(node.title or from_slug)
        bullets_html = "".join(
            f'<div class="bullet"><span class="btx">{_render(b.text, slugs, path_slugs)}</span></div>'
            for b in refs[:4]
        )
        parts.append(
            f'<div class="sg">'
            f'<h3><a href="/node/{from_slug}">{title}</a>'
            f' <span style="font-weight:normal;color:var(--mt);font-size:12px">[{_html.escape(from_slug)}]</span></h3>'
            f'<div class="bullets">{bullets_html}</div>'
            f'</div>'
        )
    return "".join(parts)


def _related_html(cfg: KGConfig, node: FileNode, exclude: set[str]) -> str:
    """Find semantically related nodes via search on this node's content."""
    query_parts = [node.title] + [b.text for b in node.live_bullets[:6]]
    query = " ".join(query_parts)[:600]
    try:
        results = _do_search(query, cfg, limit=15)
    except Exception:
        return ""
    items: list[str] = []
    for r in results:
        s = r["slug"]
        if s == node.slug or s in exclude:
            continue
        if s.startswith("_") and not s.startswith("_doc-"):
            continue
        raw_title = r.get("title") or s
        if s.startswith("_doc-"):
            title_html = f'<a href="/node/{s}"><code style="font-size:12px">{_html.escape(raw_title)}</code></a>'
            # Show first matching chunk as a short preview
            chunk_text = r["bullets"][0]["text"] if r.get("bullets") else ""
            preview = chunk_text[:200].replace("\n", " ").strip()
            if len(chunk_text) > 200:
                preview += "…"
            preview_html = (
                f'<span class="m" style="display:block;margin-top:2px;font-family:monospace;font-size:11px">'
                f'{_html.escape(preview)}</span>'
            ) if preview else ""
            items.append(
                f'<div class="node-row" style="flex-direction:column;align-items:flex-start">'
                f'{title_html}{preview_html}'
                f'</div>'
            )
        else:
            title = _html.escape(raw_title)
            items.append(
                f'<div class="node-row">'
                f'<span class="t"><a href="/node/{s}">{title}</a></span>'
                f'<span class="m">[{_html.escape(s)}]</span>'
                f'</div>'
            )
        if len(items) >= 6:
            break
    if not items:
        return ""
    return f'<div class="node-list">{"".join(items)}</div>'


def _preview_json(cfg: KGConfig, slug: str) -> str:
    """Return JSON {title, bullets} for hover preview of a node."""
    import contextlib
    from kg.reader import FileStore
    node = FileStore(cfg.nodes_dir).get(slug)
    if node is None:
        return "{}"
    bullets = [b.text[:120] for b in node.live_bullets[:4]]
    data = {"title": node.title or slug, "bullets": bullets}
    return json.dumps(data)


def _related_placeholder(slug: str) -> str:
    """Render a placeholder div + script that fetches /api/related/<slug> lazily."""
    esc = _html.escape(slug)
    # Build script without f-string braces to avoid escaping complexity
    script = (
        'fetch("/api/related/' + esc + '")'
        '.then(function(r){return r.text()})'
        '.then(function(h){if(h){document.getElementById("kg-related").innerHTML=h}})'
        '.catch(function(){})'
    )
    return (
        '<h2>Related</h2>'
        '<div id="kg-related"><p class="meta" style="opacity:.5">Loading\u2026</p></div>'
        f'<script>{script}</script>'
    )


def _render_search_page(
    cfg: KGConfig,
    query: str,
    results: list[dict],  # [{slug, title, bullets: [{text, bullet_id}]}]
    slugs: set[str],
) -> str:
    if not results:
        body = (
            f'<h1>"{_html.escape(query)}"</h1>'
            f'<p class="meta">No results.</p>'
        )
        return _page(cfg, f"Search: {query}", body, q=query)

    concept_results = [r for r in results if not r["slug"].startswith("_doc-")]
    doc_results = [r for r in results if r["slug"].startswith("_doc-")]

    parts = []
    for r in concept_results:
        slug = r["slug"]
        title = _html.escape(r.get("title") or slug)
        node_href = f"/node/{slug}"
        title_html = f'<a href="{node_href}">[[{_html.escape(slug)}]]</a> {title}'
        items = []
        for b in r["bullets"]:
            items.append(
                f'<div class="bullet">'
                f'<span class="btx">{_render(b["text"], slugs)}</span>'
                f'<span class="bid">{_html.escape(b["bullet_id"])}</span>'
                f'</div>'
            )
        parts.append(
            f'<div class="sg">'
            f'<h3>{title_html}</h3>'
            f'<div class="bullets">{"".join(items)}</div>'
            f'</div>'
        )

    doc_parts = []
    need_hljs = False
    need_marked = False
    for r in doc_results:
        slug = r["slug"]
        raw_title = r.get("title") or slug
        title = _html.escape(raw_title)
        node_href = f"/node/{slug}"
        title_html = f'<a href="{node_href}"><code style="font-size:13px">{title}</code></a>'
        lang, is_md = _file_lang(raw_title)
        if is_md:
            need_marked = True
        else:
            need_hljs = True
        items = []
        for b in r["bullets"]:
            text = b["text"]
            preview = text[:800] + ("…" if len(text) > 800 else "")
            esc = _html.escape(preview)
            if is_md:
                chunk_html = f'<div class="md-body"><pre class="md-raw hidden">{esc}</pre></div>'
            else:
                chunk_html = f'<pre style="margin:0"><code class="language-{lang}" style="font-size:11px;line-height:1.4">{esc}</code></pre>'
            items.append(
                f'<div class="chunk" style="border-radius:6px;overflow:hidden;margin-bottom:6px">'
                f'{chunk_html}'
                f'</div>'
            )
        doc_parts.append(
            f'<div class="sg">'
            f'<h3>{title_html}</h3>'
            f'{"".join(items)}'
            f'</div>'
        )

    docs_section = ""
    docs_btn = ""
    if doc_parts:
        docs_section = (
            f'<div id="docs-section" class="hidden" style="margin-top:24px">'
            f'<h2>Source files ({len(doc_results)})</h2>'
            + "".join(doc_parts)
            + "</div>"
        )
        docs_btn = (
            f'<button class="docs-toggle-btn" id="docs-btn" onclick="toggleDocs()">'
            f'Show {len(doc_results)} source file result{"s" if len(doc_results) != 1 else ""}'
            f'</button>'
        )

    extra_head = ""
    extra_script = _TOGGLE_DOCS_JS
    if need_hljs or need_marked:
        extra_head = _HLJS_CSS + _HLJS_JS + (_MARKED_JS if need_marked else "")
        hljs_init = (
            "document.querySelectorAll('.md-raw').forEach(function(el){"
            "var raw=el.textContent;var div=el.parentElement;div.innerHTML=marked.parse(raw);});"
            if need_marked else ""
        )
        extra_script += (
            "(function(){"
            + hljs_init
            + "if(typeof hljs!=='undefined')hljs.highlightAll();"
            + "})();"
        )

    body = (
        f'<h1>"{_html.escape(query)}"</h1>'
        f'<p class="meta">{len(concept_results)} nodes matched  {docs_btn}</p>'
        + "".join(parts)
        + docs_section
    )
    return _page(cfg, f"Search: {query}", body, q=query, extra_head=extra_head, extra_script=extra_script)


def _render_log_page(cfg: KGConfig, name: str, lines: int = 200) -> str:
    import contextlib
    log_path = cfg.launcher_log_path if name == "launcher" else None
    if log_path is None:
        return _render_404(cfg, f"log/{name}")
    content = ""
    with contextlib.suppress(Exception):
        if log_path.exists():
            all_lines = log_path.read_text(errors="replace").splitlines()
            content = "\n".join(all_lines[-lines:])
    if not content:
        content = f"(log empty or not found: {log_path})"
    back = f'<a href="/agents" style="font-size:12px;color:var(--mt)">← agents</a>'
    # Auto-refresh every 5s
    refresh_js = (
        '<script>setTimeout(function(){location.reload();},5000);</script>'
    )
    body = (
        f'{back}<h1 style="margin-top:8px">{_html.escape(name)} log</h1>'
        f'<p style="font-size:11px;color:var(--mt)">Last {lines} lines · {_html.escape(str(log_path))} · auto-refresh 5s</p>'
        f'<pre id="log-pre" style="font-size:11px;background:var(--bg);border:1px solid var(--bd);'
        f'border-radius:6px;padding:12px;overflow-x:auto;white-space:pre-wrap;'
        f'word-break:break-all;max-height:80vh;overflow-y:auto">'
        f'{_html.escape(content)}</pre>'
        f'<script>var p=document.getElementById("log-pre");if(p)p.scrollTop=p.scrollHeight;</script>'
        f'{refresh_js}'
    )
    return _page(cfg, f"{name} log", body)


def _render_404(cfg: KGConfig, what: str) -> str:
    body = f'<h1>Not found</h1><p class="meta">{_html.escape(what)}</p>'
    return _page(cfg, "Not found", body)


# ─── Search (FTS + vector + reranker) ─────────────────────────────────────────

def _do_search(query: str, cfg: KGConfig, limit: int = 30) -> list[dict]:
    """FTS + vector blend + reranker → ranked [{slug, title, bullets}]."""
    import contextlib

    from kg.db import get_conn
    from kg.indexer import get_calibration, score_to_quantile, search_fts

    raw = search_fts(query, cfg.db_path, limit=limit * 3, cfg=cfg)

    # Group by slug, track best FTS score (negated BM25, higher = better)
    groups: dict[str, list[dict]] = {}
    fts_scores: dict[str, float] = {}
    for r in raw:
        slug = r["slug"]
        # Include _doc-* but skip other _ prefixed internal nodes
        if slug.startswith("_") and not slug.startswith("_doc-"):
            continue
        if slug not in groups:
            groups[slug] = []
            fts_scores[slug] = -r["rank"]
        groups[slug].append(r)

    # Vector search (optional — requires vector server running)
    vec_scores: dict[str, float] = {}
    with contextlib.suppress(Exception):
        from kg.vector_client import search_vector
        for slug, score in search_vector(query, cfg, k=limit * 3):
            if slug.startswith("_") and not slug.startswith("_doc-"):
                continue
            vec_scores[slug] = float(score)
            if slug not in groups:
                groups[slug] = []
                fts_scores[slug] = 0.0

    if not groups:
        return []

    # Rank fusion with calibration fallback (mirrors context._rank_slugs)
    fts_w = cfg.search.fts_weight
    vec_w = cfg.search.vector_weight
    dual_bonus = cfg.search.dual_match_bonus

    fts_cal = get_calibration("fts", cfg.db_path, cfg)
    fts_doc_cal = get_calibration("fts_doc", cfg.db_path, cfg)
    vec_cal = get_calibration("vector", cfg.db_path, cfg)
    fts_breaks = fts_cal[1] if fts_cal else None
    fts_doc_breaks = fts_doc_cal[1] if fts_doc_cal else None
    vec_breaks = vec_cal[1] if vec_cal else None

    fts_ranked = sorted(fts_scores.items(), key=lambda x: x[1], reverse=True)
    n_fts = len(fts_ranked)
    fts_rank_pos = {s: i for i, (s, _) in enumerate(fts_ranked)}

    def _score(slug: str) -> float:
        fts_raw = fts_scores.get(slug, 0.0)
        vec_raw = vec_scores.get(slug, 0.0)
        breaks = fts_doc_breaks if slug.startswith("_doc-") else fts_breaks
        if breaks and fts_raw > 0:
            fts_q = score_to_quantile(fts_raw, breaks)
        elif n_fts > 1:
            pos = fts_rank_pos.get(slug, n_fts - 1)
            fts_q = 1.0 - pos / (n_fts - 1)
        else:
            fts_q = 1.0 if fts_raw > 0 else 0.0
        vec_q = score_to_quantile(vec_raw, vec_breaks) if vec_breaks and vec_raw > 0 else vec_raw
        bonus = dual_bonus if (fts_raw > 0 and vec_raw > 0) else 0.0
        return fts_w * fts_q + vec_w * vec_q + bonus

    ranked = sorted(groups, key=_score, reverse=True)[:limit]

    # Cross-encoder rerank (skip for doc chunks — use node-level text)
    if cfg.search.use_reranker and len(ranked) >= 2:
        with contextlib.suppress(Exception):
            from kg.reader import FileStore
            from kg.reranker import rerank
            store = FileStore(cfg.nodes_dir)
            candidates: list[tuple[str, str]] = []
            for slug in ranked:
                if slug.startswith("_doc-"):
                    # Use chunk texts as candidate text
                    text = " ".join(b["text"] for b in groups[slug][:3])
                    candidates.append((slug, text))
                else:
                    node = store.get(slug)
                    if node:
                        text = node.title + " " + " ".join(b.text for b in node.live_bullets[:5])
                        candidates.append((slug, text))
            if len(candidates) >= 2:
                reranked = rerank(query, candidates, cfg)
                ranked = [s for s, _ in reranked]

    # Fetch titles from DB
    titles: dict[str, str] = {}
    if cfg.db_path.exists():
        with contextlib.suppress(Exception):
            conn = get_conn(cfg)
            ph = ",".join("?" * len(ranked))
            titles = dict(
                conn.execute(
                    f"SELECT slug, title FROM nodes WHERE slug IN ({ph})",  # noqa: S608
                    ranked,
                ).fetchall()
            )
            conn.close()

    return [
        {"slug": s, "title": titles.get(s, s), "bullets": groups.get(s, [])}
        for s in ranked
    ]


# ─── Agent pages ──────────────────────────────────────────────────────────────


def _mux_agents(cfg: KGConfig) -> list[dict]:
    """Return agents list, merged from mux.db (runtime state), TOML defs (config), and messages.db (counts)."""
    import contextlib
    import sqlite3
    agents_by_name: dict[str, dict] = {}

    # 1. Runtime state from mux.db
    with contextlib.suppress(Exception):
        conn = sqlite3.connect(str(cfg.mux_db_path))
        conn.row_factory = sqlite3.Row
        for row in conn.execute("SELECT * FROM agents ORDER BY name"):
            a = dict(row)
            a.setdefault("pending_count", 0)
            a.setdefault("toml_status", "running")
            a.setdefault("node", "")
            a.setdefault("model", "")
            agents_by_name[a["name"]] = a
        conn.close()

    # 2. TOML definitions: toml_status (paused/draining), node, model
    agents_dir = cfg.root / ".kg" / "agents"
    if agents_dir.exists():
        from kg.agents.launcher import AgentDef  # type: ignore[attr-defined]
        for path in sorted(agents_dir.glob("*.toml")):
            with contextlib.suppress(Exception):
                defn = AgentDef.from_toml(path)
                if defn.name not in agents_by_name:
                    agents_by_name[defn.name] = {
                        "name": defn.name, "status": "idle",
                        "last_seen": None, "pending_count": 0,
                    }
                agents_by_name[defn.name]["toml_status"] = defn.status
                agents_by_name[defn.name]["node"] = defn.node
                agents_by_name[defn.name]["model"] = defn.model
                agents_by_name[defn.name]["_has_toml"] = True

    # 3. Pending counts (unacked only) from project-local messages.db
    with contextlib.suppress(Exception):
        if cfg.messages_db_path.exists():
            conn = sqlite3.connect(str(cfg.messages_db_path))
            for name, cnt in conn.execute(
                "SELECT to_agent, COUNT(*) FROM messages WHERE acked=0 GROUP BY to_agent"
            ).fetchall():
                if name in agents_by_name:
                    agents_by_name[name]["pending_count"] = cnt
            conn.close()

    # Only show agents with a TOML definition (filters out auto-registered pseudo-agents like "web")
    result = [a for a in agents_by_name.values() if a.get("_has_toml")]
    if not result:
        result = list(agents_by_name.values())  # fallback if no TOML agents
    return sorted(result, key=lambda a: a["name"])


def _mux_agent_messages(cfg: KGConfig, agent_name: str, limit: int = 100) -> list[dict]:
    """Return recent messages to/from agent_name from project-local messages.db."""
    import contextlib
    import sqlite3
    msgs: list[dict] = []
    with contextlib.suppress(Exception):
        if cfg.messages_db_path.exists():
            conn = sqlite3.connect(str(cfg.messages_db_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM messages WHERE to_agent=? OR from_agent=?"
                " ORDER BY id DESC LIMIT ?",
                (agent_name, agent_name, limit),
            ).fetchall()
            msgs = list(reversed([dict(r) for r in rows]))
            conn.close()
    return msgs


def _render_agents_page(cfg: KGConfig) -> str:
    all_agents = _mux_agents(cfg)
    slugs = _get_slugs_db(cfg)

    # Separate active vs archived
    active_agents = [a for a in all_agents if a.get("toml_status") != "archived"]
    archived_agents = [a for a in all_agents if a.get("toml_status") == "archived"]

    def _make_card(a: dict) -> str:
        name_e = _html.escape(a["name"])
        is_archived = a.get("toml_status") == "archived"
        # Runtime status badge
        rt_status = a.get("status", "idle")
        rt_cls = "ag-running" if rt_status == "running" else "ag-idle"
        rt_badge = f'<span class="ag-status {rt_cls}">{rt_status}</span>'
        # TOML status badge
        ts = a.get("toml_status", "running")
        toml_badge = ""
        if ts == "paused":
            toml_badge = '<span class="ag-status ag-paused" style="margin-left:5px">paused</span>'
        elif ts == "draining":
            toml_badge = '<span class="ag-status ag-draining" style="margin-left:5px">draining</span>'
        elif ts == "archived":
            toml_badge = '<span class="ag-status ag-archived" style="margin-left:5px">archived</span>'
        # Pending
        n = a.get("pending_count", 0)
        pending_str = f' <span style="color:#fbbf24;font-size:11px">({n} pending)</span>' if n else ""
        # Meta row
        node = a.get("node", "")
        model = a.get("model", "")
        meta_parts = []
        if node:
            meta_parts.append(f"node: {_html.escape(node)}")
        if model:
            meta_parts.append(f"model: {_html.escape(model)}")
        last = (a.get("last_seen") or "")[:19]
        if last:
            meta_parts.append(last)
        meta = f'<div class="ag-meta">{" · ".join(meta_parts)}</div>' if meta_parts else ""
        # KG node / mission links (if those nodes exist)
        _lnk_style = "font-size:11px;color:var(--ac);text-decoration:none;margin-right:8px"
        kg_links = ""
        if f"agent-{a['name']}" in slugs:
            kg_links += f'<a href="/node/agent-{name_e}" style="{_lnk_style}">KG node</a>'
        for _ms in ("-mission", "-instructions"):
            _ms_slug = f"agent-{a['name']}{_ms}"
            if _ms_slug in slugs:
                kg_links += f'<a href="/node/{_html.escape(_ms_slug)}" style="{_lnk_style}">mission</a>'
                break
        kg_links_html = f'<div style="margin-top:6px">{kg_links}</div>' if kg_links else ""
        # Control buttons — fetch()-based to avoid mobile "insecure form" warnings
        _cu = f"/agent/{name_e}/ctrl"
        def _cbtn(action: str, cls: str, label: str) -> str:
            return (
                f'<button class="ag-btn {cls}" '
                f'onclick="fetch(\'{_cu}\',{{method:\'POST\','
                f'headers:{{\'Content-Type\':\'application/x-www-form-urlencoded\'}},'
                f'body:\'action={action}\'}}).then(function(){{location.reload();}}).catch(function(){{}});">'
                f'{label}</button>'
            )
        ctrls = '<div class="ag-ctrls">'
        if is_archived:
            ctrls += _cbtn("unarchive", "ag-btn-unarchive", "unarchive")
        elif ts == "paused":
            ctrls += _cbtn("resume", "ag-btn-resume", "resume")
            ctrls += " " + _cbtn("archive", "ag-btn-archive", "archive")
        elif ts == "draining":
            ctrls += _cbtn("resume", "ag-btn-resume", "resume")
            ctrls += " " + _cbtn("archive", "ag-btn-archive", "archive")
        else:
            ctrls += _cbtn("pause", "ag-btn-pause", "pause") + " " + _cbtn("drain", "ag-btn-drain", "drain")
            ctrls += " " + _cbtn("archive", "ag-btn-archive", "archive")
        ctrls += "</div>"
        archived_cls = " ag-archived" if is_archived else ""
        return (
            f'<div class="ag-card{archived_cls}" data-name="{name_e}" data-archived="{"1" if is_archived else "0"}">'
            f'<h3 style="display:flex;align-items:center;justify-content:space-between;margin:0 0 6px">'
            f'<span>{name_e}</span>'
            f'<a href="/agent/{name_e}" style="font-size:12px;font-weight:500;'
            f'background:var(--ac);color:#0f1117;padding:3px 10px;border-radius:5px;'
            f'text-decoration:none">chat →</a>'
            f'</h3>'
            f'{rt_badge}{toml_badge}{pending_str}'
            f'{meta}{kg_links_html}{ctrls}'
            f'</div>'
        )

    cards = "".join(_make_card(a) for a in active_agents)
    if not cards and not archived_agents:
        cards = "<p style='color:var(--mt);font-size:13px'>No agents yet. Use + New to create one.</p>"

    # Create agent form (inline, toggled by JS)
    create_form = (
        f'<div id="ag-create" class="ag-create-form">'
        f'<div style="font-size:12px;font-weight:600;margin-bottom:8px;color:var(--mt)">New agent</div>'
        f'<input id="ag-new-name" type="text" placeholder="Agent name (e.g. researcher)" autocomplete="off">'
        f'<select id="ag-new-model" style="width:100%;padding:6px 8px;background:var(--bg);color:var(--tx);border:1px solid var(--bd);border-radius:5px;font-size:13px">'
        f'<option value="">Default (claude-sonnet-4-6)</option>'
        f'<option value="claude-sonnet-4-6">Sonnet 4.6 (claude-sonnet-4-6)</option>'
        f'<option value="claude-opus-4-6">Opus 4.6 (claude-opus-4-6)</option>'
        f'<option value="claude-haiku-4-5-20251001">Haiku 4.5 (claude-haiku-4-5-20251001)</option>'
        f'</select>'
        f'<div style="display:flex;gap:8px">'
        f'<button onclick="window._createAgent()">Create</button>'
        f'<button onclick="document.getElementById(\'ag-create\').style.display=\'none\'" '
        f'style="background:transparent;border:1px solid var(--bd);color:var(--tx)">Cancel</button>'
        f'</div>'
        f'</div>'
    )

    archived_toggle = ""
    if archived_agents:
        archived_cards = "".join(_make_card(a) for a in archived_agents)
        n_arch = len(archived_agents)
        archived_toggle = (
            f'<div id="ag-archived-section" style="display:none">'
            f'<div style="font-size:11px;color:var(--mt);margin:12px 0 6px;font-weight:600">Archived</div>'
            f'<div class="ag-grid">{archived_cards}</div>'
            f'</div>'
            f'<p style="margin-top:8px;font-size:12px">'
            f'<a href="#" id="ag-show-archived" style="color:var(--mt)" '
            f'onclick="var s=document.getElementById(\'ag-archived-section\'),'
            f'l=document.getElementById(\'ag-show-archived\');'
            f's.style.display=s.style.display===\'none\'?\'block\':\'none\';'
            f'l.textContent=s.style.display===\'none\'?\'Show {n_arch} archived\':\'Hide archived\';'
            f'return false">'
            f'Show {n_arch} archived</a></p>'
        )

    page_js = (
        '<script>'
        'document.getElementById("ag-filter").addEventListener("input",function(){'
        'var q=this.value.toLowerCase();'
        'document.querySelectorAll(".ag-card[data-archived=\'0\']").forEach(function(c){'
        'c.style.display=c.dataset.name.toLowerCase().includes(q)?"":"none";});});'
        'window._createAgent=function(){'
        'var n=document.getElementById("ag-new-name").value.trim();'
        'var m=document.getElementById("ag-new-model").value.trim();'
        'if(!n)return;'
        'var b="name="+encodeURIComponent(n)+(m?"&model="+encodeURIComponent(m):"");'
        'fetch("/agents/create",{method:"POST",'
        'headers:{"Content-Type":"application/x-www-form-urlencoded"},body:b})'
        '.then(function(r){if(r.ok||r.redirected)location.reload();});};'
        '</script>'
    )

    body = (
        f'<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">'
        f'<h1 style="margin:0">Agents</h1>'
        f'<button class="ag-btn" style="font-size:12px;padding:4px 12px" '
        f'onclick="var f=document.getElementById(\'ag-create\');'
        f'f.style.display=f.style.display===\'none\'||!f.style.display?\'block\':\'none\'">+ New</button>'
        f'</div>'
        f'{create_form}'
        f'<input id="ag-filter" class="ag-filter" type="search" placeholder="Filter agents…" autocomplete="off">'
        f'<div class="ag-grid">{cards}</div>'
        f'{archived_toggle}'
        f'{page_js}'
        f'<p style="margin-top:16px;font-size:12px;color:var(--mt)">'
        f'<a href="/logs/launcher" style="color:var(--mt)">launcher log →</a></p>'
    )
    return _page(cfg, "Agents", body)


def _render_agent_page(cfg: KGConfig, agent_name: str, flash: str = "") -> str:
    import datetime

    agent_name_e = _html.escape(agent_name)

    # Load agent runtime + TOML info
    agent_info: dict = {"status": "unknown", "toml_status": "running", "node": "", "model": ""}
    for a in _mux_agents(cfg):
        if a["name"] == agent_name:
            agent_info = a
            break

    # Load messages from project-local messages.db
    messages = _mux_agent_messages(cfg, agent_name)

    # Slugs for [[slug]] rendering in messages
    slugs = _get_slugs_db(cfg)

    # Load sessions (reverse-chronological) with preview text
    sessions_dir = cfg.sessions_dir / agent_name
    sessions: list[tuple[str, str, str]] = []  # (stem, mtime, preview)
    if sessions_dir.exists():
        for p in sorted(sessions_dir.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True):
            mtime = datetime.datetime.fromtimestamp(  # noqa: DTZ006
                p.stat().st_mtime
            ).strftime("%Y-%m-%d %H:%M")
            preview = ""
            import contextlib as _cl
            with _cl.suppress(Exception):
                turns = _parse_session(p)
                for turn in turns:
                    if turn["type"] == "summary" and turn.get("text"):
                        preview = turn["text"][:100]
                        break
                    if turn["type"] == "user" and turn.get("text"):
                        preview = turn["text"][:100]
                        break
            sessions.append((p.stem, mtime, preview))

    # Info bar
    ts = agent_info.get("toml_status", "running")
    rt = agent_info.get("status", "unknown")
    rt_cls = "ag-running" if rt == "running" else "ag-idle"
    ts_badge = ""
    if ts == "paused":
        ts_badge = '<span class="ag-status ag-paused" style="margin-left:6px">paused</span>'
    elif ts == "draining":
        ts_badge = '<span class="ag-status ag-draining" style="margin-left:6px">draining</span>'
    elif ts == "archived":
        ts_badge = '<span class="ag-status ag-archived" style="margin-left:6px">archived</span>'

    node_val = _html.escape(agent_info.get("node", "") or "local")
    model_val = _html.escape(agent_info.get("model", "") or "default")
    last_seen = (agent_info.get("last_seen") or "")[:19]

    # Links to agent's KG nodes — check which slugs exist
    _kg_link_style = "font-size:12px;color:var(--ac);text-decoration:none"
    _node_links: list[str] = []
    # Main agent node: agent-<name>
    _agent_node_slug = f"agent-{agent_name}"
    if _agent_node_slug in slugs:
        _node_links.append(f'<a href="/node/{_html.escape(_agent_node_slug)}" style="{_kg_link_style}">KG node</a>')
    # Mission node: agent-<name>-mission or agent-<name>-instructions (legacy)
    for _msuffix in ("-mission", "-instructions"):
        _mslugg = f"agent-{agent_name}{_msuffix}"
        if _mslugg in slugs:
            _node_links.append(f'<a href="/node/{_html.escape(_mslugg)}" style="{_kg_link_style}">mission</a>')
            break
    _node_links_html = " · ".join(_node_links)

    info_bar = (
        f'<div class="ag-info" id="ag-info-bar">'
        f'<span class="ag-status {rt_cls}">{rt}</span>{ts_badge}'
        f'<span><span class="ag-info-lbl">node </span><span class="ag-info-val">{node_val}</span></span>'
        f'<span><span class="ag-info-lbl">model </span><span class="ag-info-val">{model_val}</span></span>'
        + (f'<span class="ag-info-lbl">{last_seen}</span>' if last_seen else "")
        + (f'<span style="margin-left:auto">{_node_links_html}</span>' if _node_links_html else "")
        + f'</div>'
    )

    # Control buttons — use fetch() to avoid mobile "insecure form" warnings
    _ctrl_url = f"/agent/{agent_name_e}/ctrl"
    _ctrl_js = (
        f'<script>(function(){{'
        f'window._agCtrl=function(action){{'
        f'fetch("{_ctrl_url}",{{method:"POST",headers:{{"Content-Type":"application/x-www-form-urlencoded"}},'
        f'body:"action="+encodeURIComponent(action)}}).then(function(){{location.reload();}}).catch(function(){{}});'
        f'}};'
        f'}})();</script>'
    )
    ctrl_btns = f'<div class="ag-ctrls" style="margin-bottom:16px">'
    if ts == "archived":
        ctrl_btns += f'<button class="ag-btn ag-btn-unarchive" onclick="_agCtrl(\'unarchive\')">unarchive</button>'
    elif ts == "paused":
        ctrl_btns += (
            f'<button class="ag-btn ag-btn-resume" onclick="_agCtrl(\'resume\')">resume</button> '
            f'<button class="ag-btn ag-btn-archive" onclick="_agCtrl(\'archive\')">archive</button>'
        )
    elif ts == "draining":
        ctrl_btns += (
            f'<button class="ag-btn ag-btn-resume" onclick="_agCtrl(\'resume\')">resume</button> '
            f'<button class="ag-btn ag-btn-archive" onclick="_agCtrl(\'archive\')">archive</button>'
        )
    else:
        ctrl_btns += (
            f'<button class="ag-btn ag-btn-pause" onclick="_agCtrl(\'pause\')">pause</button> '
            f'<button class="ag-btn ag-btn-drain" onclick="_agCtrl(\'drain\')">drain</button> '
            f'<button class="ag-btn ag-btn-archive" onclick="_agCtrl(\'archive\')">archive</button>'
        )
    ctrl_btns += f"</div>{_ctrl_js}"

    # Message thread — latest on top for easy monitoring without scrolling
    thread_html = ""
    if messages:
        items = ""
        for m in reversed(messages):  # newest first
            is_in = m.get("to_agent") == agent_name
            is_urgent = m.get("urgency") == "urgent"
            cls = ("msg-in" + (" urgent" if is_urgent else "")) if is_in else "msg-out"
            frm = _html.escape(m.get("from_agent") or "?")
            msg_ts = (m.get("timestamp") or "")[:19]
            urg_tag = ' <span style="color:#f87171;font-size:10px">URGENT</span>' if is_urgent else ""
            # Render [[slug]] links and formatting in message bodies
            body_html = _render(str(m.get("body", "")), slugs)
            items += (
                f'<div class="msg {cls}">'
                f'<div class="msg-hdr">{frm}{urg_tag} · {msg_ts}</div>'
                f'<div class="msg-body">{body_html}</div>'
                f'</div>'
            )
        thread_html = f'<h2>Messages</h2><div class="msg-thread" id="msg-thread">{items}</div>'

    # Chat form — always sends as urgent (injected into running session via heartbeat hook)
    # Form submissions use fetch() to avoid mobile browser "insecure site" warnings on HTTP.
    # chat-sticky: sticks below nav so textarea is always visible while reading messages.
    chat_form = (
        f'<div class="chat-sticky"><div class="chat-form">'
        f'<form id="chat-form">'  # no action/method — fetch handles submission
        f'<textarea name="body" id="chat-body" placeholder="Message {agent_name_e}… (Enter to send, Shift+Enter for newline)" '
        f'rows="3" style="width:100%;box-sizing:border-box"></textarea>'
        f'<div class="chat-form-row" style="margin-top:6px">'
        f'<button class="chat-send-btn urgent" type="submit" id="chat-send">Send</button>'
        f'<span id="chat-status" style="font-size:11px;color:var(--mt)">Enter to send · Shift+Enter for newline</span>'
        f'</div></form>'
        f'</div>'
        f'<script>'
        f'(function(){{'
        f'var ta=document.getElementById("chat-body");'
        f'var btn=document.getElementById("chat-send");'
        f'var status=document.getElementById("chat-status");'
        f'var url="/agent/{agent_name_e}/message";'
        f'function sendMsg(){{'
        f'var body=ta?ta.value.trim():"";'
        f'if(!body)return;'
        f'if(ta)ta.value="";'
        f'if(btn)btn.disabled=true;'
        f'fetch(url,{{method:"POST",headers:{{"Content-Type":"application/x-www-form-urlencoded"}},'
        f'body:"body="+encodeURIComponent(body)+"&urgent=1"}})'
        f'.then(function(){{if(btn)btn.disabled=false;if(status)status.textContent="Sent ✓";setTimeout(function(){{if(status)status.textContent="Enter to send · Shift+Enter for newline";}},2000);}})'
        f'.catch(function(){{if(btn)btn.disabled=false;if(status){{status.textContent="Send failed";status.style.color="#f87171";}}}})'
        f'}}'
        f'var f=document.getElementById("chat-form");'
        f'if(f)f.addEventListener("submit",function(e){{e.preventDefault();sendMsg();}});'
        f'if(ta)ta.addEventListener("keydown",function(e){{'
        f'if(e.key==="Enter"&&!e.shiftKey){{e.preventDefault();sendMsg();}}'
        f'}});'
        f'}})();'
        f'</script>'
        f'</div></div>'  # close .chat-form + .chat-sticky
    )

    # SSE-based live refresh
    # update → refresh msg-thread + info-bar + idle-banner
    # thinking → show live-feed + thinking dot (fades after 6s of quiet)
    # turn → prepend new turn HTML to #live-turns (latest at top)
    # ping → confirm SSE alive (show live-dot)
    auto_refresh = (
        f'<script>'
        f'(function(){{'
        f'var agName={json.dumps(agent_name)};'
        f'var pollTid=0,sseOk=false,thinkTid=0;'
        f'function refresh(){{'
        f'fetch(location.href).then(function(r){{return r.text();}}).then(function(html){{'
        f'var p=new DOMParser(),doc=p.parseFromString(html,"text/html");'
        f'var nt=doc.getElementById("msg-thread"),ot=document.getElementById("msg-thread");'
        f'if(nt&&ot)ot.innerHTML=nt.innerHTML;'
        f'var nb=doc.getElementById("ag-info-bar"),ob=document.getElementById("ag-info-bar");'
        f'if(nb&&ob)ob.innerHTML=nb.innerHTML;'
        f'var nf=doc.getElementById("ag-idle-banner"),of=document.getElementById("ag-idle-banner");'
        f'if(of){{if(nf)of.innerHTML=nf.innerHTML;else of.style.display="none";}}'
        f'}}).catch(function(){{}});'
        f'}}'
        f'function startSSE(){{'
        f'var es=new EventSource("/agent/"+agName+"/events");'
        f'es.addEventListener("update",function(){{sseOk=true;refresh();}});'
        f'es.addEventListener("ping",function(){{'
        f'sseOk=true;'
        f'var dot=document.getElementById("live-dot");if(dot)dot.style.display="inline-block";'
        f'}});'
        f'es.addEventListener("thinking",function(){{'
        f'sseOk=true;'
        f'var feed=document.getElementById("live-feed");if(feed)feed.style.display="";'
        f'var ph=document.getElementById("live-placeholder");if(ph)ph.remove();'
        f'var dot=document.getElementById("thinking-dot");if(dot)dot.style.display="inline-block";'
        f'clearTimeout(thinkTid);'
        f'thinkTid=setTimeout(function(){{'
        f'var d=document.getElementById("thinking-dot");if(d)d.style.display="none";'
        f'}},6000);'
        f'}});'
        f'es.addEventListener("turn",function(e){{'
        f'sseOk=true;'
        f'var data;try{{data=JSON.parse(e.data);}}catch(ex){{return;}}'
        f'var turns=document.getElementById("live-turns");'
        f'var feed=document.getElementById("live-feed");'
        f'if(turns&&data.html){{'
        f'if(feed)feed.style.display="";'
        f'var ph=document.getElementById("live-placeholder");if(ph)ph.remove();'
        f'var tmp=document.createElement("div");tmp.innerHTML=data.html;'
        f'turns.insertBefore(tmp.firstChild,turns.firstChild);'
        f'}}'
        f'}});'
        f'es.onerror=function(){{'
        f'es.close();'
        f'if(!sseOk&&!pollTid)pollTid=setInterval(refresh,12000);'
        f'setTimeout(startSSE,5000);'
        f'}};'
        f'}}'
        f'startSSE();'
        f'}})();'
        f'</script>'
    )

    # Sessions list with preview — show 5 most recent, collapse the rest
    sessions_html = ""
    if sessions:
        _SESS_SHOW = 5

        def _session_row(sid: str, mtime: str, preview: str) -> str:
            preview_html = (
                f'<span style="color:var(--mt);font-size:11px;overflow:hidden;'
                f'text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0">'
                f'{_html.escape(preview)}{"…" if len(preview) == 100 else ""}</span>'
            ) if preview else ""
            return (
                f'<div class="session-row" style="flex-wrap:nowrap;gap:8px">'
                f'<a href="/agent/{agent_name_e}/session/{_html.escape(sid)}" style="flex-shrink:0">'
                f'<code style="font-size:11px">{_html.escape(sid[:24])}</code></a>'
                f'{preview_html}'
                f'<span style="color:var(--mt);font-size:12px;flex-shrink:0">{mtime}</span>'
                f'</div>'
            )

        recent = sessions[:_SESS_SHOW]
        older = sessions[_SESS_SHOW:]
        rows_html = "".join(_session_row(s, m, p) for s, m, p in recent)
        older_html = ""
        if older:
            older_rows = "".join(_session_row(s, m, p) for s, m, p in older)
            n_older = len(older)
            older_html = (
                f'<div id="sess-older" style="display:none">{older_rows}</div>'
                f'<p style="margin-top:6px;font-size:12px">'
                f'<a href="#" id="sess-older-toggle" style="color:var(--mt)" '
                f'onclick="var o=document.getElementById(\'sess-older\'),'
                f't=document.getElementById(\'sess-older-toggle\');'
                f'o.style.display=o.style.display===\'none\'?\'block\':\'none\';'
                f't.textContent=o.style.display===\'none\'?\'Show {n_older} older sessions\':\'Hide older sessions\';'
                f'return false">Show {n_older} older sessions</a></p>'
            )

        # Check for live session — get session_id from mux DB
        import contextlib as _ctxlib2
        import sqlite3 as _sq2
        live_sid = ""
        with _ctxlib2.suppress(Exception):
            _conn2 = _sq2.connect(str(cfg.mux_db_path))
            _row2 = _conn2.execute(
                "SELECT session_id FROM agents WHERE name=?", (agent_name,)
            ).fetchone()
            _conn2.close()
            if _row2 and _row2[0]:
                live_sid = _row2[0]
        live_link = ""
        if live_sid and rt == "running":
            live_link = (
                f'<div class="session-row" style="flex-wrap:nowrap;gap:8px;'
                f'border-left:3px solid var(--ac);background:rgba(88,166,255,.06)">'
                f'<a href="/agent/{agent_name_e}/session/{_html.escape(live_sid)}" style="flex-shrink:0">'
                f'<code style="font-size:11px">{_html.escape(live_sid[:24])}</code></a>'
                f'<span class="live-dot" style="flex-shrink:0"></span>'
                f'<span style="color:var(--ac);font-size:11px;flex-shrink:0">live</span>'
                f'</div>'
            )
        sessions_html = (
            f'<h2>Sessions</h2>'
            f'<div class="session-list">{live_link}{rows_html}</div>'
            f'{older_html}'
        )

    # Idle banner — shown when no session is running
    idle_banner = ""
    if rt != "running":
        idle_banner = (
            f'<div id="ag-idle-banner" style="background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.3);'
            f'border-radius:8px;padding:10px 14px;margin-bottom:12px;font-size:13px">'
            f'<strong>No session running.</strong> Messages you send will queue.<br>'
            f'<span style="color:var(--mt);font-size:12px">'
            f'If <code>kg launcher start</code> is running, it will auto-start this agent when it sees pending messages.<br>'
            f'Or start a session manually: <code style="color:var(--ac)">kg run {agent_name_e}</code>'
            f'</span>'
            f'</div>'
        )

    # Flash message (e.g. send error)
    flash_html = ""
    if flash == "error":
        flash_html = (
            '<div style="background:rgba(248,113,113,.1);border:1px solid rgba(248,113,113,.4);'
            'border-radius:8px;padding:10px 14px;margin-bottom:12px;font-size:13px;color:#f87171">'
            'Could not reach mux — is it running? Try: <code>kg mux start -d</code>'
            '</div>'
        )
    elif flash == "ok":
        flash_html = (
            '<div style="background:rgba(52,211,153,.08);border:1px solid rgba(52,211,153,.3);'
            'border-radius:8px;padding:8px 14px;margin-bottom:12px;font-size:13px;color:#34d399">'
            'Message sent.'
            '</div>'
        )

    # Live session feed — populated by SSE 'turn' events
    # When agent is running: hidden until first activity (SSE removes placeholder + shows feed)
    # When agent is idle: always visible with placeholder text
    _lf_placeholder_text = (
        "Activity appears here when a session is running."
        if rt != "running"
        else "Waiting for activity\u2026"
    )
    _lf_placeholder = (
        f'<p id="live-placeholder" style="color:var(--mt);font-size:13px;padding:8px 2px">'
        f'{_lf_placeholder_text}</p>'
    )
    _lf_display = "display:none;" if rt == "running" else ""
    live_feed = (
        f'<div id="live-feed" style="{_lf_display}margin-bottom:16px">'
        f'<h2>Live session <span class="live-dot" id="thinking-dot"></span></h2>'
        f'<div id="live-turns" class="bullets" style="max-height:320px;overflow-y:auto">'
        f'{_lf_placeholder}'
        f'</div>'
        f'</div>'
    )

    back = f'<a href="/agents" style="font-size:12px;color:var(--mt)">← agents</a>'
    live_dot = '<span id="live-dot" class="live-dot" style="display:none" title="Live SSE connected"></span>'
    body = (
        f"{back}<h1 style='margin-top:8px'>{live_dot}{agent_name_e}</h1>"
        f"{info_bar}"
        f"{ctrl_btns}"
        f"{idle_banner}"
        f"{flash_html}"
        f"{chat_form}"
        f"{live_feed}"
        f"{thread_html}"
        f"{sessions_html}"
        f"{auto_refresh}"
    )
    return _page(cfg, agent_name, body)


def _parse_session(session_path: Path) -> list[dict]:
    """Parse Claude Code session JSONL into simplified turn list."""
    import contextlib
    raw = []
    with contextlib.suppress(Exception):
        for line in session_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = entry.get("type", "")
            if t == "summary":
                raw.append({"type": "summary", "text": entry.get("summary", "")})
            elif t in ("human", "user"):
                msg = entry.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, list):
                    text_parts: list[str] = []
                    tool_results: list[dict] = []
                    for c in content:
                        if not isinstance(c, dict):
                            continue
                        if c.get("type") == "text":
                            text_parts.append(c.get("text", ""))
                        elif c.get("type") == "tool_result":
                            tr = c.get("content", "")
                            if isinstance(tr, list):
                                tr_text = "\n".join(
                                    x.get("text", "") for x in tr
                                    if isinstance(x, dict) and x.get("type") == "text"
                                )
                            else:
                                tr_text = str(tr)
                            tool_results.append({
                                "tool_use_id": c.get("tool_use_id", ""),
                                "result": tr_text,
                            })
                    text = "\n".join(text_parts)
                    if text.strip():
                        raw.append({"type": "user", "text": text, "ts": entry.get("timestamp", "")})
                    if tool_results:
                        raw.append({"type": "tool_results", "results": tool_results})
                else:
                    text = str(content)
                    if text.strip():
                        raw.append({"type": "user", "text": text, "ts": entry.get("timestamp", "")})
            elif t == "assistant":
                msg = entry.get("message", {})
                content = msg.get("content", [])
                if not isinstance(content, list):
                    content = [{"type": "text", "text": str(content)}]
                text_parts = []
                tool_calls = []
                thinking_blocks: list[str] = []
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "text":
                        text_parts.append(c.get("text", ""))
                    elif c.get("type") == "thinking":
                        th = c.get("thinking", "")
                        if th:
                            thinking_blocks.append(th)
                    elif c.get("type") == "tool_use":
                        inp = c.get("input", {})
                        inp_str = json.dumps(inp, indent=2) if isinstance(inp, dict) else str(inp)
                        tool_calls.append({
                            "name": c.get("name", "?"),
                            "input": inp_str,
                            "id": c.get("id", ""),
                        })
                if text_parts or tool_calls or thinking_blocks:
                    raw.append({
                        "type": "assistant",
                        "text": "\n".join(text_parts),
                        "tool_calls": tool_calls,
                        "thinking": thinking_blocks,
                        "ts": entry.get("timestamp", ""),
                    })

    # Merge adjacent tool_results into preceding assistant turn
    turns = []
    for item in raw:
        if item["type"] == "tool_results" and turns and turns[-1]["type"] == "assistant":
            results_by_id = {r["tool_use_id"]: r["result"] for r in item["results"]}
            for tc in turns[-1]["tool_calls"]:
                tc_id = tc.get("id", "")
                if tc_id in results_by_id:
                    tc["result"] = results_by_id[tc_id]
        else:
            turns.append(item)
    return turns


def _render_session_page(cfg: KGConfig, agent_name: str, session_id: str) -> str:
    session_path = cfg.sessions_dir / agent_name / f"{session_id}.jsonl"
    if not session_path.exists():
        return _render_404(cfg, f"session {session_id}")

    slugs = _get_slugs_db(cfg)
    turns = _parse_session(session_path)

    def _render_tool_call_html(tc: dict) -> str:
        tc_name = tc["name"]
        try:
            inp_parsed = json.loads(tc["input"])
        except Exception:
            inp_parsed = {}
        summary = _tool_summary(tc_name, inp_parsed)
        if summary and summary.startswith("_fleeting-") and summary in slugs:
            summary_html = (
                f' <a href="/node/{summary}" style="color:var(--ac);font-weight:normal;text-decoration:none">'
                f'{_html.escape(summary)}</a>'
            )
        elif summary:
            summary_html = f' <span style="color:var(--mt);font-weight:normal">{_html.escape(summary)}</span>'
        else:
            summary_html = ""
        inp_esc = _html.escape(tc["input"][:2000])
        # Result HTML — placed INSIDE .tool-call so it's always visible (not hidden with input)
        result_html = ""
        if "result" in tc:
            res_raw = tc["result"][:3000]
            res_html = _render_tool_result(res_raw, slugs)
            res_stripped = res_raw.strip()
            if "\n" not in res_stripped and len(res_stripped) <= 120:
                # Short single-line: always visible inline
                result_html = (
                    f'<div style="padding:2px 10px 5px 10px;font-size:11px;'
                    f'color:rgba(40,180,99,.85);font-family:monospace;border-top:1px solid rgba(40,180,99,.12)">'
                    f'↳ {res_html}</div>'
                )
            else:
                # Long result: collapsible with ▶/▼ indicator
                result_html = (
                    f'<div class="tool-result">'
                    f'<div class="tool-result-hdr" onclick="_tog(this)">'
                    f'<span class="arr">▶</span> result <span style="color:var(--mt)">({len(res_raw)} chars)</span>'
                    f'</div>'
                    f'<div class="tool-result-body">{res_html}</div>'
                    f'</div>'
                )
        return (
            f'<div class="tool-call">'
            f'<div class="tool-hdr" onclick="_tog(this)">'
            f'<span class="arr">▶</span> {_html.escape(tc_name)}{summary_html}</div>'
            f'<div class="tool-body">{inp_esc}</div>'
            f'{result_html}'
            f'</div>'
        )

    def _render_assistant_turn_html(turn: dict) -> str:
        thinking_html = ""
        for th in turn.get("thinking", []):
            th_esc = _html.escape(th[:4000])
            thinking_html += (
                f'<div class="thinking-block">'
                f'<div class="thinking-hdr" onclick="_tog(this)">'
                f'<span class="arr">▶</span> 💭 thinking ({len(th)} chars)</div>'
                f'<div class="thinking-body">{th_esc}</div>'
                f'</div>'
            )
        tool_html = "".join(_render_tool_call_html(tc) for tc in turn.get("tool_calls", []))
        text_html = _render(turn["text"], slugs) if turn["text"] else ""
        return (
            f'<div class="turn turn-assistant">'
            f'<div class="turn-label">Assistant · {(turn.get("ts") or "")[:19]}</div>'
            f'{thinking_html}'
            f'<div class="turn-text">{text_html}</div>'
            f'{tool_html}'
            f'</div>'
        )

    # Build items, grouping consecutive "tool-only" assistant turns
    items = ""
    grp_html = ""       # Accumulated HTML for current tool group
    grp_names: list[str] = []   # Tool names in current group

    def _flush_grp() -> str:
        nonlocal grp_html, grp_names
        if not grp_html:
            return ""
        html = grp_html
        names = grp_names
        grp_html = ""
        grp_names = []
        n = len(names)
        # Only wrap in a group when there are 2+ tool calls from separate turns
        if n < 2:
            return html
        names_str = ", ".join(_html.escape(nm) for nm in names[:6])
        if n > 6:
            names_str += f" +{n - 6} more"
        return (
            f'<div class="tool-group">'
            f'<div class="tool-group-hdr" onclick="_tog(this)">'
            f'<span class="arr">▶</span> <span class="tg-count">{n} tool calls</span>'
            f' — {names_str}</div>'
            f'<div class="tool-group-body">{html}</div>'
            f'</div>'
        )

    for turn in turns:
        if turn["type"] == "summary":
            items += _flush_grp()
            items += f'<div style="color:var(--mt);font-size:12px;font-style:italic;margin-bottom:8px">{_html.escape(turn["text"])}</div>'
        elif turn["type"] == "user":
            items += _flush_grp()
            items += (
                f'<div class="turn turn-user">'
                f'<div class="turn-label">User · {(turn.get("ts") or "")[:19]}</div>'
                f'<div class="turn-text">{_render(turn["text"], slugs)}</div>'
                f'</div>'
            )
        elif turn["type"] == "assistant":
            has_tools = bool(turn.get("tool_calls"))
            has_text = bool(turn.get("text", "").strip())
            has_thinking = bool(turn.get("thinking"))
            is_tool_only = has_tools and not has_text and not has_thinking
            if is_tool_only:
                # Accumulate into group
                grp_html += _render_assistant_turn_html(turn)
                grp_names.extend(tc["name"] for tc in turn.get("tool_calls", []))
            else:
                items += _flush_grp()
                items += _render_assistant_turn_html(turn)

    items += _flush_grp()

    if not items:
        items = '<p style="color:var(--mt)">Empty session or unrecognised format.</p>'

    # Session stats for header
    _n_user = sum(1 for t in turns if t["type"] == "user")
    _n_asst = sum(1 for t in turns if t["type"] == "assistant")
    _n_tools = sum(len(t.get("tool_calls", [])) for t in turns if t["type"] == "assistant")
    _stat_parts = []
    if _n_user:
        _stat_parts.append(f"{_n_user} user turn{'s' if _n_user != 1 else ''}")
    if _n_asst:
        _stat_parts.append(f"{_n_asst} assistant turn{'s' if _n_asst != 1 else ''}")
    if _n_tools:
        _stat_parts.append(f"{_n_tools} tool call{'s' if _n_tools != 1 else ''}")
    _stats_html = (
        f'<p class="meta">{" · ".join(_stat_parts)}</p>'
    ) if _stat_parts else ""

    _ae = _html.escape(agent_name)
    _bc_style = "font-size:12px;color:var(--mt);text-decoration:none"
    _sep = '<span style="color:var(--bd);margin:0 5px">/</span>'
    # Breadcrumb: ← agents / agent_name / mission / KG node
    breadcrumb = (
        f'<a href="/agents" style="{_bc_style}">agents</a>'
        f'{_sep}<a href="/agent/{_ae}" style="{_bc_style}">{_ae}</a>'
    )
    # Add mission + KG node links if they exist
    _node_slug = f"agent-{agent_name}"
    if _node_slug in slugs:
        breadcrumb += f'{_sep}<a href="/node/{_html.escape(_node_slug)}" style="{_bc_style}">KG node</a>'
    for _ms in ("-mission", "-instructions"):
        _ms_slug = f"agent-{agent_name}{_ms}"
        if _ms_slug in slugs:
            breadcrumb += f'{_sep}<a href="/node/{_html.escape(_ms_slug)}" style="{_bc_style}">mission</a>'
            break

    body = (
        f'<div style="margin-bottom:8px">{breadcrumb}</div>'
        f'<h1 style="margin-top:4px">Session <code style="font-size:0.8em">{_html.escape(session_id[:20])}</code></h1>'
        f'{_stats_html}'
        f'<div style="margin-top:12px">{items}</div>'
    )
    return _page(cfg, f"Session — {agent_name}", body)


# ─── SSE streaming ────────────────────────────────────────────────────────────

def _extract_turn_for_sse(entry: dict, slugs: set[str]) -> str | None:
    """Return an HTML string for a session JSONL entry, or None if not a renderable turn."""
    t = entry.get("type", "")
    if t in ("human", "user"):
        msg = entry.get("message", {})
        content = msg.get("content", "")
        if isinstance(content, list):
            text = " ".join(
                c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
            )
        else:
            text = str(content)
        if not text.strip():
            return None
        ts = entry.get("timestamp", "")[:19]
        return (
            f'<div class="turn turn-user">'
            f'<div class="turn-label">User · {_html.escape(ts)}</div>'
            f'<div class="turn-text">{_render(text, slugs)}</div>'
            f'</div>'
        )
    if t == "assistant":
        msg = entry.get("message", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            content = [{"type": "text", "text": str(content)}]
        text = "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
        tool_names = [c.get("name", "?") for c in content if isinstance(c, dict) and c.get("type") == "tool_use"]
        ts = entry.get("timestamp", "")[:19]
        tools_html = "".join(
            f'<span class="badge bt-concept" style="margin-right:3px">{_html.escape(n)}</span>'
            for n in tool_names[:5]
        )
        return (
            f'<div class="turn turn-assistant">'
            f'<div class="turn-label">Assistant · {_html.escape(ts)}'
            + (f' {tools_html}' if tools_html else "")
            + f'</div>'
            f'<div class="turn-text">{_render(text, slugs) if text.strip() else ""}</div>'
            f'</div>'
        )
    return None


def _get_live_session_path(cfg: KGConfig, agent_name: str) -> Path | None:
    """Find the live Claude Code session transcript for a running agent.

    Queries the mux DB for the agent's current session_id, then finds the
    corresponding file in ~/.claude/projects/<encoded-path>/<session_id>.jsonl.
    """
    import contextlib
    import sqlite3
    from pathlib import Path as _Path
    with contextlib.suppress(Exception):
        if not cfg.mux_db_path.exists():
            return None
        conn = sqlite3.connect(str(cfg.mux_db_path))
        row = conn.execute(
            "SELECT session_id FROM agents WHERE name=?", (agent_name,)
        ).fetchone()
        conn.close()
        if not row or not row[0]:
            return None
        session_id = row[0]
        projects_dir = _Path.home() / ".claude" / "projects"
        for p in projects_dir.glob(f"*/{session_id}.jsonl"):
            if p.exists():
                return p
    return None


def _stream_agent_events(cfg: KGConfig, agent_name: str, wfile) -> None:
    """Stream SSE events for an agent page via inotify (falls back to 1s poll).

    Watches:
    - cfg.messages_db_path parent dir (index/) for messages.db CLOSE_WRITE
    - cfg.sessions_dir/<agent_name>/ for .jsonl CLOSE_WRITE / CREATE
    - ~/.claude/projects/<encoded>/<session_id>.jsonl for live in-session turns

    Fires 'update' SSE event immediately on any relevant change.
    Sends 'ping' keepalive every 25s.
    Runs in its own thread until the client disconnected (wfile write failure).
    """
    import contextlib
    import sqlite3
    import time

    def _send(event: str, data: str) -> bool:
        try:
            wfile.write(f"event: {event}\ndata: {data}\n\n".encode())
            wfile.flush()
            return True
        except Exception:
            return False

    sessions_dir = cfg.sessions_dir / agent_name
    slugs = _get_slugs_db(cfg)

    # Seed last-seen state so we don't fire spurious events on connect
    last_msg_id: list[int] = [0]
    last_session_path: list = [None]
    last_session_size: list[int] = [0]
    last_live_path: list = [None]
    last_live_size: list[int] = [0]

    with contextlib.suppress(Exception):
        if cfg.messages_db_path.exists():
            conn = sqlite3.connect(str(cfg.messages_db_path))
            row = conn.execute(
                "SELECT MAX(id) FROM messages WHERE to_agent=? OR from_agent=?",
                (agent_name, agent_name),
            ).fetchone()
            if row and row[0]:
                last_msg_id[0] = row[0]
            conn.close()

    with contextlib.suppress(Exception):
        if sessions_dir.exists():
            files = sorted(sessions_dir.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True)
            if files:
                last_session_path[0] = files[0]
                last_session_size[0] = files[0].stat().st_size

    with contextlib.suppress(Exception):
        lp = _get_live_session_path(cfg, agent_name)
        if lp:
            last_live_path[0] = lp
            last_live_size[0] = lp.stat().st_size

    # Initial keepalive — confirms SSE connection established
    if not _send("ping", "ok"):
        return

    def _check() -> bool:
        """Check for new messages / session data.
        Emits: 'thinking' + 'turn' events on session changes; 'update' on any change.
        Returns False if the SSE write failed (client disconnected)."""
        changed = False
        with contextlib.suppress(Exception):
            if cfg.messages_db_path.exists():
                conn = sqlite3.connect(str(cfg.messages_db_path))
                rows = conn.execute(
                    "SELECT id FROM messages WHERE (to_agent=? OR from_agent=?) AND id > ?",
                    (agent_name, agent_name, last_msg_id[0]),
                ).fetchall()
                conn.close()
                if rows:
                    last_msg_id[0] = rows[-1][0]
                    changed = True
        with contextlib.suppress(Exception):
            if sessions_dir.exists():
                files = sorted(sessions_dir.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True)
                if files:
                    latest = files[0]
                    size = latest.stat().st_size
                    if latest != last_session_path[0]:
                        last_session_path[0] = latest
                        last_session_size[0] = size
                        changed = True
                        if not _send("thinking", "1"):
                            return False
                    elif size > last_session_size[0]:
                        # Read new bytes and emit individual turns as they arrive
                        new_bytes: bytes = b""
                        with contextlib.suppress(Exception):
                            with open(str(latest), "rb") as f:
                                f.seek(last_session_size[0])
                                new_bytes = f.read()
                        last_session_size[0] = size
                        changed = True
                        if not _send("thinking", "1"):
                            return False
                        # Emit each new complete turn
                        with contextlib.suppress(Exception):
                            for line in new_bytes.decode("utf-8", errors="replace").splitlines():
                                if not line.strip():
                                    continue
                                try:
                                    entry = json.loads(line)
                                    html = _extract_turn_for_sse(entry, slugs)
                                    if html:
                                        if not _send("turn", json.dumps({"html": html})):
                                            return False
                                except json.JSONDecodeError:
                                    pass
        # Check live Claude Code session file (written incrementally during session)
        with contextlib.suppress(Exception):
            live_path = _get_live_session_path(cfg, agent_name)
            if live_path and live_path.exists():
                size = live_path.stat().st_size
                if live_path != last_live_path[0]:
                    last_live_path[0] = live_path
                    last_live_size[0] = size
                    changed = True
                    if not _send("thinking", "1"):
                        return False
                elif size > last_live_size[0]:
                    new_bytes_live: bytes = b""
                    with contextlib.suppress(Exception):
                        with open(str(live_path), "rb") as f:
                            f.seek(last_live_size[0])
                            new_bytes_live = f.read()
                    last_live_size[0] = size
                    changed = True
                    if not _send("thinking", "1"):
                        return False
                    with contextlib.suppress(Exception):
                        for line in new_bytes_live.decode("utf-8", errors="replace").splitlines():
                            if not line.strip():
                                continue
                            try:
                                entry = json.loads(line)
                                html = _extract_turn_for_sse(entry, slugs)
                                if html:
                                    if not _send("turn", json.dumps({"html": html})):
                                        return False
                            except json.JSONDecodeError:
                                pass
        if changed:
            return _send("update", "1")
        return True

    # ── inotify path (Linux) ──────────────────────────────────────────────────
    try:
        import inotify_simple  # type: ignore[import]  # raises ImportError on macOS/Docker

        inotify = inotify_simple.INotify()
        flags = inotify_simple.flags  # type: ignore[attr-defined]

        # Watch index/ dir for messages.db CLOSE_WRITE
        index_dir = cfg.messages_db_path.parent
        index_dir.mkdir(parents=True, exist_ok=True)
        inotify.add_watch(str(index_dir), flags.CLOSE_WRITE | flags.MOVED_TO)

        # Watch sessions dir if it exists; otherwise watch parent for its creation
        wd_sessions_dir: int | None = None
        sessions_root = cfg.sessions_dir
        if sessions_dir.exists():
            wd_sessions_dir = inotify.add_watch(
                str(sessions_dir), flags.CLOSE_WRITE | flags.CREATE | flags.MOVED_TO
            )
        elif sessions_root.exists():
            inotify.add_watch(str(sessions_root), flags.CREATE)

        # Watch live Claude Code project dir for in-session transcript writes
        with contextlib.suppress(Exception):
            from pathlib import Path as _Path
            _kg_root_enc = "-" + str(cfg.root).lstrip("/").replace("/", "-")
            _claude_proj = _Path.home() / ".claude" / "projects" / _kg_root_enc
            if _claude_proj.exists():
                inotify.add_watch(str(_claude_proj), flags.CLOSE_WRITE | flags.MODIFY)

        _PING_S = 25.0
        next_ping = time.monotonic() + _PING_S
        try:
            while True:
                now = time.monotonic()
                timeout_ms = max(100, int((next_ping - now) * 1000))
                events = inotify.read(timeout=timeout_ms)

                should_check = False
                for ev in events:
                    name = ev.name or ""
                    if name == "messages.db" or name.endswith(".jsonl"):
                        should_check = True
                    # sessions root: agent subdir just appeared → start watching it
                    if wd_sessions_dir is None and name == agent_name and sessions_dir.exists():
                        wd_sessions_dir = inotify.add_watch(
                            str(sessions_dir), flags.CLOSE_WRITE | flags.CREATE | flags.MOVED_TO
                        )

                if should_check:
                    if not _check():
                        return

                if time.monotonic() >= next_ping:
                    if not _send("ping", "ok"):
                        return
                    next_ping = time.monotonic() + _PING_S
        finally:
            inotify.close()

    except ImportError:
        # ── polling fallback (macOS / Docker / no inotify_simple) ────────────
        ping_counter = 0
        while True:
            time.sleep(1)
            ping_counter += 1
            if not _check():
                return
            if ping_counter % 25 == 0:
                if not _send("ping", "ok"):
                    return


# ─── HTTP handler ─────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    cfg: KGConfig  # injected via make_handler()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        if path in ("/", ""):
            self._index()
        elif path.startswith("/node/"):
            slug = path[6:]
            show_chunks = qs.get("chunks", ["1"])[0] != "0"
            self._node(slug, show_chunks)
        elif path == "/search":
            self._search(qs.get("q", [""])[0])
        elif path.startswith("/api/related/"):
            self._api_related(path[13:])
        elif path.startswith("/api/preview/"):
            self._api_preview(path[13:])
        elif path == "/agents":
            self._html(_render_agents_page(self.cfg))
        elif path.startswith("/agent/"):
            self._agent_route(path, parsed.query)
        elif path == "/logs/launcher":
            self._html(_render_log_page(self.cfg, "launcher"))
        else:
            self._html(_render_404(self.cfg, path), 404)

    def _index(self) -> None:
        self._html(_render_index(self.cfg))

    def _node(self, slug: str, show_chunks: bool) -> None:
        # Source file nodes live in SQLite, not FileStore
        if slug.startswith("_doc-"):
            doc = _get_doc_node(self.cfg, slug)
            if doc is not None:
                self._html(_render_doc_page(self.cfg, doc, show_chunks))
            else:
                self._html(_render_404(self.cfg, slug), 404)
            return
        from kg.reader import FileStore
        node = FileStore(self.cfg.nodes_dir).get(slug)
        if node is None:
            self._html(_render_404(self.cfg, slug), 404)
            return
        slugs = _get_slugs_db(self.cfg)
        self._html(_render_node_page(self.cfg, node, slugs))

    def _search(self, query: str) -> None:
        if not query.strip():
            self._redirect("/")
            return
        slugs = _get_slugs_db(self.cfg)
        results = _do_search(query, self.cfg)
        self._html(_render_search_page(self.cfg, query, results, slugs))

    def _agent_route(self, path: str, query_string: str = "") -> None:
        # /agent/<name>  or  /agent/<name>/events  or  /agent/<name>/session/<id>
        parts = path[len("/agent/"):].split("/")
        qs = urllib.parse.parse_qs(query_string)
        flash = qs.get("sent", [""])[0]
        if len(parts) == 1 and parts[0]:
            self._html(_render_agent_page(self.cfg, parts[0], flash=flash))
        elif len(parts) == 2 and parts[1] == "events" and parts[0]:
            self._agent_events(parts[0])
        elif len(parts) == 3 and parts[1] == "session" and parts[2]:
            self._html(_render_session_page(self.cfg, parts[0], parts[2]))
        else:
            self._html(_render_404(self.cfg, path), 404)

    def _agent_events(self, agent_name: str) -> None:
        """SSE endpoint — streams 'update' events when messages or session changes."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        _stream_agent_events(self.cfg, agent_name, self.wfile)

    def do_POST(self) -> None:
        import contextlib

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode(errors="replace") if length else ""
        form = urllib.parse.parse_qs(raw)

        # POST /agent/<name>/message — send message via mux
        if path.startswith("/agent/") and path.endswith("/message"):
            name = path[len("/agent/"):-len("/message")]
            body = form.get("body", [""])[0].strip()
            urgent = bool(form.get("urgent", [""])[0])
            sent = "ok"
            if body and name:
                import urllib.request as _ureq
                payload = json.dumps({
                    "from": "web",
                    "body": body,
                    "urgency": "urgent" if urgent else "normal",
                    "type": "text",
                }).encode()
                req = _ureq.Request(  # noqa: S310
                    f"{self.cfg.agents.mux_url}/agent/{name}/messages",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                )
                try:
                    _ureq.urlopen(req, timeout=3)  # noqa: S310
                except Exception:
                    sent = "error"
            self._redirect(f"/agent/{name}?sent={sent}")

        # POST /agent/<name>/ctrl — pause / resume / drain / archive / unarchive
        elif path.startswith("/agent/") and path.endswith("/ctrl"):
            name = path[len("/agent/"):-len("/ctrl")]
            action = form.get("action", [""])[0]
            valid = ("pause", "resume", "drain", "archive", "unarchive")
            if name and action in valid:
                status_map = {
                    "pause": "paused", "resume": "running", "drain": "draining",
                    "archive": "archived", "unarchive": "running",
                }
                with contextlib.suppress(Exception):
                    from kg.agents.launcher import update_agent_def  # type: ignore[attr-defined]
                    update_agent_def(self.cfg, name, status=status_map[action])
            self._redirect(f"/agents")

        # POST /agents/create — create a new agent TOML + KG nodes + mux registration
        elif path == "/agents/create":
            name = form.get("name", [""])[0].strip()
            model = form.get("model", [""])[0].strip()
            if name and re.match(r"^[a-z0-9_-]+$", name):
                # 1. Write .kg/agents/<name>.toml (restart="on-failure" to avoid restart loops)
                with contextlib.suppress(Exception):
                    from kg.agents.launcher import create_agent_def  # type: ignore[attr-defined]
                    create_agent_def(self.cfg, name, node="local", model=model, restart="on-failure")
                # 2. Create KG nodes: agent-<name>-mission + agent-<name> working memory
                with contextlib.suppress(Exception):
                    from kg.reader import FileStore
                    _store = FileStore(self.cfg.nodes_dir)
                    _mission = f"agent-{name}-mission"
                    _mem = f"agent-{name}"
                    if not _store.get(_mission):
                        _store.get_or_create(_mission, node_type="concept")
                        _store.add_bullet(
                            _mission,
                            text=(
                                f"No mission set yet for agent '{name}'. "
                                "Add bullets here to define this agent's mission and standing context. "
                                "These are injected verbatim at every session start."
                            ),
                        )
                    if not _store.get(_mem):
                        _store.get_or_create(_mem, node_type="agent")
                        _store.add_bullet(
                            _mem,
                            text=(
                                f"Working memory for agent '{name}'. "
                                "Bullets accumulated here across sessions."
                            ),
                        )
                # 3. Register in mux.db so pending-count queries work immediately
                with contextlib.suppress(Exception):
                    import sqlite3 as _sql3
                    from kg.agents.mux import _init_db as _mux_init, _upsert_agent as _mux_upsert  # type: ignore[attr-defined]
                    _mux_init(self.cfg.mux_db_path)
                    with _sql3.connect(str(self.cfg.mux_db_path)) as _mconn:
                        _mux_upsert(_mconn, name, "idle", None, str(self.cfg.root))
            self._redirect("/agents")

        else:
            self.send_response(404)
            self.end_headers()

    def _api_related(self, slug: str) -> None:
        import contextlib

        from kg.reader import FileStore
        node = FileStore(self.cfg.nodes_dir).get(slug)
        if node is None:
            self._html("")
            return
        from_slugs_set: set[str] = set()
        with contextlib.suppress(Exception):
            from kg.indexer import get_backlinks
            from_slugs_set = set(get_backlinks(slug, self.cfg.db_path, self.cfg))
        html = _related_html(self.cfg, node, from_slugs_set)
        self._html(html)

    def _api_preview(self, slug: str) -> None:
        payload = _preview_json(self.cfg, slug)
        encoded = payload.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _html(self, body: str, status: int = 200) -> None:
        encoded = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _redirect(self, loc: str) -> None:
        self.send_response(302)
        self.send_header("Location", loc)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # suppress per-request logging


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


def make_handler(cfg: KGConfig) -> type[_Handler]:
    class _Bound(_Handler):
        pass
    _Bound.cfg = cfg
    return _Bound


def serve(cfg: KGConfig, host: str, port: int) -> None:
    """Start the web viewer (blocking until Ctrl+C)."""
    handler = make_handler(cfg)
    server = _ThreadingHTTPServer((host, port), handler)
    print(f"kg web  →  http://{host}:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
