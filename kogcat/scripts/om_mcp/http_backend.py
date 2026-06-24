"""HTTP backend — stdlib UDS client to the om-core sidecar.

Pure standard library (no httpx / no third-party deps): the MCP server
runs on whatever python3 is already on the machine — e.g. the macOS
system interpreter — with no bootstrap, no vendored ``lib/``.

Talks to the sidecar over an AF_UNIX socket using ``http.client``.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import time
import io
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
import om_core_paths  # noqa: E402
from om_supervisor import trigger_log  # noqa: E402

from . import paths
from .errors import (
    OmApiError,
    OmCoreBinNotFound,
    OmServerStartupTimeout,
    OmSidecarUnavailable,
    OmSidecarUnhealthy,
)

# Compat gate. om-core uses a series-reset api_minor model — CHANGELOG 0.32.0:
# "__api_minor__ resets to 0 inside the new 0.32.x series (additive bumps
# within the series resume from there)." api_minor is not comparable across
# series; we anchor on a minimum (major, minor) series parsed from
# `binary_version`, plus a minimum api_minor *within* that series.
MY_REQUIRED_SERIES = (0, 36)
# 7: om-core owns the official calibration pack manager (status/install/
# upgrade); the SessionStart official-pack hook + status row gate on it.
MY_REQUIRED_API_MINOR = 7

_REQUEST_TIMEOUT = 30.0
_SELF_HEAL_WAIT_S = 5.0

# Transport-level failures (stdlib). A non-idempotent tool that hits one of
# these is reported as om.sidecar_unreachable; idempotent tools retry once.
TRANSPORT_ERRORS = (OSError, http.client.HTTPException, EOFError)
CAPS_WAIT_ERRORS = (OmSidecarUnhealthy, ValueError, *TRANSPORT_ERRORS)


def _parse_series(version: str) -> tuple[int, int] | None:
    try:
        parts = version.split(".")
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return None


def _check_caps_compat(caps: dict) -> None:
    binary_version = str(caps.get("binary_version", ""))
    series = _parse_series(binary_version)
    if series is None:
        raise OmSidecarUnhealthy(
            f"om-core binary_version={binary_version!r} unparseable",
            hint="Expected 'X.Y.Z'. Reinstall a known-good binary.",
        )
    if series < MY_REQUIRED_SERIES:
        req = f"{MY_REQUIRED_SERIES[0]}.{MY_REQUIRED_SERIES[1]}.x"
        raise OmSidecarUnhealthy(
            f"om-core binary_version={binary_version} (series "
            f"{series[0]}.{series[1]}.x) but wrapper requires >= {req}",
            hint=(
                "Upgrade the supervised om-core binary (/plugin update om + "
                "restart Claude Code, or tools/pull-om-core.sh + "
                "`om-core install-service`)."
            ),
        )
    if series == MY_REQUIRED_SERIES and int(caps.get("api_minor", 0)) < MY_REQUIRED_API_MINOR:
        raise OmSidecarUnhealthy(
            f"om-core api_minor={caps.get('api_minor')} in series "
            f"{series[0]}.{series[1]}.x but wrapper requires "
            f">= {MY_REQUIRED_API_MINOR} within this series",
            hint=(
                f"Upgrade the om-core binary within the "
                f"{MY_REQUIRED_SERIES[0]}.{MY_REQUIRED_SERIES[1]}.x series."
            ),
        )


def _plugin_root() -> Path:
    for key in ("CODEX_PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"):
        if (raw := os.environ.get(key, "").strip()):
            return Path(raw).expanduser()
    return Path(__file__).resolve().parents[2]


def _run_startup_maintenance_once() -> None:
    """Best-effort convergence path for hosts whose SessionStart hook missed.

    The bootstrap hook is non-blocking: it resolves channel, writes the expected
    release pointer, and spawns the detached fetcher only when needed. Running it
    here is cheap on cache hit and gives Codex/other hosts a second route to
    converge after plugin update.
    """
    root = _plugin_root()
    env = os.environ.copy()
    env.setdefault("CLAUDE_PLUGIN_ROOT", str(root))
    env.setdefault("CODEX_PLUGIN_ROOT", str(root))
    scripts = [
        root / "hooks" / "bootstrap_om_core_bin.py",
        root / "hooks" / "install_om_service.py",
    ]
    for script in scripts:
        if not script.is_file():
            trigger_log(f"maintenance SKIP missing {script.name} (root={root})")
            continue
        trigger_log(f"maintenance running {script.name}")
        try:
            proc = subprocess.run(
                [sys.executable, str(script)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                timeout=20,
                check=False,
                creationflags=_NO_WINDOW,
                text=True,
            )
            out = (proc.stdout or "").strip().replace("\n", " | ")
            trigger_log(
                f"maintenance {script.name} exit={proc.returncode}"
                + (f" :: {out[-600:]}" if out else "")
            )
        except subprocess.TimeoutExpired as e:
            out = e.output or ""
            if isinstance(out, bytes):
                out = out.decode("utf-8", errors="replace")
            out = out.strip().replace("\n", " | ")
            trigger_log(
                f"maintenance {script.name} TIMEOUT 20s"
                + (f" :: {out[-600:]}" if out else "")
            )
            return
        except OSError as e:
            trigger_log(f"maintenance {script.name} OSError {e}")
            return


_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW (win32)
_DETACHED_NO_WINDOW = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | NEW_PROCESS_GROUP


def _wait_for_caps_compat(cfg: dict[str, Any], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = _raw_request(cfg, "GET", "/v1/capabilities", timeout=2.0)
            if r.is_success:
                _check_caps_compat(r.json())
                return True
        except CAPS_WAIT_ERRORS:
            pass
        time.sleep(0.25)
    return False


def _direct_spawn_enabled() -> bool:
    val = os.environ.get("OM_ALLOW_DIRECT_SPAWN", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def resolve_om_core_path() -> Path:
    """Find an om-core binary, raise OmCoreBinNotFound on miss."""
    if (env := os.environ.get("OM_CORE_BIN", "").strip()):
        if (p := om_core_paths.resolve_existing_bin()) is not None:
            return p
        raise OmCoreBinNotFound(f"OM_CORE_BIN={env} does not exist")

    if (p := om_core_paths.resolve_existing_bin()) is not None:
        return p

    raise OmCoreBinNotFound(
        "no om-core binary found",
        hint=(
            "Reinstall the om plugin from marketplace, run "
            "tools/pull-om-core.sh, or set OM_CORE_BIN."
        ),
    )


def _read_server_json() -> dict[str, Any] | None:
    p = paths.server_json()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _is_pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ProcessLookupError, ValueError):
        return False


def _is_uds_listening(sp: str) -> bool:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(sp)
        return True
    except (FileNotFoundError, ConnectionRefusedError, OSError):
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass

def _is_npipe_listening(pipe_name: str) -> bool:
    if sys.platform != "win32":
        return False
    try:
        conn = _NPipeConnection(pipe_name, timeout=0.5)
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        resp.read()
        return resp.status == 200
    except TRANSPORT_ERRORS:
        return False
    finally:
        try:
            conn.close()  # type: ignore[possibly-undefined]
        except Exception:
            pass

def _pick_transport(cfg: dict[str, Any]) -> tuple[str, str]:
    """Validate cfg and return (kind, target)."""
    transport_kind = cfg.get("transport", "uds")
    if transport_kind == "uds":
        sp = cfg.get("socket_path")
        if not isinstance(sp, str):
            raise OmSidecarUnavailable(
                "server.json missing socket_path",
                hint="Run `om-core install-service` to register the supervisor.",
        )
        return ("uds", sp)
    if transport_kind == "npipe":
        pipe = cfg.get("pipe_name")
        if not isinstance(pipe, str):
            raise OmSidecarUnavailable(
                "server.json missing pipe_name",
                hint="Run `om-core install-service` to register the supervisor.",
            )
        return ("npipe", pipe)
    raise OmSidecarUnavailable(
        f"server.json transport={transport_kind!r} not supported by this wrapper.",
        hint="Upgrade plugin / om-core to a matching version.",
    )


def _auth_headers(cfg: dict[str, Any]) -> dict[str, str]:
    token = cfg.get("token")
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def _spawn_sidecar(bin_path: Path) -> None:
    """Spawn om-core detached. Only used when OM_ALLOW_DIRECT_SPAWN=1."""
    log_path = paths.log_dir()
    log_path.mkdir(parents=True, exist_ok=True)
    log_file = log_path / "om-core.log"

    sidecar_env = os.environ.copy()
    cfg_dir = paths.config_dir()
    cfg_dir.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": open(log_file, "ab"),  # noqa: SIM115
        "stderr": subprocess.STDOUT,
        "close_fds": True,
        "env": sidecar_env,
        "cwd": str(cfg_dir),
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = _DETACHED_NO_WINDOW
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen([str(bin_path), "serve"], **kwargs)


def _live_cfg() -> dict[str, Any] | None:
    """Return cfg if server.json points at a live listener, else None."""
    cfg = _read_server_json()
    if not cfg:
        return None
    kind, target = _pick_transport(cfg)
    if kind == "uds" and _is_uds_listening(target):
        return cfg
    if kind == "npipe" and _is_npipe_listening(target):
        return cfg
    return None


def _wait_for_transport(timeout: float) -> dict[str, Any]:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        cfg = _live_cfg()
        if cfg and _is_pid_alive(cfg.get("pid")):
            return cfg
        time.sleep(0.1)
    raise OmServerStartupTimeout(
        f"om-core did not become ready within {timeout}s",
        hint=f"Check {paths.log_dir() / 'om-core.log'} for startup errors.",
    )


def _warn_direct_spawn() -> None:
    print(
        "WARNING om: OM_ALLOW_DIRECT_SPAWN=1 active; "
        "bypassing supervisor lifecycle (advanced / CI use only).",
        file=sys.stderr,
    )


def _direct_spawn_path() -> dict[str, Any]:
    _warn_direct_spawn()
    if (cfg := _live_cfg()) is not None:
        return cfg
    bin_path = resolve_om_core_path()
    _spawn_sidecar(bin_path)
    return _wait_for_transport(timeout=30.0)


_NOT_RUNNING_HINT = (
    "Run `om-core install-service` to register the supervisor "
    "(launchd on macOS / SCM or Task Scheduler on Windows / "
    "systemd --user on Linux). For one-off bypass set "
    "`OM_ALLOW_DIRECT_SPAWN=1` in your shell."
)
_NOT_RUNNING_MSG = (
    "om-core service not running (no live sidecar transport at the canonical path)."
)


def _resolve_cfg() -> dict[str, Any]:
    if (cfg := _live_cfg()) is not None:
        return cfg
    if _direct_spawn_enabled():
        return _direct_spawn_path()
    # No live sidecar. A host that skipped SessionStart (Codex on Windows: the
    # hook's shell expansion doesn't run) would otherwise never fetch. Kick the
    # in-process self-heal daemon (idempotent; it downloads in a background
    # thread — never blocks this call) and surface a progress-bearing error.
    try:
        from om_mcp import self_heal  # noqa: PLC0415
        self_heal.start_self_heal_thread()
    except Exception:  # noqa: BLE001
        pass
    raise OmSidecarUnavailable(_NOT_RUNNING_MSG, hint=_NOT_RUNNING_HINT)


# --- transport: AF_UNIX HTTP/1.1 over http.client --------------------------


class _UDSConnection(http.client.HTTPConnection):
    """http.client connection that dials an AF_UNIX socket."""

    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:  # noqa: D401
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


class _PipeHandleIO(io.RawIOBase):
    "Raw IO over a pipe handle owned (refcounted) by its _PipeSocket."
    def __init__(self, sock: "_PipeSocket"):
        self._sock = sock

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return True

    def readinto(self, b: bytearray | memoryview) -> int:
        data = _npipe_read(self._sock._handle, len(b))
        if not data:
            return 0
        b[: len(data)] = data
        return len(data)

    def write(self, b: bytes | bytearray | memoryview) -> int:
        data = bytes(b)
        _npipe_write_all(self._sock._handle, data)
        return len(data)

    def close(self) -> None:
        if not self.closed:
            self._sock._decref()
        super().close()


class _PipeSocket:
    "Mirror socket.makefile refcounting: the handle closes only once the socket AND every makefile stream are closed (http.client closes the socket on Connection:close before the body is read)."
    def __init__(self, handle: int):
        self._handle: int | None = handle
        self._io_refs = 0
        self._closed = False

    def sendall(self, data: bytes) -> None:
        _npipe_write_all(self._handle, bytes(data))

    def makefile(self, mode: str, buffering: int | None = None):
        if "b" not in mode:
            mode += "b"
        self._io_refs += 1
        raw = _PipeHandleIO(self)
        if "r" in mode and "w" in mode:
            return io.BufferedRWPair(raw, raw)
        if "w" in mode:
            return io.BufferedWriter(raw)
        return io.BufferedReader(raw)

    def _decref(self) -> None:
        self._io_refs -= 1
        if self._io_refs <= 0 and self._closed:
            self._real_close()

    def close(self) -> None:
        self._closed = True
        if self._io_refs <= 0:
            self._real_close()

    def _real_close(self) -> None:
        if self._handle is not None:
            _npipe_close(self._handle)
            self._handle = None


class _NPipeConnection(http.client.HTTPConnection):
    def __init__(self, pipe_name: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._pipe_name = pipe_name

    def connect(self) -> None:
        self.sock = _PipeSocket(_npipe_open(self._pipe_name, timeout=float(self.timeout)))


def _npipe_open(pipe_name: str, *, timeout: float) -> int:
    if sys.platform != "win32":
        raise OSError("Windows named-pipe transport is only available on win32")
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    OPEN_EXISTING = 3
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
    ERROR_PIPE_BUSY = 231
    deadline = time.monotonic() + timeout
    while True:
        handle = kernel32.CreateFileW(
            pipe_name,
            GENERIC_READ | GENERIC_WRITE,
            0,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if handle != INVALID_HANDLE_VALUE:
            return int(handle)
        err = ctypes.get_last_error()
        if err != ERROR_PIPE_BUSY or time.monotonic() >= deadline:
            raise OSError(err, f"CreateFileW failed for {pipe_name}")
        kernel32.WaitNamedPipeW(pipe_name, max(1, int((deadline - time.monotonic()) * 1000)))


def _npipe_read(handle: int, size: int) -> bytes:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    buf = ctypes.create_string_buffer(size)
    read = wintypes.DWORD(0)
    ok = kernel32.ReadFile(handle, buf, size, ctypes.byref(read), None)
    if not ok and read.value == 0:
        err = ctypes.get_last_error()
        # Server closing the pipe after its HTTP response is normal EOF, not a
        # fault: BROKEN_PIPE(109)/PIPE_NOT_CONNECTED(233)/NO_DATA(232)/EOF(38).
        if err in (109, 233, 232, 38):
            return b""
        raise OSError(err, "ReadFile failed")
    return buf.raw[: read.value]


def _npipe_write_all(handle: int, data: bytes) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    view = memoryview(data)
    while view:
        written = wintypes.DWORD(0)
        chunk = view.tobytes()
        ok = kernel32.WriteFile(
            handle,
            ctypes.c_char_p(chunk),
            len(chunk),
            ctypes.byref(written),
            None,
        )
        if not ok:
            raise OSError(ctypes.get_last_error(), "WriteFile failed")
        view = view[written.value :]


def _npipe_close(handle: int) -> None:
    if sys.platform == "win32":
        import ctypes

        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)


class Response:
    """Minimal response wrapper (the subset tools.py / raise_for_response use)."""

    __slots__ = ("status_code", "_body", "reason_phrase")

    def __init__(self, status_code: int, body: bytes, reason: str) -> None:
        self.status_code = status_code
        self._body = body
        self.reason_phrase = reason

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def text(self) -> str:
        return self._body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        if not self._body:
            return {}
        return json.loads(self._body.decode("utf-8"))


_cfg_cache: dict[str, Any] | None = None
_caps_checked = False


def reset() -> None:
    """Drop cached sidecar cfg + caps flag (after a transport failure)."""
    global _cfg_cache, _caps_checked
    _cfg_cache = None
    _caps_checked = False


# Back-compat alias for callers that used the httpx client lifecycle.
aclose = reset


def _get_cfg() -> dict[str, Any]:
    global _cfg_cache
    if _cfg_cache is not None:
        try:
            kind, target = _pick_transport(_cfg_cache)
        except OmSidecarUnavailable:
            kind, target = "", ""
        if kind == "uds" and _is_uds_listening(target):
            return _cfg_cache
        if kind == "npipe" and _is_npipe_listening(target):
            return _cfg_cache
        _cfg_cache = None
    cfg = _resolve_cfg()
    _pick_transport(cfg)
    _cfg_cache = cfg
    return cfg


def _raw_request(
    cfg: dict[str, Any],
    method: str,
    path: str,
    *,
    json_body: Any = None,
    params: dict[str, Any] | None = None,
    timeout: float = _REQUEST_TIMEOUT,
) -> Response:
    full = path
    if params:
        from urllib.parse import urlencode

        q = {k: v for k, v in params.items() if v is not None}
        if q:
            full = f"{path}?{urlencode(q)}"
    headers = _auth_headers(cfg)
    body: bytes | None = None
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    kind, target = _pick_transport(cfg)
    if kind == "uds":
        conn: http.client.HTTPConnection = _UDSConnection(target, timeout)
    elif kind == "npipe":
        conn = _NPipeConnection(target, timeout)
    else:
        raise OmSidecarUnavailable(
            f"transport={kind!r} not implemented",
            hint="Upgrade plugin / om-core to a matching version.",
        )
    try:
        conn.request(method, full, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        return Response(resp.status, data, resp.reason)
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _ensure_caps(cfg: dict[str, Any]) -> None:
    global _caps_checked
    if _caps_checked:
        return
    try:
        r = _raw_request(cfg, "GET", "/v1/capabilities", timeout=10.0)
    except TRANSPORT_ERRORS as e:
        raise OmSidecarUnhealthy(
            f"sidecar capabilities probe failed: {e}",
            hint=(
                f"Check {paths.log_dir() / 'om-core.log'} for tracebacks. "
                "If supervised, check `om-core service-status`."
            ),
        ) from e
    if not r.is_success:
        raise OmSidecarUnhealthy(
            f"sidecar capabilities probe returned HTTP {r.status_code}",
            hint=f"Check {paths.log_dir() / 'om-core.log'} for tracebacks.",
        )
    try:
        _check_caps_compat(r.json())
    except OmSidecarUnhealthy:
        _run_startup_maintenance_once()
        reset()
        if (new_cfg := _live_cfg()) is not None and _wait_for_caps_compat(new_cfg, _SELF_HEAL_WAIT_S):
            _caps_checked = True
            return
        raise
    _caps_checked = True


def request(
    method: str,
    path: str,
    *,
    json_body: Any = None,
    params: dict[str, Any] | None = None,
    timeout: float = _REQUEST_TIMEOUT,
) -> Response:
    """Issue one HTTP request to a live, version-checked om-core sidecar.

    On 401/403 the cached cfg holds a stale bearer token: a sidecar restart
    rotates server.json's token but keeps the same socket path, so the liveness
    check passes and _get_cfg() returns the old token. Drop the cache, re-read
    server.json, and retry once. A 401/403 is rejected before processing, so the
    retry is safe for writes too.
    """
    cfg = _get_cfg()
    _ensure_caps(cfg)
    resp = _raw_request(
        cfg, method, path, json_body=json_body, params=params, timeout=timeout
    )
    if resp.status_code in (401, 403):
        reset()
        cfg = _get_cfg()
        _ensure_caps(cfg)
        resp = _raw_request(
            cfg, method, path, json_body=json_body, params=params, timeout=timeout
        )
    return resp


def raise_for_response(resp: Response) -> None:
    """Translate non-2xx into OmApiError using the standard error envelope."""
    if resp.is_success:
        return
    try:
        body = resp.json()
        if not isinstance(body, dict):
            body = {}
    except (ValueError, json.JSONDecodeError):
        body = {}
    code = body.get("code") or f"HTTP_{resp.status_code}"
    message = body.get("message") or resp.text[:500] or resp.reason_phrase
    raise OmApiError(
        code=code,
        message=message,
        hint=body.get("hint"),
        details=body.get("details"),
        status_code=resp.status_code,
    )
