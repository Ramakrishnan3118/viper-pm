"""Integration tests: real daemon, real child processes, isolated VIPER_HOME."""
import os
import sys
import time

import psutil
import pytest

from viper_pm import ipc


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("VIPER_HOME", str(tmp_path / "nb"))
    yield tmp_path
    try:
        ipc.call("kill", timeout=60)
        time.sleep(0.5)
    except Exception:
        pass


def _wait(predicate, timeout=10.0, interval=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _gone(pid):
    """True when the process is dead (a zombie counts as dead: it may linger
    unreaped because the test process is its parent)."""
    if not psutil.pid_exists(pid):
        return True
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def test_lifecycle(home):
    ipc.ensure_daemon()
    spec = {
        "name": "sleeper",
        "cmd": f'{sys.executable} -c "import time; time.sleep(120)"',
        "cwd": str(home),
    }
    result = ipc.call("start", {"spec": spec})
    assert result["name"] == "sleeper"

    assert _wait(lambda: ipc.call("list")[0]["workers"][0]["status"] == "online")
    pid = ipc.call("list")[0]["workers"][0]["pid"]
    assert psutil.pid_exists(pid)

    ipc.call("stop", {"name": "sleeper"}, timeout=60)
    assert ipc.call("list")[0]["state"] == "stopped"
    assert _wait(lambda: _gone(pid), timeout=5)

    ipc.call("delete", {"name": "sleeper"}, timeout=60)
    assert ipc.call("list") == []


def test_crash_autorestart(home):
    ipc.ensure_daemon()
    spec = {
        "name": "crasher",
        "cmd": f'{sys.executable} -c "import sys; sys.exit(3)"',
        "cwd": str(home),
        "min_uptime": 1.0,
        "max_restarts": 50,
    }
    ipc.call("start", {"spec": spec})
    assert _wait(lambda: ipc.call("list")[0]["restarts"] >= 2, timeout=15)
    ipc.call("delete", {"name": "crasher"}, timeout=60)


def test_multi_worker_and_env(home):
    ipc.ensure_daemon()
    marker = home / "w{}.txt"
    script = (
        "import os, time, pathlib; "
        "pathlib.Path(os.environ['OUT'].format(os.environ['VIPER_WORKER_ID']))"
        ".write_text('hi'); time.sleep(120)"
    )
    spec = {
        "name": "multi",
        "cmd": f'{sys.executable} -c "{script}"',
        "cwd": str(home),
        "workers": 2,
        "env": {"OUT": str(marker)},
    }
    ipc.call("start", {"spec": spec})
    assert _wait(lambda: (home / "w0.txt").exists() and (home / "w1.txt").exists())
    snapshot = ipc.call("list")[0]
    assert len(snapshot["workers"]) == 2
    ipc.call("delete", {"name": "multi"}, timeout=60)


def test_daemon_reattach(home):
    """Kill the daemon; the app keeps running; a new daemon adopts it."""
    ipc.ensure_daemon()
    spec = {
        "name": "survivor",
        "cmd": f'{sys.executable} -c "import time; time.sleep(120)"',
        "cwd": str(home),
    }
    ipc.call("start", {"spec": spec})
    assert _wait(lambda: ipc.call("list")[0]["workers"][0]["status"] == "online")
    worker_pid = ipc.call("list")[0]["workers"][0]["pid"]
    daemon_pid = ipc.call("ping")["pid"]

    os.kill(daemon_pid, 9)  # daemon dies hard...
    assert _wait(lambda: _gone(daemon_pid), timeout=5)
    assert psutil.pid_exists(worker_pid)  # ...but the app survives

    ipc.ensure_daemon()  # new daemon re-attaches from the journal
    apps = ipc.call("list")
    assert apps[0]["workers"][0]["pid"] == worker_pid
    assert apps[0]["workers"][0]["status"] == "online"
    ipc.call("delete", {"name": "survivor"}, timeout=60)
    assert _wait(lambda: _gone(worker_pid), timeout=5)
