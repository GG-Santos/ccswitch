"""Daemon run state: a status file and a log file so other commands (and you)
can tell whether the watcher is alive and what it last did.

Without this the daemon is a black box: you cannot tell from another terminal
whether it is still running or when it last switched. The status file records a
heartbeat; the log file records a durable history.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from ccswitch import vault


def status_path() -> Path:
    return vault.home() / "daemon-status.json"


def log_path() -> Path:
    return vault.home() / "daemon.log"


def write_status(status: dict) -> None:
    p = status_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def read_status() -> dict | None:
    p = status_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


_LOG_MAX_BYTES = 1_000_000


def append_log(line: str) -> None:
    p = log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Single-file rotation: when the log grows past the cap, keep one prior
    # generation as daemon.log.1 so it cannot grow without bound.
    try:
        if p.exists() and p.stat().st_size > _LOG_MAX_BYTES:
            os.replace(p, p.with_suffix(p.suffix + ".1"))
    except OSError:
        pass
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    with p.open("a", encoding="utf-8") as fh:
        fh.write(f"{stamp}  {line}\n")


def pid_alive(pid: int) -> bool:
    """True if a process with `pid` is currently running."""
    if not pid:
        return False
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


def running_status() -> dict | None:
    """Return the status dict if a live daemon is recorded, else None."""
    status = read_status()
    if status and pid_alive(status.get("pid", 0)):
        return status
    return None
