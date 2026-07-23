"""The `viper` command-line interface."""
from __future__ import annotations

import os
import shlex
import sys

import click
from rich.console import Console
from rich.table import Table

from . import __version__, ipc, paths
from .spec import AppSpec, format_memory, load_config
from . import tailer

console = Console()

STATE_COLORS = {
    "online": "green",
    "degraded": "yellow",
    "restarting": "yellow",
    "starting": "cyan",
    "stopped": "dim",
    "errored": "red",
    "backoff": "yellow",
    "stopping": "yellow",
}


def _fail(msg: str) -> None:
    console.print(f"[red]error:[/red] {msg}")
    sys.exit(1)


def _call(method: str, params: dict | None = None, timeout: float = 60.0):
    try:
        ipc.ensure_daemon()
        return ipc.call(method, params, timeout=timeout)
    except ipc.DaemonUnavailable as exc:
        _fail(f"cannot reach daemon: {exc}")
    except ipc.DaemonError as exc:
        _fail(str(exc))


def _fmt_uptime(seconds: float) -> str:
    seconds = int(seconds)
    if seconds <= 0:
        return "-"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{mins}m"
    if mins:
        return f"{mins}m{secs}s"
    return f"{secs}s"


def _colored_state(state: str) -> str:
    return f"[{STATE_COLORS.get(state, 'white')}]{state}[/]"


def _print_apps(apps: list[dict]) -> None:
    if not apps:
        console.print("[dim]no apps managed — start one with: viper start <cmd> --name <name>[/dim]")
        return
    table = Table(box=None, header_style="bold")
    for col in ("app", "state", "w#", "pid", "worker", "cpu%", "mem", "uptime", "↺"):
        table.add_column(col, justify="right" if col in ("cpu%", "mem", "uptime", "↺", "pid", "w#") else "left")
    for app in apps:
        first = True
        for w in app["workers"]:
            table.add_row(
                app["name"] if first else "",
                _colored_state(app["state"]) if first else "",
                str(w["slot"]),
                str(w["pid"] or "-"),
                _colored_state(w["status"]),
                f"{w['cpu']:.1f}" if w["pid"] else "-",
                format_memory(w["rss"]) if w["pid"] else "-",
                _fmt_uptime(w["uptime"]),
                str(app["restarts"]) if first else "",
            )
            first = False
    console.print(table)


@click.group()
@click.version_option(__version__, prog_name="viper")
def main():
    """viper — production-grade process manager for Python services.

    Only ever touches processes it started itself.
    """


@main.command()
@click.argument("target")
@click.option("--name", "-n", help="App name (defaults to the command's basename).")
@click.option("--workers", "-i", type=int, default=None, help="Number of worker instances.")
@click.option("--cwd", help="Working directory (default: current directory).")
@click.option("--max-memory", help="Restart a worker when its tree exceeds this (e.g. 500M, 2G).")
@click.option("--env", "-e", multiple=True, metavar="KEY=VALUE", help="Extra environment variables.")
@click.option("--venv", help="Path to a virtualenv, or 'auto' (default) to detect .venv in cwd.")
@click.option("--no-autorestart", is_flag=True, help="Do not restart the app when it exits.")
@click.option("--max-restarts", type=int, default=None, help="Consecutive crashes before giving up.")
@click.option("--stop-grace", type=float, default=None, help="Seconds between stop signal and SIGKILL.")
def start(target, name, workers, cwd, max_memory, env, venv, no_autorestart,
          max_restarts, stop_grace):
    """Start an app: a command string, a .py file, or a viper.yml config file.

    \b
    Examples:
      viper start "uvicorn app:app --port 8000" --name api -i 2 --max-memory 1G
      viper start worker.py --name worker
      viper start viper.yml
    """
    if target.endswith((".yml", ".yaml")):
        if not os.path.isfile(target):
            _fail(f"config file not found: {target}")
        _apply_file(target, prune=False)
        return

    if os.path.isfile(target) and target.endswith(".py"):
        cmd = f"python3 {shlex.quote(os.path.abspath(target))}"
        default_name = os.path.splitext(os.path.basename(target))[0]
    else:
        cmd = target
        try:
            first_word = shlex.split(cmd)[0]
        except (ValueError, IndexError):
            _fail(f"cannot parse command: {target!r}")
        default_name = os.path.splitext(os.path.basename(first_word))[0]

    spec: dict = {
        "name": name or default_name,
        "cmd": cmd,
        "cwd": os.path.abspath(cwd) if cwd else os.getcwd(),
    }
    if workers is not None:
        spec["workers"] = workers
    if max_memory:
        spec["max_memory"] = max_memory
    if venv is not None:
        spec["venv"] = venv
    if env:
        pairs = {}
        for item in env:
            key, sep, value = item.partition("=")
            if not sep:
                _fail(f"--env expects KEY=VALUE, got {item!r}")
            pairs[key] = value
        spec["env"] = pairs
    if no_autorestart:
        spec["autorestart"] = False
    if max_restarts is not None:
        spec["max_restarts"] = max_restarts
    if stop_grace is not None:
        spec["stop_grace"] = stop_grace

    try:
        AppSpec.from_dict(spec)  # validate locally for a fast, friendly error
    except ValueError as exc:
        _fail(str(exc))
    result = _call("start", {"spec": spec})
    _print_apps([result])


