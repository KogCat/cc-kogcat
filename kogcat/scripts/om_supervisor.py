"Sidecar supervisor primitives — read version, restart, activate. Stdlib-only."
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SERVICE_LABEL = "com.kogcat.om"
_RESTART_TIMEOUT_S = 10.0
SERVICE_ACTIVATE_TIMEOUT_S = 60.0
SERVICE_STATUS_TIMEOUT_S = 15.0
INSTALL_SERVICE_TIMEOUT_S = 45.0
_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
# Synchronous om-core-bin CLI calls run with DETACHED_PROCESS (no console),
# never CREATE_NO_WINDOW: a hidden-console alloc hangs the console-subsystem
# binary's startup under Codex's MCP-server ConPTY. See docs/03-binary-lifecycle.md.
_DETACHED_PROCESS = 0x00000008 if sys.platform == "win32" else 0
_ERROR_ACCESS_DENIED = 5


def _trigger_log_path() -> Path:
    "Durable breadcrumb log for the fetch trigger/spawn path (mirrors om_mcp.paths.log_dir; stdlib-only)."
    if (env := os.environ.get("OM_LOG_HOME", "").strip()):
        base = Path(env).expanduser()
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Logs" / "om"
    elif sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        base = (Path(local) if local else Path.home() / "AppData" / "Local") / "om" / "Logs"
    else:
        state = os.environ.get("XDG_STATE_HOME", "")
        base = (Path(state).expanduser() if state else Path.home() / ".local" / "state") / "om"
    return base / "om-core-trigger.log"


def trigger_log(msg: str) -> None:
    "Append a timestamped breadcrumb to the trigger log; temp-dir fallback; never raises."
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{stamp} [pid {os.getpid()}] {msg}\n"
    for target in (_trigger_log_path(), Path(tempfile.gettempdir()) / "om-core-trigger.log"):
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as f:
                f.write(line)
            return
        except OSError:
            continue


def _cmd_label(cmd: list[str]) -> str:
    "Short, secret-free identity for a spawn cmd (the GitHub token is never passed as an arg)."
    parts = [os.path.basename(p) for p in cmd[:2]]
    if "--version" in cmd:
        i = cmd.index("--version")
        if i + 1 < len(cmd):
            parts.append("v" + cmd[i + 1])
    return " ".join(parts) or "(empty)"


def _spawn_via_wmi(cmd: list[str], cwd: str | None) -> bool:
    """Win32_Process.Create — child reparents to WmiPrvSE, escaping any caller job.

    The child gets a fresh env, so we replay the caller env via
    Win32_ProcessStartup.EnvironmentVariables (the powershell broker inherits
    it through `env=`, so it reads its own environment — no serialisation /
    quoting). Falls back to an env-less Create if that property is rejected,
    so a download still launches (loses proxy / token overrides only).
    """
    env = os.environ.copy()
    env["_OM_WMI_CMDLINE"] = subprocess.list2cmdline(cmd)
    if cwd:
        env["_OM_WMI_CWD"] = cwd
    ps = "\n".join([
        "$ErrorActionPreference='Stop'",
        "$cmdline=$env:_OM_WMI_CMDLINE",
        "$cwd=$env:_OM_WMI_CWD",
        "function New-OmProc($startup){",
        "  $a=@{CommandLine=$cmdline}",
        "  if($cwd){$a['CurrentDirectory']=$cwd}",
        "  if($startup){$a['ProcessStartupInformation']=$startup}",
        "  Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments $a",
        "}",
        "$r=$null",
        "try{",
        "  $L=[System.Collections.Generic.List[string]]::new()",
        "  foreach($e in [System.Environment]::GetEnvironmentVariables().GetEnumerator()){",
        "    if(@('_OM_WMI_CMDLINE','_OM_WMI_CWD') -notcontains $e.Key){$L.Add(\"$($e.Key)=$($e.Value)\")}",
        "  }",
        "  $s=New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{EnvironmentVariables=$L.ToArray()}",
        "  $r=New-OmProc $s",
        "}catch{$r=$null}",
        "if($null -eq $r -or $r.ReturnValue -ne 0){$r=New-OmProc $null}",
        "exit [int]$r.ReturnValue",
    ])
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            env=env, capture_output=True, text=True, timeout=30,
            creationflags=_CREATE_NO_WINDOW,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def spawn_detached(cmd: list[str], *, cwd: str | None = None) -> bool:
    """Spawn cmd so it outlives the caller's job/session. Returns True on spawn.

    Windows: a child spawned with only CREATE_NO_WINDOW|NEW_PROCESS_GROUP stays
    a member of the caller's job (Codex hook / mcp self-heal run inside one); if
    that job tears down before a slow first-install download finishes the child
    is killed mid-stream. CREATE_BREAKAWAY_FROM_JOB escapes it (preserving env);
    on jobs that forbid breakaway (ERROR_ACCESS_DENIED) fall back to WMI, which
    reparents outside any job. POSIX: new session fully detaches.
    """
    label = _cmd_label(cmd)
    base = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "cwd": cwd,
    }
    if sys.platform != "win32":
        try:
            subprocess.Popen(cmd, start_new_session=True, **base)
            trigger_log(f"spawn_detached posix new-session OK: {label}")
            return True
        except OSError as e:
            trigger_log(f"spawn_detached posix FAILED: {label} ({e})")
            return False
    try:
        subprocess.Popen(
            cmd,
            creationflags=_CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP | _CREATE_BREAKAWAY_FROM_JOB,
            **base,
        )
        trigger_log(f"spawn_detached win breakaway-from-job OK: {label}")
        return True
    except OSError as e:
        if getattr(e, "winerror", None) != _ERROR_ACCESS_DENIED:
            trigger_log(
                f"spawn_detached win breakaway FAILED winerror={getattr(e, 'winerror', None)}: {label} ({e})"
            )
            return False
        trigger_log(f"spawn_detached win breakaway ACCESS_DENIED → WMI fallback: {label}")
    ok = _spawn_via_wmi(cmd, cwd)
    trigger_log(f"spawn_detached win WMI fallback {'OK' if ok else 'FAILED'}: {label}")
    return ok


