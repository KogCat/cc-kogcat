"""In-process om-core binary self-heal — the only path that downloads/installs
the binary from inside the long-lived MCP server process.

Codex kills detached children its MCP server spawns, so the
SessionStart→bootstrap→detached-runner path dies mid-extract on Codex/Windows.
The MCP server process itself survives (Codex depends on it), so fetching in a
daemon thread of THAT process converges. The activate step still spawns a
short-lived om-core CLI child (Codex may kill it) — retried every round until a
live sidecar exists; once install-service registers + starts the sidecar, the
OS supervisor owns it, off Codex's tree. See docs/03-binary-lifecycle.md.

MUST NOT write stdout: that is the MCP JSON-RPC channel.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import bin_fetch_common as _bfc  # noqa: E402
import om_core_paths as _ocp  # noqa: E402
from om_supervisor import (  # noqa: E402
    service_status,
    trigger_install_service,
    trigger_log,
    trigger_service_activate,
)

# Backoff: dense over the first-install window, then low-frequency forever
# (never permanently stops — permanent stop is the root cause being killed).
_DENSE_INTERVAL_S = 5.0
_DENSE_WINDOW_S = 120.0
_SLOW_INTERVAL_S = 45.0
_MAX_ATTEMPTS_ENV = "OM_SELF_HEAL_MAX_ATTEMPTS"  # CI/test escape hatch; default unbounded

_started_lock = threading.Lock()
_started = False


def _set_started(value: bool) -> None:
    global _started
    with _started_lock:
        _started = value


def _plugin_root() -> Path:
    for key in ("CODEX_PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"):
        if (env := os.environ.get(key, "").strip()):
            return Path(env)
    return _SCRIPTS_DIR.parent


def _live_sidecar() -> bool:
    "True if a live sidecar transport exists. Lazy import breaks the self_heal⇄http_backend cycle."
    try:
        from om_mcp import http_backend  # noqa: PLC0415
        return http_backend._live_cfg() is not None
    except Exception:  # noqa: BLE001
        return False


def _activate(cache_path: Path) -> None:
    "Install/activate so the OS supervisor brings the sidecar up. Best-effort, retried each round."
    try:
        stable = _ocp.ensure_current_symlink(cache_path)
        status = service_status(cache_path)
        if status is None or not status.get("installed"):
            # unknown or unregistered → install-service (idempotent; registers +
            # autostarts). service-activate cannot start a never-registered host.
            ok, route = trigger_install_service(cache_path, stable, detached=False)
        else:
            ok, route = trigger_service_activate(cache_path, stable, detached=False)
        trigger_log(f"self-heal activate ok={ok} route={route}")
    except Exception as e:  # noqa: BLE001
        trigger_log(f"self-heal activate failed: {type(e).__name__}: {e}")


def converge_once() -> bool:
    """One self-heal round. Returns True iff a live sidecar now exists (loop
    stops on True). Never spawns the fetch; never touches stdout."""
    if _live_sidecar():
        return True
    if os.environ.get("OM_CORE_BIN", "").strip():
        return True  # user-managed binary; self-heal stays out

    try:
        target = _ocp.target_triple()
    except _ocp.UnsupportedTarget:
        return True  # nothing to fetch on this platform

    try:
        manifest = _ocp.resolve_release(_plugin_root(), target)
    except Exception as e:  # noqa: BLE001
        trigger_log(f"self-heal resolve_release failed: {type(e).__name__}: {e}")
        return False

    sha = manifest.get("om_core_bin_sha256")
    size = int(manifest.get("size_bytes") or 0)
    version = manifest.get("version")
    url = manifest.get("om_core_bin_url")
    fmt = manifest.get("format")
    if not (sha and version and url):
        trigger_log("self-heal: resolved release missing sha/version/url")
        return False

    try:
        _ocp.write_expected_release(version, target)
    except Exception:  # noqa: BLE001
        pass
    cache_path = _ocp.cache_bin_path(version, target)
    status_path = _ocp.bin_status_path(version, target)
    lock_path = _ocp.bin_lock_path(version, target)

    # Cache hit (onedir ready) → binary present; only (re)activate needed.
    existing = _bfc.read_status(status_path)
    if (cache_path.is_file() and existing
            and existing.get("state") == "ready"
            and existing.get("archive_sha256") == sha):
        _activate(cache_path)
        return _live_sidecar()

    free = _bfc.disk_free_mb(cache_path.parent.parent)
    need = _bfc.needed_mb(size, fmt)
    if 0 < free < need:
        trigger_log(f"self-heal: disk full ({free}MB free, need ~{need}MB)")
        return False

    # Hold the lock for the WHOLE in-process fetch (not spawn-then-release).
    # If a live detached runner already owns the slot (CC SessionStart), back off.
    fd = _bfc.try_acquire_lock(lock_path)
    if fd is None:
        return False
    try:
        if _bfc.active_runner_present(status_path):
            return False
        try:
            from _bin_fetch_runner import run_fetch  # noqa: PLC0415 — scripts/ dir
        except Exception as e:  # noqa: BLE001
            trigger_log(f"self-heal: cannot import run_fetch: {type(e).__name__}: {e}")
            return False
        rc = run_fetch(
            urls=_ocp.bin_mirror_urls(url, version),
            dest=cache_path, sha256=sha, size=size,
            status_file=status_path, version=version, fmt=fmt or "raw",
            log=lambda m: trigger_log("self-heal fetch: " + str(m).replace("\n", " | ")),
        )
        trigger_log(f"self-heal run_fetch rc={rc}")
    finally:
        _bfc.release_lock(fd)

    if cache_path.is_file():
        _activate(cache_path)
    return _live_sidecar()


def start_self_heal_thread() -> None:
    "Idempotently start the single self-heal daemon thread (no-op if already running)."
    global _started
    with _started_lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_run_loop, name="om-self-heal", daemon=True).start()


def _run_loop() -> None:
    try:
        try:
            max_attempts = int(os.environ.get(_MAX_ATTEMPTS_ENV, "0") or "0")
        except ValueError:
            max_attempts = 0
        start = time.monotonic()
        attempt = 0
        while True:
            attempt += 1
            try:
                if converge_once():
                    trigger_log(f"self-heal converged after {attempt} round(s)")
                    _set_started(False)  # allow restart if the sidecar later dies
                    return
            except BaseException as e:  # noqa: BLE001 — one round must never kill the loop
                try:
                    trigger_log(f"self-heal round {attempt} error: {type(e).__name__}: {e}")
                except Exception:
                    pass
            if max_attempts and attempt >= max_attempts:
                trigger_log(f"self-heal stopped after {attempt} attempts ({_MAX_ATTEMPTS_ENV})")
                return  # terminal — CI escape hatch, no restart
            elapsed = time.monotonic() - start
            time.sleep(_DENSE_INTERVAL_S if elapsed < _DENSE_WINDOW_S else _SLOW_INTERVAL_S)
    except BaseException:  # noqa: BLE001 — last-resort guard; never crash silently
        try:
            trigger_log("self-heal loop crashed")
        except Exception:
            pass
        _set_started(False)
