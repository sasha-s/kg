// kg — Knowledge Graph Reference Documentation
// Packages: fletcher (diagrams), codly (code), gentle-clues (callouts), glossarium (glossary)

#import "@preview/fletcher:0.5.8" as fletcher: diagram, node, edge
#import "@preview/codly:1.3.0": *
#import "@preview/codly-languages:0.1.7": *
#import "@preview/gentle-clues:1.3.0": *
#import "@preview/glossarium:0.5.10": make-glossary, register-glossary, gls, glspl, print-glossary

// ── Glossary entries (must register before first use) ─────────────────────────
#let glossary-entries = (
  (key: "node",    short: "node",
   description: "A named collection of bullets identified by a slug. Stored as a directory under `.kg/nodes/<slug>/`."),
  (key: "bullet",  short: "bullet",
   description: "A single atomic fact, decision, gotcha, or task belonging to a node. Stored as a JSONL line with a stable UUID."),
  (key: "slug",    short: "slug",
   description: "A URL-safe lowercase identifier for a node, e.g. `my-topic`. Pattern: `[a-z0-9][a-z0-9-]*`."),
  (key: "fts",     short: "FTS",     long: "Full-Text Search",
   description: "SQLite FTS5 index used for keyword-based BM25 search over bullet text."),
  (key: "mcp",     short: "MCP",     long: "Model Context Protocol",
   description: "Open protocol for exposing tools to LLMs. kg exposes memory_context, memory_search, memory_show, and memory_add_bullet."),
  (key: "calibration", short: "calibration",
   description: "The process of computing score quantile breakpoints from a random sample of nodes, used to normalize raw FTS and vector scores before fusion."),
  (key: "budget",  short: "token budget",
   description: "A credit counter per node tracking how many characters the node has contributed to context output. Triggers a review flag when credits-per-bullet exceeds the threshold."),
  (key: "watcher", short: "watcher",
   description: "A background process that uses inotify (Linux) to detect changes to `.kg/nodes/` and immediately re-indexes modified nodes into SQLite."),
  (key: "reranker", short: "reranker",
   description: "A cross-encoder model (ms-marco-MiniLM-L-6-v2) that rescores the top-N hybrid search results using the full query-bullet pair."),
)

// ── Document setup ────────────────────────────────────────────────────────────
#show: make-glossary
#register-glossary(glossary-entries)

#show: codly-init.with()
#codly(
  zebra-fill: luma(250),
  stroke: 0.5pt + luma(200),
  radius: 4pt,
  number-format: none,
)

// ── Page & font ───────────────────────────────────────────────────────────────
#set page(
  paper: "a4",
  margin: (top: 2.5cm, bottom: 2.5cm, left: 2.8cm, right: 2.8cm),
  numbering: "1",
  header: context {
    if counter(page).get().first() > 1 [
      #set text(size: 8pt, fill: luma(140))
      #h(1fr) kg — Knowledge Graph Reference
    ]
  },
)
#set text(font: "DejaVu Serif", size: 11pt, lang: "en")
#set heading(numbering: "1.1")
#set par(justify: true, leading: 0.65em)

#show heading.where(level: 1): it => {
  pagebreak(weak: true)
  v(0.5em)
  block(
    fill: rgb("#1a1a2e"),
    width: 100%,
    inset: (x: 1em, y: 0.6em),
    radius: 5pt,
    text(fill: white, weight: "bold", size: 14pt, font: "DejaVu Sans", it.body),
  )
  v(0.3em)
}
#show heading.where(level: 2): it => {
  v(0.4em)
  text(fill: rgb("#16213e"), weight: "bold", size: 12pt, font: "DejaVu Sans", it.body)
  v(0.2em)
  line(length: 100%, stroke: 0.5pt + rgb("#e0e0e0"))
  v(0.1em)
}
#show link: it => text(fill: rgb("#0f3460"), it)

// ── Palette ───────────────────────────────────────────────────────────────────
#let accent  = rgb("#0f3460")
#let muted   = luma(120)
#let dim-txt = luma(160)

// ── Helpers ───────────────────────────────────────────────────────────────────
#let ns = fletcher.shapes   // shorthand for fletcher shapes namespace

// node fill colours
#let clr-blue   = rgb("#e8f0fe")
#let clr-red    = rgb("#fce8e6")
#let clr-green  = rgb("#e6f4ea")
#let clr-purple = rgb("#f3e5f5")
#let clr-amber  = rgb("#fff3e0")
#let clr-grey   = luma(242)

