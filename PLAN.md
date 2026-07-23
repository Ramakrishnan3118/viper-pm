# viper — Production Process Manager for Python AI Services (Linux)

## 1. The Problem We're Solving

A team of Python AI developers running services on Linux servers, struggling to manage them. The typical pain points this plan targets:

- Services started in tmux/nohup/ad-hoc scripts — nobody knows what's running, where, or with which venv/env vars.
- AI apps take **minutes to load models** — a naive restart tool marks them "online" while they're still loading, or kills them mid-load.
- **Memory leaks** in long-running inference processes slowly eat the box; **GPU memory** leaks/OOMs require manual `nvidia-smi` policing.
- Crashes at night go unnoticed until users complain — no auto-restart, no alerting.
- Deploys mean downtime: stop, pull, start, pray.
- Logs are scattered; debugging means ssh + grep across files.
- Multiple teammates on the same server step on each other's processes.

**Goal:** one terminal-first tool, server-grade, that makes all of the above go away — and it must *never* touch any process it did not spawn.

Not a pm2 clone: pm2 is the familiar baseline (start/stop/restart/reload/monit), but we enhance where pm2 is weak for Python/AI production: real health checks, GPU awareness, alerting, declarative config, systemd-grade reliability.

## 2. Scope Decisions

- **Platform:** Linux/Unix servers only (Phase 1–4). Terminal-first: CLI + TUI. Windows explicitly deferred; no design decision may block it, but it drives nothing.
- **Target apps:** Python services (FastAPI/uvicorn model servers, Celery/queue workers, batch jobs, training-adjacent long jobs) — but any command runs.
- **Server-grade means:** survives reboot (systemd), survives daemon crash (state journal + re-attach), predictable state machine, audit trail of every action, no root required for per-user mode, optional system-wide mode for shared servers.
- **Distribution:** open source (MIT), PyPI/pipx, `.deb` for apt, snap if store review permits (needs classic confinement).

## 3. Core Design

```
 ┌────────────┐   JSON-RPC over Unix socket   ┌───────────────────────────────┐
 │ viper CLI  │ ────────────────────────────▶ │        viper daemon           │
 │ + monit TUI │ ◀──────────────────────────── │ (systemd service or per-user)  │
 └────────────┘        events / replies       │                               │
                                              │  per app: Supervisor           │
                                              │   ├─ workers (own proc group)  │
                                              │   ├─ health prober (HTTP/TCP/  │
                                              │   │   cmd readiness+liveness)  │
                                              │   ├─ RAM + GPU-mem watchdog    │
                                              │   ├─ log pump + rotation       │
                                              │   └─ restart policy + backoff  │
                                              │  alert dispatcher (webhooks)   │
                                              │  metrics exporter (Prometheus) │
                                              │  event journal (audit/history) │
                                              └───────────────────────────────┘
```

- **Stack:** Python 3.10+, `psutil` (proc metrics), `pynvml` (GPU, optional extra), `typer` (CLI), `textual` (TUI), `asyncio` daemon. IPC = JSON-RPC over Unix domain socket behind a transport abstraction.
- **Process safety (hard guarantee):** workers spawn with `start_new_session=True` (own process group). Every managed PID is stored as `(pid, proc_create_time)` and re-verified via psutil before *any* signal — a recycled PID is never touched. Signals go to our process group only. The daemon has no code path that enumerates or signals foreign processes.
- **Daemon resilience:** state journaled to disk on every transition; if the daemon is killed, workers keep running and a restarted daemon **re-attaches** to them (verified by pid+create-time) instead of orphaning or double-starting. This is a deliberate improvement over pm2.

## 4. Feature Set — Baseline vs. Enhancements

### Baseline (pm2 parity)
`start/stop/restart/delete/ls`, multi-worker cluster (`workers: N`), auto-restart with exponential backoff + `max_restarts` circuit breaker, `max_memory` restart, log capture + `logs -f`, `monit` TUI, `save/resurrect`, `startup` (systemd), ecosystem config file, env injection (`VIPER_WORKER_ID` for port fan-out).

### Enhancements (where we beat pm2 for AI teams)

1. **Real health checks** — per app: `readiness` (HTTP GET /health, TCP connect, or shell cmd) and `liveness` probes with `startup_grace` (e.g. 300 s for model loading). States are honest: `launching → loading → online → unhealthy → restarting → errored`. Reload and restarts gate on *readiness*, not "process exists."
2. **Zero-downtime rolling reload that actually works for model servers** — start new worker → wait until it passes readiness (model fully loaded) → drain old (SIGTERM after `drain_grace`) → next worker. Same-port via `SO_REUSEPORT`, or per-worker ports behind the team's nginx/traefik.
3. **GPU awareness** (optional `[gpu]` extra, NVML):
   - show per-worker GPU memory/utilization in `ls` and `monit`
   - `gpus: [0,1]` in config → auto-set `CUDA_VISIBLE_DEVICES` per worker (spread or pin)
   - `max_gpu_memory` watchdog → graceful restart of leaking worker
   - detect CUDA OOM exit patterns and report them as the restart reason
