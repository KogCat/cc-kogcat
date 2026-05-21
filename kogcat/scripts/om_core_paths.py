"""Shared path resolution for the om-core binary.

Cache layout — the per-version/target dir is a PyInstaller onedir bundle::

    ~/.claude/om-core-cache/<version>/<target>/             bundle dir
    ~/.claude/om-core-cache/<version>/<target>/om-core-bin  executable
    ~/.claude/om-core-cache/<version>/<target>/_internal/   bundled libs
    ~/.claude/om-core-cache/<version>/<target>.lock         fetch lock
    ~/.claude/om-core-cache/<version>/<target>.status.json  fetch statefile

The bundle dir holds only the artifact so the fetcher can atomically swap
it on extract; fetch bookkeeping (lock/statefile) lives alongside as
siblings. Versioned by the binary's own version (manifest.json) — multiple
plugin releases pinning the same om-core share one download. Override
cache root via ``OM_CORE_CACHE_ROOT``.

Manifest lives in the plugin tree (git-tracked); the binary does not.

Stdlib-only: imported by bootstrap before third-party deps are available.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from pathlib import Path


class UnsupportedTarget(RuntimeError):
    """Raised when the current platform has no om-core binary release."""


class CrossCacheRootError(RuntimeError):
    """Refusing to point the stable symlink at a binary outside the active
    ``cache_root()`` tree. The pointer is shared OS supervisor state; a
    foreign target (e.g. a pytest tmp_path) would repoint the live
    launchd/systemd entry at an ephemeral binary.
    """


def target_triple() -> str:
    """Return the Rust-style target triple for the current platform."""
    machine = platform.machine().lower()
    system = platform.system().lower()
    if system == "darwin":
        if machine in ("arm64", "aarch64"):
            return "aarch64-apple-darwin"
        if machine in ("x86_64", "amd64"):
            return "x86_64-apple-darwin"
    if system == "windows":
        return "x86_64-pc-windows-msvc"
    if system == "linux":
        return f"{machine}-unknown-linux-gnu"
    raise UnsupportedTarget(f"unsupported platform: {system}/{machine}")


def binary_filename() -> str:
    """`om-core-bin` everywhere except Windows where it's `om-core-bin.exe`."""
    return "om-core-bin.exe" if sys.platform == "win32" else "om-core-bin"


def cache_root() -> Path:
    """Cache directory. Overridable via ``OM_CORE_CACHE_ROOT``."""
    if (env := os.environ.get("OM_CORE_CACHE_ROOT", "").strip()):
        return Path(env).expanduser()
    return Path.home() / ".claude" / "om-core-cache"


def cache_bin_path(version: str, target: str | None = None) -> Path:
    """Resolved cache path for a specific om-core version + target."""
    target = target or target_triple()
    return cache_root() / version / target / binary_filename()


def current_bin_path(target: str | None = None) -> Path:
    """Stable per-target symlink the OS supervisor points at.

    Layout: ``<cache_root>/current/<target>/om-core-bin``. Symlink target
    is swapped atomically on each binary upgrade — supervisor definition
    decoupled from binary version.
    """
    target = target or target_triple()
    return cache_root() / "current" / target / binary_filename()


