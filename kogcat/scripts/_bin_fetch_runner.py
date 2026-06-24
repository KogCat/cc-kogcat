#!/usr/bin/env python3
"""Detached om-core binary fetch runner — spawned by bootstrap_om_core_bin.py.

Runs in the background long after SessionStart returns, owning the actual
binary download, sha verification, and — for onedir `tar.xz` / `zip`
assets — extraction + atomic bundle swap. Writes progress through a statefile so
`tools.py`'s binary readiness gate can surface `OM_CORE_BIN_DOWNLOADING`
hints.

Why detached:
  This used to be a synchronous urllib loop inside the SessionStart
  hook. Two problems forced the move to detached:
    1. Claude Code sends SIGTERM to SessionStart hooks that exceed its
       timeout — slow networks left `*.tmp` orphans.
    2. CC's loading-phase UI suppresses hook stdout/stderr (host
       limitation). The synchronous progress prints we wrote there were
       never visible to the user.
  Detached + statefile + MCP-hint gate is the only feedback channel
  Claude Code permits for plugin-side downloads.

Anti-stall design (why this isn't just a urllib copy loop):
  - Per-chunk read timeout via `urlopen(timeout=N)`. CPython propagates N
    to both `socket.create_connection` AND the resulting socket's
    `settimeout`, so subsequent `resp.read()` calls raise `socket.timeout`
    when no bytes arrive within N seconds. Without this the read blocks
    on OS-level keepalive (often minutes) — this was the actual silent
    hang failure mode.
  - On stall: log + reconnect with `Range: bytes=N-` to resume from
    where the previous attempt left off. Up to 3 retries; the partial
    file is preserved between attempts so a flaky network eventually
    completes instead of starting over.
  - sha256 verified end-to-end after the final chunk lands (any tampered
    range from a misbehaving CDN gets caught here, not at first use).

Statefile schema (single file `<cache>/<version>/<target>.status.json`):

  start      →  {state: "downloading", started_at, pid, version,
                 bytes_downloaded: 0, total_bytes_hint, last_progress_at}
  per chunk  →  rewrite with bytes_downloaded + last_progress_at
                (rate-limited to once per 500ms — file rename is cheap
                but not free, and the MCP gate only reads this on each
                tool call anyway)
  success    →  {state: "ready", finished_at, bytes_downloaded=size, ...}
  failure    →  {state: "failed", finished_at, reason, bytes_downloaded, ...}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

CHUNK_SIZE = 1 << 20            # 1 MiB
SOCKET_TIMEOUT_S = 30.0         # CPython urlopen applies this to connect AND
                                # subsequent socket reads — the latter is
                                # what catches mid-stream stalls (the actual
                                # silent-hang failure mode this runner exists
                                # to prevent).
MAX_RESUME_RETRIES = 3
# Throughput floor: SOCKET_TIMEOUT_S only catches a fully frozen socket. A link
# that trickles bytes (CN-mainland OSS reached through a foreign-exit VPN) keeps
# the read alive forever. Abandon a sustained-below-floor transfer so the run
# fails fast (next trigger re-resolves the channel) instead of crawling.
MIN_RATE_BPS = 40 * 1024         # 40 KB/s
RATE_WINDOW_S = 20.0             # measure rate over this window before judging
PROGRESS_WRITE_INTERVAL_S = 0.5  # min wall time between statefile rewrites


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_status(statefile: Path, payload: dict) -> None:
    """Atomic write — temp file + rename, so readers never see a partial JSON."""
    statefile.parent.mkdir(parents=True, exist_ok=True)
    tmp = statefile.with_suffix(statefile.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, statefile)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


@lru_cache(maxsize=1)
def _resolve_github_token() -> str | None:
    """Resolve a GitHub PAT from env (OM_CORE_BIN_TOKEN > GITHUB_TOKEN > None).

    The public binary host serves every asset anonymously; an env token only
    lifts the per-IP rate limit and lets an explicit override reach a private
    asset. Env-only by design — no subprocess can hang the detached fetcher.
    """
    for env_name in ("OM_CORE_BIN_TOKEN", "GITHUB_TOKEN"):
        if (v := os.environ.get(env_name, "").strip()):
            return v
    return None


def _open_with_chunk_timeout(
    url: str, range_start: int, total_size_hint: int,
) -> tuple[urllib.request.addinfourl, int]:
    """Open `url` with optional Range header, return (response, total_size).

    Per-chunk timeout: CPython's `urlopen(timeout=N)` propagates N to the
    underlying `socket.create_connection` call AND to the resulting
    socket's `settimeout` — so subsequent `resp.read(CHUNK_SIZE)` calls
    raise `socket.timeout` when no bytes arrive within N seconds. That's
    the actual silent-hang fix; without this the read would block on OS
    keepalive (often minutes).

    Total size: prefers Content-Length, falls back to the manifest hint
    (used for percentage display only — not used as a stop condition).

    Authorization: when `_resolve_github_token` finds an env token it's
    attached as `Authorization: Bearer <token>` (raises the per-IP rate
    limit; the public host serves anonymously either way). CPython drops
    Authorization across host boundaries, so the eventual S3 redirect for
    the asset blob stays anonymous.
    """
    headers = {"User-Agent": "om-bin-fetch/1.0"}
    if (token := _resolve_github_token()):
        headers["Authorization"] = f"Bearer {token}"
    if range_start > 0:
        headers["Range"] = f"bytes={range_start}-"
    req = urllib.request.Request(url, headers=headers)
    resp = urllib.request.urlopen(req, timeout=SOCKET_TIMEOUT_S)  # noqa: S310 — manifest URL

    content_length = resp.headers.get("Content-Length")
    if range_start > 0 and resp.status == 206:
        # Server honored the Range: total = range_start + body_length.
        body_len = int(content_length) if content_length else max(total_size_hint - range_start, 0)
        total = range_start + body_len
    elif content_length:
        total = int(content_length)
    else:
        total = total_size_hint
    return resp, total


def _download_with_resume(
    url: str,
    tmp_path: Path,
    expected_size: int,
    on_progress,
) -> int:
    """Stream URL to tmp_path with chunk-timeout + Range-resume retries.

    `on_progress(bytes_downloaded)` fires every chunk for the heartbeat
    statefile. Returns total bytes written.

    Retry policy:
      - Up to MAX_RESUME_RETRIES reconnects on socket.timeout / URLError.
      - Each reconnect sends `Range: bytes=N-` from the current file size.
      - tmp_path is opened in "ab" mode after the first attempt so writes
        append to whatever previous attempts already saved.
      - sha256 verification happens in the caller, after this returns —
        if a misbehaving server fed us garbage on a Range request the
        sha catches it.
    """
    attempt = 0
    while True:
        attempt += 1
        bytes_so_far = tmp_path.stat().st_size if tmp_path.exists() else 0
        try:
            resp, total = _open_with_chunk_timeout(url, bytes_so_far, expected_size)
        except (urllib.error.URLError, OSError) as e:
            if attempt > MAX_RESUME_RETRIES:
                raise
            print(
                f"[om-core-runner] connect failed (attempt {attempt}/{MAX_RESUME_RETRIES}): {e}; retrying",
                file=sys.stderr, flush=True,
            )
            continue

        # Open file in append-binary so Range resumes add onto prior bytes.
        # First attempt also uses "ab" — empty file at start, identical effect.
        try:
            window_t0 = time.monotonic()
            window_b0 = bytes_so_far
            with resp, tmp_path.open("ab") as f:
                while True:
                    try:
                        chunk = resp.read(CHUNK_SIZE)
                    except (socket.timeout, TimeoutError) as e:
                        if attempt > MAX_RESUME_RETRIES:
                            raise
                        print(
                            f"[om-core-runner] read stalled at "
                            f"{bytes_so_far / (1 << 20):.1f}MB (attempt "
                            f"{attempt}/{MAX_RESUME_RETRIES}): {e}; resuming via Range",
                            file=sys.stderr, flush=True,
                        )
                        break  # break inner loop → outer loop reconnects
                    if not chunk:
                        # Clean EOF — verify size matches expectation.
                        if total and bytes_so_far < total:
                            # Server closed early. Treat as a stall and resume.
                            if attempt > MAX_RESUME_RETRIES:
                                raise OSError(
                                    f"server closed connection at "
                                    f"{bytes_so_far}/{total} bytes after "
                                    f"{MAX_RESUME_RETRIES} retries"
                                )
                            print(
                                f"[om-core-runner] short read "
                                f"{bytes_so_far}/{total} (attempt "
                                f"{attempt}/{MAX_RESUME_RETRIES}); resuming",
                                file=sys.stderr, flush=True,
                            )
                            break  # outer loop reconnects via Range
                        return bytes_so_far  # success
                    f.write(chunk)
                    bytes_so_far += len(chunk)
                    on_progress(bytes_so_far, total)
                    elapsed = time.monotonic() - window_t0
                    if elapsed >= RATE_WINDOW_S:
                        rate = (bytes_so_far - window_b0) / elapsed
                        if rate < MIN_RATE_BPS:
                            if attempt > MAX_RESUME_RETRIES:
                                raise OSError(
                                    f"throughput {rate / 1024:.1f}KB/s below floor "
                                    f"after {MAX_RESUME_RETRIES} retries"
                                )
                            print(
                                f"[om-core-runner] throughput {rate / 1024:.0f}KB/s "
                                f"below floor at {bytes_so_far / (1 << 20):.1f}MB "
                                f"(attempt {attempt}/{MAX_RESUME_RETRIES}); reconnecting",
                                file=sys.stderr, flush=True,
                            )
                            break  # outer loop reconnects via Range
                        window_t0 = time.monotonic()
                        window_b0 = bytes_so_far
        except (urllib.error.URLError, OSError) as e:
            if attempt > MAX_RESUME_RETRIES:
                raise
            print(
                f"[om-core-runner] transfer error (attempt {attempt}/{MAX_RESUME_RETRIES}): {e}; resuming",
                file=sys.stderr, flush=True,
            )
            continue


def _extract_archive(archive: Path, dest_dir: Path, fmt: str) -> None:
    """Extract a onedir bundle archive into `dest_dir` (members at the root).

    tar.xz (macOS/Linux) and zip (Windows) — both stdlib codecs, so the
    runner never shells out to `tar` / PowerShell. Windows ships zip because
    the bundled `tar.exe`'s xz codec is unreliable across Win10 versions.
    """
    if fmt == "zip":
        import zipfile  # noqa: PLC0415 — stdlib, lazy

        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest_dir)
        return
    import tarfile  # noqa: PLC0415 — stdlib, lazy

    with tarfile.open(archive, "r:xz") as tf:
        tf.extractall(dest_dir, filter="data")


def _install_onedir_bundle(archive: Path, dest: Path, fmt: str) -> None:
    """Extract an onedir bundle archive and atomically swap it into place.

    `dest` is the executable path inside the bundle
    (`<cache>/<version>/<target>/om-core-bin[.exe]`); `dest.parent` is the
    bundle dir. The archive carries the bundle's members at its root — the
    executable plus `_internal/`.

    Extract to a staging sibling dir, then rename it into the bundle path.
    A running sidecar keeps its old mmap'd inodes alive across the rename:
    the new bundle is an entirely fresh tree, so no in-use file is ever
    truncated (the failure mode a plain extract-in-place would cause).
    """
    bundle_dir = dest.parent
    version_dir = bundle_dir.parent
    staging = version_dir / f".extract.{bundle_dir.name}.tmp"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        _extract_archive(archive, staging, fmt)
        exe = staging / dest.name
        if not exe.is_file():
            raise OSError(
                f"archive missing expected executable {dest.name!r} at its root"
            )
        os.chmod(exe, 0o755)
        # Atomic swap: move any existing bundle aside, rename staging in.
        old: Path | None = None
        if bundle_dir.exists():
            old = bundle_dir.with_name(f".old.{bundle_dir.name}.{os.getpid()}")
            os.replace(bundle_dir, old)
        try:
            os.replace(staging, bundle_dir)
        except OSError:
            if old is not None:
                os.replace(old, bundle_dir)  # roll back
            raise
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def run_fetch(
    *,
    urls: list[str],
    dest: Path,
    sha256: str,
    size: int,
    status_file: Path,
    version: str,
    fmt: str,
    log,
) -> int:
    """Fetch → verify → install the om-core binary. Returns 0 ready / 1 failed.

    `urls` are byte-identical asset mirrors in priority order (channel-race
    winner first, other host as fallback); the shared sha256 gate verifies
    whichever one delivered. A mirror that errors or trickles below the
    throughput floor advances to the next; the `.part` is kept so the next
    mirror Range-resumes from where the last left off.

    Shared by the detached CLI (`main`) and the in-process self-heal thread
    (`om_mcp.self_heal`). Every diagnostic goes through `log(str)` — detached
    passes a --log-file writer, in-process passes trigger_log.

    MUST NOT write to stdout: run in-process inside the MCP server, stdout is
    the JSON-RPC channel and any stray byte corrupts the protocol frame.
    """
    dest = Path(dest)
    statefile = Path(status_file)
    is_archive = fmt in ("tar.xz", "zip")
    bundle_dir = dest.parent
    version_dir = bundle_dir.parent
    version_dir.mkdir(parents=True, exist_ok=True)
    if not is_archive:
        bundle_dir.mkdir(parents=True, exist_ok=True)

    started = _utc_now_iso()
    pid = os.getpid()
    log(f"fetch start version={version} format={fmt} pid={pid}")
    base_state = {
        "state": "downloading",
        "started_at": started,
        "pid": pid,
        "version": version,
        "bytes_downloaded": 0,
        "total_bytes_hint": size or None,
        "last_progress_at": started,
    }
    _write_status(statefile, base_state)

    # Persistent resume tmp lives in version_dir (sibling of the bundle dir,
    # same fs as dest) — never inside the bundle dir (must stay a pure tree
    # for the atomic onedir swap).
    tmp_suffix = f".{fmt}.part" if is_archive else ".bin.part"
    tmp_path = version_dir / f".om-core-fetch.{bundle_dir.name}.{version}{tmp_suffix}"

    last_write = [0.0]

    def on_progress(bytes_now: int, total: int | None) -> None:
        now = time.monotonic()
        if now - last_write[0] < PROGRESS_WRITE_INTERVAL_S and bytes_now != total:
            return
        last_write[0] = now
        payload = dict(base_state)
        payload["bytes_downloaded"] = bytes_now
        if total:
            payload["total_bytes_hint"] = total
        payload["last_progress_at"] = _utc_now_iso()
        try:
            _write_status(statefile, payload)
        except OSError:
            pass

    try:
        last_err: Exception | None = None
        for mirror in urls:
            try:
                bytes_written = _download_with_resume(
                    mirror, tmp_path, size or 0, on_progress
                )
                break  # mirror delivered — sha gate below verifies it
            except Exception as e:  # noqa: BLE001 — try the next mirror
                last_err = e
                log(f"mirror failed ({mirror}): {type(e).__name__}: {e}; next")
        else:
            raise last_err or OSError("no download mirrors")
    except Exception as e:  # noqa: BLE001 — capture any failure for the gate
        log(traceback.format_exc())
        bytes_so_far = tmp_path.stat().st_size if tmp_path.exists() else 0
        reason = f"{type(e).__name__}: {e}"
        _write_status(statefile, {
            "state": "failed",
            "started_at": started,
            "finished_at": _utc_now_iso(),
            "pid": pid,
            "version": version,
            "reason": reason,
            "bytes_downloaded": bytes_so_far,
            "total_bytes_hint": size or None,
            "last_progress_at": _utc_now_iso(),
        })
        return 1  # leave tmp on disk — next run resumes from here

    actual_sha = _sha256(tmp_path)
    if actual_sha != sha256:
        _write_status(statefile, {
            "state": "failed",
            "started_at": started,
            "finished_at": _utc_now_iso(),
            "pid": pid,
            "version": version,
            "reason": f"sha256 mismatch (expected {sha256[:12]}…, got {actual_sha[:12]}…)",
            "bytes_downloaded": bytes_written,
            "total_bytes_hint": size or None,
            "last_progress_at": _utc_now_iso(),
        })
        try:
            tmp_path.unlink()
        except OSError:
            pass
        return 1

    if is_archive:
        try:
            _install_onedir_bundle(tmp_path, dest, fmt)
        except Exception as e:  # noqa: BLE001 — surface to the readiness gate
            log(traceback.format_exc())
            _write_status(statefile, {
                "state": "failed",
                "started_at": started,
                "finished_at": _utc_now_iso(),
                "pid": pid,
                "version": version,
                "reason": f"bundle extract/install failed: {type(e).__name__}: {e}",
                "bytes_downloaded": bytes_written,
                "total_bytes_hint": size or None,
                "last_progress_at": _utc_now_iso(),
            })
            try:
                tmp_path.unlink()
            except OSError:
                pass
            return 1
        try:
            tmp_path.unlink()
        except OSError:
            pass
    else:
        os.chmod(tmp_path, 0o755)
        os.replace(tmp_path, dest)

    # Swap stable symlink + activate supervisor. Best-effort; the self-heal
    # loop / next SessionStart converges if this hop is interrupted.
    activated = False
    activate_route = "none"
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import om_core_paths as _ocp  # noqa: PLC0415
        import om_supervisor as _osup  # noqa: PLC0415
        stable = _ocp.ensure_current_symlink(dest)
        status = _osup.service_status(dest)
        if status is not None and not status.get("installed"):
            activated, activate_route = _osup.trigger_install_service(
                dest, stable, detached=False,
            )
        else:
            activated, activate_route = _osup.trigger_service_activate(
                dest, stable, detached=False,
            )
        if activated:
            log(f"service activated → {stable} → {dest} (route={activate_route})")
        else:
            log("service activation failed; supervisor respawn will pick up the new bundle")
    except Exception as e:  # noqa: BLE001
        log(f"symlink/activate hop failed ({type(e).__name__}: {e}); supervisor respawn will pick up")

    finished = _utc_now_iso()
    _write_status(statefile, {
        "state": "ready",
        "started_at": started,
        "finished_at": finished,
        "pid": pid,
        "version": version,
        "bytes_downloaded": bytes_written,
        "total_bytes_hint": bytes_written,
        "last_progress_at": finished,
        "activated": activated,
        "archive_sha256": sha256,
    })
    log(f"binary ready at {dest}")

    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import om_core_paths as _ocp_prune  # noqa: PLC0415
        removed = _ocp_prune.prune_cache(keep=3)
        if removed:
            log(f"pruned old bundles: {removed}")
    except Exception as e:  # noqa: BLE001 — pruning must never fail the fetch
        log(f"cache prune skipped ({type(e).__name__}: {e})")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, nargs="+",
                        help="one or more byte-identical asset mirrors, priority order")
    parser.add_argument("--dest", required=True, help="final cache path for verified binary")
    parser.add_argument("--sha256", required=True, help="expected sha256 of complete binary")
    parser.add_argument("--size", type=int, default=0, help="expected size in bytes (manifest hint)")
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument(
        "--format", default="raw",
        help="'tar.xz' / 'zip' → onedir bundle archive (extract + atomic "
             "swap); 'raw' (default) → legacy single-file binary",
    )
    parser.add_argument(
        "--log-file", default=None,
        help="self-open this log instead of inheriting stdout — required for "
             "WMI-spawned runs (job-breakaway fallback) which can't inherit fds",
    )
    args = parser.parse_args()

    if args.log_file:
        try:
            _lf = open(args.log_file, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
            sys.stdout = _lf
            sys.stderr = _lf
        except OSError:
            pass

    # Detached CLI routes run_fetch diagnostics to the redirected stderr
    # (= --log-file). In-process callers pass trigger_log instead.
    def _log(msg: str) -> None:
        print(f"[om-core-runner] {msg}", file=sys.stderr, flush=True)

    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from om_supervisor import trigger_log as _tlog  # noqa: PLC0415
        _tlog(f"runner main() started version={args.version} format={args.format}")
    except Exception:  # noqa: BLE001 — breadcrumb must never break the runner
        pass

    return run_fetch(
        urls=args.url,
        dest=Path(args.dest),
        sha256=args.sha256,
        size=args.size,
        status_file=Path(args.status_file),
        version=args.version,
        fmt=args.format,
        log=_log,
    )


if __name__ == "__main__":
    sys.exit(main())
