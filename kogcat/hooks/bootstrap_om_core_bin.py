"""Bootstrap om-core binary — schedule detached fetch, never block SessionStart.

This hook schedules a detached fetch instead of downloading the 64MB
binary synchronously inside SessionStart. Synchronous fetch had two
production failure modes:

  1. Slow networks pushed runtime past Claude Code's hook timeout; CC sent
     SIGTERM, leaving `*.tmp` orphans.
  2. CC's loading-phase UI suppresses hook stdout/stderr (host-side
     limitation, not plugin-bypassable). The synchronous progress prints
     we emitted from `_fetch_with_progress` were never visible.

This hook is now non-blocking:
  - Cache hit (sha-verified by sidecar's runtime resolver, not here): return ~5ms.
  - Legacy plugin-tree binary present: synchronous adopt-into-cache (local
    fs copy, ms-level on SSD); preserved through 0.22, removed in 0.23.
  - Otherwise: spawn detached `_bin_fetch_runner.py`, write initial
    statefile, exit ~50ms. The runner owns the actual download, sha
    verification, and atomic rename. The wrapper's MCP readiness gate
    (`scripts/om_mcp/tools.py::_om_core_bin_ready_or_raise`) reads the
    statefile and surfaces progress in `OM_CORE_BIN_DOWNLOADING` hints.

Release resolution (`om_core_paths.resolve_release`): channel-first — a
small channel.json GET (≤5s, short-TTL cached, falls back to the bundled
per-target manifest when offline) decides which version/url/sha to fetch.
This file is the *write side*; runtime resolvers read independently.

Failure policy: warn but never block. Unsupported platforms / missing
manifest / lock contention all return 0; downstream tool calls eventually
surface OmCoreBinNotFound or the gate's OM_CORE_BIN_DOWNLOADING hint, both
of which are actionable.

Stdlib-only on purpose: keeps the hook's dependency surface minimal.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_FILE_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FETCH_TIMEOUT_SECONDS = 600  # env OM_CORE_BIN_FETCH_TIMEOUT_SECONDS overrides


def _plugin_root() -> Path:
    """Resolve plugin root across Claude Code, Codex, tests, and direct runs."""
    for key in ("CODEX_PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"):
        if (env := os.environ.get(key, "").strip()):
            return Path(env)
    return _FILE_PLUGIN_ROOT


# Shared resolver lives under scripts/ — the hooks dir is not on sys.path
# by default, so we reach in directly. The shared module is stdlib-only
# on purpose. We resolve via
# the file location here (not env) so the import always succeeds even
# when CLAUDE_PLUGIN_ROOT is fake / mistyped.
sys.path.insert(0, str(_FILE_PLUGIN_ROOT / "scripts"))
from om_core_paths import (  # noqa: E402
    UnsupportedTarget,
    bin_lock_path,
    bin_status_path,
    bin_mirror_urls,
    cache_bin_path,
    current_bin_path,
    ensure_current_symlink,
    resolve_release,
    target_triple,
    write_expected_release,
)
from om_supervisor import (  # noqa: E402
    read_running_sidecar_version,
    service_status,
    spawn_detached,
    trigger_install_service,
    trigger_log,
    trigger_service_activate,
)
from bin_fetch_common import (  # noqa: E402  — single source; aliased to keep call sites
    disk_free_mb as _disk_free_mb,
    is_pid_alive as _is_pid_alive,
    is_stale as _is_stale,
    read_status as _read_status,
    release_lock as _release_lock,
    try_acquire_lock as _try_acquire_lock,
)


def _log(msg: str) -> None:
    print(f"[om-core] {msg}", file=sys.stderr, flush=True)
    trigger_log(f"bootstrap: {msg}")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_status(statefile: Path, payload: dict) -> None:
    statefile.parent.mkdir(parents=True, exist_ok=True)
    tmp = statefile.with_suffix(statefile.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, statefile)


def _spawn_detached_runner(
    runner: Path, *, urls: list[str], dest: Path, sha256: str, size: int,
    status_file: Path, version: str, fmt: str | None, log_file: Path,
) -> None:
    """Spawn `_bin_fetch_runner.py` so it outlives the caller's job/session.

    Delegates to `spawn_detached` (win32 breakaway-from-job + WMI fallback,
    POSIX new session) so a transient hook / mcp-self-heal job tearing down
    can't kill the runner mid-download. The runner self-opens `--log-file`
    (the WMI fallback can't inherit an stdout fd). Stdlib-only."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, str(runner),
        "--url", *urls,
        "--dest", str(dest),
        "--sha256", sha256,
        "--size", str(size),
        "--status-file", str(status_file),
        "--version", version,
        "--format", fmt or "raw",
        "--log-file", str(log_file),
    ]
    spawn_detached(cmd, cwd=_runner_cwd())


