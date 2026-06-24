"""project_config — kb_root resolution + active verticals discovery."""
import os
from functools import lru_cache
from pathlib import Path


def _resolve_kb_root() -> Path:
    """Resolve kb_root.

    Priority: ``$KB_ROOT`` → ``$CLAUDE_PLUGIN_OPTION_kb_root`` →
    ``$CLAUDE_PLUGIN_DATA/kb_root`` file → ``~/.claude/plugins/data/*/kb_root``.
    """
    env = os.environ.get("KB_ROOT", "").strip()
    if env:
        return Path(env).resolve()
    # CC userConfig casing is unspecified; check both common forms.
    for var in ("CLAUDE_PLUGIN_OPTION_kb_root", "CLAUDE_PLUGIN_OPTION_KB_ROOT"):
        opt = os.environ.get(var, "").strip()
        if opt:
            return Path(opt).resolve()
    candidates: list[Path] = []
    data = os.environ.get("CLAUDE_PLUGIN_DATA", "").strip()
    if data:
        candidates.append(Path(data) / "kb_root")
    plugins_data_root = Path.home() / ".claude" / "plugins" / "data"
    if plugins_data_root.is_dir():
        try:
            candidates.extend(sorted(plugins_data_root.glob("*/kb_root")))
        except OSError:
            pass
    for p in candidates:
        try:
            if p.is_file():
                v = p.read_text(encoding="utf-8").strip()
                if v:
                    return Path(v).resolve()
        except OSError:
            continue
    raise RuntimeError(
        "kb_root not resolvable. Tried (in order): $KB_ROOT env, "
        "$CLAUDE_PLUGIN_OPTION_kb_root env, $CLAUDE_PLUGIN_DATA/kb_root file, "
        "~/.claude/plugins/data/om/kb_root file. If the om plugin was just "
        "installed/reconfigured, restart Claude Code so the SessionStart hook "
        "can persist kb_root; or set KB_ROOT=/path/to/your/kb manually."
    )


ROOT = _resolve_kb_root()

SYNTHESIS_VERTICAL = "synthesis"

_RESERVED = {
    SYNTHESIS_VERTICAL,
    "specs",
    "scripts",
    "node_modules",
}


@lru_cache(maxsize=1)
def data_verticals() -> tuple[str, ...]:
    """Return active data verticals (sorted)."""
    result: list[str] = []
    if not ROOT.is_dir():
        return ()
    for d in sorted(ROOT.iterdir()):
        if not d.is_dir():
            continue
        name = d.name
        if name.startswith(".") or name in _RESERVED:
            continue
        if (d / "sources").is_dir():
            result.append(name)
    return tuple(result)


def is_known_vertical(name: str) -> bool:
    return name in data_verticals() or name == SYNTHESIS_VERTICAL


def reload() -> None:
    data_verticals.cache_clear()


if __name__ == "__main__":
    import json
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    print(json.dumps({
        "root": str(ROOT),
        "plugin_root": plugin_root,
        "data_verticals": list(data_verticals()),
    }, indent=2, ensure_ascii=False))
