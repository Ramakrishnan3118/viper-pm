"""Per-app supervisor: owns N workers, keeps them alive, enforces limits.

Safety guarantee: we only ever signal processes we spawned. Every managed PID
is stored together with its process create_time, and both are re-verified via
psutil before any signal is sent — a recycled PID is never touched. Workers
run in their own process group (start_new_session), so signals reach the
worker's own tree and nothing else.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import signal as sig_mod
import subprocess
import time
from pathlib import Path

import psutil

from . import paths
from .spec import AppSpec

TICK = 1.0            # monitor loop interval (seconds)
MEM_EVERY = 3         # memory check every N ticks
BACKOFF_BASE = 0.5    # first restart delay (seconds)
BACKOFF_MAX = 30.0    # cap on restart delay


class Worker:
    def __init__(self, slot: int):
        self.slot = slot
        self.pid: int | None = None
        self.create_time: float | None = None
        self.started_at: float | None = None
        self.status = "stopped"   # stopped|online|stopping|backoff|errored
        self.popen: subprocess.Popen | None = None
        self.ps: psutil.Process | None = None
        self.cpu = 0.0
        self.rss = 0
        self.consecutive = 0      # consecutive fast crashes
        self.backoff_until = 0.0
        self.last_exit_code: int | None = None


class Supervisor:
    def __init__(self, spec: AppSpec, desired: str, event_cb, save_cb):
        self.spec = spec
        self.desired = desired            # "running" | "stopped"
        self.workers = [Worker(i) for i in range(spec.workers)]
        self.restarts = 0
        self.lock = asyncio.Lock()
        self._ticks = 0
        self._event_cb = event_cb
        self._save = save_cb

    def event(self, msg: str) -> None:
        self._event_cb(self.spec.name, msg)

    # ---------- journal ----------

    def to_state(self) -> dict:
        return {
            "spec": self.spec.to_dict(),
            "desired": self.desired,
            "restarts": self.restarts,
            "workers": [
                {
                    "slot": w.slot,
                    "pid": w.pid,
                    "create_time": w.create_time,
                    "started_at": w.started_at,
                }
                for w in self.workers
                if w.pid
            ],
        }

    def reattach(self, worker_records: list[dict]) -> None:
        """After a daemon restart, adopt workers that are still running."""
        for rec in worker_records:
            slot = rec.get("slot", 0)
            if slot >= len(self.workers):
                continue
            w = self.workers[slot]
            pid, ct = rec.get("pid"), rec.get("create_time")
            if pid and ct:
                try:
                    ps = psutil.Process(pid)
                    if abs(ps.create_time() - ct) < 0.5 and ps.status() != psutil.STATUS_ZOMBIE:
                        w.pid, w.create_time, w.ps = pid, ct, ps
                        w.started_at = rec.get("started_at") or time.time()
                        w.status = "online"
                        self.event(f"re-attached to worker {slot} (pid {pid})")
                        continue
                except psutil.Error:
                    pass
            self.event(f"worker {slot} (pid {pid}) is gone; will restart if desired")

    # ---------- process safety ----------

    def _alive(self, w: Worker) -> bool:
        """True only if the exact process we spawned is still running."""
        if not w.pid:
            return False
        if w.popen is not None and w.popen.poll() is not None:
            return False
        try:
            ps = w.ps or psutil.Process(w.pid)
            if w.create_time is not None and abs(ps.create_time() - w.create_time) > 0.5:
                return False  # PID recycled by another process — never touch it
            if ps.status() == psutil.STATUS_ZOMBIE:
                return False
            w.ps = ps
            return True
        except psutil.Error:
            return False

    def _signal(self, w: Worker, signum: int) -> None:
        if not self._alive(w):
            return
        try:
            os.killpg(w.pid, signum)   # own session => pgid == pid
        except ProcessLookupError:
            pass
        except PermissionError:
            try:
                os.kill(w.pid, signum)
            except OSError:
                pass

    # ---------- spawn / stop ----------

    def _resolve_venv(self) -> str | None:
        v = self.spec.venv
        if not v:
            return None
        if v == "auto":
            base = self.spec.cwd or ""
            for cand in (".venv", "venv"):
                path = os.path.join(base, cand)
                if os.path.isfile(os.path.join(path, "bin", "python")):
                    return os.path.abspath(path)
            return None
        return v if os.path.isdir(v) else None

    def _build_env(self, slot: int) -> dict:
        env = dict(os.environ)
        venv = self._resolve_venv()
        if venv:
            env["VIRTUAL_ENV"] = venv
            env["PATH"] = os.path.join(venv, "bin") + os.pathsep + env.get("PATH", "")
            env.pop("PYTHONHOME", None)
        env["PYTHONUNBUFFERED"] = "1"
        env.update(self.spec.env)
        env["VIPER_APP_NAME"] = self.spec.name
        env["VIPER_WORKER_ID"] = str(slot)
        return env

    def _spawn(self, w: Worker) -> None:
        spec = self.spec
        argv = shlex.split(spec.cmd)
        cwd = spec.cwd or str(Path.home())
        paths.ensure_dirs()
        out = open(paths.out_log(spec.name, w.slot), "ab")
        err = open(paths.err_log(spec.name, w.slot), "ab")
        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=self._build_env(w.slot),
                stdout=out,           # workers write straight to files, so they
                stderr=err,           # survive a daemon crash/restart untouched
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            self._register_failure(w, f"failed to spawn: {exc}")
            return
        finally:
            out.close()
            err.close()
        w.popen = proc
        w.pid = proc.pid
        w.started_at = time.time()
        w.status = "online"
        w.last_exit_code = None
        try:
            w.ps = psutil.Process(proc.pid)
            w.create_time = w.ps.create_time()
        except psutil.Error:
            w.ps = None
            w.create_time = None
        self.event(f"worker {w.slot} started (pid {proc.pid})")
        self._save()

    def _register_failure(self, w: Worker, reason: str) -> None:
        self.restarts += 1
        w.consecutive += 1
        if not self.spec.autorestart:
            w.status = "stopped"
            self.event(f"worker {w.slot} {reason}; autorestart disabled")
        elif w.consecutive > self.spec.max_restarts:
            w.status = "errored"
            self.event(
                f"worker {w.slot} {reason}; failed {w.consecutive} times in a row — "
                f"giving up (use 'viper restart {self.spec.name}' to retry)"
            )
        else:
            delay = min(BACKOFF_BASE * 2 ** (w.consecutive - 1), BACKOFF_MAX)
            w.status = "backoff"
            w.backoff_until = time.time() + delay
            self.event(f"worker {w.slot} {reason}; restarting in {delay:.1f}s")
        self._save()

    async def _stop_worker(self, w: Worker) -> None:
        if not self._alive(w):
            self._clear(w)
            return
        w.status = "stopping"
        pid = w.pid
        self._signal(w, getattr(sig_mod, self.spec.stop_signal))
        deadline = time.time() + self.spec.stop_grace
        while time.time() < deadline:
            if not self._alive(w):
                break
            await asyncio.sleep(0.1)
        if self._alive(w):
            self.event(f"worker {w.slot} (pid {pid}) ignored {self.spec.stop_signal} "
                       f"for {self.spec.stop_grace:.0f}s; sending SIGKILL")
            self._signal(w, sig_mod.SIGKILL)
            for _ in range(50):
                if not self._alive(w):
                    break
                await asyncio.sleep(0.1)
        self._clear(w)
        self.event(f"worker {w.slot} (pid {pid}) stopped")

    def _clear(self, w: Worker) -> None:
        if w.popen is not None:
            try:
                w.last_exit_code = w.popen.poll()
            except Exception:
                pass
        w.pid = None
        w.create_time = None
        w.ps = None
        w.popen = None
        w.status = "stopped"
        w.cpu = 0.0
        w.rss = 0

    # ---------- operations ----------

    async def start(self) -> None:
        async with self.lock:
            self.desired = "running"
            if len(self.workers) != self.spec.workers:
                self.workers = [Worker(i) for i in range(self.spec.workers)]
            for w in self.workers:
                w.consecutive = 0
                if w.status == "errored":
                    w.status = "stopped"
                if not self._alive(w):
                    self._spawn(w)
            self._save()

    async def stop(self) -> None:
        async with self.lock:
            self.desired = "stopped"
            await asyncio.gather(*(self._stop_worker(w) for w in self.workers))
            self._save()

    async def restart(self, new_spec: AppSpec | None = None) -> None:
        async with self.lock:
            await asyncio.gather(*(self._stop_worker(w) for w in self.workers))
            if new_spec is not None:
                self.spec = new_spec
            self.desired = "running"
            self.workers = [Worker(i) for i in range(self.spec.workers)]
            for w in self.workers:
                self._spawn(w)
            self._save()

    async def reload(self) -> None:
        """Rolling restart: one worker at a time, so N-1 workers keep serving."""
        async with self.lock:
            self.desired = "running"
            for w in self.workers:
                await self._stop_worker(w)
                w.consecutive = 0
                self._spawn(w)
                deadline = time.time() + 5.0
                while time.time() < deadline:
                    await asyncio.sleep(0.2)
                    if self._alive(w):
                        break
                if not self._alive(w):
                    self.event(f"worker {w.slot} did not come back during reload; "
                               f"the monitor will keep retrying")
            self._save()

    # ---------- monitor loop ----------

    async def monitor(self) -> None:
        while True:
            try:
                await asyncio.sleep(TICK)
                async with self.lock:
                    await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never let the monitor die
                self.event(f"monitor error: {exc!r}")

    async def _tick(self) -> None:
        self._ticks += 1
        if self.desired != "running":
            return
        for w in self.workers:
            if w.status in ("stopping", "errored"):
                continue
            if w.status == "backoff":
                if time.time() >= w.backoff_until:
                    self._spawn(w)
                continue
            if not self._alive(w):
                if w.status == "online":
                    self._handle_exit(w)
                elif w.status == "stopped":
                    self._spawn(w)   # e.g. died while the daemon was away
                continue
            # worker is healthy
            if w.consecutive and w.started_at and time.time() - w.started_at >= self.spec.min_uptime:
                w.consecutive = 0
            if w.ps is not None:
                try:
                    w.cpu = w.ps.cpu_percent(None)
                except psutil.Error:
                    pass
            if self._ticks % MEM_EVERY == 0:
                w.rss = self._tree_rss(w)
                if self.spec.max_memory and w.rss > self.spec.max_memory:
                    self.event(
                        f"worker {w.slot} (pid {w.pid}) exceeded memory limit "
                        f"({w.rss // 1024 // 1024}M > {self.spec.max_memory // 1024 // 1024}M); restarting"
                    )
                    self.restarts += 1
                    await self._stop_worker(w)
                    self._spawn(w)

    def _handle_exit(self, w: Worker) -> None:
        uptime = time.time() - (w.started_at or time.time())
        pid = w.pid
        if uptime >= self.spec.min_uptime:
            w.consecutive = 0   # a stable run resets the crash streak
        self._clear(w)
        code = w.last_exit_code
        self._register_failure(
            w, f"(pid {pid}) exited (code {code}, uptime {uptime:.1f}s)"
        )

    def _tree_rss(self, w: Worker) -> int:
        if w.ps is None:
            return 0
        try:
            total = w.ps.memory_info().rss
            for child in w.ps.children(recursive=True):
                try:
                    total += child.memory_info().rss
                except psutil.Error:
                    pass
            return total
        except psutil.Error:
            return 0

    # ---------- reporting ----------

    def app_state(self) -> str:
        if self.desired == "stopped":
            return "stopped"
        statuses = [w.status for w in self.workers]
        if any(s == "online" for s in statuses):
            return "online" if all(s == "online" for s in statuses) else "degraded"
        if statuses and all(s == "errored" for s in statuses):
            return "errored"
        if any(s in ("backoff", "stopping") for s in statuses):
            return "restarting"
        return "starting"

    def snapshot(self) -> dict:
        now = time.time()
        return {
            "name": self.spec.name,
            "state": self.app_state(),
            "desired": self.desired,
            "restarts": self.restarts,
            "cmd": self.spec.cmd,
            "max_memory": self.spec.max_memory,
            "workers": [
                {
                    "slot": w.slot,
                    "pid": w.pid,
                    "status": w.status,
                    "cpu": round(w.cpu, 1),
                    "rss": w.rss,
                    "uptime": (now - w.started_at) if (w.started_at and w.status == "online") else 0,
                }
                for w in self.workers
            ],
        }
