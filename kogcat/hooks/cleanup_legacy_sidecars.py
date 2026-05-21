"""SessionStart hook: shut down stale pre-spec-19 sidecars (spec 19 §4.1).

The original 5-sidecar bug came from the wrapper spawning lazily on first
call without singleton coordination. After upgrading to plugin 0.26 those
sidecars are still alive on the user's machine (POSIX `start_new_session=
True` + deleted-inode binaries keep them running). They bind TCP loopback
ports + write the legacy `server.json` schema with `port` / `token`
fields, which the new wrapper can't talk to.

This hook idempotently:
1. Reads the active supervisor's registered binary path
   (launchd plist on macOS, systemd unit on Linux, scheduled task on
   Windows). That path is the source of truth for "which om-core
   process is supervised — leave it alone".
2. Walks running processes for executables matching `om-core` /
   `om-core-bin`.
3. **Filters to legacy only** — processes whose executable path
   (resolved against symlinks) does NOT match the supervisor's
   registered path. If the supervisor config can't be read at all, we
   leave every process alone — better to skip a cleanup pass than
   accidentally kill the live supervisor.
4. For each legacy match:
   a. Reads its `server.json` if any.
   b. POSTs `/v1/shutdown` over TCP loopback (legacy path).
   c. Falls back to SIGTERM, then SIGKILL.
5. Removes stale `server.json` ONLY if it has the legacy schema
   (`port`+`token`, no `transport`). UDS server.json from the
   supervised sidecar is preserved.

0.27 fix: the run is unconditional. Pre-0.27 a `MARKER_NAME` short-
circuited subsequent sessions, so a legacy sidecar spawned by a Tauri
build / manual command after marker placement was never cleaned up.
Scan is fast (<100ms typical) so the marker is no longer worth the
foot-gun.

0.27.1 fix: the supervised-path check used to be a hardcoded substring
match against `.claude/om-core-cache/` — brittle (broke when binary
lived under `.claude/plugins/cache/...`, broke when the user set
`OM_CORE_CACHE_ROOT`, broke for any future layout change). Now the
supervisor config is read directly via `_resolve_supervised_bin()`,
so wherever the supervisor was registered to invoke from, that's
what we treat as supervised — no path assumption.

Stdlib only. Best-effort throughout — any failure is logged but never
breaks SessionStart.
"""
from __future__ import annotations

import json
import os
import socket as _socket
import subprocess
import sys
import time
from pathlib import Path

PROBE_TIMEOUT = 5.0


def _resolve_supervised_bin() -> str | None:
    """Read the binary path the active supervisor is registered to invoke.

    Returns an absolute path string (resolved against symlinks via
    `os.path.realpath`) or `None` if the supervisor config can't be
    located/parsed. `None` instructs the caller to *skip the entire
    cleanup pass* — better to leave stale processes for a session
    than risk killing the live supervisor.

    Per platform:
      - macOS: parse `~/Library/LaunchAgents/com.kogcat.om.plist`
        ProgramArguments[0]
      - Linux: parse `~/.config/systemd/user/om.service` ExecStart= line
      - Windows: read `om-core service-status` (no fast file-read path
        for SCM/Task Scheduler — but cleanup hook on Windows is rare;
        fall through to None for now)
    """
    if sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "com.kogcat.om.plist"
        if not plist.is_file():
            return None
        try:
            import plistlib
            with plist.open("rb") as fh:
                data = plistlib.load(fh)
        except (OSError, ValueError, Exception):  # noqa: BLE001
            return None
        args = data.get("ProgramArguments")
        if isinstance(args, list) and args and isinstance(args[0], str):
            try:
                return os.path.realpath(args[0])
            except OSError:
                return args[0]
        return None

    if sys.platform.startswith("linux"):
        unit = Path.home() / ".config" / "systemd" / "user" / "om.service"
        if not unit.is_file():
            return None
        try:
            for raw in unit.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line.startswith("ExecStart="):
                    continue
                rest = line[len("ExecStart="):].lstrip()
                # systemd ExecStart= can have a leading "@", "-", "+", "!" etc.
                # for special semantics; strip those before tokenising.
                while rest and rest[0] in "@-+!:":
                    rest = rest[1:].lstrip()
                first_token = rest.split()[0] if rest.split() else ""
                if not first_token:
                    return None
                try:
                    return os.path.realpath(first_token)
                except OSError:
                    return first_token
        except OSError:
            return None
        return None

    # Windows: SCM / Task Scheduler. No cheap file read; safer to return
    # None and let the user trigger explicit cleanup if needed.
    return None