def ensure_current_symlink(actual: Path, target: str | None = None) -> Path:
    """Atomically point the stable ``current/<target>`` dir at the onedir
    bundle containing ``actual`` (the ``om-core-bin`` executable).

    With onedir the supervisor must see the executable AND its sibling
    ``_internal/`` together, so the *directory* is symlinked, not the file:
    ``current/<target>`` → ``<version>/<target>``. ``current_bin_path()``
    (= ``current/<target>/om-core-bin``) then resolves through it, so a
    binary upgrade is a single atomic symlink swap — no plist/unit rewrite.

    Writes ``current/<target>.tmp`` symlink, then ``os.replace`` to swap —
    same-fs ``rename(2)`` is atomic on POSIX. Idempotent.

    Windows without SeCreateSymbolicLinkPrivilege: falls back to a
    directory copy via ``shutil.copytree`` (still atomically renamed in).
    """
    actual = actual.resolve()
    bundle_dir = actual.parent  # <cache>/<version>/<target>/
    stable_exe = current_bin_path(target)
    stable_dir = stable_exe.parent  # <cache>/current/<target>
    # Guard against a subprocess that lost OM_CORE_CACHE_ROOT silently
    # repointing the production symlink at its tmp_path.
    root = cache_root().resolve()
    try:
        bundle_dir.relative_to(root)
    except ValueError:
        raise CrossCacheRootError(
            f"refusing to point {stable_dir} at {bundle_dir} "
            f"(outside cache_root {root}); set OM_CORE_CACHE_ROOT in the "
            f"caller's env, or pass a binary that lives under the active "
            f"cache tree"
        )
    stable_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp = stable_dir.with_name(stable_dir.name + ".tmp")
    if tmp.is_symlink():
        tmp.unlink()
    elif tmp.is_dir():
        shutil.rmtree(tmp)
    elif tmp.exists():
        tmp.unlink()
    try:
        os.symlink(bundle_dir, tmp, target_is_directory=True)
    except OSError:
        shutil.copytree(bundle_dir, tmp)
    # Pre-onedir (onefile) layout left ``current/<target>`` as a real
    # directory holding a file-symlink ``om-core-bin``. os.replace cannot
    # rename a symlink over an existing real directory (EISDIR) — the
    # first onedir activation removes that legacy dir so the dir-symlink
    # can take its place. An existing symlink is left untouched for
    # os.replace to swap atomically (steady-state upgrade path).
    if not stable_dir.is_symlink() and stable_dir.is_dir():
        shutil.rmtree(stable_dir)
    os.replace(tmp, stable_dir)
    return stable_exe


def bin_status_path(version: str, target: str | None = None) -> Path:
    """Statefile written by the binary fetcher while downloading.

    A sibling of the ``<version>/<target>/`` bundle dir, not inside it —
    the bundle dir is atomically swapped on extract, so fetch bookkeeping
    must live outside it.
    """
    target = target or target_triple()
    return cache_root() / version / f"{target}.status.json"


def bin_lock_path(version: str, target: str | None = None) -> Path:
    """Advisory lock for single-flight download. Sibling of the bundle dir."""
    target = target or target_triple()
    return cache_root() / version / f"{target}.lock"


def prune_cache(keep: int = 3, cache: Path | None = None) -> list[str]:
    """Delete all but the ``keep`` newest versioned bundle dirs.

    onedir bundles are ~100MB+ each; without pruning every binary upgrade
    leaves a full copy behind. Called by the fetcher after a successful
    activate. A running sidecar keeps its mmap'd inodes alive across the
    unlink, so deleting an older in-use version frees only the dir entry.

    Returns the list of version strings removed.
    """
    root = cache or cache_root()
    if not root.is_dir():
        return []
    versions: list[tuple[tuple[int, int, int], str, Path]] = []
    for child in root.iterdir():
        if not child.is_dir() or child.name == "current":
            continue
        key = _semver_key(child.name)
        if key == (0, 0, 0) and child.name != "0.0.0":
            continue  # not a version dir
        versions.append((key, child.name, child))
    versions.sort(reverse=True)
    removed: list[str] = []
    for _key, name, path in versions[max(keep, 1):]:
        try:
            shutil.rmtree(path)
            removed.append(name)
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------------------
# Expected-release pointer — SessionStart → readiness-gate handoff.
#
# bootstrap_om_core_bin.py resolves the release at SessionStart and records
# the resolved version here. The per-tool-call readiness gate reads this
# fixed-path pointer instead of re-running channel resolution, keeping the
# gate's hot path purely local — no network, no plugin-tree manifest read.
# Not version-keyed: a single file the gate always knows where to find.
# ---------------------------------------------------------------------------

def expected_release_path() -> Path:
    """Fixed-path pointer recording the version SessionStart resolved."""
    return cache_root() / "expected-release.json"


def write_expected_release(version: str, target: str | None = None) -> None:
    """Record the SessionStart-resolved release. Atomic write; best-effort
    — a failure here must never break bootstrap."""
    target = target or target_triple()
    p = expected_release_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(
            json.dumps({"version": version, "target": target}),
            encoding="utf-8",
        )
        os.replace(tmp, p)
    except OSError:
        pass


