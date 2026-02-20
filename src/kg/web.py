"""Simple HTTP web viewer for the kg knowledge graph.

Routes:
    GET /              → node index (all nodes, alphabetical)
    GET /node/<slug>   → single node with full bullet list + backlinks
    GET /search?q=...  → FTS + vector blended search with reranker
"""

from __future__ import annotations

import html as _html
import re
import socketserver
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kg.config import KGConfig
    from kg.models import FileNode

# ─── Text rendering ───────────────────────────────────────────────────────────

_SLUG_RE = re.compile(r"\[([a-z0-9][a-z0-9\-]*[a-z0-9])\]")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_CODE_RE = re.compile(r"`(.+?)`")


def _render(text: str, slugs: set[str]) -> str:
    """Escape text and convert [slug] links, **bold**, `code` to HTML."""
    def _link(m: re.Match[str]) -> str:
        s = m.group(1)
        return f'<a href="/node/{s}">[{s}]</a>' if s in slugs else f'<span class="dead">[{s}]</span>'
    return _BOLD_RE.sub(
        r"<strong>\1</strong>",
        _SLUG_RE.sub(_link, _CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", _html.escape(text))),
    )


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
"""

_NODE_TYPES = {"concept", "task", "decision", "agent", "session"}
_BULLET_COLORS = {"gotcha", "decision", "task", "note", "success", "failure"}


def _badge(node_type: str) -> str:
    cls = f"bt-{node_type}" if node_type in _NODE_TYPES else "bt-other"
    return f'<span class="badge {cls}">{_html.escape(node_type)}</span>'


def _page(cfg: KGConfig, title: str, body: str, q: str = "") -> str:
    qesc = _html.escape(q)
    name = _html.escape(cfg.name)
    t = _html.escape(title)
    return (
        f'<!DOCTYPE html>\n<html lang="en">\n<head>'
        f'<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{t} — {name}</title>'
        f'<style>{_CSS}</style></head>\n<body>'
        f'<nav><a class="brand" href="/">{name}</a>'
        f'<form action="/search" method="get">'
        f'<input name="q" type="search" placeholder="Search nodes…" value="{qesc}" autocomplete="off">'
        f'<button type="submit">Search</button></form></nav>'
        f'<main>{body}</main></body></html>'
    )


# ─── Page renderers ───────────────────────────────────────────────────────────

def _render_index(cfg: KGConfig, nodes: list[FileNode]) -> str:
    public = sorted(
        (n for n in nodes if not n.slug.startswith("_")),
        key=lambda n: n.title.lower(),
    )
    rows = []
    for n in public:
        bc = len(n.live_bullets)
        s = "" if bc == 1 else "s"
        rows.append(
            f'<div class="node-row">'
            f'<span class="t"><a href="/node/{n.slug}">{_html.escape(n.title)}</a></span>'
            f'<span class="m">{_badge(n.type)}&nbsp;&nbsp;{bc} bullet{s}</span>'
            f'</div>'
        )
    body = (
        f'<h1>{_html.escape(cfg.name)}</h1>'
        f'<p class="meta">{len(public)} nodes</p>'
        f'<div class="node-list">{"".join(rows)}</div>'
    )
    return _page(cfg, cfg.name, body)


def _render_node_page(cfg: KGConfig, node: FileNode, slugs: set[str]) -> str:
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
            f'<span class="btx">{_render(b.text, slugs)}{sp}</span>'
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
    bl = _backlinks_html(cfg, node.slug, slugs)
    if bl:
        body += f"<h2>Referenced by</h2>{bl}"
    return _page(cfg, node.title, body)


def _backlinks_html(cfg: KGConfig, slug: str, slugs: set[str]) -> str:
    if not cfg.db_path.exists():
        return ""
    from kg.db import get_conn
    conn = get_conn(cfg)
    rows = conn.execute(
        "SELECT b.node_slug, n.title, b.text FROM bullets b "
        "JOIN nodes n ON n.slug = b.node_slug "
        "WHERE b.text LIKE ? AND b.node_slug != ? LIMIT 30",
        (f"%[{slug}]%", slug),
    ).fetchall()
    conn.close()
    if not rows:
        return ""
    items = []
    for from_slug, from_title, text in rows:
        label = _html.escape(from_title or from_slug)
        items.append(
            f'<div class="bullet">'
            f'<span class="btp"><a href="/node/{from_slug}">[{_html.escape(from_slug)}]</a></span>'
            f'<span class="btx">{_render(text, slugs)}</span>'
            f'<span class="bid">{_html.escape(label)}</span>'
            f'</div>'
        )
    return f'<div class="bullets">{"".join(items)}</div>'


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

    parts = []
    for r in results:
        slug = r["slug"]
        title = _html.escape(r.get("title") or slug)
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
            f'<h3><a href="/node/{slug}">[{_html.escape(slug)}]</a> {title}</h3>'
            f'<div class="bullets">{"".join(items)}</div>'
            f'</div>'
        )
    body = (
        f'<h1>"{_html.escape(query)}"</h1>'
        f'<p class="meta">{len(results)} nodes matched</p>'
        + "".join(parts)
    )
    return _page(cfg, f"Search: {query}", body, q=query)


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
        if slug.startswith("_"):
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
            if not slug.startswith("_"):
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
    vec_cal = get_calibration("vector", cfg.db_path, cfg)
    fts_breaks = fts_cal[1] if fts_cal else None
    vec_breaks = vec_cal[1] if vec_cal else None

    fts_ranked = sorted(fts_scores.items(), key=lambda x: x[1], reverse=True)
    n_fts = len(fts_ranked)
    fts_rank_pos = {s: i for i, (s, _) in enumerate(fts_ranked)}

    def _score(slug: str) -> float:
        fts_raw = fts_scores.get(slug, 0.0)
        vec_raw = vec_scores.get(slug, 0.0)
        if fts_breaks and fts_raw > 0:
            fts_q = score_to_quantile(fts_raw, fts_breaks)
        elif n_fts > 1:
            pos = fts_rank_pos.get(slug, n_fts - 1)
            fts_q = 1.0 - pos / (n_fts - 1)
        else:
            fts_q = 1.0 if fts_raw > 0 else 0.0
        vec_q = score_to_quantile(vec_raw, vec_breaks) if vec_breaks and vec_raw > 0 else vec_raw
        bonus = dual_bonus if (fts_raw > 0 and vec_raw > 0) else 0.0
        return fts_w * fts_q + vec_w * vec_q + bonus

    ranked = sorted(groups, key=_score, reverse=True)[:limit]

    # Cross-encoder rerank
    if cfg.search.use_reranker and len(ranked) >= 2:
        with contextlib.suppress(Exception):
            from kg.reader import FileStore
            from kg.reranker import rerank
            store = FileStore(cfg.nodes_dir)
            candidates: list[tuple[str, str]] = []
            for slug in ranked:
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
            self._node(path[6:])
        elif path == "/search":
            self._search(qs.get("q", [""])[0])
        else:
            self._html(_render_404(self.cfg, path), 404)

    def _index(self) -> None:
        from kg.reader import FileStore
        nodes = list(FileStore(self.cfg.nodes_dir).iter_nodes())
        self._html(_render_index(self.cfg, nodes))

    def _node(self, slug: str) -> None:
        from kg.reader import FileStore
        store = FileStore(self.cfg.nodes_dir)
        node = store.get(slug)
        if node is None:
            self._html(_render_404(self.cfg, slug), 404)
            return
        slugs = set(store.list_slugs())
        self._html(_render_node_page(self.cfg, node, slugs))

    def _search(self, query: str) -> None:
        if not query.strip():
            self._redirect("/")
            return
        from kg.reader import FileStore
        slugs = set(FileStore(self.cfg.nodes_dir).list_slugs())
        results = _do_search(query, self.cfg)
        self._html(_render_search_page(self.cfg, query, results, slugs))

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