def _server_config_path() -> Path:
    env = os.environ.get("OM_CONFIG_HOME", "").strip()
    if env:
        return Path(env).expanduser() / "server.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "om" / "server.json"
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "om" / "server.json"
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "om" / "server.json"


def _neutral_cwd() -> str | None:
    "Stable dir (om config dir) for spawned-process cwd so it never holds the plugin cache (Windows: a held cwd blocks plugin delete/rename)."
    try:
        d = _server_config_path().parent
        d.mkdir(parents=True, exist_ok=True)
        return str(d)
    except OSError:
        if sys.platform == "win32":
            root = os.environ.get("SYSTEMDRIVE", "C:") + "\\"
            return root if os.path.isdir(root) else None
        return "/"


def _is_pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ProcessLookupError, ValueError):
        return False


def read_running_sidecar_version() -> str | None:
    "Return supervised sidecar binary_version, or None if unobservable."
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
    "Direct OS supervisor restart. Returns True on rc=0."
    if sys.platform == "darwin":
        proc = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{SERVICE_LABEL}"],
            capture_output=True, text=True, timeout=_RESTART_TIMEOUT_S, check=False,
        )
        return proc.returncode == 0
    if sys.platform.startswith("linux"):
        proc = subprocess.run(
            ["systemctl", "--user", "restart", "om-core.service"],
            capture_output=True, text=True, timeout=_RESTART_TIMEOUT_S, check=False,
        )
        return proc.returncode == 0
    # win32: supervision is a schtasks task / Run key, not an SCM service —
    # restart is already folded into service-activate, so no separate path.
    return False


def service_status(bin_path: Path) -> dict | None:
    "Run `<bin> service-status`, return parsed dict or None if unobservable."
    try:
        proc = subprocess.run(
            [str(bin_path), "service-status"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=SERVICE_STATUS_TIMEOUT_S, check=False,
            creationflags=_DETACHED_PROCESS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except (ValueError, json.JSONDecodeError):
        return None


def trigger_install_service(
    bin_path: Path,
    stable_path: Path | None = None,
    *,
    detached: bool = False,
) -> tuple[bool, str]:
    "Run `<bin> install-service [--bin <stable>]` to register+autostart. Returns (ok, route)."
    cmd = [str(bin_path), "install-service"]
    if stable_path is not None:
        cmd += ["--bin", str(stable_path)]
    if detached:
        return (True, "install-service") if spawn_detached(cmd, cwd=_neutral_cwd()) else (False, "none")

    try:
        proc = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=INSTALL_SERVICE_TIMEOUT_S, check=False,
            creationflags=_DETACHED_PROCESS, cwd=_neutral_cwd(),
        )
        if proc.returncode == 0:
            return True, "install-service"
    except (subprocess.TimeoutExpired, OSError):
        pass
    return False, "none"


def trigger_service_activate(
    bin_path: Path,
    stable_path: Path | None = None,
    *,
    detached: bool = False,
) -> tuple[bool, str]:
    "Run `<bin> service-activate`, fall back to direct restart. Returns (ok, route)."
    if detached:
        cmd = [str(bin_path), "service-activate"]
        if stable_path is not None:
            cmd += ["--bin", str(stable_path)]
        return (True, "service-activate") if spawn_detached(cmd, cwd=_neutral_cwd()) else (False, "none")

    try:
        cmd = [str(bin_path), "service-activate"]
        if stable_path is not None:
            cmd += ["--bin", str(stable_path)]
        proc = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=SERVICE_ACTIVATE_TIMEOUT_S, check=False,
            creationflags=_DETACHED_PROCESS, cwd=_neutral_cwd(),
        )
        if proc.returncode == 0:
            return True, "service-activate"
    except (subprocess.TimeoutExpired, OSError):
        pass

    if restart_supervised_service():
        return True, "kickstart"
    return False, "none"
