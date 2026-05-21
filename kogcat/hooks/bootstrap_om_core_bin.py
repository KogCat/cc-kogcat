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
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_FILE_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FETCH_TIMEOUT_SECONDS = 600  # env OM_CORE_BIN_FETCH_TIMEOUT_SECONDS overrides


def _plugin_root() -> Path:
    """Prefer CLAUDE_PLUGIN_ROOT env (set by Claude Code at hook invocation
    and by tests). Falls back to the file's own location for unusual
    invocations (eg. direct `python hooks/...` outside plugin context).

    Why env-first: aligns with `inject_memory.py`; lets tests substitute
    a fake plugin tree without rewriting the hook entry point.
    """
    if (env := os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()):
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
    cache_bin_path,
    current_bin_path,
    ensure_current_symlink,
    resolve_release,
    target_triple,
    write_expected_release,
)
from om_supervisor import (  # noqa: E402
    read_running_sidecar_version,
    trigger_service_activate,
)


def _log(msg: str) -> None:
    print(f"[om-core] {msg}", file=sys.stderr, flush=True)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _disk_free_mb(path: Path) -> int:
    try:
        usage = shutil.disk_usage(path)
        return usage.free // (1 << 20)
    except OSError:
        return -1


def _write_status(statefile: Path, payload: dict) -> None:
    statefile.parent.mkdir(parents=True, exist_ok=True)
    tmp = statefile.with_suffix(statefile.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, statefile)


