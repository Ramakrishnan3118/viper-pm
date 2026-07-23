"""Client side of the daemon RPC: newline-delimited JSON over a Unix socket."""
import json
import socket
import subprocess
import sys
import time

from . import paths

DEFAULT_TIMEOUT = 10.0


class DaemonError(Exception):
    """The daemon processed the request and returned an error."""


class DaemonUnavailable(Exception):
    """Could not talk to the daemon at all."""


def call(method: str, params: dict | None = None, timeout: float = DEFAULT_TIMEOUT):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(paths.socket_path()))
        payload = json.dumps({"method": method, "params": params or {}}) + "\n"
        sock.sendall(payload.encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
    except (FileNotFoundError, ConnectionRefusedError, ConnectionResetError,
            BrokenPipeError, socket.timeout, OSError) as exc:
        raise DaemonUnavailable(str(exc)) from exc
    finally:
        sock.close()
    if not buf:
        raise DaemonUnavailable("daemon closed the connection")
    resp = json.loads(buf)
    if not resp.get("ok"):
        raise DaemonError(resp.get("error", "unknown daemon error"))
    return resp.get("result")


def daemon_alive() -> bool:
    try:
        call("ping", timeout=2.0)
        return True
    except (DaemonUnavailable, DaemonError):
        return False


def ensure_daemon() -> None:
    """Connect to the daemon, spawning it first if it is not running."""
    if daemon_alive():
        return
    paths.ensure_dirs()
    with open(paths.daemon_log_path(), "ab") as logf:
        subprocess.Popen(
            [sys.executable, "-m", "viper_pm.daemon"],
            stdout=logf,
            stderr=logf,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if daemon_alive():
            return
        time.sleep(0.15)
    raise DaemonUnavailable(f"daemon did not start; see {paths.daemon_log_path()}")
