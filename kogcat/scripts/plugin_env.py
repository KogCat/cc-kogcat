"""Host-neutral plugin environment helpers.

Claude Code historically exports CLAUDE_PLUGIN_ROOT. Codex may launch the
same plugin with a different root env, or no root env in shell-like contexts.
Every runtime script can still self-resolve from this file's location.
"""
from __future__ import annotations

import os
from pathlib import Path


def plugin_root(fallback_file: str | None = None) -> Path:
    for key in ("CODEX_PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"):
        if (raw := os.environ.get(key, "").strip()):
            return Path(raw).expanduser()
    if fallback_file:
        return Path(fallback_file).resolve().parent.parent
    return Path(__file__).resolve().parent.parent


def plugin_data() -> Path | None:
    for key in ("CODEX_PLUGIN_DATA", "CLAUDE_PLUGIN_DATA"):
        if (raw := os.environ.get(key, "").strip()):
            return Path(raw).expanduser()
    return None


def export_compat_env(root: Path, data: Path | None = None) -> None:
    root_s = str(root)
    os.environ.setdefault("CLAUDE_PLUGIN_ROOT", root_s)
    os.environ.setdefault("CODEX_PLUGIN_ROOT", root_s)
    if data is not None:
        data_s = str(data)
        os.environ.setdefault("CLAUDE_PLUGIN_DATA", data_s)
        os.environ.setdefault("CODEX_PLUGIN_DATA", data_s)
