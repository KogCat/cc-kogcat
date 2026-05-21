#!/usr/bin/env python3
"""PreToolUse hook — block writes to protected om KB assets.

Protected (under kb_root):
  - kb.db / kb.db-wal / kb.db-shm    SQLite store (concept/edge/synthesis/anti-echo)
  - om.lock                           pack lockfile
  - packs/**                          installed vendor packs (read-only)
  - .archive/**                       immutable source snapshots

Wired as PreToolUse for Write|Edit|MultiEdit|NotebookEdit|Bash.
Exit 2 → Claude Code blocks the tool call and surfaces stderr.

Bash whitelist: commands that invoke om's own pipeline scripts pass
through, since they are the *legitimate* writers of these assets.
"""
import json
import os
import re
import shlex
import sys
from pathlib import Path


PROTECTED_FILES = {"kb.db", "kb.db-wal", "kb.db-shm", "om.lock"}
PROTECTED_DIRS = ("packs", ".archive")

# Bash verbs that mutate the filesystem.
DESTRUCTIVE_CMDS = {
    "rm", "mv", "cp", "chmod", "chown", "truncate", "dd", "tee",
    "ln", "rmdir", "touch", "install",
}

# Whitelist: command invokes om's own pipeline → trust it.
ALLOWED_PATTERNS = [
    re.compile(r"\bscripts/[A-Za-z0-9_]+\.py\b"),
    re.compile(r"\bbin/om\b"),
    re.compile(r"\$\{?CLAUDE_PLUGIN_ROOT\}?"),
]


def _resolve_kb_root() -> Path | None:
    for var in ("KB_ROOT", "CLAUDE_PLUGIN_OPTION_kb_root", "CLAUDE_PLUGIN_OPTION_KB_ROOT"):
        v = os.environ.get(var, "").strip()
        if v:
            return Path(v).resolve()
    data = os.environ.get("CLAUDE_PLUGIN_DATA", "").strip()
    if data:
        f = Path(data) / "kb_root"
        if f.is_file():
            v = f.read_text(encoding="utf-8").strip()
            if v:
                return Path(v).resolve()
    return None


def _is_protected(path: Path, kb_root: Path) -> str | None:
    """Return reason string if `path` is inside a protected zone, else None."""
    try:
        rel = path.resolve().relative_to(kb_root)
    except Exception:
        return None
    parts = rel.parts
    if not parts:
        return None
    if parts[0] in PROTECTED_FILES:
        return f"protected file {parts[0]}"
    if parts[0] in PROTECTED_DIRS:
        return f"protected dir {parts[0]}/"
    return None


def _block(reason: str) -> int:
    print(f"[om guard] BLOCK: {reason}", file=sys.stderr)
    print(
        "  受保护资产由 om pipeline / scripts/pack.py 内部维护，请勿直接改动。\n"
        "  如确需修改，先停止当前操作并与用户确认。",
        file=sys.stderr,
    )
    return 2


def _check_file_tool(event: dict, kb_root: Path) -> int:
    ti = event.get("tool_input") or event.get("toolInput") or {}
    fp = ti.get("file_path") or ti.get("filePath") or ti.get("notebook_path")
    if not fp:
        return 0
    p = Path(fp)
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    reason = _is_protected(p, kb_root)
    if reason:
        return _block(f"{event.get('tool_name','?')} → {p} ({reason})")
    return 0


def _command_targets(cmd: str) -> list[str]:
    try:
        toks = shlex.split(cmd, comments=True, posix=True)
    except Exception:
        toks = re.findall(r"\S+", cmd)
    out = [t for t in toks if t and not t.startswith("-")]
    for m in re.finditer(r">>?\s*([^\s|;&<>]+)", cmd):
        out.append(m.group(1))
    return out


def _check_bash(event: dict, kb_root: Path) -> int:
    ti = event.get("tool_input") or event.get("toolInput") or {}
    cmd = (ti.get("command") or "").strip()
    if not cmd:
        return 0

    # Whitelist: if the command runs om's own scripts, allow.
    for pat in ALLOWED_PATTERNS:
        if pat.search(cmd):
            return 0

    # Cheap surface signals for "this command writes something".
    has_destructive = False
    try:
        toks = shlex.split(cmd, comments=True, posix=True)
    except Exception:
        toks = cmd.split()
    for t in toks:
        if os.path.basename(t.split("=", 1)[0]) in DESTRUCTIVE_CMDS:
            has_destructive = True
            break
    has_redirect = bool(re.search(r"(?<![<>])>>?(?!&)", cmd))
    has_sed_inplace = bool(re.search(r"\bsed\b[^|;&]*\s-i\b", cmd))
    has_awk_inplace = bool(re.search(r"\bawk\b[^|;&]*\s-i\s+inplace\b", cmd))
    has_sqlite_write = bool(
        re.search(r"\bsqlite3\b[^|;&]*\b(DELETE|UPDATE|INSERT|DROP|ALTER|REPLACE)\b",
                  cmd, flags=re.IGNORECASE)
    )

    if not (has_destructive or has_redirect or has_sed_inplace
            or has_awk_inplace or has_sqlite_write):
        return 0

    for t in _command_targets(cmd):
        name = os.path.basename(t)
        if name in PROTECTED_FILES:
            return _block(f"Bash mutates protected file: {name}  ({cmd[:120]})")
        segs = [s for s in t.split("/") if s]
        if any(s in PROTECTED_DIRS for s in segs):
            return _block(f"Bash mutates protected dir token: {t}  ({cmd[:120]})")
        p = Path(t)
        if not p.is_absolute():
            p = Path.cwd() / p
        reason = _is_protected(p, kb_root)
        if reason:
            return _block(f"Bash mutates {p} ({reason})  ({cmd[:120]})")
    return 0


def main() -> int:
    kb_root = _resolve_kb_root()
    if not kb_root:
        return 0
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0

    tool = event.get("tool_name") or event.get("toolName") or ""
    if tool in {"Write", "Edit", "MultiEdit", "NotebookEdit"}:
        return _check_file_tool(event, kb_root)
    if tool == "Bash":
        return _check_bash(event, kb_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
