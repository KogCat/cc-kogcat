"""SessionStart hook: ensure the om-core supervisor entry exists (spec 19 §Layer 1).

Idempotent: probes `om-core service-status`. If "installed" is False (or
the binary itself is missing), invokes `om-core install-service`. Hard
timeouts — if the binary is still being fetched by the runner OR cold
start exceeds the bound, we exit 0 silently and the next session
retries (the binary cold start is ~6-22s on macOS arm64).

Stdlib-only — like every hook in this plugin, it has no third-party
dependency.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# Reuse the binary resolver shared with the wrapper / bootstrap path.
_FILE_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _FILE_PLUGIN_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

try:
    import om_core_paths  # noqa: E402
except ImportError:
    print(
        "[om-install-service] om_core_paths not importable; skipping",
        file=sys.stderr,
    )
    sys.exit(0)


# PyInstaller --onefile cold start unpacks ~64MB into $TMPDIR/_MEI* —
# measured 6-22s on macOS arm64 depending on disk warmth. Generous
# timeouts prevent a "binary is fine, hook just gave up" SessionStart
# misfire. The user is offline-blocked while these wait, so we still
# bound them rather than passing timeout=None.
PROBE_TIMEOUT = 15.0
INSTALL_TIMEOUT = 45.0


def _resolve_bin() -> Path | None:
    """Resolve the binary the supervisor should register.

    Spec 19: this is the plugin-shipped binary at the canonical cache
    path (`~/.claude/om-core-cache/<manifest_version>/<target>/`), NOT
    whatever `OM_CORE_BIN` happens to point at. The user may have
    `OM_CORE_BIN` set to an old / sibling-app binary (e.g. an Obsidian
    plugin's own copy); registering THAT with launchd would break the
    supervisor since the binary may not even know `install-service`.

    Read manifest.json from plugin tree → compute cache path directly,
    bypassing `resolve_existing_bin()`'s OM_CORE_BIN-first chain.
    """
    try:
        plugin_root = om_core_paths._detect_plugin_root()
        if plugin_root is None:
            return None
        target = om_core_paths.target_triple()
        manifest = om_core_paths.read_manifest(plugin_root, target)
        version = manifest.get("version")
        if not version:
            return None
        cache_bin = om_core_paths.cache_bin_path(version, target)
        return cache_bin if cache_bin.exists() else None
    except Exception:  # noqa: BLE001
        return None


def _run(bin_path: Path, *args: str, timeout: float) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            [str(bin_path), *args],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        print(
            f"[om-install-service] {bin_path.name} {' '.join(args)} failed: {e}",
            file=sys.stderr,
        )
        return None


def _macos_plist_is_current() -> bool:
    """True iff the on-disk plist matches the current schema AND points at
    the stable-pointer symlink path (post-0.28.0 layout).

    Two checks compose:
      (a) `KeepAlive == True` — pre-0.27 wrote a dict `{SuccessfulExit:
          false, Crashed: true}`, which doesn't respawn on clean exit
          (silent sidecar disappearance after graceful shutdown).
      (b) `ProgramArguments[0] == om_core_paths.current_bin_path()` —
          pre-0.28 wrote the *versioned* cache path (e.g.
          `<cache>/0.27.1/<target>/om-core-bin`), which goes stale every
          binary upgrade and silently rolls back to the old version.
          0.28+ writes the stable per-user symlink (`<cache>/current/
          <target>/om-core-bin`) so the plist itself never has to change.

    Either check failing → return False → caller re-runs install-service
    with `--bin <stable>`, which rewrites the plist into the new layout
    and `launchctl bootstrap`s it. One-time migration on first SessionStart
    after upgrading to 0.28.x; subsequent upgrades are pure symlink swaps.

    Stdlib-only — `plistlib` is in the standard library; om_core_paths
    is the plugin's own module.
    """
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.kogcat.om.plist"
    if not plist_path.is_file():
        return False
    try:
        import plistlib  # noqa: PLC0415
        with plist_path.open("rb") as fh:
            plist = plistlib.load(fh)
    except (OSError, ValueError, Exception):  # noqa: BLE001
        # Unreadable / malformed plist → conservative: re-install.
        return False
    if plist.get("KeepAlive") is not True:
        return False
    args = plist.get("ProgramArguments") or []
    if not args:
        return False
    try:
        expected_stable = str(om_core_paths.current_bin_path())
    except Exception:  # noqa: BLE001
        # Resolver miss (target_triple / cache_root errored). Don't loop
        # re-installing; treat as current and let the next run pick up.
        return True
    return args[0] == expected_stable


def _is_already_installed(bin_path: Path) -> bool:
    """Cheap probe — pure filesystem check, avoids PyInstaller cold start.

    Calling `om-core-bin service-status` would unpack the 64MB onefile
    binary into /tmp on every SessionStart (10-25s cold start), even
    when the answer is trivially "yes, plist exists". Instead, peek
    the supervisor's canonical state file directly.

    Per platform:
      - macOS: ~/Library/LaunchAgents/com.kogcat.om.plist + schema check
      - Linux: ~/.config/systemd/user/om.service
      - Windows: registry-backed; fall through to binary call

    Conservative bias: if the fast path can't conclude, return False
    so we re-run install-service (idempotent on the binary side).
    """
    home = Path.home()
    if sys.platform == "darwin":
        return _macos_plist_is_current()
    if sys.platform.startswith("linux"):
        return (home / ".config" / "systemd" / "user" / "om.service").is_file()
    # Windows: registry-backed SCM / schtasks. No reliable cheap probe; fall
    # back to binary call (cold start cost is amortised across the install).
    proc = _run(bin_path, "service-status", timeout=PROBE_TIMEOUT)
    if proc is None or proc.returncode != 0:
        return False
    try:
        status = json.loads(proc.stdout)
    except (ValueError, json.JSONDecodeError):
        return False
    return bool(status.get("installed"))


def main() -> int:
    # Trace marker so we can prove the hook fired (vs. CC's SessionStart
    # mechanism not invoking it). Removed once stable.
    import time
    cfg_dir_str = os.environ.get("OM_CONFIG_HOME", "").strip()
    if cfg_dir_str:
        from pathlib import Path as _P
        cfg_dir = _P(cfg_dir_str).expanduser()
    elif sys.platform == "darwin":
        from pathlib import Path as _P
        cfg_dir = _P.home() / "Library" / "Application Support" / "om"
    elif sys.platform == "win32":
        from pathlib import Path as _P
        cfg_dir = _P(os.environ.get("APPDATA", _P.home() / "AppData" / "Roaming")) / "om"
    else:
        from pathlib import Path as _P
        cfg_dir = _P(os.environ.get("XDG_CONFIG_HOME", _P.home() / ".config")) / "om"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / ".hook_ran_install_service").write_text(
        f"{time.time()}\n", encoding="utf-8",
    )

    bin_path = _resolve_bin()
    if bin_path is None:
        # Binary not yet on disk — runner is still fetching. Next session retries.
        return 0
    if _is_already_installed(bin_path):
        return 0

    # Stable-pointer install (om-core ≥ 0.28.0):
    # 1. Atomically point `current_bin_path()` symlink at the resolved binary.
    # 2. Pass that stable path to `install-service --bin` so the supervisor
    #    registers it (instead of the version-specific cache path that the
    #    legacy mode uses). Subsequent upgrades only swap the symlink +
    #    `service-activate`; this hook only runs again if the plist drifts.
    #
    # Older binaries (pre-0.28.0) ignore unknown args via argparse strict
    # mode — they error out. We detect that by trying once, and if the
    # binary rejects --bin, fall through to legacy `install-service` (no
    # --bin) so the upgrade path stays unblocked.
    try:
        stable = om_core_paths.ensure_current_symlink(bin_path)
    except Exception as e:  # noqa: BLE001 — never block SessionStart
        print(
            f"[om-install-service] symlink swap failed ({type(e).__name__}: {e}); "
            f"falling back to legacy install-service (versioned path)",
            file=sys.stderr,
        )
        stable = None

    if stable is not None:
        proc = _run(bin_path, "install-service", "--bin", str(stable),
                    timeout=INSTALL_TIMEOUT)
        if proc is not None and proc.returncode != 0 and "--bin" in (proc.stderr or ""):
            # Pre-0.28.0 binary doesn't know --bin. Retry without.
            print(
                f"[om-install-service] binary predates --bin support; "
                f"retrying legacy install-service",
                file=sys.stderr,
            )
            proc = _run(bin_path, "install-service", timeout=INSTALL_TIMEOUT)
    else:
        proc = _run(bin_path, "install-service", timeout=INSTALL_TIMEOUT)

    if proc is None:
        return 0
    if proc.returncode != 0:
        # Don't break SessionStart on install failure — wrappers will surface
        # `OM_SIDECAR_UNAVAILABLE` with the same hint, and the user can
        # retry manually.
        msg = (proc.stderr or proc.stdout).strip()
        print(
            f"[om-install-service] install-service rc={proc.returncode}: {msg}",
            file=sys.stderr,
        )
        return 0

    print(
        f"[om-install-service] om-core service registered with the OS supervisor",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
