"""Cross-platform config / log dirs for the wrapper.

Re-implements ``om_core.infra.paths`` so the wrapper resolves the same
``server.json`` the sidecar wrote, without importing ``om_core`` (only
the binary may be on disk). Overrides: ``OM_CONFIG_HOME``, ``OM_LOG_HOME``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP = "om"


def _override(env_var: str, default: Path) -> Path:
    value = os.environ.get(env_var, "").strip()
    return Path(value).expanduser().resolve() if value else default


def config_dir() -> Path:
    if sys.platform == "darwin":
        default = Path.home() / "Library" / "Application Support" / APP
    elif sys.platform == "win32":
        # om-core config_dir = platformdirs user_config_dir = CSIDL_LOCAL_APPDATA
        # (%LOCALAPPDATA%), NOT Roaming. Must match or the wrapper reads the
        # wrong server.json.
        default = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP
    else:
        default = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP
    return _override("OM_CONFIG_HOME", default)


def log_dir() -> Path:
    if sys.platform == "darwin":
        default = Path.home() / "Library" / "Logs" / APP
    elif sys.platform == "win32":
        default = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP / "Logs"
    else:
        default = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / APP
    return _override("OM_LOG_HOME", default)


def server_json() -> Path:
    return config_dir() / "server.json"


def active_kb_json() -> Path:
    return config_dir() / "active_kb.json"


def socket_path() -> Path:
    """POSIX UDS path. Mirrors ``om_core.infra.paths.socket_path``."""
    if sys.platform == "win32":
        raise NotImplementedError(
            "Use pipe_name() on Windows; socket_path() is POSIX-only."
        )
    return config_dir() / "om.sock"


def pipe_name() -> str:
    """Windows named-pipe path. Per-user hashed to avoid terminal-server collisions."""
    if sys.platform != "win32":
        raise NotImplementedError(
            "Use socket_path() on POSIX; pipe_name() is Windows-only."
        )
    import hashlib

    user = _windows_user_sid() or os.environ.get("USERNAME", "anon")
    digest = hashlib.sha256(user.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
    return rf"\\.\pipe\om-{digest}"


def _windows_user_sid() -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            return None
        try:
            needed = wintypes.DWORD(0)
            advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
            buf = ctypes.create_string_buffer(needed.value)
            if not advapi32.GetTokenInformation(token, 1, buf, needed, ctypes.byref(needed)):
                return None
            sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            sid_text = wintypes.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(sid_text)):
                return None
            try:
                return sid_text.value
            finally:
                kernel32.LocalFree(sid_text)
        finally:
            kernel32.CloseHandle(token)
    except Exception:
        return None