def _config_dir() -> Path:
    if (env := os.environ.get("OM_CONFIG_HOME", "").strip()):
        return Path(env).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "om"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "om"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "om"


def _read_server_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _legacy_server_json(cfg: dict | None) -> bool:
    """Pre-spec-19 server.json carries `port` + `token`; new schema has `transport`."""
    if cfg is None:
        return False
    return "port" in cfg and "token" in cfg and "transport" not in cfg


def _shutdown_via_tcp(cfg: dict) -> bool:
    """Best-effort POST /v1/shutdown over the legacy TCP listener."""
    port = cfg.get("port")
    token = cfg.get("token")
    if not isinstance(port, int) or not isinstance(token, str):
        return False
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(("127.0.0.1", port))
        try:
            body = b'{"grace_seconds":0}'
            req = (
                f"POST /v1/shutdown HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Authorization: Bearer {token}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Connection: close\r\n\r\n"
            ).encode("ascii") + body
            s.sendall(req)
            _ = s.recv(64)
        finally:
            s.close()
        return True
    except (OSError, _socket.timeout):
        return False


_OM_CORE_NAMES = {"om-core", "om-core-bin"}


def _is_om_core_executable(name: str, exe: str) -> bool:
    """Match on exact basename of name/exe (or an om-core-v* legacy
    suffix). Substring/cmdline match is too loose — a shell snapshot
    referencing the path would otherwise be picked up."""
    base_name = (name or "").rsplit("/", 1)[-1]
    base_exe = (exe or "").rsplit("/", 1)[-1]
    for cand in (base_name, base_exe):
        if not cand:
            continue
        if cand in _OM_CORE_NAMES:
            return True
        if cand.startswith("om-core-v") or cand.startswith("om-core-bin-"):
            return True
    return False


def _is_legacy_binary(exe_path: str, supervised_bin: str | None) -> bool:
    """True iff this process's executable is NOT the supervisor-registered one.

    `supervised_bin` is either the absolute (realpath-resolved) binary
    path the active supervisor is set to invoke, or `None` if we can't
    determine it. When `None`, every process is treated as **non-legacy**
    (return False) so cleanup leaves them alone — see module docstring
    rationale.

    Comparison normalises both sides via `os.path.realpath` so symlink
    indirection doesn't cause a false legacy classification.
    """
    if supervised_bin is None or not exe_path:
        return False
    try:
        a = os.path.realpath(exe_path)
    except OSError:
        a = exe_path
    return a != supervised_bin


