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


def _break_sentences(text: str) -> str:
    """Insert newlines at sentence boundaries, skipping backtick code spans."""
    parts = re.split(r"(`[^`]*`)", text)
    return "".join(_SENT_RE.sub(".\n", p) if i % 2 == 0 else p for i, p in enumerate(parts))


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
        return f'<a href="/node/{s}">[[{s}]]</a>' if s in slugs else f'<span class="dead">[[{s}]]</span>'

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
.ag-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px;margin-top:16px}
.ag-card{background:var(--sf);border:1px solid var(--bd);border-radius:8px;padding:14px 16px}
.ag-card h3{font-size:14px;font-weight:600;margin-bottom:6px}
.ag-card h3 a{color:var(--tx)}
.ag-status{display:inline-block;font-size:11px;padding:2px 8px;border-radius:10px;font-weight:600}
.ag-running{background:#064e3b;color:#34d399}
.ag-idle{background:#1f2937;color:#9ca3af}
.msg-thread{display:flex;flex-direction:column;gap:8px;margin-top:12px}
.msg{padding:10px 14px;border-radius:8px;border-left:3px solid var(--bd)}
.msg-in{background:var(--sf);border-color:var(--ac)}
.msg-out{background:rgba(255,255,255,.03);border-color:#7c3aed}
.msg-hdr{font-size:11px;color:var(--mt);margin-bottom:4px}
.msg-body{font-size:13px;white-space:pre-wrap;word-break:break-word}
.session-list{display:flex;flex-direction:column;gap:4px;margin-top:10px}
.session-row{display:flex;gap:10px;align-items:center;padding:6px 10px;border-radius:6px;border:1px solid transparent}
.session-row:hover{background:var(--sf);border-color:var(--bd)}
/* session log */
.turn{margin-bottom:12px}
.turn-user{background:rgba(88,166,255,.08);border-left:3px solid var(--ac);padding:10px 14px;border-radius:0 6px 6px 0}
.turn-assistant{padding:10px 0}
.turn-label{font-size:10px;color:var(--mt);text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px}
.turn-text{font-size:13px;white-space:pre-wrap;word-break:break-word}
.tool-call{background:var(--sf);border:1px solid var(--bd);border-radius:6px;margin:6px 0;overflow:hidden}
.tool-hdr{display:flex;align-items:center;gap:8px;padding:5px 10px;background:rgba(255,255,255,.04);font-size:11px;font-family:monospace;cursor:pointer}
.tool-hdr:hover{background:rgba(255,255,255,.07)}
.tool-body{padding:10px;font-size:11px;font-family:monospace;white-space:pre-wrap;word-break:break-word;max-height:300px;overflow-y:auto;display:none}
.tool-body.open{display:block}
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


def _page(cfg: KGConfig, title: str, body: str, q: str = "", extra_head: str = "", extra_script: str = "") -> str:
    qesc = _html.escape(q)
    name = _html.escape(cfg.name)
    t = _html.escape(title)
    script_tag = f"<script>{extra_script}</script>" if extra_script else ""
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
        f'<button type="submit">Search</button></form></nav>'
        f'<main>{body}</main>'
        f'{script_tag}'
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
        "if(!u.searchParams.has('chunks')&&localStorage.getItem('kg-chunks')==='1'){"
        "  u.searchParams.set('chunks','1');location.replace(u.toString());}"
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
    body = (
        f'<h1>{_html.escape(node.title)}</h1>'
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
    """Return agents list from mux SQLite, or [] if unavailable."""
    import contextlib
    import sqlite3
    result: list[dict] = []
    with contextlib.suppress(Exception):
        conn = sqlite3.connect(str(cfg.mux_db_path))
        conn.row_factory = sqlite3.Row
        agents = [dict(r) for r in conn.execute("SELECT * FROM agents ORDER BY name").fetchall()]
        pending = dict(conn.execute(
            "SELECT to_agent, COUNT(*) FROM messages WHERE status='pending' GROUP BY to_agent"
        ).fetchall())
        msgs_by_agent = {}
        for row in conn.execute(
            "SELECT to_agent, from_agent, timestamp, urgency, body, status FROM messages ORDER BY id"
        ).fetchall():
            msgs_by_agent.setdefault(row[0], []).append(dict(zip(
                ["to_agent", "from_agent", "timestamp", "urgency", "body", "status"], row,
                strict=False,
            )))
        conn.close()
        for a in agents:
            a["pending_count"] = pending.get(a["name"], 0)
            a["messages"] = msgs_by_agent.get(a["name"], [])
        result = agents
    return result


def _render_agents_page(cfg: KGConfig) -> str:
    agents = _mux_agents(cfg)
    if not agents:
        body = "<h1>Agents</h1><p style='color:var(--mt)'>No agents registered. Start an agent with <code>[agents] enabled = true</code> in kg.toml.</p>"
        return _page(cfg, "Agents", body)

    cards = ""
    for a in agents:
        status_cls = "ag-running" if a["status"] == "running" else "ag-idle"
        n = a.get("pending_count", 0)
        pending_str = f' <span style="color:#fbbf24">({n} pending)</span>' if n else ""
        last = (a.get("last_seen") or "")[:19]
        cards += (
            f'<div class="ag-card">'
            f'<h3><a href="/agent/{_html.escape(a["name"])}">{_html.escape(a["name"])}</a></h3>'
            f'<span class="ag-status {status_cls}">{_html.escape(a["status"])}</span>{pending_str}'
            f'<div style="color:var(--mt);font-size:11px;margin-top:6px">{last}</div>'
            f'</div>'
        )
    body = f'<h1>Agents</h1><div class="ag-grid">{cards}</div>'
    return _page(cfg, "Agents", body)


def _render_agent_page(cfg: KGConfig, agent_name: str) -> str:
    import contextlib
    import datetime
    import sqlite3

    # Load messages
    messages: list[dict] = []
    with contextlib.suppress(Exception):
        conn = sqlite3.connect(str(cfg.mux_db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM messages WHERE to_agent=? OR from_agent=? ORDER BY id",
            (agent_name, agent_name),
        ).fetchall()
        messages = [dict(r) for r in rows]
        conn.close()

    # Load sessions
    sessions_dir = cfg.sessions_dir / agent_name
    sessions: list[tuple[str, str]] = []
    if sessions_dir.exists():
        for p in sorted(sessions_dir.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True):
            mtime = datetime.datetime.fromtimestamp(  # noqa: DTZ006
                p.stat().st_mtime
            ).strftime("%Y-%m-%d %H:%M")
            sessions.append((p.stem, mtime))

    # Render message thread
    thread_html = ""
    if messages:
        items = ""
        for m in messages:
            is_in = m["to_agent"] == agent_name
            cls = "msg-in" if is_in else "msg-out"
            frm = m.get("from_agent") or "?"
            ts = (m.get("timestamp") or "")[:19]
            urg = " 🔴" if m.get("urgency") == "urgent" else ""
            st = m.get("status", "")
            body_txt = _html.escape(str(m.get("body", "")))
            items += (
                f'<div class="msg {cls}">'
                f'<div class="msg-hdr">{_html.escape(frm)}{urg} · {ts} · {st}</div>'
                f'<div class="msg-body">{body_txt}</div>'
                f'</div>'
            )
        thread_html = f'<h2>Messages</h2><div class="msg-thread">{items}</div>'

    # Send form
    send_form = (
        f'<h2>Send Message</h2>'
        f'<form class="send-form" method="post" action="/agent/{_html.escape(agent_name)}/message">'
        f'<textarea name="body" placeholder="Type a message…"></textarea>'
        f'<button type="submit">Send</button>'
        f'</form>'
    )

    # Sessions list
    sessions_html = ""
    if sessions:
        rows_html = "".join(
            f'<div class="session-row">'
            f'<a href="/agent/{_html.escape(agent_name)}/session/{sid}">'
            f'<code>{_html.escape(sid[:16])}…</code></a>'
            f'<span style="color:var(--mt);font-size:12px">{mtime}</span>'
            f'</div>'
            for sid, mtime in sessions
        )
        sessions_html = f'<h2>Sessions</h2><div class="session-list">{rows_html}</div>'

    body = (
        f"<h1>{_html.escape(agent_name)}</h1>"
        f"{thread_html}"
        f"{send_form}"
        f"{sessions_html}"
    )
    return _page(cfg, agent_name, body)


def _parse_session(session_path: Path) -> list[dict]:
    """Parse Claude Code session JSONL into simplified turn list."""
    import contextlib
    turns = []
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
                turns.append({"type": "summary", "text": entry.get("summary", "")})
            elif t in ("human", "user"):
                msg = entry.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, list):
                    text = " ".join(
                        c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
                    )
                else:
                    text = str(content)
                if text.strip():
                    turns.append({"type": "user", "text": text, "ts": entry.get("timestamp", "")})
            elif t == "assistant":
                msg = entry.get("message", {})
                content = msg.get("content", [])
                if not isinstance(content, list):
                    content = [{"type": "text", "text": str(content)}]
                text_parts = []
                tool_calls = []
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "text":
                        text_parts.append(c.get("text", ""))
                    elif c.get("type") == "tool_use":
                        inp = c.get("input", {})
                        inp_str = json.dumps(inp, indent=2) if isinstance(inp, dict) else str(inp)
                        tool_calls.append({"name": c.get("name", "?"), "input": inp_str})
                if text_parts or tool_calls:
                    turns.append({
                        "type": "assistant",
                        "text": "\n".join(text_parts),
                        "tool_calls": tool_calls,
                        "ts": entry.get("timestamp", ""),
                    })
    return turns


def _render_session_page(cfg: KGConfig, agent_name: str, session_id: str) -> str:
    session_path = cfg.sessions_dir / agent_name / f"{session_id}.jsonl"
    if not session_path.exists():
        return _render_404(cfg, f"session {session_id}")

    turns = _parse_session(session_path)
    items = ""
    for turn in turns:
        if turn["type"] == "summary":
            items += f'<div style="color:var(--mt);font-size:12px;font-style:italic;margin-bottom:8px">{_html.escape(turn["text"])}</div>'
        elif turn["type"] == "user":
            items += (
                f'<div class="turn turn-user">'
                f'<div class="turn-label">User · {(turn.get("ts") or "")[:19]}</div>'
                f'<div class="turn-text">{_html.escape(turn["text"])}</div>'
                f'</div>'
            )
        elif turn["type"] == "assistant":
            tool_html = ""
            for tc in turn.get("tool_calls", []):
                inp_esc = _html.escape(tc["input"][:2000])
                tool_html += (
                    f'<div class="tool-call">'
                    f'<div class="tool-hdr" onclick="this.nextElementSibling.classList.toggle(\'open\')">'
                    f'▶ {_html.escape(tc["name"])}</div>'
                    f'<div class="tool-body">{inp_esc}</div>'
                    f'</div>'
                )
            text_esc = _html.escape(turn["text"]) if turn["text"] else ""
            items += (
                f'<div class="turn turn-assistant">'
                f'<div class="turn-label">Assistant · {(turn.get("ts") or "")[:19]}</div>'
                f'<div class="turn-text">{text_esc}</div>'
                f'{tool_html}'
                f'</div>'
            )

    if not items:
        items = '<p style="color:var(--mt)">Empty session or unrecognised format.</p>'

    back = f'<a href="/agent/{_html.escape(agent_name)}" style="font-size:12px;color:var(--mt)">← {_html.escape(agent_name)}</a>'
    body = (
        f'{back}<h1 style="margin-top:8px">Session <code style="font-size:0.8em">{_html.escape(session_id[:20])}</code></h1>'
        f'<div style="margin-top:12px">{items}</div>'
    )
    return _page(cfg, f"Session — {agent_name}", body)


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
            show_chunks = qs.get("chunks", ["0"])[0] == "1"
            self._node(slug, show_chunks)
        elif path == "/search":
            self._search(qs.get("q", [""])[0])
        elif path.startswith("/api/related/"):
            self._api_related(path[13:])
        elif path == "/agents":
            self._html(_render_agents_page(self.cfg))
        elif path.startswith("/agent/"):
            self._agent_route(path)
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

    def _agent_route(self, path: str) -> None:
        # /agent/<name>  or  /agent/<name>/session/<id>
        parts = path[len("/agent/"):].split("/")
        if len(parts) == 1 and parts[0]:
            self._html(_render_agent_page(self.cfg, parts[0]))
        elif len(parts) == 3 and parts[1] == "session" and parts[2]:
            self._html(_render_session_page(self.cfg, parts[0], parts[2]))
        else:
            self._html(_render_404(self.cfg, path), 404)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        # POST /agent/<name>/message — send message via web UI
        if path.startswith("/agent/") and path.endswith("/message"):
            name = path[len("/agent/"):-len("/message")]
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode(errors="replace") if length else ""
            form = urllib.parse.parse_qs(raw)
            body = form.get("body", [""])[0].strip()
            if body and name:
                import contextlib
                import urllib.request as _ureq
                payload = json.dumps({"from": "web", "body": body}).encode()
                req = _ureq.Request(  # noqa: S310
                    f"{self.cfg.agents.mux_url}/agent/{name}/messages",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                )
                with contextlib.suppress(Exception):
                    _ureq.urlopen(req, timeout=3)  # noqa: S310
            self._redirect(f"/agent/{name}")
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
