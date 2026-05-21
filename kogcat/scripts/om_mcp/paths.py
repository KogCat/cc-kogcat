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
        default = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP
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

    user = os.environ.get("USERNAME", "anon")
    digest = hashlib.sha1(user.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
    return rf"\\.\pipe\om-{digest}"
