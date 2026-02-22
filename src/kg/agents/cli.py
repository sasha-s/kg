"""kg mux — agent message broker CLI commands."""

from __future__ import annotations

import json
import sqlite3
import urllib.request
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from kg.config import KGConfig


def _load_cfg() -> KGConfig:
    from kg.config import load_config
    try:
        return load_config()
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@click.group("mux")
def mux_cli() -> None:
    """Agent message broker — local mux server."""


@mux_cli.command("start")
@click.option("--foreground", "-f", is_flag=True, help="Run in foreground (blocking)")
def mux_start(foreground: bool) -> None:
    """Start the mux server."""
    from kg.agents.mux import start_background, start_server
    cfg = _load_cfg()
    if not cfg.agents.enabled:
        raise click.ClickException(
            "Agents not enabled. Add `[agents]\\nenabled = true` to kg.toml"
        )
    if foreground:
        start_server(cfg)
    else:
        ok, msg = start_background(cfg)
        click.echo(f"{'✓' if ok else '✗'} {msg}")


@mux_cli.command("stop")
def mux_stop() -> None:
    """Stop the mux server."""
    from kg.agents.mux import stop_background
    cfg = _load_cfg()
    ok, msg = stop_background(cfg)
    click.echo(f"{'✓' if ok else '✗'} {msg}")


@mux_cli.command("status")
def mux_status_cmd() -> None:
    """Show mux status and registered agents."""
    from kg.agents.mux import mux_status
    cfg = _load_cfg()

    click.echo(f"Mux:    {mux_status(cfg)}")
    click.echo(f"URL:    {cfg.agents.mux_url}")
    click.echo(f"Agent:  {cfg.agents.name or '(not set)'}")
    click.echo(f"DB:     {cfg.mux_db_path}")

    if not cfg.mux_db_path.exists():
        click.echo("\nNo mux database found. Start with `kg mux start`.")
        return

    conn = sqlite3.connect(str(cfg.mux_db_path))
    conn.row_factory = sqlite3.Row
    agents = conn.execute("SELECT * FROM agents ORDER BY name").fetchall()
    pending = dict(conn.execute(
        "SELECT to_agent, COUNT(*) FROM messages WHERE status='pending' GROUP BY to_agent"
    ).fetchall())
    total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()

    click.echo(f"\nMessages total: {total}")
    if agents:
        click.echo("\nAgents:")
        for a in agents:
            n = pending.get(a["name"], 0)
            pstr = f"  [{n} pending]" if n else ""
            click.echo(
                f"  {a['name']:20} {a['status']:10} "
                f"{(a['last_seen'] or '')[:19]}{pstr}"
            )
    else:
        click.echo("\nNo agents registered yet.")


@mux_cli.command("send")
@click.argument("to_agent")
@click.argument("body")
@click.option("--from", "from_agent", default="", help="Sender name")
@click.option("--urgent", is_flag=True, help="Mark as urgent")
def mux_send(to_agent: str, body: str, from_agent: str, urgent: bool) -> None:
    """Send a message to an agent."""
    cfg = _load_cfg()
    sender = from_agent or cfg.agents.name or "cli"
    payload = json.dumps({
        "from": sender,
        "body": body,
        "urgency": "urgent" if urgent else "normal",
        "type": "text",
    }).encode()
    url = f"{cfg.agents.mux_url}/agent/{to_agent}/messages"
    req = urllib.request.Request(  # noqa: S310
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            result = json.loads(resp.read())
        click.echo(f"✓ Sent (id: {result.get('id', '?')})")
    except Exception as exc:
        raise click.ClickException(f"Failed: {exc}") from exc


@mux_cli.command("messages")
@click.option("--agent", "-a", default="", help="Filter by agent")
@click.option("--status", "-s", default="", help="Filter by status")
@click.option("--limit", "-l", default=20, show_default=True)
def mux_messages(agent: str, status: str, limit: int) -> None:
    """List messages in the mux database."""
    cfg = _load_cfg()
    if not cfg.mux_db_path.exists():
        click.echo("No mux database. Start mux first with `kg mux start`.")
        return

    conn = sqlite3.connect(str(cfg.mux_db_path))
    conn.row_factory = sqlite3.Row
    conds, params = [], []
    if agent:
        conds.append("(to_agent=? OR from_agent=?)")
        params += [agent, agent]
    if status:
        conds.append("status=?")
        params.append(status)
    where = f"WHERE {' AND '.join(conds)}" if conds else ""
    rows = conn.execute(
        f"SELECT * FROM messages {where} ORDER BY id DESC LIMIT ?",  # noqa: S608
        [*params, limit],
    ).fetchall()
    conn.close()

    if not rows:
        click.echo("No messages found.")
        return

    for row in reversed(rows):
        ts = (row["timestamp"] or "")[:19]
        urg = " [URGENT]" if row["urgency"] == "urgent" else ""
        click.echo(
            f"[{row['status']:9}] {ts}  "
            f"{row['from_agent']:12} → {row['to_agent']:12}{urg}\n"
            f"           {str(row['body'])[:120]}"
        )
