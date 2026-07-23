"""Filesystem layout. Everything lives under $VIPER_HOME (default ~/.viper)."""
import os
from pathlib import Path


def base_dir() -> Path:
    return Path(os.environ.get("VIPER_HOME", str(Path.home() / ".viper")))


def logs_dir() -> Path:
    return base_dir() / "logs"


def socket_path() -> Path:
    # AF_UNIX paths are limited to ~108 chars; for deep VIPER_HOMEs fall back
    # to a short per-home socket in the runtime dir (client and daemon both
    # derive it from base_dir, so they always agree).
    p = base_dir() / "daemon.sock"
    if len(str(p)) <= 96:
        return p
    import hashlib
    digest = hashlib.sha1(str(base_dir()).encode()).hexdigest()[:10]
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(runtime) / f"viper-{os.getuid()}-{digest}.sock"


def state_path() -> Path:
    return base_dir() / "state.json"


def dump_path() -> Path:
    return base_dir() / "dump.json"


def events_path() -> Path:
    return base_dir() / "events.log"


def daemon_log_path() -> Path:
    return base_dir() / "daemon.log"


def daemon_pid_path() -> Path:
    return base_dir() / "daemon.pid"


def out_log(name: str, slot: int) -> Path:
    return logs_dir() / f"{name}-{slot}-out.log"


def err_log(name: str, slot: int) -> Path:
    return logs_dir() / f"{name}-{slot}-err.log"


def ensure_dirs() -> None:
    logs_dir().mkdir(parents=True, exist_ok=True)