def _lifecycle(method: str, name: str) -> None:
    results = _call(method, {"name": name}, timeout=120.0)
    if method == "delete":
        for r in results:
            console.print(f"deleted [bold]{r['name']}[/bold]")
        return
    _print_apps(results)


@main.command()
@click.argument("name")
def stop(name):
    """Stop an app (or 'all'). Keeps it in the list; start again anytime."""
    _lifecycle("stop", name)


@main.command()
@click.argument("name")
def restart(name):
    """Hard restart: stop all workers, then start them again."""
    _lifecycle("restart", name)


@main.command()
@click.argument("name")
def reload(name):
    """Rolling restart: workers restart one at a time (N-1 keep serving)."""
    _lifecycle("reload", name)


@main.command()
@click.argument("name")
def delete(name):
    """Stop an app (or 'all') and remove it from management."""
    _lifecycle("delete", name)


@main.command(name="ls")
def ls():
    """List managed apps and their workers."""
    _print_apps(_call("list"))


@main.command(name="status")
def status():
    """Alias for ls."""
    _print_apps(_call("list"))


@main.command()
@click.argument("config", default="viper.yml")
@click.option("--prune", is_flag=True, help="Also delete managed apps missing from the file.")
def apply(config, prune):
    """Converge running state to a viper.yml file (the team's deploy verb)."""
    if not os.path.isfile(config):
        _fail(f"config file not found: {config}")
    _apply_file(config, prune)


def _apply_file(path: str, prune: bool) -> None:
    try:
        specs = load_config(path)
    except (ValueError, OSError) as exc:
        _fail(str(exc))
    result = _call(
        "apply",
        {"specs": [s.to_dict() for s in specs], "prune": prune},
        timeout=300.0,
    )
    for verb in ("started", "updated", "removed", "unchanged"):
        if result[verb]:
            console.print(f"{verb}: [bold]{', '.join(result[verb])}[/bold]")
    _print_apps(_call("list"))


@main.command()
@click.argument("name")
@click.option("--lines", "-n", default=30, help="Lines of history per file.")
@click.option("--follow", "-f", is_flag=True, help="Keep printing new lines.")
@click.option("--err", "only_err", is_flag=True, help="Only stderr.")
@click.option("--out", "only_out", is_flag=True, help="Only stdout.")
def logs(name, lines, follow, only_err, only_out):
    """Show (and follow) an app's logs."""
    streams = ["out", "err"]
    if only_err:
        streams = ["err"]
    elif only_out:
        streams = ["out"]
    files = []
    for path in sorted(paths.logs_dir().glob(f"{name}-*-*.log")):
        stem = path.name[len(name) + 1 : -4]          # "<slot>-<stream>"
        slot, _, stream = stem.partition("-")
        if stream in streams and slot.isdigit():
            files.append((f"{slot}|{stream}", path))
    if not files:
        _fail(f"no log files for app {name!r} in {paths.logs_dir()}")
    try:
        tailer.tail(files, lines, follow)
    except KeyboardInterrupt:
        pass


@main.command()
@click.argument("name", required=False)
@click.option("--lines", "-n", default=40)
@click.option("--follow", "-f", is_flag=True)
def events(name, lines, follow):
    """Show the audit trail: starts, exits, restarts, memory kills, and why."""
    path = paths.events_path()
    if not path.exists():
        _fail("no events recorded yet")
    line_filter = (lambda text: f"[{name}]" in text) if name else None
    try:
        tailer.tail([("events", path)], lines, follow, line_filter=line_filter)
    except KeyboardInterrupt:
        pass


@main.command()
def save():
    """Snapshot the current app list for `viper resurrect`."""
    result = _call("save")
    console.print(f"saved to {result['saved']}")


@main.command()
def resurrect():
    """Start every app that was running at the last `viper save`."""
    result = _call("resurrect", timeout=300.0)
    started = result["started"]
    console.print(f"resurrected: [bold]{', '.join(started) if started else 'nothing to do'}[/bold]")
    _print_apps(_call("list"))


@main.command()
def kill():
    """Stop ALL managed apps and shut the daemon down."""
    if not ipc.daemon_alive():
        console.print("[dim]daemon is not running[/dim]")
        return
    _call("kill", timeout=300.0)
    console.print("all apps stopped, daemon shut down")


@main.command()
def ping():
    """Show daemon status."""
    info = _call("ping")
    console.print(
        f"daemon [green]online[/green] — pid {info['pid']}, v{info['version']}, "
        f"{info['apps']} app(s), up {_fmt_uptime(info['uptime'])}, home {info['home']}"
    )