def _read_status(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _is_stale(iso_started_at: str, timeout_s: int) -> bool:
    """Treat malformed timestamps as stale so a corrupt statefile doesn't
    permanently block respawning."""
    try:
        started = datetime.fromisoformat(iso_started_at.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return True
    return (datetime.now(timezone.utc) - started).total_seconds() > timeout_s


def _is_pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ProcessLookupError, ValueError):
        return False


def _try_acquire_lock(lock_path: Path) -> int | None:
    """Non-blocking advisory lock; returns fd on success, None if held."""
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


def _release_lock(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _spawn_detached_runner(
    runner: Path, *, url: str, dest: Path, sha256: str, size: int,
    status_file: Path, version: str, fmt: str | None, log_file: Path,
) -> None:
    """Spawn `_bin_fetch_runner.py` detached.

    POSIX: `start_new_session=True` puts the runner in its own process
    group so SIGTERM to the hook process doesn't propagate. Windows
    mirrors with DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP.

    No PYTHONPATH injection: the runner is stdlib-only.
    """
    log_file.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": open(log_file, "ab"),  # noqa: SIM115 — fd handed to subprocess
        "stderr": subprocess.STDOUT,
        "close_fds": True,
        "env": env,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000208
    else:
        kwargs["start_new_session"] = True

    cmd = [
        sys.executable, str(runner),
        "--url", url,
        "--dest", str(dest),
        "--sha256", sha256,
        "--size", str(size),
        "--status-file", str(status_file),
        "--version", version,
        "--format", fmt or "raw",
    ]
    subprocess.Popen(cmd, **kwargs)


def _resolve_log_file() -> Path:
    """Background runner logs live in the shared om log dir."""
    try:
        from om_mcp import paths  # type: ignore[import-not-found]
        return paths.log_dir() / "om-core-fetch.log"
    except ImportError:
        from tempfile import gettempdir
        return Path(gettempdir()) / "om-core-fetch.log"


def _activate_cached_bundle(cache_path: Path, expected_version: str) -> None:
    """Reconcile ``current/<target>`` *and* the running sidecar version.

    Two-stage convergence each SessionStart:

    1. **Symlink reconcile** — ``ensure_current_symlink`` writes the
       ``current/<target>`` pointer at the cached bundle. Idempotent:
       no-op when the pointer is already there. The first-install path
       and the onedir-cache half-applied path both flow through here.

    2. **Running-version verify** — read ``server.json`` to learn which
       binary the supervisor is actually executing right now, compare
       against ``expected_version`` (the channel/manifest pick this
       SessionStart resolved). When they diverge — meaning a prior fetch
       cycle wrote the new binary + symlink but the
       ``service-activate`` step never landed (timeout, the new binary
       was unreachable mid-fetch, etc.) — kick the supervisor again so
       the user does not have to.

    The verify hop is the long-term invariant guaranteeing eventual
    convergence: SessionStart MUST end with ``running == expected``
    unless the sidecar is unobservable (no ``server.json`` / stale PID),
    in which case launchd/systemd's own KeepAlive will respawn it
    against the freshly-pointed symlink — we explicitly don't fight
    that path by spawning concurrently.

    Spawn is detached so the hook never blocks SessionStart on the
    activate cold start. Best-effort throughout — never raises into the
    hook.
    """
    try:
        stable = current_bin_path()
    except Exception:  # noqa: BLE001
        return

    # ── stage 1: symlink reconcile ───────────────────────────────────
    before = os.path.realpath(stable) if stable.exists() else None
    try:
        ensure_current_symlink(cache_path)
    except Exception as exc:  # noqa: BLE001 — never block SessionStart
        _log(f"current-pointer reconcile failed ({type(exc).__name__}: {exc})")
        return
    after = os.path.realpath(stable) if stable.exists() else None
    symlink_moved = (before is not None and before != after)

    # ── stage 2: running-version verify ──────────────────────────────
    running_version = read_running_sidecar_version()
    if running_version is None:
        # No observable sidecar — supervisor's KeepAlive will respawn
        # against the freshly-pointed stable symlink. Don't compete.
        if symlink_moved:
            _log(f"current pointer repaired → {cache_path.parent}; "
                 f"sidecar absent — supervisor KeepAlive will pick up")
        return

    if running_version == expected_version:
        # Fully converged. The common case for unchanged installs.
        if symlink_moved:
            _log(f"current pointer repaired → {cache_path.parent}; "
                 f"sidecar already on {expected_version}")
        return

    # Real mismatch: symlink points at expected_version but the running
    # sidecar is still some older binary. Kick the supervisor.
    _log(f"sidecar runs {running_version!r}, expected {expected_version!r}; "
         f"spawning detached service-activate")
    ok, route = trigger_service_activate(cache_path, stable, detached=True)
    if not ok:
        _log("service-activate spawn failed; supervisor's next respawn "
             "will pick up the new bundle naturally")
    elif route != "service-activate":
        # detached=True only spawns service-activate (no synchronous
        # fallback path) — route should always be "service-activate" on
        # success. Logged here for observability if that ever changes.
        _log(f"detached activate took route={route}")


def main() -> int:
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
    # `format` == "tar.xz" → the asset is an onedir bundle archive the
    # runner extracts; absent / "raw" → a legacy single-file binary.
    fmt = manifest.get("format")
    if not (expected_sha and expected_version and url):
        _log("resolved release missing required fields (sha256/version/url); skipping")
        return 0

    # Record the resolved version for the per-tool-call readiness gate. The
    # gate reads this pointer instead of re-resolving, so its hot path stays
    # local (no channel fetch). Best-effort — never block bootstrap on it.
    write_expected_release(expected_version, target)

    cache_path = cache_bin_path(expected_version, target)
    status_path = bin_status_path(expected_version, target)
    lock_path = bin_lock_path(expected_version, target)

    # Cache hit → nothing to do.
    #   onedir (tar.xz): trust the fetcher's atomic-install record — the
    #     bundle executable is present and the statefile says `ready` with
    #     the verified archive sha. Re-hashing a ~100MB bundle on every
    #     SessionStart is too costly, and the archive sha is what
    #     manifest/channel pin anyway.
    #   raw (legacy fallback): hash the single-file binary directly and
    #     reconcile the statefile to `ready` if a prior run left it stale.
    if fmt == "tar.xz":
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
    # the runner discover ENOSPC mid-stream. A tar.xz onedir asset needs
    # room for the archive download + the ~3x-larger extracted bundle.
    size_floor = expected_size or 100 * (1 << 20)
    needed_mb = size_floor * (4 if fmt == "tar.xz" else 1) // (1 << 20) + 50
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

        runner = Path(__file__).parent / "_bin_fetch_runner.py"
        if not runner.is_file():
            _log(f"runner script missing: {runner}; skipping")
            return 0

        log_file = _resolve_log_file()
        _spawn_detached_runner(
            runner,
            url=url,
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