// ─────────────────────────────────────────────────────────────────────────────
//  COVER PAGE
// ─────────────────────────────────────────────────────────────────────────────
#set page(numbering: none)

#align(center)[
  #v(4cm)

  #block(
    fill: rgb("#1a1a2e"),
    width: 14cm,
    inset: (x: 2cm, y: 1.5cm),
    radius: 10pt,
  )[
    #text(fill: white, size: 40pt, weight: "black", font: "DejaVu Sans")[kg]
    #v(0.4em)
    #text(fill: rgb("#a8b2d8"), size: 16pt, font: "DejaVu Sans")[Knowledge Graph]
    #v(0.8em)
    #line(length: 6cm, stroke: 1pt + rgb("#4a5568"))
    #v(0.8em)
    #text(fill: rgb("#718096"), size: 11pt)[Reference Documentation]
  ]

  #v(2cm)

  #grid(
    columns: 3,
    gutter: 2em,
    align: center,
    block(width: 4cm)[
      #text(fill: accent, size: 22pt, weight: "bold", font: "DejaVu Sans")[JSONL]
      #v(0.3em)
      #text(fill: muted, size: 9pt)[Source of Truth]
    ],
    block(width: 4cm)[
      #text(fill: accent, size: 22pt, weight: "bold", font: "DejaVu Sans")[SQLite]
      #v(0.3em)
      #text(fill: muted, size: 9pt)[Derived Index]
    ],
    block(width: 4cm)[
      #text(fill: accent, size: 22pt, weight: "bold", font: "DejaVu Sans")[MCP]
      #v(0.3em)
      #text(fill: muted, size: 9pt)[LLM Integration]
    ],
  )

  #v(3cm)
  #text(fill: muted, size: 9pt)[
    Lightweight · Git-friendly · Hybrid Search · Local-first
  ]
]

#pagebreak()

// ─────────────────────────────────────────────────────────────────────────────
//  TABLE OF CONTENTS
// ─────────────────────────────────────────────────────────────────────────────
#set page(numbering: "i")
#counter(page).update(1)

#outline(title: [#text(font: "DejaVu Sans")[Contents]], depth: 2, indent: 1.5em)

#pagebreak()

// ─────────────────────────────────────────────────────────────────────────────
//  MAIN CONTENT
// ─────────────────────────────────────────────────────────────────────────────
#set page(numbering: "1")
#counter(page).update(1)

= Introduction

kg is a lightweight, file-first knowledge graph designed for software projects and AI-assisted workflows. It keeps knowledge as human-readable JSONL files that are trivially grep-able, diff-able, and git-trackable, while deriving a SQLite index for fast hybrid search.

#info(title: "Design Philosophy")[
  *Files are the source of truth.* SQLite is a derived cache you can always rebuild with `kg reindex`. The graph survives any database corruption — your notes never will.
]

== Why kg?

Traditional note systems force you to choose between structure (databases, heavy editors) and simplicity (plain text). kg offers a third path:

#grid(
  columns: (1fr, 1fr),
  gutter: 1.5em,
  block(stroke: 0.5pt + luma(200), radius: 5pt, inset: 1em, width: 100%)[
    *Plain text wins for durability*
    - `jq`, `rg`, `git log` work natively
    - Merge conflicts are readable
    - No lock-in: export is just `cat`
    - Works offline, always
  ],
  block(stroke: 0.5pt + luma(200), radius: 5pt, inset: 1em, width: 100%)[
    *Index wins for retrieval*
    - FTS5 BM25 keyword search
    - Vector similarity (local or cloud)
    - Calibrated hybrid score fusion
    - Cross-encoder reranking
  ],
)

== Key Concepts

A *#gls("node")* groups related *#glspl("bullet")* under a *#gls("slug")*. Think of nodes as Zettelkasten cards and bullets as atomic facts on those cards.

```
project/
  .kg/
    nodes/
      my-topic/
        node.jsonl    ← bullets (append-only log, git-tracked)
        meta.jsonl    ← votes, usage counts (git-tracked)
    index/
      graph.db        ← SQLite: FTS5 + embeddings + backlinks (gitignored)
  kg.toml             ← project config
```

#warning(title: "graph.db is gitignored")[
  Never commit `graph.db`. It is always derivable from the JSONL files via `kg reindex`. The `.kg/nodes/` directory is what you version-control.
]

