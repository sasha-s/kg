"""Local message broker (mux) for Claude Code agents.

SQLite schema:
  messages(id, from_agent, to_agent, timestamp, urgency, type, body, status)
  agents(name, status, last_seen, pid, node_url, acked_through)

HTTP API:
  GET  /                          — status
  GET  /agents                    — list all agents (JSON)
  GET  /agent/{name}/pending      — get+deliver pending messages
  POST /agent/{name}/messages     — send a message to agent
  POST /agent/{name}/heartbeat    — update heartbeat, check urgent
  POST /agent/{name}/session-start — crash recovery + drain pending
  POST /agent/{name}/stop         — ack delivered or block for urgent
  POST /agent/{name}/session-end  — mark idle on crash/exit

Run as module to start server:
  python -m kg.agents.mux --serve <kg-root>
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


# ─── SQLite ───────────────────────────────────────────────────────────────────


def _init_db(db_path: Path) -> None:
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
                status      TEXT NOT NULL DEFAULT 'pending'
            );
            CREATE TABLE IF NOT EXISTS agents (
                name            TEXT PRIMARY KEY,
                status          TEXT NOT NULL DEFAULT 'idle',
                last_seen       TEXT,
                pid             INTEGER,
                node_url        TEXT,
                acked_through   TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_msg_to_status
                ON messages(to_agent, status, id);
        """)
        conn.commit()
    finally:
        conn.close()