4. **Alerting** — webhook on `crashed`, `restart-loop`, `unhealthy`, `memory-restart`, `back online`: generic webhook + Slack/Discord payloads, per-app or global, with rate limiting. (Struggling teams find out *from the tool*, not from users.)
5. **Observability** — Prometheus `/metrics` endpoint on the daemon (per-app CPU, RSS, GPU mem, restarts, state); `viper events [app]` shows the journaled history (who restarted what when, why each restart happened — OOM vs crash vs manual); structured JSON log option.
6. **Declarative team workflow** — `viper.yml` lives in each project repo; `viper apply viper.yml` converges running state to the file (adds/updates/removes) so deploys are: `git pull && viper apply` — reproducible across the team, no snowflake commands. Env profiles (`--profile prod`).
7. **Python-native ergonomics** — auto-detect `.venv/bin/python` (or explicit `interpreter:`), uv/poetry-friendly, `PYTHONUNBUFFERED=1` by default so logs stream live, graceful-stop defaults tuned for uvicorn/gunicorn/celery.
8. **Shared-server hygiene** — per-user daemons by default (teammates can't touch each other's apps); optional system mode (systemd system service + socket group permissions) when the team wants one shared view.

## 5. CLI Surface

```
viper start "uvicorn app:app --port 8000" --name llm-api --workers 2 --max-memory 6G \
             --ready http://127.0.0.1:8000/health --startup-grace 300 --gpus 0,1
viper apply viper.yml [--profile prod]     # converge to config (the team's main verb)
viper ls                                    # name, state, workers, cpu, ram, gpu-mem, uptime, ↺
viper stop|restart|reload <name|all>        # reload = readiness-gated rolling restart
viper logs <name> [-f] [--err] [--lines N]
viper monit                                 # TUI: apps, workers, cpu/ram/gpu graphs, live logs
viper events [name]                         # audit/history: every state change + reason
viper save | resurrect | startup | kill
viper doctor                                # env sanity: socket, systemd, nvml, permissions
```

`viper.yml`:

```yaml
apps:
  - name: llm-api
    cmd: uvicorn app:app --host 0.0.0.0 --port 8000
    cwd: /srv/llm-api
    interpreter: auto            # finds .venv
    workers: 2
    gpus: spread                 # or [0,1], or omit
    max_memory: 6G
    max_gpu_memory: 20G
    ready: { http: "http://127.0.0.1:8000/health", startup_grace: 300 }
    stop: { signal: SIGTERM, grace: 30 }
    restart: { backoff: exponential, max: 10 }
    env: { MODEL_PATH: /models/llama }
    alerts: { webhook: "https://hooks.slack.com/...", on: [crash, restart-loop, unhealthy] }

  - name: embed-worker
    cmd: celery -A tasks worker -c 4
    cwd: /srv/pipeline
```

## 6. Phased Delivery

- **P1 — Reliable core:** daemon + UDS RPC, start/stop/restart/ls/delete, log capture + `logs -f`, auto-restart/backoff/circuit-breaker, state journal + daemon re-attach, `viper.yml` + `apply`. *Exit: manage a real uvicorn service; kill -9 the daemon; nothing breaks.*
- **P2 — Production semantics:** multi-worker, readiness/liveness probes + `startup_grace`, readiness-gated rolling `reload`, RAM watchdog, `save/resurrect`, `startup` (systemd user + system).
- **P3 — Eyes and ears:** `monit` TUI, `events` journal surfacing, alert webhooks, Prometheus exporter, log rotation, `doctor`.
- **P4 — GPU + packaging:** NVML integration (metrics, CUDA_VISIBLE_DEVICES assignment, GPU watchdog); PyPI + pipx; `.deb` via dh-python + GitHub-hosted apt repo; snapcraft (classic confinement request); CI matrix, docs site, man page.
- **P5 (deferred) — Windows**, multi-host dashboard (one TUI over several servers' daemons via SSH tunnel) if the team ends up wanting it.

## 7. Repo Layout

```
src/viper/{cli,daemon,supervisor,health,ipc,logs,metrics,alerts,gpu,tui}/
tests/            # pytest + integration tests against real dummy servers
packaging/{debian,snap}/
docs/
pyproject.toml    LICENSE(MIT)
```

## 8. Open Questions (defaults chosen; correct me)

1. **Name:** "viper" collides with the Unix `viper` user + likely on PyPI → CLI `viper`, package `viper-pm`? Or pick a fresh name.
2. **What hurts most today?** Ordering inside P2/P3 flexes: crashes going unnoticed → alerts earlier; deploy downtime → reload earlier; GPU policing → NVML earlier.
3. **Shared servers:** do teammates share boxes (need system mode + shared visibility sooner) or one-service-per-box?
4. **Reverse proxy in front?** If nginx/traefik already load-balances, per-worker ports are fine and reload gets simpler; if not, `SO_REUSEPORT` path matters.
5. **License:** MIT ok?