= Architecture

== Component Overview

The diagram below shows the main components and data flows in kg.

#figure(
  diagram(
    node-stroke: 0.8pt,
    node-corner-radius: 4pt,
    spacing: (3.2cm, 1.5cm),
    // 3-col × 3-row layout; x ∈ {0,1,2}, y ∈ {0,1,2}
    // total horizontal span: 2 × 3.2 = 6.4cm + node widths ≈ 12cm ✓

    // ── Row 0: write path ─────────────────────────────────────────────────
    node((0, 0),
      [`.kg/nodes/` #linebreak() #text(size: 8pt, fill: dim-txt)[JSONL files]],
      fill: clr-blue, name: <jsonl>),
    node((1, 0),
      [inotify watcher #linebreak() #text(size: 8pt, fill: dim-txt)[file events]],
      fill: clr-red, name: <watcher>),
    node((2, 0),
      [SQLite #linebreak() #text(size: 8pt, fill: dim-txt)[graph.db]],
      fill: clr-green, name: <sqlite>),

    // ── Row 1: services ───────────────────────────────────────────────────
    node((1, 1),
      [Embedder #linebreak() #text(size: 8pt, fill: dim-txt)[fastembed / Gemini]],
      fill: clr-grey, name: <embedder>),
    node((2, 1),
      [Vector server #linebreak() #text(size: 8pt, fill: dim-txt)[port 7344]],
      fill: clr-grey, name: <vecserver>),

    // ── Row 2: consumers ──────────────────────────────────────────────────
    node((0, 2),
      [Claude Code #linebreak() #text(size: 8pt, fill: dim-txt)[memory_context()]],
      fill: clr-green, name: <claude>),
    node((1, 2),
      [MCP server #linebreak() #text(size: 8pt, fill: dim-txt)[stdio / FastMCP]],
      fill: clr-purple, name: <mcp>),
    node((2, 2),
      [`kg context` #linebreak() #text(size: 8pt, fill: dim-txt)[hybrid search]],
      fill: clr-amber, name: <context>),

    // ── Row 1 left: web ───────────────────────────────────────────────────
    node((0, 1),
      [`kg web` #linebreak() #text(size: 8pt, fill: dim-txt)[port 7343]],
      fill: clr-grey, name: <web>),

    // ── Edges ─────────────────────────────────────────────────────────────
    edge(<jsonl>,    <watcher>,  "->",
      label: text(size: 8pt)[inotify events]),
    edge(<watcher>,  <sqlite>,   "->",
      label: text(size: 8pt)[FTS index]),
    edge(<watcher>,  <embedder>, "->",
      label: text(size: 8pt)[on change]),
    edge(<embedder>, <sqlite>,   "->",
      label: text(size: 8pt)[store vectors]),
    edge(<sqlite>,   <vecserver>,"->",
      label: text(size: 8pt)[load on start]),
    edge(<sqlite>,   <context>,  "->",
      label: text(size: 8pt)[FTS query]),
    edge(<vecserver>,<context>,  "->",
      label: text(size: 8pt)[ANN results]),
    edge(<context>,  <mcp>,      "->",
      label: text(size: 8pt)[results]),
    edge(<mcp>,      <claude>,   "->",
      label: text(size: 8pt)[MCP tools]),
    edge(<sqlite>,   <web>,      "->",
      bend: 30deg, label: text(size: 8pt)[browse nodes]),
  ),
  caption: [kg component architecture and data flows],
)

== Write Path

When you run `kg add my-topic "some fact"`:

+ The CLI appends a new bullet (JSONL line with stable UUID) to `.kg/nodes/my-topic/node.jsonl`, creating the directory if needed.
+ The inotify #gls("watcher") detects the file change within milliseconds.
+ The watcher calls the indexer, which updates the FTS5 table in SQLite.
+ If embeddings are configured, the embedder generates a vector and stores it in the `embeddings` table.
+ Backlinks (`[[other-slug]]` references) are extracted and written to the `backlinks` table.

#tip(title: "Only the watcher writes to SQLite")[
  CLI commands like `kg add`, `kg update`, `kg delete` only write JSONL. The watcher is the *sole* writer to `graph.db`. This eliminates concurrent-write corruption.
]

== Read Path

```
kg context "query"
  │
  ├─ FTS search  ──────────────────────────┐
  │    SQLite FTS5 BM25 ranking            │
  │    OR-expanded + prefix wildcards      │  calibrated
  │                                        ├─ score fusion ──→ reranker ──→ output
  ├─ Vector search ────────────────────────┘
  │    cosine similarity via vector server │
  │    nearest-neighbor over embeddings    │
  │
  └─ Session dedup: skip already-seen bullets
```

== Storage Layout

#figure(
  table(
    columns: (auto, auto, 1fr),
    stroke: 0.5pt + luma(200),
    fill: (_, row) => if row == 0 { luma(235) } else { white },
    align: (left, left, left),
    [*Path*], [*Format*], [*Purpose*],
    [`kg.toml`], [TOML], [Project configuration (name, embeddings, sources)],
    [`.kg/nodes/<slug>/node.jsonl`], [JSONL], [Bullets — source of truth, git-tracked],
    [`.kg/nodes/<slug>/meta.jsonl`], [JSONL], [Vote counts, usage stats (git-tracked)],
    [`.kg/index/graph.db`], [SQLite], [FTS5 index, embeddings, backlinks (gitignored)],
    [`.kg/skills/`], [Markdown], [Claude Code skills, via `.claude→.kg` symlink],
    [`~/.cache/kg/embeddings/`], [diskcache], [Embedding vector cache (cross-project)],
  ),
  caption: [File storage layout],
)

= Installation

== From Git (Latest)

```bash
uv tool install "git+https://github.com/sasha-s/kg.git"
```

With optional extras for embedding support and live file watching:

```bash
uv tool install "kg[embeddings,watch] @ git+https://github.com/sasha-s/kg.git"
```

== Development / Editable

```bash
git clone https://github.com/sasha-s/kg.git
uv tool install --editable ./kg
```

== Optional Extras

#figure(
  table(
    columns: (auto, 1fr, 1fr),
    stroke: 0.5pt + luma(200),
    fill: (_, row) => if row == 0 { luma(235) } else { white },
    [*Extra*], [*Packages*], [*Use*],
    [`embeddings`], [`fastembed`, `google-genai`, `diskcache`, `numpy`], [Local + cloud embeddings],
    [`watch`], [`inotify-simple`], [Live file watching (Linux)],
    [`turso`], [`libsql`], [Turso remote SQLite (requires cmake)],
    [`dev`], [`ruff`, `basedpyright`, `pytest`], [Development tools],
  ),
  caption: [Optional install extras],
)

= Quickstart

== Initialize a Project

```bash
# In your project directory:
kg init             # writes kg.toml, creates .kg/
kg start            # index + calibrate + watcher + vector server + MCP + hooks
```

After `kg start`, the `.claude → .kg` symlink is created so Claude Code can discover the MCP server and local skills automatically.

== First Notes

```bash
kg add my-topic "discovered: X is faster than Y in this benchmark"
kg add my-topic "gotcha: Y breaks when input is empty" --type gotcha
kg add my-topic "decision: use X for production" --type decision
```

== Search

```bash
kg search "fast benchmark"         # FTS keyword search
kg context "which is faster"       # hybrid FTS + vector, calibrated + reranked
```

= CLI Reference

== Core Commands

#figure(
  table(
    columns: (auto, 1fr),
    stroke: 0.5pt + luma(200),
    fill: (_, row) => if row == 0 { luma(235) } else if calc.odd(row) { luma(250) } else { white },
    align: (left, left),
    [*Command*], [*Description*],
    [`kg init`], [Create `kg.toml` and `.kg/` directory structure],
    [`kg start`], [Index + calibrate + start watcher + vector server + MCP + install hooks],
    [`kg stop`], [Stop watcher and vector server],
    [`kg reload`], [Hot-reload `kg.toml` config (sends SIGHUP to watcher, no restart)],
    [`kg reindex`], [Rebuild SQLite from all `node.jsonl` files (stops/restarts watcher)],
    [`kg calibrate`], [Compute FTS/vector score quantiles for hybrid fusion],
    [`kg upgrade`], [Run schema migrations + reindex (safe to run anytime)],
    [`kg status`], [Show node counts, calibration state, watcher, vector server, config],
    [`kg serve`], [Start MCP server on stdio (used by Claude Code)],
    [`kg web`], [Start web viewer at `http://localhost:7343`],
  ),
  caption: [Core management commands],
)

== Node Commands

#figure(
  table(
    columns: (auto, 1fr),
    stroke: 0.5pt + luma(200),
    fill: (_, row) => if row == 0 { luma(235) } else if calc.odd(row) { luma(250) } else { white },
    [*Command*], [*Description*],
    [`kg add <slug> <text> [--type TYPE]`], [Add a bullet to a node (auto-creates node if missing)],
    [`kg create <slug> <title> [--type TYPE]`], [Create a node with explicit title (idempotent)],
    [`kg show <slug>`], [Print all bullets in a node],
    [`kg update <bullet-id> <text>`], [Update bullet text by ID],
    [`kg delete <bullet-id>`], [Delete a bullet by ID],
    [`kg nodes [PATTERN]`], [List nodes (glob on slug, e.g. `notes-*`)],
    [`kg nodes --bullets`], [Sort nodes by bullet count descending],
    [`kg nodes --recent`], [Sort nodes by most-recently-updated],
    [`kg nodes --docs`], [Show `_doc-*` source-file nodes instead of curated nodes],
  ),
  caption: [Node management commands],
)

The `--type` flag accepts: `fact`, `gotcha`, `decision`, `task`, `note`, `success`, `failure`.

== Search & Retrieval

#figure(
  table(
    columns: (auto, 1fr),
    stroke: 0.5pt + luma(200),
    fill: (_, row) => if row == 0 { luma(235) } else if calc.odd(row) { luma(250) } else { white },
    [*Command*], [*Description*],
    [`kg search <query> [-n N]`], [FTS keyword search, returns raw results],
    [`kg context <query> [-s SESSION_ID]`], [Hybrid search + rerank, formatted for LLM injection],
    [`kg context <query> -q <intent>`], [Use a separate query string for the reranking step],
  ),
  caption: [Search and retrieval commands],
)

== Review & Quality

#figure(
  table(
    columns: (auto, 1fr),
    stroke: 0.5pt + luma(200),
    fill: (_, row) => if row == 0 { luma(235) } else if calc.odd(row) { luma(250) } else { white },
    [*Command*], [*Description*],
    [`kg review`], [List nodes ordered by credits-per-bullet (review debt)],
    [`kg review <slug>`], [Mark node as reviewed (clears token budget to 0)],
    [`kg vote useful <bullet-id>...`], [Signal that a bullet is high quality],
    [`kg vote harmful <bullet-id>...`], [Signal that a bullet is wrong or misleading],
  ),
  caption: [Review and quality commands],
)

= Search & Context

== The Hybrid Search Pipeline

#figure(
  diagram(
    node-stroke: 0.8pt,
    node-corner-radius: 4pt,
    spacing: (5cm, 1.4cm),
    // Vertical pipeline: x ∈ {-0.6, 0, 0.6}, y ∈ {0..5}
    // Horizontal span: 1.2 × 5 = 6cm + node widths ≈ 9cm ✓

    // Row 0: Input
    node((0, 0),
      [Query #linebreak() #text(size: 8pt, fill: dim-txt)[natural language]],
      shape: ns.ellipse, fill: clr-blue, name: <q>),

    // Row 1: Split — FTS (left) + Vector (right)
    node((-0.6, 1),
      [FTS5 / BM25 #linebreak() #text(size: 8pt, fill: dim-txt)[OR + prefix wildcards]],
      fill: clr-amber, name: <fts>),
    node((0.6, 1),
      [Vector search #linebreak() #text(size: 8pt, fill: dim-txt)[cosine similarity]],
      fill: clr-purple, name: <vec>),

    // Row 2: Fusion
    node((0, 2),
      [Calibrated fusion #linebreak() #text(size: 8pt, fill: dim-txt)[quantile normalization]],
      fill: clr-green, name: <fuse>),

    // Row 3: Session adjustments
    node((-0.6, 3),
      [Session dedup #linebreak() #text(size: 8pt, fill: dim-txt)[skip seen bullets]],
      fill: clr-grey, name: <dedup>),
    node((0.6, 3),
      [Session boost #linebreak() #text(size: 8pt, fill: dim-txt)[1.3× seen nodes]],
      fill: clr-grey, name: <boost>),

    // Row 4: Reranker
    node((0, 4),
      [Cross-encoder reranker #linebreak() #text(size: 8pt, fill: dim-txt)[ms-marco-MiniLM]],
      fill: clr-red, name: <rerank>),

    // Row 5: Output
    node((0, 5),
      [Context output #linebreak() #text(size: 8pt, fill: dim-txt)[LLM-ready text]],
      shape: ns.ellipse, fill: clr-green, name: <out>),

    // Edges
    edge(<q>,      <fts>,    "->"),
    edge(<q>,      <vec>,    "->"),
    edge(<fts>,    <fuse>,   "->", label: text(size: 8pt)[FTS score]),
    edge(<vec>,    <fuse>,   "->", label: text(size: 8pt)[vec score], label-side: right),
    edge(<fuse>,   <dedup>,  "->"),
    edge(<fuse>,   <boost>,  "->"),
    edge(<dedup>,  <rerank>, "->"),
    edge(<boost>,  <rerank>, "->"),
    edge(<rerank>, <out>,    "->"),
  ),
  caption: [Hybrid search and context retrieval pipeline],
)

== FTS Search

SQLite FTS5 BM25 ranking with automatic query expansion:

- Each search term becomes `term OR term*` (prefix wildcard for partial matches)
- Multiple terms are OR-joined for broader recall
- Weight controlled by `fts_weight` in `kg.toml` (default 0.5)

== Vector Search

A lightweight HTTP vector server (port 7344) loads embeddings from SQLite at startup and serves approximate nearest-neighbor queries.

#figure(
  table(
    columns: (auto, auto, 1fr),
    stroke: 0.5pt + luma(200),
    fill: (_, row) => if row == 0 { luma(235) } else { white },
    [*Prefix in kg.toml*], [*Provider*], [*Notes*],
    [`gemini:...`], [Google Gemini], [Cloud — requires `GEMINI_API_KEY`],
    [`openai:...`], [OpenAI], [Cloud — requires `OPENAI_API_KEY`],
    [`fastembed:...`], [fastembed], [Local, no API key — runs on-device],
    [(bare model name)], [fastembed], [e.g. `BAAI/bge-small-en-v1.5`],
  ),
  caption: [Supported embedding providers],
)

== Score Calibration

Raw FTS and vector scores are incomparable across queries and models. #gls("calibration") maps them to percentile quantiles before fusion:

+ Sample ~200 random nodes from the graph
+ Run FTS and vector search against each sample
+ Compute breakpoints at p0, p10, p25, p50, p75, p90, p100
+ At query time, binary-search each raw score into its quantile bucket
+ Fuse: `score = fts_weight × fts_q + vec_weight × vec_q + dual_match_bonus`

#tip[
  Run `kg calibrate` after adding many new nodes or after changing your embedding model. The watcher auto-recalibrates when more than `auto_calibrate_threshold` (5% by default) of bullets change.
]

== Cross-Encoder Reranking

After fusion, the top-N candidates are passed to a cross-encoder #gls("reranker"). The reranker sees the full `(query, bullet)` pair — capturing semantic nuance that cosine similarity misses.

```toml
[search]
use_reranker = true
reranker_model = "Xenova/ms-marco-MiniLM-L-6-v2"
```

= Configuration

`kg.toml` lives in your project root. All sections are optional.

```toml
[kg]
name = "my-project"

[embeddings]
model = "gemini:gemini-embedding-001"
# model = "fastembed:BAAI/bge-small-en-v1.5"   # local alternative

[search]
fts_weight = 0.5
vector_weight = 0.5
dual_match_bonus = 0.1
use_reranker = true
reranker_model = "Xenova/ms-marco-MiniLM-L-6-v2"
auto_calibrate_threshold = 0.05  # recalibrate when 5% of bullets change

[review]
budget_threshold = 3000   # credits-per-bullet before review flag (~15 serves)

[server]
port = 7343         # web viewer
vector_port = 7344  # vector server

[[sources]]
name = "workspace"
path = "."
include = ["**/*.py", "**/*.md"]
use_git = true       # only index git-tracked files
```

API secrets in `.env` (gitignored by `kg init`):

```bash
GEMINI_API_KEY=...
TURSO_URL=libsql://...
TURSO_TOKEN=...
```

== Source File Indexing

The `[[sources]]` section indexes source files into the same FTS index as curated nodes. Content is automatically chunked (avg 1500 bytes) without LLM extraction.

```toml
[[sources]]
name = "codebase"
path = "/path/to/project"
include = ["**/*.py", "**/*.ts", "**/*.md"]
exclude = [".kg/**", "node_modules/**"]
use_git = true
```

#warning(title: "fnmatch differences: Python 3.11 vs 3.12")[
  In Python 3.11, `fnmatch` treats `*` as matching `/` — so `*.py` matches `src/main.py`. In Python 3.12+, `*` does *not* match `/`. Use `**/*.py` patterns for reliability across Python versions.
]

Indexed files appear as `_doc-*` nodes in SQLite, browsable via `kg nodes --docs` and the web viewer.

= Review & Token Budget

== The Review System

Every time a node contributes bullets to `kg context` output, the character count is added to that node's *#gls("budget", display: "token budget")*. When `budget ÷ bullet_count` exceeds `budget_threshold` (default 3000), the node is flagged for review.

Dividing by bullet count means every node — large or small — is held to the same standard.

```bash
kg review               # list flagged nodes, ordered by credits-per-bullet
kg review <slug>        # inspect flagged bullets + mark as reviewed (clears budget)
```

#figure(
  diagram(
    node-stroke: 0.8pt,
    node-corner-radius: 4pt,
    spacing: (3.2cm, 1.5cm),
    // 3-col × 3-row; x ∈ {0,1,2}, y ∈ {0,1,2}
    // horizontal span: 2 × 3.2 = 6.4cm + node widths ≈ 12cm ✓

    // Row 0: forward path
    node((0, 0),
      [`kg context` #linebreak() #text(size: 8pt, fill: dim-txt)[serve bullets]],
      fill: clr-blue, name: <serve>),
    node((1, 0),
      [Budget += chars #linebreak() #text(size: 8pt, fill: dim-txt)[per node, per serve]],
      fill: clr-amber, name: <accum>),
    node((2, 0),
      [budget/bullets #linebreak() > threshold?],
      shape: ns.diamond, fill: clr-grey, name: <check>),

    // Row -1: no-flag branch (above)
    node((2, -1),
      [Continue #linebreak() accumulating],
      fill: clr-green, name: <cont>),

    // Row 1: flag branch (below)
    node((2, 1),
      [Flag ⚠ in #linebreak() `kg review`],
      fill: clr-red, name: <flag>),
    node((1, 1),
      [Human updates #linebreak() bullets],
      fill: clr-purple, name: <human>),
    node((0, 1),
      [`kg review <slug>` #linebreak() budget → 0],
      fill: clr-green, name: <clear>),

    // Edges
    edge(<serve>,  <accum>,  "->"),
    edge(<accum>,  <check>,  "->"),
    edge(<check>,  <cont>,   "->", label: text(size: 8pt)[no]),
    edge(<check>,  <flag>,   "->", label: text(size: 8pt)[yes]),
    edge(<flag>,   <human>,  "->"),
    edge(<human>,  <clear>,  "->"),
    edge(<clear>,  <accum>,  "->", bend: -30deg,
      label: text(size: 8pt)[reset]),
  ),
  caption: [Token budget lifecycle and review loop],
)

== Vote Quality Signals

Bullets can be voted on to signal quality. A bullet with more harmful than useful votes is shown with a ⚠ prefix in `kg show` output.

```bash
kg vote useful  <bullet-id>    # signal high quality
kg vote harmful <bullet-id>    # signal wrong or misleading
```

`kg review` surfaces nodes with net-harmful bullets even if under the budget threshold.

= MCP Server & Claude Code

== Overview

kg implements the #gls("mcp") so any MCP-compatible LLM client can use it as a knowledge tool.

```bash
kg start   # registers MCP automatically in ~/.claude/settings.json
```

Or add manually to `~/.claude/settings.json`:

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

== MCP Tools

#figure(
  table(
    columns: (auto, 1fr),
    stroke: 0.5pt + luma(200),
    fill: (_, row) => if row == 0 { luma(235) } else if calc.odd(row) { luma(250) } else { white },
    [*Tool*], [*Description*],
    [`memory_context(query, session_id?)`], [Hybrid search + rerank, auto-dedup within session, LLM-ready output],
    [`memory_search(query, limit?)`], [Raw FTS keyword search, returns matching bullets],
    [`memory_show(slug)`], [Show all bullets for a node (read-only — does not clear budget)],
    [`memory_add_bullet(node_slug, text, bullet_type?)`], [Add a bullet to a node (auto-creates node if missing)],
    [`memory_mark_reviewed(slug)`], [Clear a node's token budget (marks node as reviewed)],
  ),
  caption: [MCP tools exposed by `kg serve`],
)

== .claude → .kg Symlink

`kg start` creates `<project>/.claude → .kg`. This lets Claude Code automatically discover:

- The MCP server registration (`settings.json`)
- Local skills under `.kg/skills/` (accessible as `.claude/skills/`)
- Project-specific instructions in `.kg/CLAUDE.md` (if present)

#tip(title: "Bundled Skills")[
  The `/note` and `/notes` skills ship with kg and are installed by `kg start`. `/note` adds a timestamped bullet with async cross-reference enrichment. `/notes` lists recent daily notes.
]

== Session Context Hook

`kg start` installs a `UserPromptSubmit` hook that injects the current session ID into each prompt:

```json
{
  "hooks": {
    "UserPromptSubmit": [{
      "hooks": [{ "type": "command", "command": "python -m kg.hooks.session_context" }]
    }]
  }
}
```

This enables per-session deduplication: bullets already surfaced in the current conversation are filtered from future `memory_context` results.

== Stop Hook: Fleeting Notes Extraction

The stop hook runs at session end and promotes fleeting notes to permanent nodes:

+ Reads `_fleeting-<session-id>` node
+ Identifies patterns worth promoting to permanent nodes
+ Adds bullets to appropriate nodes with `[[cross-references]]`
+ Deletes the fleeting bullets after promotion

```bash
# During a session — low-friction scratch notes:
kg add _fleeting-abc123 "discovered: lock file blocks concurrent kg reindex"

# At session end, the stop hook promotes this to the right permanent node.
```

= Web Viewer

== Overview

`kg web` serves a read-only UI at `http://localhost:7343`.

```bash
kg web              # start web server (default port 7343)
kg web --port 8080  # custom port
```

== Routes

#figure(
  table(
    columns: (auto, 1fr),
    stroke: 0.5pt + luma(200),
    fill: (_, row) => if row == 0 { luma(235) } else if calc.odd(row) { luma(250) } else { white },
    [*Route*], [*Content*],
    [`/`], [Node index: all curated nodes + collapsible docs section],
    [`/node/<slug>`], [Node page: bullets, backlinks, related nodes (lazy-loaded)],
    [`/node/_doc-xxx`], [Source file page: metadata, chunks, syntax highlighting, GitHub link],
    [`/search?q=<query>`], [Search results page (FTS + vector blend)],
    [`/api/related/<slug>`], [JSON endpoint for lazy-loading related nodes],
  ),
  caption: [Web viewer routes],
)

== Features

- *Backlinks*: every node page shows all nodes that reference `[[this-slug]]` in their bullets
- *Related nodes*: lazy-loaded sidebar using the node title + first 6 bullets as query
- *Source file rendering*: syntax highlighting (highlight.js CDN) and Markdown rendering (marked.js CDN)
- *Auto-linkification*: file paths like `src/kg/web.py` in bullet text are auto-linked to their `_doc-*` nodes
- *GitHub link*: doc pages show a "GitHub ↗" link pointing to the file at its last-modified commit hash

= Troubleshooting

== SQLite Disk I/O Error

On virtualized filesystems (OrbStack, NFS, Docker volumes), `graph.db` can become a 0-byte empty file, causing `disk I/O error` on all `kg` commands:

```bash
# Fix:
kg stop
rm .kg/index/graph.db*
kg reindex        # stops watcher, rebuilds, restarts
```

#warning[
  Always stop the watcher *before* removing `graph.db`. The watcher holds WAL/SHM files; deleting them while the watcher runs causes corruption on the next write.
]

== Zero Files Indexed from Sources

If `kg status` shows 0 files indexed for a source:

- Check include patterns: use `**/*.py` not `*.py` (see Python 3.12 fnmatch note)
- If `use_git = true`: verify with `git ls-files` in the source directory that files are tracked
- Run `kg index` to trigger an immediate re-index cycle
- Check `kg status` config section for path/git validation errors

== Embeddings Coverage Below 100%

After adding many nodes while the vector server was stopped, some nodes may have no embeddings. Start the server and recalibrate:

```bash
kg start      # ensure vector server is running
kg calibrate  # regenerates missing embeddings + recalibrates scores
```

== Vec Scores Missing from Calibration

If `kg status` shows "no vec calibration", the vector server was likely stopped during the last `kg calibrate` run:

```bash
kg start      # start the vector server
kg calibrate  # re-run calibration
```

= Glossary

#print-glossary(glossary-entries)
