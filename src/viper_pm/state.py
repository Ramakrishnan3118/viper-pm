"""Journal of daemon state, written atomically on every transition.

Survives daemon crashes: a restarted daemon re-attaches to still-running
workers using the recorded (pid, create_time) pairs.
"""
import json
import os

from . import paths


def save_state(data: dict) -> None:
    paths.ensure_dirs()
    target = paths.state_path()
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, target)


def load_state() -> dict | None:
    try:
        return json.loads(paths.state_path().read_text())
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError):
        return None
