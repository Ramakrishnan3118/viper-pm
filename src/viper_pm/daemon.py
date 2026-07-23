"""The viper daemon: supervises all apps, serves RPC on a Unix socket.

Run as `python -m viper_pm.daemon` (the CLI auto-spawns it on first use).
If the daemon dies, managed apps keep running — on restart it re-attaches
to them from the state journal.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket as socket_mod
import sys
import time

from . import __version__, paths
from . import state as state_mod
from .spec import AppSpec
from .supervisor import Supervisor


class Daemon:
    def __init__(self):
        self.sups: dict[str, Supervisor] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.started = time.time()
        self.stop_event = asyncio.Event()

    # ---------- events / state ----------

    def event(self, app: str, msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{app}] {msg}"
        print(line, flush=True)
        try:
            with open(paths.events_path(), "a") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def save(self) -> None:
        state_mod.save_state(
            {"version": 1, "apps": [s.to_state() for s in self.sups.values()]}
        )

    def load(self) -> None:
        data = state_mod.load_state()
        if not data:
            return
        for app in data.get("apps", []):
            try:
                spec = AppSpec.from_dict(app["spec"])
            except Exception as exc:
                self.event("daemon", f"skipping unreadable app in state file: {exc}")
                continue
            sup = self._new_sup(spec, app.get("desired", "stopped"))
            sup.restarts = app.get("restarts", 0)
            sup.reattach(app.get("workers", []))
        if self.sups:
            self.event("daemon", f"restored {len(self.sups)} app(s) from journal")

    def _new_sup(self, spec: AppSpec, desired: str = "stopped") -> Supervisor:
        sup = Supervisor(spec, desired, event_cb=self.event, save_cb=self.save)
        self.sups[spec.name] = sup
        self.tasks[spec.name] = asyncio.get_event_loop().create_task(sup.monitor())
        return sup

    def _remove_sup(self, name: str) -> None:
        self.tasks.pop(name).cancel()
        self.sups.pop(name)

    def _targets(self, name: str) -> list[Supervisor]:
        if name == "all":
            return list(self.sups.values())
        if name not in self.sups:
            known = ", ".join(sorted(self.sups)) or "none"
            raise ValueError(f"no such app: {name!r} (managed apps: {known})")
        return [self.sups[name]]

    # ---------- RPC ----------

    async def rpc(self, method: str, params: dict):
        if method == "ping":
            return {
                "pid": os.getpid(),
                "version": __version__,
                "uptime": time.time() - self.started,
                "apps": len(self.sups),
                "home": str(paths.base_dir()),
            }

        if method == "list":
            return [s.snapshot() for s in self.sups.values()]

        if method == "start":
            spec = AppSpec.from_dict(params["spec"])
            if spec.name in self.sups:
                sup = self.sups[spec.name]
                if sup.spec.to_dict() != spec.to_dict():
                    raise ValueError(
                        f"app {spec.name!r} already exists with a different config; "
                        f"use 'viper apply' to update it or delete it first"
                    )
                await sup.start()
            else:
                sup = self._new_sup(spec, "running")
                await sup.start()
            self.save()
            return sup.snapshot()

        if method in ("stop", "restart", "reload", "delete"):
            results = []
            for sup in self._targets(params["name"]):
                if method == "stop":
                    await sup.stop()
                elif method == "restart":
                    await sup.restart()
                elif method == "reload":
                    await sup.reload()
                else:
                    await sup.stop()
                    self._remove_sup(sup.spec.name)
                    self.event(sup.spec.name, "deleted")
                results.append(
                    {"name": sup.spec.name, "deleted": True}
                    if method == "delete"
                    else sup.snapshot()
                )
            self.save()
            return results

        if method == "apply":
            specs = [AppSpec.from_dict(d) for d in params["specs"]]
            prune = bool(params.get("prune"))
            result = {"started": [], "updated": [], "unchanged": [], "removed": []}
            seen = set()
            for spec in specs:
                seen.add(spec.name)
                sup = self.sups.get(spec.name)
                if sup is None:
                    sup = self._new_sup(spec, "running")
                    await sup.start()
                    result["started"].append(spec.name)
                elif sup.spec.to_dict() != spec.to_dict():
                    await sup.restart(new_spec=spec)
                    result["updated"].append(spec.name)
                elif sup.desired != "running":
                    await sup.start()
                    result["started"].append(spec.name)
                else:
                    result["unchanged"].append(spec.name)
            if prune:
                for name in [n for n in self.sups if n not in seen]:
                    sup = self.sups[name]
                    await sup.stop()
                    self._remove_sup(name)
                    result["removed"].append(name)
            self.save()
            return result

        if method == "save":
            self.save()
            shutil.copyfile(paths.state_path(), paths.dump_path())
            return {"saved": str(paths.dump_path())}

        if method == "resurrect":
            dump = paths.dump_path()
            if not dump.exists():
                raise ValueError("no dump found; run 'viper save' first")
            data = json.loads(dump.read_text())
            started = []
            for app in data.get("apps", []):
                if app.get("desired") != "running":
                    continue
                spec = AppSpec.from_dict(app["spec"])
                sup = self.sups.get(spec.name)
                if sup is None:
                    sup = self._new_sup(spec, "stopped")
                else:
                    sup.spec = spec
                await sup.start()
                started.append(spec.name)
            self.save()
            return {"started": started}

        if method == "kill":
            self.event("daemon", "kill requested: stopping all apps and shutting down")
            for sup in list(self.sups.values()):
                await sup.stop()
            self.save()
            asyncio.get_event_loop().call_later(0.2, self.stop_event.set)
            return {"stopped": True}

        raise ValueError(f"unknown method: {method!r}")

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    req = json.loads(line)
                    result = await self.rpc(req.get("method"), req.get("params") or {})
                    resp = {"ok": True, "result": result}
                except Exception as exc:
                    resp = {"ok": False, "error": str(exc)}
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass


async def main_async() -> int:
    paths.ensure_dirs()
    sock_path = paths.socket_path()

    # single-instance guard: if the socket answers, another daemon owns it
    if sock_path.exists():
        probe = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        probe.settimeout(1.0)
        try:
            probe.connect(str(sock_path))
            probe.close()
            print("another viper daemon is already running; exiting", flush=True)
            return 1
        except OSError:
            sock_path.unlink()  # stale socket from a dead daemon

    daemon = Daemon()
    daemon.load()
    server = await asyncio.start_unix_server(
        daemon.handle_client, path=str(sock_path), limit=1 << 24
    )
    paths.daemon_pid_path().write_text(str(os.getpid()))

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, daemon.stop_event.set)

    daemon.event("daemon", f"started (pid {os.getpid()}, viper v{__version__}, "
                           f"home {paths.base_dir()})")
    await daemon.stop_event.wait()

    daemon.save()
    daemon.event("daemon", "shutting down; managed apps keep running")
    server.close()
    await server.wait_closed()
    try:
        sock_path.unlink()
        paths.daemon_pid_path().unlink()
    except OSError:
        pass
    return 0


def main() -> None:
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