def _list_om_core_pids(supervised_bin: str | None) -> list[tuple[int, str]]:
    """Return [(pid, exe_or_cmdline)] for legacy om-core processes only.

    `supervised_bin` is the supervisor-registered binary path (or None);
    processes whose exe equals it after realpath normalisation are
    left alone.

    Uses psutil if available; falls back to `ps` otherwise. Stdlib-only
    fallback skips processes whose cmdline can't be read (perm denied).
    """
    try:
        import psutil  # noqa: PLC0415
    except ImportError:
        return _list_via_ps(supervised_bin)

    out: list[tuple[int, str]] = []
    for p in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            name = (p.info.get("name") or "").lower()
            exe = p.info.get("exe") or ""
            cmd = " ".join(p.info.get("cmdline") or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if not _is_om_core_executable(name, exe.lower()):
            continue
        if p.info["pid"] == os.getpid():
            continue
        label = exe or name or cmd
        if _is_legacy_binary(exe, supervised_bin):
            out.append((p.info["pid"], label))
    return out


def _list_via_ps(supervised_bin: str | None) -> list[tuple[int, str]]:
    """psutil-less fallback. Same legacy-only filter as the psutil path."""
    if sys.platform == "win32":
        return []
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True, text=True, check=False, timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    out: list[tuple[int, str]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if "om-core" not in line:
            continue
        head, _, rest = line.partition(" ")
        try:
            pid = int(head)
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        # Exec path is the first token of the command.
        first_tok = rest.split(None, 1)[0] if rest else ""
        if not _is_om_core_executable("", first_tok.lower()):
            continue
        if _is_legacy_binary(first_tok, supervised_bin):
            out.append((pid, rest))
    return out


def _kill(pid: int, sig: int) -> bool:
    try:
        os.kill(pid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _wait_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _kill(pid, 0):
            return True
        time.sleep(0.1)
    return False


def main() -> int:
    cfg_dir = _config_dir()
    cfg_dir.mkdir(parents=True, exist_ok=True)
    # Trace marker so we can prove the hook fired (vs. CC's SessionStart
    # mechanism not invoking it). Removed once stable.
    (cfg_dir / ".hook_ran_cleanup").write_text(
        f"{time.time()}\n", encoding="utf-8",
    )

    server_json_path = cfg_dir / "server.json"
    spawn_lock_path = cfg_dir / ".spawn.lock"

    cfg = _read_server_json(server_json_path)
    legacy_cfg = cfg if _legacy_server_json(cfg) else None

    # Read the supervisor-registered binary path so we can tell apart
    # supervised vs. legacy processes without relying on a hardcoded
    # path prefix (0.27.1 fix). `None` here means we can't determine
    # which om-core is the live one — `_list_om_core_pids(None)` then
    # returns no candidates, leaving every running process alone.
    supervised_bin = _resolve_supervised_bin()

    # Always scan — see module docstring (0.27 fix). The supervised
    # om-core is filtered out inside `_list_om_core_pids` so this kill
    # loop only ever targets legacy survivors.
    pids = _list_om_core_pids(supervised_bin)
    if not pids and legacy_cfg is None:
        return 0

    if pids:
        print(
            f"[om-cleanup] found {len(pids)} legacy om-core process(es); shutting down",
            file=sys.stderr,
        )

    if legacy_cfg is not None:
        _shutdown_via_tcp(legacy_cfg)

    import signal  # noqa: PLC0415
    for pid, label in pids:
        if not _kill(pid, signal.SIGTERM):
            continue  # already gone
        if _wait_exit(pid, timeout=PROBE_TIMEOUT):
            print(f"[om-cleanup] terminated {pid} ({label})", file=sys.stderr)
            continue
        if _kill(pid, signal.SIGKILL):
            if _wait_exit(pid, timeout=2.0):
                print(f"[om-cleanup] force-killed {pid} ({label})", file=sys.stderr)
                continue
        print(
            f"[om-cleanup] survivor (could not kill): {pid} ({label})",
            file=sys.stderr,
        )

    # Only nuke server.json if it has the legacy schema. UDS server.json
    # written by the supervised sidecar must be preserved — pre-0.27 we
    # nuked unconditionally, which raced with a freshly started supervised
    # sidecar and could leave the wrapper temporarily blind.
    if legacy_cfg is not None:
        try:
            server_json_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"[om-cleanup] could not remove {server_json_path}: {e}", file=sys.stderr)

    # Spawn lock is unconditionally legacy — the new supervisor doesn't use it.
    try:
        spawn_lock_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"[om-cleanup] could not remove {spawn_lock_path}: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
