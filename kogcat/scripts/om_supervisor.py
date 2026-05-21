"""Supervisor primitives for the om-core sidecar.

Shared between the SessionStart bootstrap hook (proactive verify) and the
binary fetch runner (reactive activate after download). Stdlib-only —
imported by hooks before third-party deps are available.

Responsibilities
----------------

1. **Read** the currently-supervised sidecar version from ``server.json``
   without touching the HTTP surface (no bearer-token plumbing here;
   bearer rotates whenever the sidecar respawns and is awkward to
   resolve from a hook context).

2. **Restart** the supervisor unit directly (``launchctl kickstart -k`` /
   ``systemctl --user restart`` / ``sc.exe`` …) as a last-resort
   activation path independent of the new binary itself being launchable.

3. **Activate** end-to-end — try ``<new_bin> service-activate`` first
   (lets the binary's own CLI carry the platform supervisor knowledge),
   fall back to ``restart_supervised_service()`` if that fails.

Why a separate module from ``om_core_paths``
--------------------------------------------

``om_core_paths`` handles "where binaries / caches / pointers live" —
pure path resolution. Supervisor interaction (process probing, OS
service control) is a distinct concern; mixing the two would bloat the
path module past where new contributors can read it in one pass.

Failure-mode contract
---------------------

Every public helper is **best-effort**. Functions return values
(``None`` / ``False``) rather than raise — supervisor hops are layered
defences (SessionStart verify → fetcher fallback → next-respawn natural
KeepAlive) so any one layer failing must not block the others.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# launchd / systemd / Windows service identifier — must match the unit
# label written by ``om-core install-service`` (om_core/infra/service_*).
SERVICE_LABEL = "com.kogcat.om"

# Default subprocess timeout for the supervisor restart command itself.
# ``launchctl kickstart -k`` typically returns in milliseconds (the kill
# + respawn dispatch is async on the kernel side), but on macOS 14 it
# has been observed to synchronously wait for the existing process to
# exit when the unit is KeepAlive=True. 10s gives the kernel breathing
# room without unduly delaying the caller.
_SUPERVISOR_RESTART_TIMEOUT_S = 10.0

# Timeout for the new binary's own ``service-activate`` subcommand. Must
# exceed hypercorn's ``graceful_timeout`` (currently 30s in om-core) plus
# margin — when the existing sidecar drains slowly (e.g. schema-version
# mismatch after an ingest-side migration) launchd waits on the old
# process to exit before respawning, and that wait time bubbles up here.
SERVICE_ACTIVATE_TIMEOUT_S = 60.0


def _server_config_path() -> Path:
    """Mirror the resolution order in ``om_core/infra/server_config.py``.

    Honours ``OM_CONFIG_HOME`` first, then platform-default
    (``~/Library/Application Support/om`` on macOS,
    ``$XDG_CONFIG_HOME/om`` else, ``~/.config/om`` ultimate fallback).
    Path-only; no IO here so callers can branch on ``exists()`` cheaply.
    """
    env = os.environ.get("OM_CONFIG_HOME", "").strip()
    if env:
        return Path(env).expanduser() / "server.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "om" / "server.json"
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "om" / "server.json"


def _is_pid_alive(pid: int | None) -> bool:
    """Probe whether ``pid`` exists. ``os.kill(pid, 0)`` is the canonical
    POSIX liveness check; on Windows the same signal-zero pattern is
    honoured by the CPython runtime. Returns False on any error.
    """
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ProcessLookupError, ValueError):
        return False


def read_running_sidecar_version() -> str | None:
    """Return the version of the currently-supervised sidecar, or None.

    Strategy: read ``server.json`` (written by the sidecar on each start)
    and verify the recorded PID is still alive. Returning ``None``
    signals "no observable sidecar" — callers MUST NOT treat this as a
    version mismatch (the supervisor's own KeepAlive path is the right
    recovery, not the activate path).

    Avoids HTTP because the bearer token rotates with each respawn and
    resolving it from a hook context is fragile; ``server.json`` carries
    ``binary_version`` directly so a file read is sufficient and faster.

    File-staleness handling: if the recorded PID is dead the file is
    stale (sidecar crashed before launchd respawned it). Treat as
    ``None`` rather than returning the dead binary's version — otherwise
    a freshly-installed-but-not-yet-running upgrade would look like a
    mismatch and trigger redundant activate attempts.
    """
    cfg_path = _server_config_path()
    if not cfg_path.is_file():
        return None
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not _is_pid_alive(cfg.get("pid")):
        return None
    version = cfg.get("binary_version")
    if not isinstance(version, str) or not version:
        return None
    return version


def restart_supervised_service() -> bool:
    """Directly tell the OS supervisor to restart its sidecar unit.

    Last-resort path — used when ``<new_bin> service-activate`` itself
    fails (e.g. timeout, the new binary won't launch, the activate CLI
    raised). This path does **not** require the new binary to be
    launchable; it works as long as the unit was previously registered
    (``launchctl bootstrap`` / ``systemctl --user enable`` etc. has run
    at some point) and the supervisor knows where the stable symlink
    points.

    Returns True on observable success (``returncode == 0``). Caller
    decides what to do on False; this helper itself never raises.
    """
    if sys.platform == "darwin":
        target = f"gui/{os.getuid()}/{SERVICE_LABEL}"
        proc = subprocess.run(
            ["launchctl", "kickstart", "-k", target],
            capture_output=True, text=True,
            timeout=_SUPERVISOR_RESTART_TIMEOUT_S,
            check=False,
        )
        return proc.returncode == 0

    if sys.platform.startswith("linux"):
        # systemd user service — matches the unit name install-service
        # would have written. The ``--user`` scope avoids needing
        # privileges; restart is a stop+start composite.
        proc = subprocess.run(
            ["systemctl", "--user", "restart", "om-core.service"],
            capture_output=True, text=True,
            timeout=_SUPERVISOR_RESTART_TIMEOUT_S,
            check=False,
        )
        return proc.returncode == 0

    if sys.platform == "win32":
        # Service manager restart on Windows: stop, then start. ``sc.exe``
        # is built-in and matches what ``om-core install-service`` wires
        # up. We tolerate the stop failing (service may already be down)
        # but require start to succeed.
        subprocess.run(
            ["sc.exe", "stop", "om-core"],
            capture_output=True, text=True,
            timeout=_SUPERVISOR_RESTART_TIMEOUT_S,
            check=False,
        )
        proc = subprocess.run(
            ["sc.exe", "start", "om-core"],
            capture_output=True, text=True,
            timeout=_SUPERVISOR_RESTART_TIMEOUT_S,
            check=False,
        )
        return proc.returncode == 0

    return False


def trigger_service_activate(
    bin_path: Path,
    stable_path: Path | None = None,
    *,
    detached: bool = False,
) -> tuple[bool, str]:
    """End-to-end activation: prefer the new binary's own activate path,
    fall back to direct OS supervisor restart.

    Returns ``(activated, route)`` where ``route`` is one of
    ``"service-activate"`` (the binary's CLI took it), ``"kickstart"``
    (we restarted the supervisor directly), or ``"none"`` (both paths
    failed). Caller uses ``route`` for logging / observability.

    Modes
    -----

    ``detached=False`` (default) — synchronous: run service-activate,
    wait up to ``SERVICE_ACTIVATE_TIMEOUT_S``, observe returncode. Used
    by the fetcher where blocking ~60s on the activate step is
    acceptable (the fetcher is itself detached from SessionStart).

    ``detached=True`` — fire-and-forget: spawn service-activate in a
    new session, return immediately. Used by the SessionStart bootstrap
    where the hook MUST NOT block the user. The fallback kickstart is
    skipped in this mode (the supervisor's own KeepAlive will eventually
    respawn against the new symlink even if the detached activate
    silently fails). Returns ``("service-activate", route)`` on
    successful spawn; ``("none", ...)`` only if Popen itself raises.

    ``stable_path`` is the current/<target> symlink — passed to
    ``service-activate --bin`` so the new binary's CLI knows which
    stable path to rewrite into the supervisor unit. When ``None`` the
    sub-command resolves it via om_core_paths internally.
    """
    if detached:
        try:
            popen_kwargs: dict = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "close_fds": True,
            }
            if sys.platform == "win32":
                # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                popen_kwargs["creationflags"] = 0x00000208
            else:
                popen_kwargs["start_new_session"] = True
            cmd = [str(bin_path), "service-activate"]
            if stable_path is not None:
                cmd += ["--bin", str(stable_path)]
            subprocess.Popen(cmd, **popen_kwargs)
            return True, "service-activate"
        except OSError:
            return False, "none"

    # Synchronous: run service-activate, observe outcome.
    try:
        cmd = [str(bin_path), "service-activate"]
        if stable_path is not None:
            cmd += ["--bin", str(stable_path)]
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=SERVICE_ACTIVATE_TIMEOUT_S, check=False,
        )
        if proc.returncode == 0:
            return True, "service-activate"
    except (subprocess.TimeoutExpired, OSError):
        pass

    # Fall back to direct OS supervisor restart.
    if restart_supervised_service():
        return True, "kickstart"
    return False, "none"
