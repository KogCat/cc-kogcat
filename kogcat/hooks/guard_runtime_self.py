#!/usr/bin/env python3
"""PreToolUse hook: block agent writes to KogCat runtime files."""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path


MUTATING_COMMANDS = {
    "rm", "mv", "cp", "chmod", "chown", "truncate", "dd", "tee",
    "ln", "rmdir", "touch", "install", "rsync", "tar", "unzip", "ditto",
}
# Commands that write/remove their positional operands (mv removes its source).
OPERAND_WRITE_COMMANDS = {
    "rm", "rmdir", "mv", "chmod", "chown", "truncate", "dd", "touch", "tee",
}
# Commands that write only their final argument (leading operands are reads).
DEST_ONLY_COMMANDS = {"cp", "ln", "install", "rsync", "ditto"}
_SEGMENT_SEPARATORS = {"|", "||", "&&", ";", "&"}
PLUGIN_PROTECTED_DIRS = {
    "hooks", "scripts", "skills", "commands", "specs", "bin",
    ".claude-plugin", ".codex-plugin",
}
PLUGIN_PROTECTED_FILES = {
    ".mcp.json", "plugin.json", "marketplace.json",
}
KNOWN_ENV = {
    "CLAUDE_PLUGIN_ROOT",
    "CODEX_PLUGIN_ROOT",
    "OM_CORE_CACHE_ROOT",
    "OM_CONFIG_HOME",
    "OM_DATA_HOME",
    "HOME",
}