def _resolve_log_file() -> Path:
    """Background runner logs live in the shared om log dir."""
    try:
        from om_mcp import paths  # type: ignore[import-not-found]
        return paths.log_dir() / "om-core-fetch.log"
    except ImportError:
        from tempfile import gettempdir
        return Path(gettempdir()) / "om-core-fetch.log"


def _runner_cwd() -> str | None:
    "Stable dir for the runner's cwd so it never holds the plugin cache (Windows: a held cwd blocks plugin delete/rename). Runner uses absolute paths, so cwd is free to move."
    try:
        from om_mcp import paths  # type: ignore[import-not-found]
        d = paths.config_dir()
        d.mkdir(parents=True, exist_ok=True)
        return str(d)
    except (ImportError, OSError):
        if sys.platform == "win32":
            root = os.environ.get("SYSTEMDRIVE", "C:") + "\\"
            return root if os.path.isdir(root) else None
        return "/"


def _activate_cached_bundle(cache_path: Path, expected_version: str) -> None:
    "Reconcile current/<target> + verify running sidecar version. Best-effort."
    try:
        stable = current_bin_path()
    except Exception:  # noqa: BLE001
        return

    before = os.path.realpath(stable) if stable.exists() else None
    try:
        ensure_current_symlink(cache_path)
    except Exception as exc:  # noqa: BLE001
        _log(f"current-pointer reconcile failed ({type(exc).__name__}: {exc})")
        return
    after = os.path.realpath(stable) if stable.exists() else None
    symlink_moved = (before is not None and before != after)

    running_version = read_running_sidecar_version()
    if running_version is not None:
        # Sidecar up → only reconcile version (no service-status probe).
        if running_version == expected_version:
            return
        _log(f"sidecar runs {running_version!r}, expected {expected_version!r}; "
             f"spawning detached service-activate")
        ok, route = trigger_service_activate(cache_path, stable, detached=True)
        if not ok:
            _log("service-activate spawn failed; supervisor respawn will pick up")
        elif route != "service-activate":
            _log(f"detached activate took route={route}")
        return

    # No live sidecar → ensure registered (install-service, not just activate). See docs 03.
    status = service_status(cache_path)
    if status is not None and not status.get("installed"):
        _log("supervisor not registered; spawning detached install-service")
        ok, _route = trigger_install_service(cache_path, stable, detached=True)
        if not ok:
            _log("install-service spawn failed; next pass retries")
        return

    # Registered but down (or status unobservable) — let KeepAlive / next pass handle.
    if symlink_moved:
        _log(f"current pointer repaired → {cache_path.parent}")


