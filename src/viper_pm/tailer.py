"""Tail and follow log files (used by `viper logs` and `viper events`)."""
from __future__ import annotations

import os
import time
from pathlib import Path

_READ_BACK = 1 << 20  # read at most 1 MiB from the end for the initial tail


def last_lines(path: Path, n: int) -> list[str]:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - _READ_BACK))
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > _READ_BACK and lines:
        lines = lines[1:]  # first line may be partial
    return lines[-n:]


def tail(files: list[tuple[str, Path]], lines: int, follow: bool,
         line_filter=None, printer=print) -> None:
    """files: list of (label, path). Prints last `lines` of each, then follows."""
    multi = len(files) > 1

    def emit(label: str, text: str) -> None:
        if line_filter and not line_filter(text):
            return
        printer(f"{label} | {text}" if multi else text)

    for label, path in files:
        for text in last_lines(path, lines):
            emit(label, text)

    if not follow:
        return

    handles = {}
    positions = {}
    try:
        while True:
            for label, path in files:
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                if path not in handles:
                    handles[path] = open(path, "rb")
                    handles[path].seek(size)
                    positions[path] = size
                    continue
                if size < positions[path]:  # truncated/rotated: start over
                    handles[path].seek(0)
                    positions[path] = 0
                data = handles[path].read()
                if data:
                    positions[path] += len(data)
                    for text in data.decode("utf-8", errors="replace").splitlines():
                        emit(label, text)
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        for f in handles.values():
            f.close()