def _drain_pending(conn: sqlite3.Connection, agent: str) -> list[dict]:
    """Return pending messages for agent and mark them delivered atomically."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM messages WHERE to_agent=? AND status='pending' ORDER BY id",
        (agent,),
    ).fetchall()
    msgs = [dict(r) for r in rows]
    if msgs:
        ids = [m["id"] for m in msgs]
        conn.execute(
            f"UPDATE messages SET status='delivered'"  # noqa: S608
            f" WHERE id IN ({','.join('?' * len(ids))})",
            ids,
        )
        conn.commit()
    return msgs


def _upsert_agent(conn: sqlite3.Connection, name: str, status: str, pid: int | None) -> None:
    conn.execute(
        "INSERT INTO agents(name, status, last_seen, pid) VALUES(?,?,?,?)"
        " ON CONFLICT(name) DO UPDATE SET"
        "   status=excluded.status,"
        "   last_seen=excluded.last_seen,"
        "   pid=excluded.pid",
        (name, status, _now_iso(), pid),
    )


# ─── Session archiving ────────────────────────────────────────────────────────


def _archive_session(agent_name: str, session_id: str, transcript_path: str, sessions_dir: Path) -> None:
    """Copy CC session transcript to .kg/sessions/<agent>/<session_id>.jsonl."""
    import shutil
    src = Path(transcript_path)
    if not src.exists():
        return
    dest_dir = sessions_dir / agent_name
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{session_id}.jsonl"
    if not dest.exists():
        shutil.copy2(src, dest)


# ─── HTTP handler ─────────────────────────────────────────────────────────────


def _make_handler(db_path: Path, sessions_dir: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            import urllib.parse
            path = urllib.parse.urlparse(self.path).path

            if path in ("/", ""):
                self._text(f"kg mux running\ndb: {db_path}\nport: see config")
                return

            if path == "/agents":
                with sqlite3.connect(str(db_path)) as conn:
                    conn.row_factory = sqlite3.Row
                    agents = [dict(r) for r in conn.execute(
                        "SELECT * FROM agents ORDER BY name"
                    ).fetchall()]
                    pending = dict(conn.execute(
                        "SELECT to_agent, COUNT(*) FROM messages"
                        " WHERE status='pending' GROUP BY to_agent"
                    ).fetchall())
                for a in agents:
                    a["pending_count"] = pending.get(a["name"], 0)
                self._json({"agents": agents})
                return

            parts = path.split("/")
            if len(parts) == 4 and parts[1] == "agent" and parts[3] == "pending":
                name = parts[2]
                with sqlite3.connect(str(db_path)) as conn:
                    msgs = _drain_pending(conn, name)
                self._json({"messages": msgs})
                return

            self._json({"error": "not found"}, 404)

        def do_POST(self) -> None:
            import urllib.parse
            path = urllib.parse.urlparse(self.path).path
            body = self._read_body()
            parts = path.split("/")  # ['', 'agent', name, action]

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

        def _handle_send(self, name: str, body: dict) -> None:
            msg_id = _new_id()
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO agents(name) VALUES(?)", (name,)
                )
                conn.execute(
                    "INSERT INTO messages"
                    " (id, from_agent, to_agent, timestamp, urgency, type, body, status)"
                    " VALUES(?,?,?,?,?,?,?,'pending')",
                    (
                        msg_id,
                        body.get("from", ""),
                        name,
                        _now_iso(),
                        body.get("urgency", "normal"),
                        body.get("type", "text"),
                        str(body.get("body", "")),
                    ),
                )
            self._json({"id": msg_id})

        def _handle_heartbeat(self, name: str, body: dict) -> None:
            with sqlite3.connect(str(db_path)) as conn:
                _upsert_agent(conn, name, "running", body.get("pid"))
                # Check for urgent messages to inject
                conn.row_factory = sqlite3.Row
                urgent = conn.execute(
                    "SELECT * FROM messages"
                    " WHERE to_agent=? AND urgency='urgent' AND status='pending'"
                    " ORDER BY id LIMIT 1",
                    (name,),
                ).fetchone()
                if urgent:
                    conn.execute(
                        "UPDATE messages SET status='delivered' WHERE id=?",
                        (urgent["id"],),
                    )
            if urgent:
                self._json({
                    "additionalContext": (
                        f"URGENT message from {urgent['from_agent']}:\n\n{urgent['body']}"
                    )
                })
            else:
                self._json({})

        def _handle_session_start(self, name: str, body: dict) -> None:
            with sqlite3.connect(str(db_path)) as conn:
                # Reset stuck 'delivered' messages → 'pending' (crash recovery)
                conn.execute(
                    "UPDATE messages SET status='pending'"
                    " WHERE to_agent=? AND status='delivered'",
                    (name,),
                )
                _upsert_agent(conn, name, "running", body.get("pid"))
                msgs = _drain_pending(conn, name)
            self._json({"messages": msgs})

        def _handle_stop(self, name: str, body: dict) -> None:
            stop_hook_active = bool(body.get("stop_hook_active", False))

            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                # Check for urgent pending
                urgent = conn.execute(
                    "SELECT * FROM messages"
                    " WHERE to_agent=? AND urgency='urgent' AND status='pending'"
                    " ORDER BY id LIMIT 1",
                    (name,),
                ).fetchone()

                if urgent and not stop_hook_active:
                    # Block: deliver urgent message
                    conn.execute(
                        "UPDATE messages SET status='delivered' WHERE id=?",
                        (urgent["id"],),
                    )
                    self._json({
                        "decision": "block",
                        "reason": (
                            f"URGENT message from {urgent['from_agent']}:\n\n{urgent['body']}"
                        ),
                    })
                    return

                # Ack all delivered messages for this agent
                conn.execute(
                    "UPDATE messages SET status='acked'"
                    " WHERE to_agent=? AND status='delivered'",
                    (name,),
                )
                conn.execute(
                    "UPDATE agents SET status='idle', pid=NULL WHERE name=?",
                    (name,),
                )

            # Archive session transcript if provided
            session_id = body.get("session_id", "")
            transcript_path = body.get("transcript_path", "")
            if session_id and transcript_path:
                _archive_session(name, session_id, transcript_path, sessions_dir)

            self._json({})

        def _handle_session_end(self, name: str) -> None:
            """Mark agent idle on crash/exit (no ack — messages stay for re-delivery)."""
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute(
                    "UPDATE agents SET status='idle', pid=NULL WHERE name=?",
                    (name,),
                )
            self._json({})

        # ── HTTP helpers ─────────────────────────────────────────────────────

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if length:
                try:
                    return json.loads(self.rfile.read(length))
                except (json.JSONDecodeError, Exception):
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


# ─── Server lifecycle ─────────────────────────────────────────────────────────


def start_server(cfg: KGConfig) -> None:
    """Start the mux server (blocking). Used for foreground/subprocess mode."""
    _init_db(cfg.mux_db_path)
    cfg.sessions_dir.mkdir(parents=True, exist_ok=True)
    handler = _make_handler(cfg.mux_db_path, cfg.sessions_dir)
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
    cfg.sessions_dir.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        [sys.executable, "-m", "kg.agents.mux", "--serve", str(cfg.root)],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid_path.write_text(str(proc.pid))
    time.sleep(0.4)

    # Verify started
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
    """Return human-readable status string."""
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
    if "--serve" in sys.argv:
        idx = sys.argv.index("--serve")
        root = Path(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else None
        from kg.config import load_config
        _cfg = load_config(root)
        start_server(_cfg)
    else:
        print("Usage: python -m kg.agents.mux --serve <kg-root>")
        sys.exit(1)
