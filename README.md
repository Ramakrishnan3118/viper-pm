# viper

**Production-grade process manager for Python services on Linux servers.**
Start apps from the terminal, keep them alive, watch their memory, tail their
logs — and never touch a process it didn't start itself.

Built for teams running Python/AI services (model servers, workers, APIs) who
are tired of tmux sessions, `nohup`, and 3 a.m. surprises.

```bash
viper start "uvicorn app:app --port 8000" --name api -i 2 --max-memory 2G
viper ls
viper logs api -f
```

## Why not pm2?

pm2 is great — and written in Node.js. `viper` is pure Python (psutil +
asyncio), designed around what Python/AI services actually need: virtualenv
auto-detection, memory watchdogs for leaky inference processes, a daemon that
survives its own crash, and (on the roadmap) readiness probes for
slow-loading models and GPU awareness.

## Install

```bash
pipx install viper-pm        # or: pip install viper-pm
# apt (Debian/Ubuntu): see the two-line repo setup in docs/RELEASING.md, then:
# sudo apt install viper-pm   (installs python3 + all deps automatically)
```

From source:

```bash
git clone <repo> && cd viper && pip install -e .
```

## Quickstart

```bash
# start anything: a command, a .py file, or a config file
viper start "uvicorn app:app --port 8000" --name api -i 2 --max-memory 1G
viper start worker.py --name worker
viper start viper.yml

viper ls                 # table of apps, workers, cpu, memory, uptime, restarts
viper logs api -f        # follow logs (per-worker prefixes)
viper events api         # audit trail: every start/exit/restart and *why*
viper reload api         # rolling restart: workers restart one at a time
viper stop api           # graceful stop (SIGTERM, grace period, then SIGKILL)
viper delete api         # stop + remove from management
viper kill               # stop everything and shut the daemon down
```

The daemon starts automatically on first use and runs per-user. If the daemon
itself is killed, **your apps keep running** — the next `viper` command
respawns it and it re-attaches to every live worker from its journal.

## The team workflow: `viper apply`

Keep a `viper.yml` in each project repo; deploys become:

```bash
git pull && viper apply viper.yml
```

`apply` converges the server to the file: new apps start, changed apps
restart with the new config, unchanged apps are left alone
(`--prune` also removes apps missing from the file).

```yaml
apps:
  - name: api
    cmd: uvicorn app:app --host 0.0.0.0 --port 8000
    cwd: /srv/api            # relative paths resolve against this file
    venv: auto               # finds .venv/ or venv/ in cwd (or give a path)
    workers: 2               # each worker gets VIPER_WORKER_ID=0,1,...
    max_memory: 2G           # restart a worker whose process tree exceeds this
    env:
      MODEL_PATH: /models/base
    stop_signal: SIGTERM
    stop_grace: 30           # seconds before SIGKILL
    autorestart: true
    max_restarts: 10         # consecutive fast crashes before giving up
    min_uptime: 10           # seconds that count as a "stable" run

  - name: worker
    cmd: celery -A tasks worker
    cwd: /srv/pipeline
```

## Reboot persistence

```bash
viper save        # snapshot the current app list
viper resurrect   # bring it back (e.g. from a @reboot cron or systemd unit)
```

(`viper startup`, which generates the systemd unit for you, is on the
roadmap; until then a one-line `@reboot viper resurrect` cron works.)

## Guarantees

- **Never touches foreign processes.** Every managed PID is stored with its
  process create-time and both are re-verified before any signal is sent — a
  recycled PID is never signalled. Workers run in their own process group, so
  signals reach the worker's own tree and nothing else.
- **Daemon crashes are non-events.** Workers write logs straight to files and
  keep running; a restarted daemon re-attaches from the journal.
- **Honest restart behaviour.** Exponential backoff (0.5s → 30s cap), a
  circuit breaker after `max_restarts` consecutive fast crashes (state
  `errored`, visible in `viper ls`), and every restart's reason recorded in
  `viper events`.

## Environment your app sees

| Variable | Meaning |
|---|---|
| `VIPER_APP_NAME` | the app's name |
| `VIPER_WORKER_ID` | worker index `0..N-1` (use it to fan out ports) |
| `PYTHONUNBUFFERED=1` | set by default so logs stream live |
| `VIRTUAL_ENV`, `PATH` | pointed at the detected/configured virtualenv |

Files live under `~/.viper/` (override with `VIPER_HOME`): per-worker logs
in `logs/`, the state journal, the events audit log, and the daemon log.

## Roadmap

Readiness/liveness health checks with `startup_grace` for slow model loads →
readiness-gated zero-downtime reload → `viper monit` live TUI → alert
webhooks (Slack) → Prometheus metrics → GPU awareness (CUDA_VISIBLE_DEVICES
assignment, GPU-memory watchdog) → `viper startup` systemd generation → apt
repo + snap. See `PLAN.md` for the full plan and `docs/PACKAGING.md` for the
apt/snap path.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e .[dev]
.venv/bin/python -m pytest tests/
```

## License

MIT — see [LICENSE](LICENSE).