def main() -> int:
    trigger_log("bootstrap main() start")
    # OM_CORE_BIN env override: power user / dev / CI path. If they set it,
    # they own resolution; this hook is a no-op.
    if (env := os.environ.get("OM_CORE_BIN", "").strip()):
        p = Path(env).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return 0
        _log(f"OM_CORE_BIN={env} not executable; ignoring override and continuing")

    try:
        target = target_triple()
    except UnsupportedTarget as exc:
        _log(f"unsupported platform ({exc}); skipping. Use OM_CORE_BIN override or `python -m om_core` for dev mode.")
        return 0

    plugin_root = _plugin_root()
    try:
        manifest = resolve_release(plugin_root, target)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        _log(f"no release resolvable — channel unreachable and bundled "
             f"manifest unavailable ({exc}); skipping. Use OM_CORE_BIN "
             f"override or `python -m om_core`.")
        return 0

    expected_sha = manifest.get("om_core_bin_sha256")
    expected_size = int(manifest.get("size_bytes") or 0)
    expected_version = manifest.get("version")
    url = manifest.get("om_core_bin_url")
    # `format` in ("tar.xz", "zip") → the asset is an onedir bundle archive
    # the runner extracts; absent / "raw" → a legacy single-file binary.
    fmt = manifest.get("format")
    if not (expected_sha and expected_version and url):
        _log("resolved release missing required fields (sha256/version/url); skipping")
        return 0

    trigger_log(f"bootstrap: resolved release version={expected_version} fmt={fmt} url={url}")

    # Record the resolved version for the per-tool-call readiness gate. The
    # gate reads this pointer instead of re-resolving, so its hot path stays
    # local (no channel fetch). Best-effort — never block bootstrap on it.
    write_expected_release(expected_version, target)

    cache_path = cache_bin_path(expected_version, target)
    status_path = bin_status_path(expected_version, target)
    lock_path = bin_lock_path(expected_version, target)

    # Cache hit → nothing to do.
    #   onedir (tar.xz/zip): trust the fetcher's atomic-install record — the
    #     bundle executable is present and the statefile says `ready` with
    #     the verified archive sha. Re-hashing a ~100MB bundle on every
    #     SessionStart is too costly, and the archive sha is what
    #     manifest/channel pin anyway.
    #   raw (legacy fallback): hash the single-file binary directly and
    #     reconcile the statefile to `ready` if a prior run left it stale.
    if fmt in ("tar.xz", "zip"):
        existing = _read_status(status_path)
        if (cache_path.is_file()
                and existing
                and existing.get("state") == "ready"
                and existing.get("archive_sha256") == expected_sha):
            _activate_cached_bundle(cache_path, expected_version)
            return 0
    elif cache_path.is_file():
        try:
            actual_sha = _sha256(cache_path)
        except OSError as exc:
            _log(f"could not hash cached binary {cache_path}: {exc}; will re-fetch")
            actual_sha = ""
        if actual_sha == expected_sha:
            existing = _read_status(status_path)
            if not existing or existing.get("state") != "ready":
                _write_status(status_path, {
                    "state": "ready",
                    "started_at": _utc_now_iso(),
                    "finished_at": _utc_now_iso(),
                    "version": expected_version,
                    "bytes_downloaded": expected_size or cache_path.stat().st_size,
                    "total_bytes_hint": expected_size or cache_path.stat().st_size,
                    "last_progress_at": _utc_now_iso(),
                })
            _activate_cached_bundle(cache_path, expected_version)
            return 0
        # Sha mismatch — delete and continue to fetch path. Spec 18 §C2:
        # no grace period, version pinning beats schema drift.
        _log(f"cached binary sha mismatch (expected {expected_sha[:12]}…, got {actual_sha[:12]}…); re-fetching")
        try:
            cache_path.unlink()
        except OSError:
            pass

    # Disk free guard — fail fast with a clear message rather than letting
    # the runner discover ENOSPC mid-stream. A tar.xz/zip onedir asset needs
    # room for the archive download + the ~3x-larger extracted bundle.
    size_floor = expected_size or 100 * (1 << 20)
    needed_mb = size_floor * (4 if fmt in ("tar.xz", "zip") else 1) // (1 << 20) + 50
    free_mb = _disk_free_mb(cache_path.parent.parent)
    if 0 < free_mb < needed_mb:
        _write_status(status_path, {
            "state": "failed",
            "started_at": _utc_now_iso(),
            "finished_at": _utc_now_iso(),
            "version": expected_version,
            "reason": f"disk full ({free_mb}MB free, need ~{needed_mb}MB)",
            "bytes_downloaded": 0,
            "total_bytes_hint": expected_size or None,
            "last_progress_at": _utc_now_iso(),
        })
        _log(f"disk full ({free_mb}MB free, need ~{needed_mb}MB); free space and retry")
        return 0

    # Single-flight lock: prevents two SessionStart hooks from spawning
    # parallel runners on the same cache slot.
    lock_fd = _try_acquire_lock(lock_path)
    if lock_fd is None:
        # Another hook instance won the race; trust it. We don't double-spawn.
        return 0

    try:
        timeout_s = int(os.environ.get(
            "OM_CORE_BIN_FETCH_TIMEOUT_SECONDS",
            str(DEFAULT_FETCH_TIMEOUT_SECONDS),
        ))
        existing = _read_status(status_path)
        if existing:
            stage = existing.get("state")
            if stage == "ready":
                # Statefile says ready but we already established cache_path
                # doesn't exist (we'd have returned above). Reconcile by
                # falling through to spawn — runner will re-create the binary.
                pass
            elif stage == "downloading":
                started = existing.get("started_at", "")
                pid = existing.get("pid")
                if _is_pid_alive(pid) and not _is_stale(started, timeout_s):
                    return 0  # healthy in-flight runner; don't double-spawn

        runner = _FILE_PLUGIN_ROOT / "scripts" / "_bin_fetch_runner.py"
        if not runner.is_file():
            _log(f"runner script missing: {runner}; skipping")
            return 0

        log_file = _resolve_log_file()
        _spawn_detached_runner(
            runner,
            urls=bin_mirror_urls(url, expected_version),
            dest=cache_path,
            sha256=expected_sha,
            size=expected_size,
            status_file=status_path,
            version=expected_version,
            fmt=fmt,
            log_file=log_file,
        )
        _log(
            f"binary fetch runner spawned (version={expected_version}, "
            f"target={target}, log='{log_file}')"
        )
        return 0
    finally:
        _release_lock(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
