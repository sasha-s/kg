"""kg mux — agent message broker CLI commands."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
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


# ─── Agent management ─────────────────────────────────────────────────────────


@mux_cli.group("agent")
def agent_cli() -> None:
    """Manage registered agents."""


@agent_cli.command("create", no_args_is_help=True)
@click.argument("name")
@click.option("--node", default="", help="Node that runs this agent (default: KG_NODE_NAME or 'local')")
@click.option("--no-auto-start", "auto_start", is_flag=True, default=True, help="Don't start on launcher boot")
@click.option("--restart", default="always", type=click.Choice(["always", "on-failure", "never"]), show_default=True)
@click.option("--no-wake", "wake_on_message", is_flag=True, default=True, help="Don't wake on pending messages")
@click.option("--model", default="", help="Claude model override")
def agent_create(name: str, node: str, auto_start: bool, restart: str, wake_on_message: bool, model: str) -> None:
    """Create a named agent (register in mux + write .toml + create KG nodes)."""
    import os
    from kg.agents.launcher import create_agent_def
    from kg.agents.mux import _init_db
    cfg = _load_cfg()

    if not cfg.agents.enabled:
        raise click.ClickException(
            "Agents not enabled. Add `[agents]\\nenabled = true` to kg.toml"
        )

    node = node or os.environ.get("KG_NODE_NAME", "local")

    # Ensure mux DB exists and register the agent (with kg_root so messages work immediately)
    cfg._mux_user_dir.mkdir(parents=True, exist_ok=True)
    _init_db(cfg.mux_db_path)
    from kg.agents.mux import _upsert_agent
    with sqlite3.connect(str(cfg.mux_db_path)) as conn:
        _upsert_agent(conn, name, "idle", None, str(cfg.root))
    click.echo(f"✓ Registered agent '{name}' in mux (kg_root={cfg.root})")

    # Write .kg/agents/<name>.toml
    toml_path = create_agent_def(
        cfg, name, node,
        auto_start=auto_start, restart=restart,
        wake_on_message=wake_on_message, model=model,
    )
    click.echo(f"✓ Created {toml_path.relative_to(cfg.root)}")

    # Create KG nodes via `kg add` (auto-creates node if missing)
    kg_bin = shutil.which("kg")
    if not kg_bin:
        raise click.ClickException("`kg` not found on PATH. Cannot create KG nodes.")

    mission_slug = f"agent-{name}-mission"
    memory_slug = f"agent-{name}"

    # Check if nodes already exist (avoid duplicate bullets)
    conn2 = sqlite3.connect(str(cfg.db_path)) if cfg.db_path.exists() else None
    mission_exists = False
    memory_exists = False
    if conn2:
        mission_exists = bool(conn2.execute(
            "SELECT 1 FROM nodes WHERE slug = ?", (mission_slug,)
        ).fetchone()) or bool(conn2.execute(
            "SELECT 1 FROM nodes WHERE slug = ?", (f"agent-{name}-instructions",)
        ).fetchone())
        memory_exists = bool(conn2.execute(
            "SELECT 1 FROM nodes WHERE slug = ?", (memory_slug,)
        ).fetchone())
        conn2.close()

    if not mission_exists:
        subprocess.run(
            [kg_bin, "add", mission_slug,
             f"No mission set yet for agent '{name}'. "
             "Add bullets here to define this agent's mission and standing context. "
             "These are injected verbatim at every session start."],
            check=False, capture_output=True,
        )
        click.echo(f"✓ Created node '{mission_slug}'")
    else:
        click.echo(f"  Node '{mission_slug}' already exists")

    if not memory_exists:
        subprocess.run(
            [kg_bin, "add", memory_slug,
             f"Working memory for agent '{name}'. "
             "Bullets accumulated here across sessions. "
             "Accessible via kg MCP tools."],
            check=False, capture_output=True,
        )
        click.echo(f"✓ Created node '{memory_slug}'")
    else:
        click.echo(f"  Node '{memory_slug}' already exists")

    click.echo(f"\nTo launch this agent:")
    click.echo(f"  KG_AGENT_NAME={name} claude")


@agent_cli.command("list")
def agent_list() -> None:
    """List registered agents."""
    cfg = _load_cfg()
    if not cfg.mux_db_path.exists():
        click.echo("No mux database. Run `kg mux start` first.")
        return
    conn = sqlite3.connect(str(cfg.mux_db_path))
    conn.row_factory = sqlite3.Row
    agents = conn.execute("SELECT * FROM agents ORDER BY name").fetchall()
    conn.close()

    # Pending counts from project-local messages.db (if available)
    pending: dict[str, int] = {}
    if cfg.messages_db_path.exists():
        try:
            mconn = sqlite3.connect(str(cfg.messages_db_path))
            pending = dict(mconn.execute(
                "SELECT to_agent, COUNT(*) FROM messages WHERE acked=0 GROUP BY to_agent"
            ).fetchall())
            mconn.close()
        except Exception:
            pass

    if not agents:
        click.echo("No agents registered. Use `kg mux agent create <name>`.")
        return
    for a in agents:
        n = pending.get(a["name"], 0)
        pstr = f"  [{n} pending]" if n else ""
        sid = (a["session_id"] or "") if "session_id" in a.keys() else ""
        sid_str = f"  session={sid[:16]}" if sid else ""
        click.echo(
            f"  {a['name']:20} {a['status']:10} "
            f"{(a['last_seen'] or '')[:19]}{sid_str}{pstr}"
        )


@agent_cli.command("delete", no_args_is_help=True)
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def agent_delete(name: str, yes: bool) -> None:
    """Remove an agent from the mux registry."""
    cfg = _load_cfg()
    if not cfg.mux_db_path.exists():
        raise click.ClickException("No mux database found.")
    if not yes:
        click.confirm(f"Delete agent '{name}' from mux? (KG nodes are kept)", abort=True)
    conn = sqlite3.connect(str(cfg.mux_db_path))
    conn.execute("DELETE FROM agents WHERE name = ?", (name,))
    conn.commit()
    conn.close()
    click.echo(f"✓ Removed agent '{name}' from mux (KG nodes untouched)")


@mux_cli.command("start")
@click.option("--foreground", "-f", is_flag=True, help="Run in foreground (blocking)")
@click.option("--daemon", "-d", is_flag=True, help="Run in background (default)")
def mux_start(foreground: bool, daemon: bool) -> None:  # noqa: ARG001
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
    click.echo(f"Agent:  {cfg.agent_name or '(none — set KG_AGENT_NAME env var)'}")
    click.echo(f"DB:     {cfg.mux_db_path}")

    if not cfg.mux_db_path.exists():
        click.echo("\nNo mux database found. Start with `kg mux start`.")
        return

    conn = sqlite3.connect(str(cfg.mux_db_path))
    conn.row_factory = sqlite3.Row
    agents = conn.execute("SELECT * FROM agents ORDER BY name").fetchall()
    conn.close()

    if agents:
        click.echo("\nAgents:")
        for a in agents:
            kg_root = a["kg_root"] or ""
            click.echo(
                f"  {a['name']:20} {a['status']:10} "
                f"{(a['last_seen'] or '')[:19]}"
                + (f"  [{kg_root}]" if kg_root else "")
            )
    else:
        click.echo("\nNo agents registered yet.")


@mux_cli.command("send", no_args_is_help=True)
@click.argument("to_agent")
@click.argument("body")
@click.option("--from", "from_agent", default="", help="Sender name")
@click.option("--urgent", is_flag=True, help="Mark as urgent")
def mux_send(to_agent: str, body: str, from_agent: str, urgent: bool) -> None:
    """Send a message to an agent."""
    cfg = _load_cfg()
    sender = from_agent or cfg.agent_name or "cli"
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
@click.option("--agent", "-a", default="", help="Show inbox for this agent (uses current project kg_root)")
@click.option("--from", "from_agent", default="", help="Filter by sender")
@click.option("--urgency", "-u", default="", help="Filter by urgency (normal/urgent)")
@click.option("--limit", "-l", default=20, show_default=True)
def mux_messages(agent: str, from_agent: str, urgency: str, limit: int) -> None:
    """List messages from the project-local messages index."""
    cfg = _load_cfg()
    db_path = cfg.messages_db_path
    if not db_path.exists():
        click.echo("No messages database found. Messages are written when agents communicate.")
        return

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conds, params = [], []
    if agent:
        conds.append("(to_agent=? OR from_agent=?)")
        params += [agent, agent]
    if from_agent:
        conds.append("from_agent=?")
        params.append(from_agent)
    if urgency:
        conds.append("urgency=?")
        params.append(urgency)
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
            f"{ts}  {row['from_agent']:12} → {row['to_agent']:12}{urg}\n"
            f"  {str(row['body'])[:120]}"
        )


# ─── kg launcher ──────────────────────────────────────────────────────────────


def _node_name() -> str:
    import os
    return os.environ.get("KG_NODE_NAME", "local")


@click.group("launcher")
def launcher_cli() -> None:
    """Agent process supervisor — manages agent lifetimes per node."""


@launcher_cli.command("start")
@click.option("--foreground", "-f", is_flag=True)
@click.option("--daemon", "-d", is_flag=True, help="Run in background (default)")
@click.option("--poll", default=0.0, show_default=True, help="Poll interval in seconds (0 = use KG_LAUNCHER_POLL env or default 30s)")
def launcher_start(foreground: bool, daemon: bool, poll: float) -> None:  # noqa: ARG001
    """Start the launcher for this node.

    Poll interval priority: --poll > KG_LAUNCHER_POLL env var > default 30s.
    Uses inotify on Linux for immediate pickup of new .kg/agents/*.toml files.
    """
    import os
    from kg.agents.launcher import Launcher, start_background
    cfg = _load_cfg()
    node = _node_name()
    # Resolve poll interval: explicit flag > env var > default
    if poll <= 0:
        poll = float(os.environ.get("KG_LAUNCHER_POLL", "30"))
    if not node or node == "local":
        click.echo(f"Node: local  (set KG_NODE_NAME to use a different node name)")
    else:
        click.echo(f"Node: {node}")

    if foreground:
        import logging
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        Launcher(cfg, node).run(poll)
    else:
        ok, msg = start_background(cfg, node)
        click.echo(f"{'✓' if ok else '✗'} {msg}")


@launcher_cli.command("stop")
def launcher_stop() -> None:
    """Stop the launcher."""
    from kg.agents.launcher import stop_background
    ok, msg = stop_background()
    click.echo(f"{'✓' if ok else '✗'} {msg}")


@launcher_cli.command("status")
def launcher_status_cmd() -> None:
    """Show launcher status and managed agents."""
    from kg.agents.launcher import launcher_status
    click.echo(f"Launcher: {launcher_status()}")
    click.echo(f"Node:     {_node_name()}")

    cfg = _load_cfg()
    agents_dir = cfg.root / ".kg" / "agents"
    if not agents_dir.exists():
        click.echo("\nNo agents defined. Use `kg mux agent create <name>`.")
        return

    from kg.agents.launcher import AgentDef
    click.echo("\nAgent definitions:")
    for path in sorted(agents_dir.glob("*.toml")):
        try:
            defn = AgentDef.from_toml(path)
            mine = " ◀ this node" if defn.node == _node_name() else ""
            status_tag = f" [{defn.status}]" if defn.status != "running" else ""
            click.echo(
                f"  {defn.name:20} node={defn.node:15} "
                f"restart={defn.restart:10} auto_start={defn.auto_start}"
                f"{status_tag}{mine}"
            )
        except Exception as exc:
            click.echo(f"  {path.stem:20} (parse error: {exc})")


@launcher_cli.group("agent")
def launcher_agent_cli() -> None:
    """Control individual agent lifecycle."""


@launcher_agent_cli.command("pause", no_args_is_help=True)
@click.argument("name")
def launcher_agent_pause(name: str) -> None:
    """Pause an agent (launcher terminates it and will not restart)."""
    from kg.agents.launcher import update_agent_def
    cfg = _load_cfg()
    defn = update_agent_def(cfg, name, status="paused")
    click.echo(f"✓ Agent '{defn.name}' set to paused — launcher will terminate it")


@launcher_agent_cli.command("resume", no_args_is_help=True)
@click.argument("name")
def launcher_agent_resume(name: str) -> None:
    """Resume a paused or draining agent (launcher will start it)."""
    from kg.agents.launcher import update_agent_def
    cfg = _load_cfg()
    defn = update_agent_def(cfg, name, status="running")
    click.echo(f"✓ Agent '{defn.name}' set to running — launcher will start it")


@launcher_agent_cli.command("drain", no_args_is_help=True)
@click.argument("name")
def launcher_agent_drain(name: str) -> None:
    """Drain an agent (finish current session, then stop — no restart)."""
    from kg.agents.launcher import update_agent_def
    cfg = _load_cfg()
    defn = update_agent_def(cfg, name, status="draining")
    click.echo(f"✓ Agent '{defn.name}' set to draining — will stop after current session")


@launcher_agent_cli.command("migrate", no_args_is_help=True)
@click.argument("name")
@click.option("--to", "target_node", required=True, help="Target node name")
@click.option("--drain", "drain_first", is_flag=True, help="Drain before migrating (set status=draining, then update node)")
def launcher_agent_migrate(name: str, target_node: str, drain_first: bool) -> None:
    """Migrate an agent to another node.

    Without --drain: atomically updates the node in the TOML. The old node's
    launcher will kill the agent, the target node's launcher will start it.

    With --drain: sets status=draining first so the agent finishes its current
    session before the TOML is updated. You must run this command again (without
    --drain) once the agent has stopped.
    """
    from kg.agents.launcher import AgentDef, update_agent_def
    cfg = _load_cfg()
    agents_dir = cfg.root / ".kg" / "agents"
    path = agents_dir / f"{name}.toml"
    if not path.exists():
        raise click.ClickException(f"No agent TOML found: {path}")

    defn = AgentDef.from_toml(path)

    if drain_first:
        if defn.status == "draining":
            click.echo(f"  Agent '{name}' is already draining.")
            click.echo(f"  When it stops, run: kg launcher agent migrate {name} --to {target_node}")
            return
        update_agent_def(cfg, name, status="draining")
        click.echo(f"✓ Agent '{name}' set to draining on node '{defn.node}'")
        click.echo(f"  When it stops, run: kg launcher agent migrate {name} --to {target_node}")
    else:
        old_node = defn.node
        update_agent_def(cfg, name, node=target_node, status="running")
        click.echo(f"✓ Agent '{name}' migrated: {old_node} → {target_node}")


# ─── kg run — convenience launcher ───────────────────────────────────────────


@click.command("run", no_args_is_help=True)
@click.argument("name")
@click.option("--unsafe", is_flag=True, help="Pass --dangerously-skip-permissions to claude")
@click.option("--no-print", is_flag=True, help="Don't echo the launch command")
def agent_run_cmd(name: str, unsafe: bool, no_print: bool) -> None:
    """Launch a claude session as a named agent (sets KG_AGENT_NAME).

    Reads .kg/agents/<name>.toml for model and working_dir if it exists.
    Falls back to current directory if no TOML found.

    Examples:
      kg run alice
      kg run alice --unsafe
    """
    import os

    cfg = _load_cfg()
    env = os.environ.copy()
    env["KG_AGENT_NAME"] = name
    env.pop("CLAUDECODE", None)  # allow launch from inside another claude session
    cwd = str(cfg.root)

    # Load TOML for model/working_dir if available
    toml_path = cfg.root / ".kg" / "agents" / f"{name}.toml"
    if toml_path.exists():
        try:
            from kg.agents.launcher import AgentDef
            defn = AgentDef.from_toml(toml_path)
            if defn.model:
                env["ANTHROPIC_MODEL"] = defn.model
            if defn.working_dir:
                cwd = defn.working_dir
        except Exception:
            pass

    cmd = ["claude"]
    if unsafe:
        cmd.append("--dangerously-skip-permissions")

    if not no_print:
        extra = " --dangerously-skip-permissions" if unsafe else ""
        click.echo(f"→ KG_AGENT_NAME={name} claude{extra}  (cwd: {cwd})")

    try:
        subprocess.run(cmd, env=env, cwd=cwd, check=False)
    except FileNotFoundError as exc:
        raise click.ClickException("`claude` not found on PATH") from exc
