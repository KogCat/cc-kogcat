"""Shared bin-fetch helpers — single source for lock / staleness / disk-guard
/ active-runner detection, used by both the SessionStart detached path
(`hooks/bootstrap_om_core_bin.py`) and the in-process self-heal thread
(`scripts/om_mcp/self_heal.py`). Stdlib-only."""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# Stale-runner threshold; bootstrap overrides via OM_CORE_BIN_FETCH_TIMEOUT_SECONDS.
DEFAULT_FETCH_TIMEOUT_SECONDS = 600


def read_status(path: Path) -> dict | None:
    "Parse a fetch statefile, or None if absent/corrupt."
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def is_stale(iso_started_at: str, timeout_s: int) -> bool:
    "Malformed timestamp → stale, so a corrupt statefile never permanently blocks respawn."
    try:
        started = datetime.fromisoformat(iso_started_at.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return True
    return (datetime.now(timezone.utc) - started).total_seconds() > timeout_s


def is_pid_alive(pid: int | None) -> bool:
    "True if the pid exists (signal-0 probe)."
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ProcessLookupError, ValueError):
        return False


def active_runner_present(
    status_path: Path, timeout_s: int = DEFAULT_FETCH_TIMEOUT_SECONDS,
) -> bool:
    "True when a live, non-stale `downloading` runner owns the slot — back off instead of double-fetching."
    st = read_status(status_path)
    if not st or st.get("state") != "downloading":
        return False
    return is_pid_alive(st.get("pid")) and not is_stale(st.get("started_at", ""), timeout_s)


def disk_free_mb(path: Path) -> int:
    "Free MB on the fs holding `path`, or -1 if unobservable."
    try:
        return shutil.disk_usage(path).free // (1 << 20)
    except OSError:
        return -1


def needed_mb(expected_size: int, fmt: str | None) -> int:
    "Disk headroom for archive download + ~3x extracted onedir bundle."
    floor = expected_size or 100 * (1 << 20)
    return floor * (4 if fmt in ("tar.xz", "zip") else 1) // (1 << 20) + 50


def try_acquire_lock(lock_path: Path) -> int | None:
    "Non-blocking advisory lock (flock/msvcrt); fd on success, None if held. Cross-process single-flight."
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        if sys.platform == "win32":
            import msvcrt  # type: ignore[import-not-found]
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl  # type: ignore[import-not-found]
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        os.close(fd)
        return None


def release_lock(fd: int) -> None:
    "Close the lock fd (releases the advisory lock)."
    try:
        os.close(fd)
    except OSError:
        pass
