#!/usr/bin/env python3
"""Detached om-core binary fetch runner — spawned by bootstrap_om_core_bin.py.

Runs in the background long after SessionStart returns, owning the actual
binary download, sha verification, and — for onedir `tar.xz` assets —
extraction + atomic bundle swap. Writes progress through a statefile so
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
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

# CLI search locations augmented onto runtime PATH for `gh` lookup.
# The runner is spawned by SessionStart → bootstrap → detached process,
# inheriting whatever PATH the host gave Claude Code. Under launchd /
# Claude Desktop that's the bare `/usr/bin:/bin`, missing every common
# user-installed CLI prefix. Hardcode the well-known prefixes so the
# `gh auth token` fallback works regardless of spawn topology.
_USER_CLI_PREFIXES = (
    "/opt/homebrew/bin",   # Apple Silicon Homebrew
    "/opt/homebrew/sbin",
    "/usr/local/bin",      # Intel Homebrew / standard
    "/usr/local/sbin",
    "/opt/local/bin",      # MacPorts
    "/opt/local/sbin",
    os.path.expanduser("~/.local/bin"),  # pipx / pip --user / nix
)

CHUNK_SIZE = 1 << 20            # 1 MiB
SOCKET_TIMEOUT_S = 30.0         # CPython urlopen applies this to connect AND
                                # subsequent socket reads — the latter is
                                # what catches mid-stream stalls (the actual
                                # silent-hang failure mode this runner exists
                                # to prevent).
MAX_RESUME_RETRIES = 3
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


def _find_gh_binary() -> str | None:
    """Locate the `gh` CLI absolute path, searching beyond runtime PATH.

    SessionStart hook → bootstrap → runner inherits a minimal PATH under
    launchd / Claude Desktop spawn (just `/usr/bin:/bin`). Anything
    installed by Homebrew / MacPorts / pipx / nix lives outside that
    PATH, so a naive `subprocess.run(["gh", ...])` raises FileNotFoundError
    even when the user clearly has `gh` configured. shutil.which with an
    augmented search path covers the common user-install prefixes
    explicitly without mutating the process-wide PATH (which would leak
    into every later subprocess and is unnecessary here).
    """
    search_path = os.pathsep.join(
        [os.environ.get("PATH", ""), *_USER_CLI_PREFIXES]
    )
    return shutil.which("gh", path=search_path)


@lru_cache(maxsize=1)
def _resolve_github_token() -> str | None:
    """Find a GitHub PAT for private-repo asset downloads. Cached per-process.

    Resolution order (single source of truth, no double-bookkeeping):
      1. OM_CORE_BIN_TOKEN env — explicit override scoped to this fetcher
      2. GITHUB_TOKEN env — generic GitHub PAT, conventional in CI
      3. `gh auth token` subprocess — single-source-of-truth for any user
         already logged in via `gh auth login`. Reads from keychain /
         keyring / file store transparently. The gh binary is located via
         `_find_gh_binary()` which searches common user-install prefixes,
         not just runtime PATH (Claude Desktop / launchd spawn gives the
         runner a bare `/usr/bin:/bin` that misses Homebrew etc).
      4. None — anonymous fetch (works for public repos)

    All errors swallowed: a missing token must never crash the runner;
    the resulting anonymous request will surface a clean 401/404 instead,
    which higher layers (statefile state=failed, readiness gate) report
    to the user with actionable hints.

    The lru_cache means the gh subprocess fires at most once per fetcher
    process, even across resume retries.
    """
    for env_name in ("OM_CORE_BIN_TOKEN", "GITHUB_TOKEN"):
        if (v := os.environ.get(env_name, "").strip()):
            return v
    gh_bin = _find_gh_binary()
    if not gh_bin:
        return None
    try:
        proc = subprocess.run(
            [gh_bin, "auth", "token"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if proc.returncode == 0 and (token := proc.stdout.strip()):
            return token
    except (subprocess.SubprocessError, OSError):
        pass
    return None


_RELEASE_URL_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/]+)/releases/download/([^/]+)/(.+)$"
)


def _parse_release_url(url: str) -> tuple[str, str, str, str] | None:
    """Parse a GitHub release-asset web URL → (owner, repo, tag, asset).

    Returns None for non-matching URLs (custom CDNs, signed S3 URLs, etc.) so
    callers can fall through to the urllib path unchanged.
    """
    m = _RELEASE_URL_RE.match(url)
    return m.groups() if m else None  # type: ignore[return-value]


def _try_download_via_gh(
    url: str, tmp_path: Path, expected_size: int, on_progress,
) -> int | None:
    """Try `gh release download` first; return bytes_written on success,
    None when the gh path is not applicable (no gh binary on PATH, URL
    doesn't match the GH release-asset shape).

    Why the gh path is mandatory for **private** repos: the public web URL
    `github.com/<o>/<r>/releases/download/<tag>/<asset>` 302-redirects to a
    short-lived signed S3 URL, but CPython's redirect handler intentionally
    drops Authorization across host boundaries (S3 rejects GitHub PATs
    anyway), so the redirect target arrives anonymous and S3 returns 404.
    `gh release download` instead resolves the asset id via
    `/repos/<o>/<r>/releases/tags/<tag>` and fetches via
    `/repos/<o>/<r>/releases/assets/<id>` with `Accept:
    application/octet-stream`, which honors the user's `gh auth login`
    token (keychain / keyring / file store) and follows the storage
    redirect server-side. Public repos work either way; gh is preferred
    there too because it auto-resumes and handles GH rate-limits.

    Failures bubble up as OSError so the caller can fall back to the
    urllib path (gh missing, network outage, version skew on the gh
    binary, etc.). The lru-cached token lookup in `_resolve_github_token`
    is intentionally NOT consulted here: gh reads its own credential
    store, single-source-of-truth.
    """
    parsed = _parse_release_url(url)
    if not parsed:
        return None
    gh_bin = _find_gh_binary()
    if not gh_bin:
        return None
    owner, repo, tag, asset = parsed

    # Stream into the same .tmp path the urllib path uses so atomic-rename
    # in the caller works either way. --clobber so a stale partial from a
    # prior attempt is overwritten cleanly.
    proc = subprocess.Popen(
        [
            gh_bin, "release", "download", tag,
            "--repo", f"{owner}/{repo}",
            "--pattern", asset,
            "--output", str(tmp_path),
            "--clobber",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    # Poll loop — gh has its own progress UI (suppressed via stderr=PIPE)
    # but doesn't expose machine-readable progress, so we stat the tmp
    # file ourselves to drive the heartbeat statefile. The MCP readiness
    # gate consumes this same statefile, so progress visibility stays
    # symmetric with the urllib path.
    while True:
        rc = proc.poll()
        if tmp_path.exists():
            try:
                on_progress(tmp_path.stat().st_size, expected_size or None)
            except OSError:
                pass
        if rc is not None:
            break
        time.sleep(PROGRESS_WRITE_INTERVAL_S)

    if proc.returncode != 0:
        stderr_bytes = b""
        try:
            stderr_bytes = proc.stderr.read() if proc.stderr else b""
        except OSError:
            pass
        msg = stderr_bytes.decode(errors="replace").strip() or "(no stderr)"
        # Common failure mode users can act on: gh not authenticated to a
        # private repo. Surface a tight one-line reason; full stderr already
        # went to the runner log via _resolve_log_file.
        raise OSError(
            f"gh release download {tag}/{asset} failed (rc={proc.returncode}): "
            f"{msg.splitlines()[0] if msg else ''}"
        )

    return tmp_path.stat().st_size if tmp_path.exists() else 0


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

    Authorization: when `_resolve_github_token` finds a token (env or gh
    auth), it's attached as `Authorization: Bearer <token>`. This makes
    private-repo release assets fetchable for any user with read access
    (dev / internal team). Public repos work either way — GitHub ignores
    the header on anonymous-allowed endpoints. CPython's default redirect
    handler does not propagate Authorization across host boundaries, so
    the eventual S3 redirect for the asset blob stays anonymous (S3
    rejects GitHub PATs).
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
        except (urllib.error.URLError, OSError) as e:
            if attempt > MAX_RESUME_RETRIES:
                raise
            print(
                f"[om-core-runner] transfer error (attempt {attempt}/{MAX_RESUME_RETRIES}): {e}; resuming",
                file=sys.stderr, flush=True,
            )
            continue


def _install_onedir_bundle(archive: Path, dest: Path) -> None:
    """Extract a tar.xz onedir archive and atomically swap it into place.

    `dest` is the executable path inside the bundle
    (`<cache>/<version>/<target>/om-core-bin`); `dest.parent` is the bundle
    dir. The archive carries the bundle's members at its root — the
    executable plus `_internal/`.

    Extract to a staging sibling dir, then rename it into the bundle path.
    A running sidecar keeps its old mmap'd inodes alive across the rename:
    the new bundle is an entirely fresh tree, so no in-use file is ever
    truncated (the failure mode a plain extract-in-place would cause).
    """
    import tarfile  # noqa: PLC0415 — stdlib, lazy

    bundle_dir = dest.parent
    version_dir = bundle_dir.parent
    staging = version_dir / f".extract.{bundle_dir.name}.tmp"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        with tarfile.open(archive, "r:xz") as tf:
            tf.extractall(staging, filter="data")
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--dest", required=True, help="final cache path for verified binary")
    parser.add_argument("--sha256", required=True, help="expected sha256 of complete binary")
    parser.add_argument("--size", type=int, default=0, help="expected size in bytes (manifest hint)")
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument(
        "--format", default="raw",
        help="'tar.xz' → onedir bundle archive (extract + atomic swap); "
             "'raw' (default) → legacy single-file binary",
    )
    args = parser.parse_args()

    dest = Path(args.dest)
    statefile = Path(args.status_file)
    is_archive = args.format == "tar.xz"
    bundle_dir = dest.parent          # <cache>/<version>/<target>/
    version_dir = bundle_dir.parent   # <cache>/<version>/
    version_dir.mkdir(parents=True, exist_ok=True)
    if not is_archive:
        # Legacy raw binary lands directly in the bundle dir.
        bundle_dir.mkdir(parents=True, exist_ok=True)

    started = _utc_now_iso()
    pid = os.getpid()
    base_state = {
        "state": "downloading",
        "started_at": started,
        "pid": pid,
        "version": args.version,
        "bytes_downloaded": 0,
        "total_bytes_hint": args.size or None,
        "last_progress_at": started,
    }
    _write_status(statefile, base_state)

    # Persistent tmp file across resume attempts. Lives in `version_dir`
    # (a sibling of the bundle dir), same filesystem as the final dest so
    # the eventual rename is rename-only — and crucially NOT inside the
    # bundle dir, which must stay a pure tree for the atomic onedir swap.
    tmp_suffix = ".tar.xz.part" if is_archive else ".bin.part"
    tmp_path = version_dir / f".om-core-fetch.{bundle_dir.name}.{args.version}{tmp_suffix}"

    # Throttle statefile rewrites: file rename is atomic but not free, and
    # the wrapper gate only reads this on tool calls. 500ms cadence is
    # plenty for a human-perceptible UI without spamming the FS.
    last_write = [0.0]  # mutable closure cell

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
            pass  # next tick may succeed; never let progress IO kill the download

    bytes_written: int | None = None
    gh_error: BaseException | None = None
    try:
        bytes_written = _try_download_via_gh(
            args.url, tmp_path, args.size or 0, on_progress,
        )
    except Exception as e:  # noqa: BLE001 — capture for fallback decision
        # gh path attempted and failed (gh present + matching URL but
        # subprocess returned non-zero). Log + try urllib resume; if
        # urllib also fails, the gh failure is what users typically need
        # to act on (e.g. `gh auth login` for private repos), so we
        # surface it in the final reason string.
        gh_error = e
        traceback.print_exc(file=sys.stderr)
        # Wipe partial gh output so urllib starts from byte 0 instead of
        # trying to resume something gh was mid-write to.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass

    try:
        if bytes_written is None:
            bytes_written = _download_with_resume(
                args.url, tmp_path, args.size or 0, on_progress,
            )
    except Exception as e:  # noqa: BLE001 — capture any failure for the gate
        traceback.print_exc(file=sys.stderr)
        bytes_so_far = tmp_path.stat().st_size if tmp_path.exists() else 0
        # When both paths failed, prefer the gh-path reason: that's the
        # one users can typically act on (`gh auth login`, install gh,
        # check repo access). urllib failures on private-repo assets are
        # the symptom, not the root cause.
        reason = f"{type(e).__name__}: {e}"
        if gh_error is not None:
            reason = f"gh: {type(gh_error).__name__}: {gh_error} | urllib: {reason}"
        _write_status(statefile, {
            "state": "failed",
            "started_at": started,
            "finished_at": _utc_now_iso(),
            "pid": pid,
            "version": args.version,
            "reason": reason,
            "bytes_downloaded": bytes_so_far,
            "total_bytes_hint": args.size or None,
            "last_progress_at": _utc_now_iso(),
        })
        # Leave tmp on disk — next run can resume from where we stopped.
        return 1

    # sha verify before atomic rename. If a CDN served bad bytes on a
    # Range request, the partial content might still hash to something
    # plausible per chunk but the full sha will mismatch.
    actual_sha = _sha256(tmp_path)
    if actual_sha != args.sha256:
        _write_status(statefile, {
            "state": "failed",
            "started_at": started,
            "finished_at": _utc_now_iso(),
            "pid": pid,
            "version": args.version,
            "reason": f"sha256 mismatch (expected {args.sha256[:12]}…, got {actual_sha[:12]}…)",
            "bytes_downloaded": bytes_written,
            "total_bytes_hint": args.size or None,
            "last_progress_at": _utc_now_iso(),
        })
        # Bad bytes — wipe so next run starts clean rather than resuming
        # what's clearly corrupted.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        return 1

    if is_archive:
        try:
            _install_onedir_bundle(tmp_path, dest)
        except Exception as e:  # noqa: BLE001 — surface to the readiness gate
            traceback.print_exc(file=sys.stderr)
            _write_status(statefile, {
                "state": "failed",
                "started_at": started,
                "finished_at": _utc_now_iso(),
                "pid": pid,
                "version": args.version,
                "reason": f"bundle extract/install failed: {type(e).__name__}: {e}",
                "bytes_downloaded": bytes_written,
                "total_bytes_hint": args.size or None,
                "last_progress_at": _utc_now_iso(),
            })
            try:
                tmp_path.unlink()
            except OSError:
                pass
            return 1
        # Archive consumed — the extracted bundle is the artifact now.
        try:
            tmp_path.unlink()
        except OSError:
            pass
    else:
        os.chmod(tmp_path, 0o755)
        os.replace(tmp_path, dest)

    # Stable-pointer activation (om-core ≥ 0.28.0):
    # 1. Atomically swap `<cache>/current/<target>/om-core-bin` symlink to
    #    point at this freshly-downloaded versioned binary. The supervisor
    #    plist / unit / SCM entry is registered against the stable symlink
    #    once at install-service time and never changes — so no plist
    #    rewrite is needed on upgrade.
    # 2. Call `<dest> service-activate` via om_supervisor.trigger_service_activate
    #    so the supervisor restarts under the new symlink target. New
    #    binary's own CLI is the primary actor (it knows the latest
    #    activation logic for its own version); if that subprocess fails
    #    (timeout, the new binary won't launch, etc.) the helper falls
    #    back to a direct supervisor call — `launchctl kickstart -k` on
    #    macOS, `systemctl --user restart` on Linux, `sc.exe stop+start`
    #    on Windows.
    #
    # Both steps best-effort: if anything fails the ready state is still
    # written (the binary is on disk and verified) and the bootstrap
    # SessionStart verify-and-spawn loop converges on the next CC launch
    # (running-version vs expected mismatch → detached service-activate).
    activated = False
    activate_route = "none"
    try:
        # Local import — om_core_paths + om_supervisor live in plugin's
        # scripts/, runtime path is augmented by the installer / wrapper.
        # Try-block keeps the ready state writable even on a busted plugin
        # tree.
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import om_core_paths as _ocp  # noqa: PLC0415
        import om_supervisor as _osup  # noqa: PLC0415
        stable = _ocp.ensure_current_symlink(dest)
        # trigger_service_activate runs `<dest> service-activate --bin <stable>`
        # with a timeout that exceeds hypercorn's graceful_timeout (so the
        # cross-schema slow-drain case can complete without spuriously
        # falling back), and on failure issues a direct
        # `launchctl kickstart -k` / `systemctl --user restart` / sc.exe so
        # we still end up on the new binary even if the new binary's own
        # service-activate CLI is unreachable.
        activated, activate_route = _osup.trigger_service_activate(
            dest, stable, detached=False,
        )
        if activated:
            print(
                f"[om-core-runner] service activated → {stable} → {dest} "
                f"(route={activate_route})",
                file=sys.stderr, flush=True,
            )
        else:
            print(
                f"[om-core-runner] service activation failed across all routes; "
                f"supervisor's next respawn will pick up the new bundle",
                file=sys.stderr, flush=True,
            )
    except Exception as e:  # noqa: BLE001 — never let activation failures
        # block the ready state write; binary is verified on disk.
        print(
            f"[om-core-runner] symlink/activate hop failed ({type(e).__name__}: "
            f"{e}); supervisor's next respawn will pick up the new bundle",
            file=sys.stderr, flush=True,
        )

    finished = _utc_now_iso()
    _write_status(statefile, {
        "state": "ready",
        "started_at": started,
        "finished_at": finished,
        "pid": pid,
        "version": args.version,
        "bytes_downloaded": bytes_written,
        "total_bytes_hint": bytes_written,
        "last_progress_at": finished,
        "activated": activated,
        # sha of the fetched asset (archive for tar.xz, binary for raw) —
        # bootstrap's onedir cache-hit check verifies against this instead
        # of re-hashing the whole extracted bundle.
        "archive_sha256": args.sha256,
    })
    print(f"[om-core-runner] binary ready at {dest}", file=sys.stderr, flush=True)

    # Prune superseded bundles — onedir dirs are ~100MB+, so without this
    # every upgrade leaves a full copy behind. Best-effort; a running
    # sidecar keeps its mmap'd inodes alive across the unlink.
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import om_core_paths as _ocp_prune  # noqa: PLC0415
        removed = _ocp_prune.prune_cache(keep=3)
        if removed:
            print(
                f"[om-core-runner] pruned old bundles: {removed}",
                file=sys.stderr, flush=True,
            )
    except Exception as e:  # noqa: BLE001 — pruning must never fail the fetch
        print(
            f"[om-core-runner] cache prune skipped ({type(e).__name__}: {e})",
            file=sys.stderr, flush=True,
        )

    # Legacy admin-reload fallback removed in plugin 0.39.3: the
    # /v1/admin/reload endpoint that this path targeted was retired in
    # om-core 0.33.0 (commit 6b46fc7's grep across scripts/ and src/
    # missed this hooks/ caller), so the call had been silently 404'ing
    # since. The replacement is now embedded in trigger_service_activate
    # above (direct launchctl kickstart -k on macOS, systemd user
    # restart on Linux, sc.exe stop+start on Windows) — runs only when
    # `<dest> service-activate` itself fails, exact same trigger point
    # but a path that doesn't depend on a deleted route. The bootstrap
    # SessionStart hook's verify-and-spawn loop (running-version vs
    # expected) provides a second convergence layer if even that
    # fallback misses.

    return 0


# `_post_admin_reload_best_effort` removed in plugin 0.39.3 — it posted
# /v1/admin/reload over UDS, but that endpoint was retired in om-core
# 0.33.0 (commit 6b46fc7) and had been silently 404-no-op'ing since.
# The replacement path is `om_supervisor.restart_supervised_service()`,
# invoked from inside `trigger_service_activate`'s fallback branch and
# also reachable from the bootstrap SessionStart verify-and-spawn loop.


if __name__ == "__main__":
    sys.exit(main())
