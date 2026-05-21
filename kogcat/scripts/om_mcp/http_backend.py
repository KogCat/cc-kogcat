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
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
import om_core_paths  # noqa: E402

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
MY_REQUIRED_SERIES = (0, 35)
# 1: the embedding warmup gate calls GET /v1/embedding/status, added in
# om-core 0.35.4 (api_minor 1 within the 0.35.x series).
MY_REQUIRED_API_MINOR = 1

_REQUEST_TIMEOUT = 30.0

# Transport-level failures (stdlib). A non-idempotent tool that hits one of
# these is reported as om.sidecar_unreachable; idempotent tools retry once.
TRANSPORT_ERRORS = (OSError, http.client.HTTPException, EOFError)


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
    if transport_kind == "pipe":
        raise OmSidecarUnavailable(
            "Windows named-pipe transport not yet supported.",
            hint="Run on POSIX, or set OM_TRANSPORT=uds if your platform supports AF_UNIX.",
        )
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
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": open(log_file, "ab"),  # noqa: SIM115
        "stderr": subprocess.STDOUT,
        "close_fds": True,
        "env": sidecar_env,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000208  # CREATE_NEW_PROCESS_GROUP | DETACHED
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen([str(bin_path), "serve"], **kwargs)


def _live_uds_cfg() -> dict[str, Any] | None:
    """Return cfg if server.json points at a live UDS listener, else None."""
    cfg = _read_server_json()
    if cfg and cfg.get("transport") == "uds":
        sp = cfg.get("socket_path")
        if isinstance(sp, str) and _is_uds_listening(sp):
            return cfg
    return None


def _wait_for_uds(timeout: float) -> dict[str, Any]:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        cfg = _live_uds_cfg()
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
    if (cfg := _live_uds_cfg()) is not None:
        return cfg
    bin_path = resolve_om_core_path()
    _spawn_sidecar(bin_path)
    return _wait_for_uds(timeout=30.0)


_NOT_RUNNING_HINT = (
    "Run `om-core install-service` to register the supervisor "
    "(launchd on macOS / SCM or Task Scheduler on Windows / "
    "systemd --user on Linux). For one-off bypass set "
    "`OM_ALLOW_DIRECT_SPAWN=1` in your shell."
)
_NOT_RUNNING_MSG = (
    "om-core service not running (no live UDS listener at the canonical path)."
)


def _resolve_cfg() -> dict[str, Any]:
    if (cfg := _live_uds_cfg()) is not None:
        return cfg
    if _direct_spawn_enabled():
        return _direct_spawn_path()
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
        sp = _cfg_cache.get("socket_path")
        if isinstance(sp, str) and _is_uds_listening(sp):
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
    conn = _UDSConnection(str(cfg["socket_path"]), timeout)
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
    _check_caps_compat(r.json())
    _caps_checked = True


def request(
    method: str,
    path: str,
    *,
    json_body: Any = None,
    params: dict[str, Any] | None = None,
    timeout: float = _REQUEST_TIMEOUT,
) -> Response:
    """Issue one HTTP request to a live, version-checked om-core sidecar."""
    cfg = _get_cfg()
    _ensure_caps(cfg)
    return _raw_request(
        cfg, method, path, json_body=json_body, params=params, timeout=timeout
    )


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
