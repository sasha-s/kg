"""Local message broker (mux) for Claude Code agents.

Agent registry (user-level):
  ~/.local/share/kg/mux.db       — agents(name, status, last_seen, pid, kg_root)

Per-project message store (at each agent's kg_root):
  .kg/messages/<recipient>/inbox/from/<sender>/
    messages-00001.jsonl         — normal messages (append-only segments)
    urgent-00001.jsonl           — urgent messages (separate, append-only)
  .kg/messages/<recipient>/acks/<sender>.json
    {"normal_delivered": "", "normal_acked": "",
     "urgent_delivered": "", "urgent_acked": ""}
  .kg/index/messages.db          — SQLite index (derived, never git-tracked)

HTTP API:
  GET  /                          — status
  GET  /agents                    — list agents (JSON)
  GET  /agent/{name}/pending      — new messages since last delivery
  POST /agent/{name}/messages     — send a message to agent
  POST /agent/{name}/heartbeat    — heartbeat + urgent check
  POST /agent/{name}/session-start — register + drain all unacked
  POST /agent/{name}/stop         — ack messages or block for urgent
  POST /agent/{name}/session-end  — mark idle on crash (no ack)
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import socketserver
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kg.config import KGConfig


# ─── ID + time helpers ────────────────────────────────────────────────────────


def _new_id() -> str:
    """Sortable unique ID: nanosecond timestamp + random suffix."""
    return f"{time.time_ns():020d}_{secrets.token_hex(6)}"


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.UTC).isoformat()


# ─── Path helpers ─────────────────────────────────────────────────────────────


def _inbox_dir(kg_root: Path, recipient: str, sender: str) -> Path:
    return kg_root / ".kg" / "messages" / recipient / "inbox" / "from" / sender


def _ack_path(kg_root: Path, recipient: str, sender: str) -> Path:
    return kg_root / ".kg" / "messages" / recipient / "acks" / f"{sender}.json"


def _messages_db_path(kg_root: Path) -> Path:
    return kg_root / ".kg" / "index" / "messages.db"


# ─── Ack file helpers ─────────────────────────────────────────────────────────

_ACK_DEFAULTS: dict[str, str] = {
    "normal_delivered": "",
    "normal_acked": "",
    "urgent_delivered": "",
    "urgent_session_delivered": "",  # only updated by session-start / get_pending_messages
    "urgent_acked": "",
}


def _read_ack(kg_root: Path, recipient: str, sender: str) -> dict[str, str]:
    path = _ack_path(kg_root, recipient, sender)
    if path.exists():
        try:
            return {**_ACK_DEFAULTS, **json.loads(path.read_text())}
        except Exception:
            pass
    return dict(_ACK_DEFAULTS)


def _write_ack(kg_root: Path, recipient: str, sender: str, **updates: str) -> None:
    path = _ack_path(kg_root, recipient, sender)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = _read_ack(kg_root, recipient, sender)
    current.update(updates)
    path.write_text(json.dumps(current, indent=2))


# ─── Segment helpers ──────────────────────────────────────────────────────────


def _current_segment(inbox_dir: Path, prefix: str, segment_lines: int) -> Path:
    """Return current writable segment, rolling to a new one at the line limit."""
    inbox_dir.mkdir(parents=True, exist_ok=True)
    segments = sorted(inbox_dir.glob(f"{prefix}-?????.jsonl"))
    if segments:
        current = segments[-1]
        try:
            count = sum(1 for _ in current.open())
        except Exception:
            count = 0
        if count < segment_lines:
            return current
        num = int(current.stem.rsplit("-", 1)[-1]) + 1
    else:
        num = 1
    return inbox_dir / f"{prefix}-{num:05d}.jsonl"


# ─── Messages DB (per-project SQLite index) ───────────────────────────────────


def _init_messages_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id          TEXT PRIMARY KEY,
                from_agent  TEXT NOT NULL,
                to_agent    TEXT NOT NULL,
                timestamp   TEXT NOT NULL,
                urgency     TEXT NOT NULL DEFAULT 'normal',
                type        TEXT NOT NULL DEFAULT 'text',
                body        TEXT NOT NULL,
                segment     TEXT NOT NULL,
                line        INTEGER NOT NULL,
                acked       INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_to_from_urgency
                ON messages(to_agent, from_agent, urgency, id);
        """)
        conn.commit()
        # Migration: add acked column to existing DBs
        try:
            conn.execute("ALTER TABLE messages ADD COLUMN acked INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        except Exception:
            pass  # column already exists
    finally:
        conn.close()


def _index_message(db_path: Path, msg: dict, segment: str, line: int) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT OR IGNORE INTO messages"
            " (id, from_agent, to_agent, timestamp, urgency, type, body, segment, line)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (
                msg["id"], msg["from_agent"], msg["to_agent"], msg["timestamp"],
                msg.get("urgency", "normal"), msg.get("type", "text"), msg["body"],
                segment, line,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _ack_messages_in_db(
    db_path: Path, recipient: str, sender: str,
    normal_acked_id: str, urgent_acked_id: str,
) -> None:
    """Mark messages as acked in messages.db index."""
    if not db_path.exists():
        return
    conn = sqlite3.connect(str(db_path))
    try:
        if normal_acked_id:
            conn.execute(
                "UPDATE messages SET acked=1"
                " WHERE to_agent=? AND from_agent=? AND urgency='normal' AND id<=?",
                (recipient, sender, normal_acked_id),
            )
        if urgent_acked_id:
            conn.execute(
                "UPDATE messages SET acked=1"
                " WHERE to_agent=? AND from_agent=? AND urgency='urgent' AND id<=?",
                (recipient, sender, urgent_acked_id),
            )
        conn.commit()
    finally:
        conn.close()


def _count_unacked(db_path: Path, recipient: str, sender: str, after_id: str) -> int:
    """Count unacked normal messages from sender to recipient."""
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM messages"
            " WHERE to_agent=? AND from_agent=? AND urgency='normal'"
            " AND (? = '' OR id > ?)",
            (recipient, sender, after_id, after_id),
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def _read_unacked(
    db_path: Path, recipient: str, sender: str, urgency: str, after_id: str,
) -> list[dict]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM messages"
            " WHERE to_agent=? AND from_agent=? AND urgency=?"
            " AND (? = '' OR id > ?)"
            " ORDER BY id",
            (recipient, sender, urgency, after_id, after_id),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _list_senders(db_path: Path, recipient: str) -> list[str]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT DISTINCT from_agent FROM messages WHERE to_agent=? AND from_agent != ''",
            (recipient,),
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


# ─── Agent registry (mux.db — user-level) ────────────────────────────────────


def _init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS agents (
                name       TEXT PRIMARY KEY,
                status     TEXT NOT NULL DEFAULT 'idle',
                last_seen  TEXT,
                pid        INTEGER,
                kg_root    TEXT,
                session_id TEXT
            );
        """)
        conn.commit()
        # Migrations for existing DBs
        for col, typedef in [("session_id", "TEXT")]:
            try:
                conn.execute(f"ALTER TABLE agents ADD COLUMN {col} {typedef}")
                conn.commit()
            except Exception:
                pass  # already exists
    finally:
        conn.close()


def _upsert_agent(
    conn: sqlite3.Connection, name: str, status: str,
    pid: int | None, kg_root: str = "", session_id: str = "",
) -> None:
    conn.execute(
        "INSERT INTO agents(name, status, last_seen, pid, kg_root, session_id)"
        " VALUES(?,?,?,?,?,?)"
        " ON CONFLICT(name) DO UPDATE SET"
        "   status=excluded.status,"
        "   last_seen=excluded.last_seen,"
        "   pid=excluded.pid,"
        # only update kg_root if a non-empty value is provided
        "   kg_root=COALESCE(NULLIF(excluded.kg_root,''), kg_root),"
        "   session_id=COALESCE(NULLIF(excluded.session_id,''), session_id)",
        (name, status, _now_iso(), pid, kg_root, session_id),
    )


def _get_kg_root(conn: sqlite3.Connection, name: str) -> Path | None:
    row = conn.execute("SELECT kg_root FROM agents WHERE name=?", (name,)).fetchone()
    if row and row[0]:
        return Path(row[0])
    return None


# ─── HTTP handler ─────────────────────────────────────────────────────────────


def _make_handler(
    mux_db: Path,
    max_inbox: int = 50,
    segment_lines: int = 500,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            import urllib.parse
            path = urllib.parse.urlparse(self.path).path

            if path in ("/", ""):
                self._text(f"kg mux running\ndb: {mux_db}")
                return

            if path == "/agents":
                with sqlite3.connect(str(mux_db)) as conn:
                    conn.row_factory = sqlite3.Row
                    agents = [dict(r) for r in conn.execute(
                        "SELECT name, status, last_seen, pid FROM agents ORDER BY name"
                    ).fetchall()]
                self._json({"agents": agents})
                return

            parts = path.split("/")
            if len(parts) == 4 and parts[1] == "agent" and parts[3] == "pending":
                self._handle_pending(parts[2])
                return
            if len(parts) == 4 and parts[1] == "agent" and parts[3] == "pending-count":
                self._handle_pending_count(parts[2])
                return

            self._json({"error": "not found"}, 404)

        def do_POST(self) -> None:
            import urllib.parse
            path = urllib.parse.urlparse(self.path).path
            body = self._read_body()
            parts = path.split("/")

            if len(parts) != 4 or parts[1] != "agent":
                self._json({"error": "not found"}, 404)
                return

            name, action = parts[2], parts[3]
            if action == "messages":
                self._handle_send(name, body)
            elif action == "heartbeat":
                self._handle_heartbeat(name, body)
            elif action == "session-start":
                self._handle_session_start(name, body)
            elif action == "stop":
                self._handle_stop(name, body)
            elif action == "session-end":
                self._handle_session_end(name)
            else:
                self._json({"error": "unknown action"}, 404)

        # ── action handlers ──────────────────────────────────────────────────

        def _handle_send(self, to_name: str, body: dict) -> None:
            from_name = body.get("from", "")
            urgency = body.get("urgency", "normal")

            with sqlite3.connect(str(mux_db)) as conn:
                kg_root = _get_kg_root(conn, to_name)

            if kg_root is None:
                # Fall back to sender's kg_root (e.g. agent replying to "web" user)
                if from_name:
                    with sqlite3.connect(str(mux_db)) as conn:
                        kg_root = _get_kg_root(conn, from_name)
                if kg_root is None:
                    self._json({
                        "error": f"agent '{to_name}' not registered — run: kg mux agent create {to_name}"
                    }, 404)
                    return
                # Auto-register recipient with sender's kg_root so future lookups work
                with sqlite3.connect(str(mux_db)) as conn:
                    _upsert_agent(conn, to_name, "idle", None, str(kg_root))

            # Auto-register sender so replies can be routed back
            if from_name and from_name != to_name:
                with sqlite3.connect(str(mux_db)) as conn:
                    existing = conn.execute(
                        "SELECT kg_root FROM agents WHERE name=?", (from_name,)
                    ).fetchone()
                    if not existing or not existing[0]:
                        _upsert_agent(conn, from_name, "idle", None, str(kg_root))

            # Cap check: normal messages only, requires known sender
            if urgency != "urgent" and max_inbox > 0 and from_name:
                db_path = _messages_db_path(kg_root)
                _init_messages_db(db_path)
                ack = _read_ack(kg_root, to_name, from_name)
                unacked = _count_unacked(db_path, to_name, from_name, ack["normal_acked"])
                if unacked >= max_inbox:
                    self._json({
                        "error": "inbox full",
                        "unacked": unacked,
                        "max_inbox": max_inbox,
                    }, 429)
                    return

            msg_id = _new_id()
            msg = {
                "id": msg_id,
                "from_agent": from_name,
                "to_agent": to_name,
                "timestamp": _now_iso(),
                "urgency": urgency,
                "type": body.get("type", "text"),
                "body": str(body.get("body", "")),
            }

            # Append to JSONL segment
            inbox = _inbox_dir(kg_root, to_name, from_name)
            prefix = "urgent" if urgency == "urgent" else "messages"
            seg_path = _current_segment(inbox, prefix, segment_lines)
            with seg_path.open("a") as f:
                f.write(json.dumps(msg) + "\n")

            # Count line number for index
            try:
                line = sum(1 for _ in seg_path.open())
            except Exception:
                line = 1

            # Update messages.db index
            db_path = _messages_db_path(kg_root)
            _init_messages_db(db_path)
            seg_rel = str(seg_path.relative_to(kg_root / ".kg" / "messages"))
            _index_message(db_path, msg, seg_rel, line)

            # Implicit urgent ack: if from_name is replying to to_name, ack unacked
            # urgents FROM to_name in from_name's inbox. This means "sent a reply → acked".
            if from_name and from_name != to_name:
                with sqlite3.connect(str(mux_db)) as conn:
                    sender_kg_root = _get_kg_root(conn, from_name)
                if sender_kg_root:
                    sender_db = _messages_db_path(sender_kg_root)
                    sender_ack = _read_ack(sender_kg_root, from_name, to_name)
                    session_del = sender_ack["urgent_session_delivered"]
                    if session_del and session_del > sender_ack["urgent_acked"]:
                        _write_ack(sender_kg_root, from_name, to_name,
                                   urgent_acked=session_del)
                        _ack_messages_in_db(sender_db, from_name, to_name, "", session_del)

            self._json({"id": msg_id})

        def _handle_pending(self, name: str) -> None:
            """Return new normal messages since last delivery (UserPromptSubmit)."""
            with sqlite3.connect(str(mux_db)) as conn:
                kg_root = _get_kg_root(conn, name)
            if kg_root is None:
                self._json({"messages": []})
                return

            db_path = _messages_db_path(kg_root)
            all_msgs: list[dict] = []
            for sender in _list_senders(db_path, name):
                ack = _read_ack(kg_root, name, sender)
                after = ack["normal_delivered"]
                msgs = _read_unacked(db_path, name, sender, "normal", after)
                if msgs:
                    all_msgs.extend(msgs)
                    _write_ack(kg_root, name, sender, normal_delivered=msgs[-1]["id"])

            all_msgs.sort(key=lambda m: m["id"])
            self._json({"messages": all_msgs})

        def _handle_pending_count(self, name: str) -> None:
            """Non-destructive count of undelivered messages (normal + urgent). Used by launcher."""
            with sqlite3.connect(str(mux_db)) as conn:
                kg_root = _get_kg_root(conn, name)
            if kg_root is None:
                self._json({"count": 0})
                return
            db_path = _messages_db_path(kg_root)
            count = 0
            for sender in _list_senders(db_path, name):
                ack = _read_ack(kg_root, name, sender)
                count += _count_unacked(db_path, name, sender, ack["normal_acked"])
                # count unprocessed urgents: use urgent_acked (not urgent_delivered) so
                # messages that were injected but not yet replied-to still wake the launcher
                urgent_msgs = _read_unacked(db_path, name, sender, "urgent", ack["urgent_acked"])
                count += len(urgent_msgs)
            self._json({"count": count})

        def _handle_heartbeat(self, name: str, body: dict) -> None:
            """Heartbeat + deliver one urgent message if pending."""
            with sqlite3.connect(str(mux_db)) as conn:
                _upsert_agent(conn, name, "running", body.get("pid"), body.get("kg_root", ""))
                kg_root = _get_kg_root(conn, name)
            if kg_root is None:
                self._json({})
                return

            db_path = _messages_db_path(kg_root)
            for sender in _list_senders(db_path, name):
                ack = _read_ack(kg_root, name, sender)
                after = ack["urgent_delivered"]
                msgs = _read_unacked(db_path, name, sender, "urgent", after)
                if msgs:
                    urgent = msgs[0]
                    _write_ack(kg_root, name, sender, urgent_delivered=urgent["id"])
                    self._json({
                        "additionalContext": (
                            f"URGENT message from {urgent['from_agent']}:\n\n{urgent['body']}"
                        )
                    })
                    return

            self._json({})

        def _handle_session_start(self, name: str, body: dict) -> None:
            """Register agent, drain all unacked messages (with crash recovery)."""
            kg_root_str = body.get("kg_root", "")
            with sqlite3.connect(str(mux_db)) as conn:
                _upsert_agent(conn, name, "running", body.get("pid"), kg_root_str,
                              body.get("session_id", ""))
                kg_root = _get_kg_root(conn, name)

            if kg_root is None:
                self._json({"messages": []})
                return

            db_path = _messages_db_path(kg_root)
            _init_messages_db(db_path)

            all_msgs: list[dict] = []
            for sender in _list_senders(db_path, name):
                ack = _read_ack(kg_root, name, sender)
                # Crash recovery: re-read from acked_through (ignore stale delivered_through)
                normal = _read_unacked(db_path, name, sender, "normal", ack["normal_acked"])
                urgent = _read_unacked(db_path, name, sender, "urgent", ack["urgent_acked"])

                updates: dict[str, str] = {}
                if normal:
                    all_msgs.extend(normal)
                    updates["normal_delivered"] = normal[-1]["id"]
                if urgent:
                    all_msgs.extend(urgent)
                    # session_delivered tracks session-start delivery (Stop uses this, not heartbeat's)
                    updates["urgent_delivered"] = urgent[-1]["id"]
                    updates["urgent_session_delivered"] = urgent[-1]["id"]
                if updates:
                    _write_ack(kg_root, name, sender, **updates)

            all_msgs.sort(key=lambda m: m["id"])
            self._json({"messages": all_msgs})

        def _handle_stop(self, name: str, body: dict) -> None:
            """Ack delivered messages or block for undelivered urgent."""
            stop_hook_active = bool(body.get("stop_hook_active", False))

            with sqlite3.connect(str(mux_db)) as conn:
                kg_root = _get_kg_root(conn, name)
            if kg_root is None:
                self._json({})
                return

            db_path = _messages_db_path(kg_root)
            senders = _list_senders(db_path, name)

            # Block if there are urgent messages not yet delivered
            if not stop_hook_active:
                for sender in senders:
                    ack = _read_ack(kg_root, name, sender)
                    msgs = _read_unacked(db_path, name, sender, "urgent", ack["urgent_delivered"])
                    if msgs:
                        urgent = msgs[0]
                        _write_ack(kg_root, name, sender, urgent_delivered=urgent["id"])
                        self._json({
                            "decision": "block",
                            "reason": (
                                f"URGENT message from {urgent['from_agent']}:\n\n{urgent['body']}"
                            ),
                        })
                        return

            # Commit normal acks only.
            # Urgents are NOT acked here — they're re-delivered on the next session-start
            # so the agent gets another chance to reply if it was killed mid-processing.
            # Urgents get acked when session-start delivers NEW urgents (implying the
            # previous session's urgents were processed successfully).
            for sender in senders:
                ack = _read_ack(kg_root, name, sender)
                updates: dict[str, str] = {}
                if ack["normal_delivered"]:
                    updates["normal_acked"] = ack["normal_delivered"]
                if updates:
                    _write_ack(kg_root, name, sender, **updates)
                    _ack_messages_in_db(
                        db_path, name, sender,
                        updates.get("normal_acked", ""),
                        "",
                    )

            with sqlite3.connect(str(mux_db)) as conn:
                conn.execute(
                    "UPDATE agents SET status='idle', pid=NULL WHERE name=?", (name,)
                )

            self._json({})

        def _handle_session_end(self, name: str) -> None:
            """Mark idle on crash — no ack update, messages re-delivered next session."""
            with sqlite3.connect(str(mux_db)) as conn:
                conn.execute(
                    "UPDATE agents SET status='idle', pid=NULL WHERE name=?", (name,)
                )
            self._json({})

        # ── HTTP helpers ─────────────────────────────────────────────────────

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if length:
                try:
                    return json.loads(self.rfile.read(length))
                except Exception:
                    return {}
            return {}

        def _json(self, data: dict, status: int = 200) -> None:
            enc = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(enc)))
            self.end_headers()
            self.wfile.write(enc)

        def _text(self, text: str, status: int = 200) -> None:
            enc = text.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(enc)))
            self.end_headers()
            self.wfile.write(enc)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass  # suppress per-request logging

    return Handler


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ─── Heartbeat reaper ─────────────────────────────────────────────────────────


def _start_reaper(mux_db: Path, timeout_minutes: int) -> None:
    """Background thread: mark agents idle if last_seen older than timeout_minutes."""
    import threading

    if timeout_minutes <= 0:
        return

    def _reap() -> None:
        import datetime
        while True:
            time.sleep(60)
            try:
                cutoff = (
                    datetime.datetime.now(datetime.UTC)
                    - datetime.timedelta(minutes=timeout_minutes)
                ).isoformat()
                with sqlite3.connect(str(mux_db)) as conn:
                    conn.execute(
                        "UPDATE agents SET status='idle', pid=NULL"
                        " WHERE status='running' AND last_seen < ?",
                        (cutoff,),
                    )
            except Exception:
                pass

    t = threading.Thread(target=_reap, daemon=True, name="kg-mux-reaper")
    t.start()


# ─── Server lifecycle ─────────────────────────────────────────────────────────


def start_server(cfg: KGConfig) -> None:
    """Start the mux server (blocking). Used for foreground/subprocess mode."""
    import datetime  # noqa: F401 — ensure available for reaper
    _init_db(cfg.mux_db_path)
    handler = _make_handler(cfg.mux_db_path, cfg.agents.max_inbox, cfg.agents.segment_lines)
    _start_reaper(cfg.mux_db_path, cfg.agents.heartbeat_timeout)
    server = _ThreadingHTTPServer(("127.0.0.1", cfg.agents.mux_port), handler)
    print(f"kg mux  →  http://127.0.0.1:{cfg.agents.mux_port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def start_background(cfg: KGConfig) -> tuple[bool, str]:
    """Start mux server as background process. Returns (success, message)."""
    import subprocess
    import urllib.request

    pid_path = cfg.mux_pid_path

    # Check if already running
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)
            return True, f"mux already running (pid {pid})"
        except (ProcessLookupError, ValueError):
            pid_path.unlink(missing_ok=True)

    _init_db(cfg.mux_db_path)

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "kg.agents.mux",
            "--db", str(cfg.mux_db_path),
            "--port", str(cfg.agents.mux_port),
            "--max-inbox", str(cfg.agents.max_inbox),
            "--segment-lines", str(cfg.agents.segment_lines),
            "--heartbeat-timeout", str(cfg.agents.heartbeat_timeout),
        ],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(proc.pid))
    time.sleep(0.4)

    try:
        urllib.request.urlopen(
            f"http://127.0.0.1:{cfg.agents.mux_port}/", timeout=2
        )
        return True, f"mux started (pid {proc.pid}) on :{cfg.agents.mux_port}"
    except Exception:
        pid_path.unlink(missing_ok=True)
        return False, f"mux failed to start (check port {cfg.agents.mux_port})"


def stop_background(cfg: KGConfig) -> tuple[bool, str]:
    """Stop background mux server."""
    pid_path = cfg.mux_pid_path
    if not pid_path.exists():
        return True, "mux not running"
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        pid_path.unlink(missing_ok=True)
        return True, f"mux stopped (pid {pid})"
    except (ProcessLookupError, ValueError):
        pid_path.unlink(missing_ok=True)
        return True, "mux was not running (removed stale pid)"


def mux_status(cfg: KGConfig) -> str:
    pid_path = cfg.mux_pid_path
    if not pid_path.exists():
        return "stopped"
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, 0)
        return f"running (pid {pid}) on :{cfg.agents.mux_port}"
    except (ProcessLookupError, ValueError):
        return "stopped (stale pid file)"


# ─── Entry point (subprocess mode) ───────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import datetime  # noqa: F401
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--port", type=int, default=7346)
    parser.add_argument("--max-inbox", type=int, default=50)
    parser.add_argument("--segment-lines", type=int, default=500)
    parser.add_argument("--heartbeat-timeout", type=int, default=10)
    args = parser.parse_args()

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _init_db(db_path)
    handler = _make_handler(db_path, args.max_inbox, args.segment_lines)
    _start_reaper(db_path, args.heartbeat_timeout)
    server = _ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"kg mux  →  http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