def read_expected_release() -> dict | None:
    """Load the expected-release pointer. None if absent / unreadable."""
    p = expected_release_path()
    try:
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return None


def manifest_path(plugin_root: Path, target: str | None = None) -> Path:
    """Manifest stays in the plugin tree (git-tracked, version-pinned)."""
    target = target or target_triple()
    return Path(plugin_root) / "bin" / f"om-core-{target}" / "manifest.json"


def read_manifest(plugin_root: Path, target: str | None = None) -> dict:
    """Load manifest.json. Raises FileNotFoundError if missing."""
    p = manifest_path(plugin_root, target)
    return json.loads(p.read_text(encoding="utf-8"))


def _detect_plugin_root() -> Path | None:
    """Read ``CLAUDE_PLUGIN_ROOT`` env. Returns None if unset."""
    if (env := os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()):
        return Path(env)
    return None


def resolve_existing_bin(plugin_root: Path | None = None) -> Path | None:
    """Read-side resolver. Does not download.

    Order: ``OM_CORE_BIN`` env → cache keyed by manifest version → ``$PATH``.
    Returns None on miss.
    """
    if (env := os.environ.get("OM_CORE_BIN", "").strip()):
        p = Path(env).expanduser()
        if p.exists():
            return p
        return None

    plugin_root = plugin_root or _detect_plugin_root()

    if plugin_root is not None:
        try:
            target = target_triple()
        except UnsupportedTarget:
            target = None

        if target is not None:
            try:
                manifest = read_manifest(plugin_root, target)
                version = manifest.get("version")
                if version:
                    cached = cache_bin_path(version, target)
                    if cached.exists():
                        return cached
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                pass

    for name in ("om-core-bin", "om-core"):
        if (sys_bin := shutil.which(name)):
            return Path(sys_bin)

    return None


# ---------------------------------------------------------------------------
# Channel-based release resolution (binary ⇄ client decoupling)
#
# Pre-decoupling, the fetched binary version was pinned in the plugin tree
# (`bin/<target>/manifest.json`) — upgrading the binary required a plugin
# release. `resolve_release` instead consults a rolling channel index
# published on the binary host, so a new binary reaches clients without any
# plugin release. The bundled manifest stays as the offline / first-run
# fallback floor.
# ---------------------------------------------------------------------------

# Lowest om-core series (major, minor) this client generation can drive.
# Channel resolution refuses any release below this. Keep in lockstep with
# `om_mcp/http_backend.py::MY_REQUIRED_SERIES` — that constant is the runtime
# /v1/capabilities gate; this one is the fetch-time gate. Both move together
# when om-core crosses a series break.
MIN_SERIES = (0, 35)

# channel.json schema this client generation understands. Bumped 2 → 3
# when per-target entries gained the `format` field (onedir `.tar.xz`
# archives vs. the legacy raw single-file binary). A channel advertising
# a different schema is rejected → fall back to the bundled manifest,
# so a pre-onedir client never tries to run a `.tar.xz` as an executable.
SUPPORTED_CHANNEL_SCHEMA = 3

# Rolling channel index, published by release-om-core.yml's commit-manifests
# job onto the binary host's mutable `channel` release. Schema: build_channel.py.
# OM_CORE_CHANNEL_URL overrides this — tests point it at a file:// path, and
# a mirror deployment could repoint it.
CHANNEL_URL = (
    "https://github.com/KogCat/om-core-binaries/releases/download/"
    "channel/channel.json"
)

# channel.json is fetched only by resolve_release, which runs at
# SessionStart (bootstrap) — once per session, not a hot path. So it is
# fetched fresh every call: a newly published binary is discovered on the
# next CC restart with no staleness window. The on-disk copy at
# cache_root()/channel.json is kept solely as an offline fallback for when
# the fresh fetch fails (offline / host down).
_CHANNEL_FETCH_TIMEOUT_S = 5.0


def channel_cache_path() -> Path:
    """On-disk copy of the last good channel.json — offline fallback only,
    a sibling of the per-version cache dirs under cache_root()."""
    return cache_root() / "channel.json"


def _channel_url() -> str:
    """Channel index URL — OM_CORE_CHANNEL_URL overrides the default."""
    return os.environ.get("OM_CORE_CHANNEL_URL", "").strip() or CHANNEL_URL


