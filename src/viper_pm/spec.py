"""App specification: what to run and how to keep it alive."""
from __future__ import annotations

import os
import re
import shlex
import signal as signal_mod
from dataclasses import dataclass, field, asdict, fields

import yaml

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?)B?\s*$", re.I)
_SIZE_MULT = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}

# accepted aliases in YAML / dict input -> canonical field name
_ALIASES = {"interpreter": "venv", "instances": "workers"}


def parse_memory(value) -> int | None:
    """'300M', '1.5G', '512', 1048576 -> bytes."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    m = _SIZE_RE.match(str(value))
    if not m:
        raise ValueError(f"invalid memory size: {value!r} (use e.g. 300M, 2G)")
    return int(float(m.group(1)) * _SIZE_MULT[m.group(2).upper()])


def format_memory(nbytes) -> str:
    if not nbytes:
        return "-"
    for unit, div in (("G", 1024**3), ("M", 1024**2), ("K", 1024)):
        if nbytes >= div:
            return f"{nbytes / div:.1f}{unit}"
    return f"{nbytes}B"


@dataclass
class AppSpec:
    name: str
    cmd: str
    cwd: str = ""
    venv: str = "auto"          # "auto" = detect .venv in cwd, "" = disable, or a path
    workers: int = 1
    env: dict = field(default_factory=dict)
    max_memory: int | None = None   # bytes; worker restarted when its tree exceeds this
    stop_signal: str = "SIGTERM"
    stop_grace: float = 10.0        # seconds between stop signal and SIGKILL
    autorestart: bool = True
    max_restarts: int = 10          # consecutive fast crashes before giving up
    min_uptime: float = 10.0        # seconds a worker must stay up to reset the crash counter

    def validate(self) -> None:
        if not _NAME_RE.match(self.name or ""):
            raise ValueError(
                f"invalid app name {self.name!r}: use letters, digits, '-', '_', '.'"
            )
        if not self.cmd or not shlex.split(self.cmd):
            raise ValueError(f"app {self.name!r}: cmd is empty")
        if self.workers < 1:
            raise ValueError(f"app {self.name!r}: workers must be >= 1")
        if not hasattr(signal_mod, self.stop_signal):
            raise ValueError(f"app {self.name!r}: unknown stop_signal {self.stop_signal!r}")
        if self.stop_grace < 0 or self.min_uptime < 0:
            raise ValueError(f"app {self.name!r}: negative durations not allowed")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AppSpec":
        known = {f.name for f in fields(cls)}
        out = {}
        for key, value in dict(data).items():
            key = _ALIASES.get(key, key)
            if key not in known:
                raise ValueError(f"unknown app option: {key!r}")
            out[key] = value
        if "max_memory" in out:
            out["max_memory"] = parse_memory(out["max_memory"])
        if "env" in out and out["env"]:
            out["env"] = {str(k): str(v) for k, v in out["env"].items()}
        if "workers" in out:
            out["workers"] = int(out["workers"])
        spec = cls(**out)
        spec.validate()
        return spec


def load_config(path: str) -> list[AppSpec]:
    """Load a viper.yml file. Relative cwd entries resolve against the file's directory."""
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    apps = data.get("apps")
    if not isinstance(apps, list) or not apps:
        raise ValueError(f"{path}: expected a top-level 'apps:' list")
    base = os.path.dirname(os.path.abspath(path))
    specs = []
    for entry in apps:
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: each item under 'apps:' must be a mapping")
        entry = dict(entry)
        cwd = entry.get("cwd") or "."
        entry["cwd"] = os.path.normpath(os.path.join(base, os.path.expanduser(cwd)))
        specs.append(AppSpec.from_dict(entry))
    names = [s.name for s in specs]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ValueError(f"{path}: duplicate app names: {', '.join(sorted(dupes))}")
    return specs