def _resolve(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _plugin_root() -> Path | None:
    for key in ("CODEX_PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"):
        raw = os.environ.get(key, "").strip()
        if raw:
            return _resolve(Path(raw))
    return _resolve(Path(__file__).parent.parent)


def _data_root() -> Path:
    raw = os.environ.get("OM_DATA_HOME", "").strip()
    if raw:
        return _resolve(Path(raw))
    if sys.platform == "darwin":
        return _resolve(Path.home() / "Library" / "Application Support" / "om")
    if sys.platform == "win32":
        return _resolve(Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "om")
    return _resolve(Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "om")


def _cache_root() -> Path:
    raw = os.environ.get("OM_CORE_CACHE_ROOT", "").strip()
    return _resolve(Path(raw)) if raw else _resolve(_data_root() / "bin")


def _config_root() -> Path:
    raw = os.environ.get("OM_CONFIG_HOME", "").strip()
    if raw:
        return _resolve(Path(raw))
    if sys.platform == "darwin":
        return _resolve(Path.home() / "Library" / "Application Support" / "om")
    if sys.platform == "win32":
        return _resolve(Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "om")
    return _resolve(Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "om")


def _owner_for_path(path: Path, plugin_root: Path, cache_root: Path, config_root: Path) -> str | None:
    try:
        rel = path.relative_to(plugin_root)
    except ValueError:
        rel = None
    if rel is not None and rel.parts:
        if rel.parts[0] in PLUGIN_PROTECTED_DIRS or rel.parts[0] in PLUGIN_PROTECTED_FILES:
            return "plugin"

    try:
        rel = path.relative_to(cache_root)
    except ValueError:
        rel = None
    if rel is not None and rel.parts:
        if rel.parts[0] in {"current", "expected-release.json", "channel.json"}:
            return "om-core-cache"
        if len(rel.parts) >= 2:
            return "om-core-cache"

    try:
        rel = path.relative_to(config_root)
    except ValueError:
        rel = None
    if rel is not None and len(rel.parts) == 1:
        if rel.parts[0] in {"server.json", "active_kb.json", "om.sock", "settings.json"}:
            return "om-config"

    if sys.platform == "darwin":
        plist = _resolve(Path.home() / "Library" / "LaunchAgents" / "com.kogcat.om.plist")
        if path == plist:
            return "supervisor"
    return None


def _block(path: Path, owner: str) -> int:
    print("[om self-guard] BLOCK: attempted to modify KogCat runtime file", file=sys.stderr)
    print(f"  target: {path}", file=sys.stderr)
    print(f"  owner: {owner}", file=sys.stderr)
    print(
        "\nKogCat runtime files are managed by the installer/updater.\n"
        "Normal tasks must not edit them. For development, use a source checkout\n"
        "with OM_ALLOW_RUNTIME_SELF_EDIT=1.",
        file=sys.stderr,
    )
    return 2


def _path_from_token(token: str, cwd: Path) -> Path:
    expanded = token
    for key in KNOWN_ENV:
        val = os.environ.get(key)
        if val:
            expanded = expanded.replace("${" + key + "}", val).replace("$" + key, val)
    p = Path(expanded).expanduser()
    if not p.is_absolute():
        p = cwd / p
    return _resolve(p)


def _event_cwd(event: dict) -> Path:
    ti = event.get("tool_input") or event.get("toolInput") or {}
    raw = ti.get("cwd") or event.get("cwd") or ""
    return _resolve(Path(raw)) if raw else _resolve(Path.cwd())


def _check_path(path: Path, plugin_root: Path, cache_root: Path, config_root: Path) -> int:
    owner = _owner_for_path(path, plugin_root, cache_root, config_root)
    if owner:
        return _block(path, owner)
    return 0


def _has_mutation_signal(cmd: str, toks: list[str]) -> bool:
    for tok in toks:
        if os.path.basename(tok.split("=", 1)[0]) in MUTATING_COMMANDS:
            return True
    patterns = [
        r"(?<![<>])>>?(?!&)",
        r"\bsed\b[^|;&]*\s-i\b",
        r"\bawk\b[^|;&]*\s-i\s+inplace\b",
        r"\bsqlite3\b[^|;&]*\b(DELETE|UPDATE|INSERT|DROP|ALTER|REPLACE)\b",
        r"\b(python|python3|perl|ruby|node)\b[^|;&]*\b(open|writeFile|unlink|remove|rmdir)\b",
    ]
    return any(re.search(p, cmd, flags=re.IGNORECASE) for p in patterns)


def _tokens(cmd: str) -> list[str]:
    try:
        return shlex.split(cmd, comments=True, posix=True)
    except Exception:
        return re.findall(r"\S+", cmd)


def _segment_targets(seg: list[str]) -> list[str]:
    """Write targets for one command segment (shell operators already split out)."""
    if not seg:
        return []
    base = os.path.basename(seg[0])
    operands = [t for t in seg[1:] if t and not t.startswith("-")]
    if base in OPERAND_WRITE_COMMANDS:
        return operands
    if base in {"sed", "awk"} and any(t.startswith("-i") for t in seg):
        return operands
    if base in DEST_ONLY_COMMANDS:
        return operands[-1:]
    targets: list[str] = []
    if base == "tar":
        create = any(re.match(r"-{1,2}[A-Za-z]*c", t) for t in seg if t.startswith("-"))
        for i, tok in enumerate(seg[:-1]):
            if tok == "-C" or (create and tok == "-f"):
                targets.append(seg[i + 1])
    elif base == "unzip":
        for i, tok in enumerate(seg[:-1]):
            if tok == "-d":
                targets.append(seg[i + 1])
    return targets


def _command_targets(cmd: str, toks: list[str]) -> list[str]:
    # Redirect destinations are writes regardless of the leading command.
    targets = [m.group(1) for m in re.finditer(r"(?<![<>])>>?\s*([^\s|;&<>]+)", cmd)]
    seg: list[str] = []
    for tok in toks:
        if tok in _SEGMENT_SEPARATORS:
            targets += _segment_targets(seg)
            seg = []
        else:
            seg.append(tok)
    targets += _segment_targets(seg)
    return targets


def _check_file_tool(event: dict, plugin_root: Path, cache_root: Path, config_root: Path) -> int:
    ti = event.get("tool_input") or event.get("toolInput") or {}
    raw = ti.get("file_path") or ti.get("filePath") or ti.get("notebook_path")
    if not raw:
        return 0
    return _check_path(_path_from_token(str(raw), _event_cwd(event)), plugin_root, cache_root, config_root)


def _check_bash(event: dict, plugin_root: Path, cache_root: Path, config_root: Path) -> int:
    ti = event.get("tool_input") or event.get("toolInput") or {}
    cmd = str(ti.get("command") or "").strip()
    if not cmd:
        return 0
    toks = _tokens(cmd)
    if not _has_mutation_signal(cmd, toks):
        return 0
    cwd = _event_cwd(event)
    for target in _command_targets(cmd, toks):
        rc = _check_path(_path_from_token(target, cwd), plugin_root, cache_root, config_root)
        if rc:
            return rc
    return 0


def main() -> int:
    if os.environ.get("OM_ALLOW_RUNTIME_SELF_EDIT", "").strip() == "1":
        return 0
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0
    plugin_root = _plugin_root()
    if plugin_root is None:
        return 0
    cache_root = _cache_root()
    config_root = _config_root()
    tool = event.get("tool_name") or event.get("toolName") or ""
    if tool in {"Write", "Edit", "MultiEdit", "NotebookEdit"}:
        return _check_file_tool(event, plugin_root, cache_root, config_root)
    if tool in {"Bash", "Execute"}:
        return _check_bash(event, plugin_root, cache_root, config_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