def _semver_key(version: str) -> tuple[int, int, int]:
    """`X.Y.Z` → sortable tuple. Pre-release suffixes stripped; malformed or
    short versions degrade to 0-padding rather than raising."""
    nums: list[int] = []
    for part in version.split(".")[:3]:
        head = part.split("-", 1)[0]
        nums.append(int(head) if head.isdigit() else 0)
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2]


def _fetch_channel_json() -> dict | None:
    """Return the channel index — fresh network fetch, on-disk copy as
    offline fallback.

    A bounded urllib GET is attempted on every call. On success the result
    is persisted to cache_root()/channel.json and returned. On any failure
    (offline, host down, malformed JSON) the last persisted copy is
    returned regardless of age; if none exists, None.

    Called only by resolve_release, which runs at SessionStart (bootstrap)
    — once per session — so a fresh fetch every call costs nothing on a
    hot path and keeps binary discovery current. Stdlib-only — this runs
    inside the SessionStart hook before third-party deps are installed.
    """
    cache_p = channel_cache_path()
    try:
        import urllib.request  # noqa: PLC0415 — stdlib, lazy import
        req = urllib.request.Request(
            _channel_url(), headers={"User-Agent": "om-bin-fetch/1.0"},
        )
        with urllib.request.urlopen(  # noqa: S310 — fixed https URL
            req, timeout=_CHANNEL_FETCH_TIMEOUT_S,
        ) as resp:
            raw = resp.read()
        data = json.loads(raw)
        try:
            cache_p.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_p.with_name(cache_p.name + ".tmp")
            tmp.write_bytes(raw)
            os.replace(tmp, cache_p)
        except OSError:
            pass  # persisting the offline-fallback copy is best-effort
        return data
    except Exception:  # noqa: BLE001 — offline / host-down / malformed all fall through
        pass

    try:
        if cache_p.is_file():
            return json.loads(cache_p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _select_from_channel(channel: dict, target: str) -> dict | None:
    """Pick the highest channel release at or above MIN_SERIES that ships a
    binary for `target`. Returns a manifest-shaped dict, or None when the
    channel carries no compatible release or advertises an unsupported
    schema."""
    if channel.get("schema_version") != SUPPORTED_CHANNEL_SCHEMA:
        return None
    releases = channel.get("releases")
    if not isinstance(releases, list):
        return None
    best: tuple[str, dict] | None = None
    best_key: tuple[int, int, int] | None = None
    for r in releases:
        if not isinstance(r, dict):
            continue
        version = str(r.get("om_core_version", ""))
        key = _semver_key(version)
        if key[:2] < MIN_SERIES:
            continue
        entry = (r.get("targets") or {}).get(target)
        if not isinstance(entry, dict):
            continue
        if not entry.get("url") or not entry.get("sha256"):
            continue
        if best_key is None or key > best_key:
            best, best_key = (version, entry), key
    if best is None:
        return None
    version, entry = best
    return {
        "version": version,
        "target": target,
        "om_core_bin_url": entry["url"],
        "om_core_bin_sha256": entry["sha256"],
        "size_bytes": int(entry.get("size_bytes", 0) or 0),
        # `format` absent on a legacy entry → raw single-file binary;
        # "tar.xz" → onedir bundle archive (extract on fetch).
        "format": entry.get("format"),
    }


def resolve_release(plugin_root: Path, target: str | None = None) -> dict:
    """Resolve which om-core binary to fetch — channel-first, manifest-fallback.

    Pulls the rolling channel.json and picks the newest release meeting
    MIN_SERIES. Falls back to the plugin-tree per-target manifest.json when
    the channel is unreachable or carries no compatible release.

    Returns a manifest-shaped dict (version, target, om_core_bin_url,
    om_core_bin_sha256, size_bytes). Propagates FileNotFoundError /
    JSONDecodeError / OSError from `read_manifest` only when BOTH the channel
    and the bundled manifest are unavailable.
    """
    target = target or target_triple()
    channel = _fetch_channel_json()
    if channel is not None:
        picked = _select_from_channel(channel, target)
        if picked is not None:
            return picked
    return read_manifest(plugin_root, target)
