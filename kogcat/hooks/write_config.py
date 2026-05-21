#!/usr/bin/env python3
"""SessionStart hook — persist kb_root so scripts can resolve it regardless
of how they're launched.

Primary script entry point is `bin/om <script.py>` (CC auto-prepends
`<plugin>/bin` to PATH for all Bash tool shells). The dispatcher self-resolves
the plugin root via $0 and reads kb_root from the file this hook writes.

We also append `export` lines to $CLAUDE_ENV_FILE for forward compatibility;
as of CC 2.1.111 the file is created and passed to hooks but not sourced
back into Bash tool shells, so env vars set this way don't propagate. Kept
so that once CC closes that gap, users on newer versions get them for free.

M3 dual-write (spec 10 §4.4.1): also write om-core's active_kb.json so the
sidecar (Tauri or wrapper-spawned) reads the same KB. Plugin is still the
authoritative source for now; om-core's `/v1/kb/activate` will own this in
M5+ when Tauri onboarding lands.

Input priority for kb_root:
  - argv[1] (CC hooks.json command substitution: ${user_config.kb_root})
  - $CLAUDE_PLUGIN_OPTION_kb_root env (CC auto-exports to hook subprocesses)
  - $KB_ROOT env (Codex path — Codex has no userConfig; set in config.toml)
"""
import json
import os
import shlex
import sys
from pathlib import Path


def _om_config_dir() -> Path:
    """Mirror om_core.infra.paths.config_dir() — keep stdlib-only here."""
    override = os.environ.get("OM_CONFIG_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "om"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "om"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "om"


def _write_active_kb(kb_root: str) -> None:
    """Best-effort dual-write of ~/.config/om/active_kb.json (spec 10 §4.4.1).

    om-core reads this on startup to know which KB to bind to. Until Tauri
    onboarding (M5+) takes over, the plugin's user_config.kb_root remains
    authoritative — we just mirror it here so a sidecar started by Tauri or
    by the wrapper sees the same KB.
    """
    try:
        cfg_dir = _om_config_dir()
        cfg_dir.mkdir(parents=True, exist_ok=True)
        active = cfg_dir / "active_kb.json"
        kb_path = str(Path(kb_root).expanduser().resolve())
        existing = None
        if active.is_file():
            try:
                existing = json.loads(active.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                existing = None
        # Don't clobber a richer payload Tauri may have written; only update path.
        payload = {**(existing or {}), "path": kb_path, "source": "kogcat"}
        active.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"[om] active_kb.json write skipped: {e}", file=sys.stderr, flush=True)


def _write_hf_endpoint() -> None:
    """Mirror the plugin's `hf_endpoint` userConfig into om-core settings.json.

    The sidecar is a launchd-resident process that does not inherit the
    plugin's environment, so the embedding warmup reads its HuggingFace
    mirror from `settings.json` (`embedding.hf_endpoint`). CC exports the
    userConfig field as `CLAUDE_PLUGIN_OPTION_hf_endpoint`. An unreadable
    settings.json is left untouched to avoid clobbering `llm` / `memory`.
    """
    endpoint = os.environ.get("CLAUDE_PLUGIN_OPTION_hf_endpoint", "").strip()
    try:
        cfg_dir = _om_config_dir()
        settings = cfg_dir / "settings.json"
        data: dict = {}
        if settings.is_file():
            try:
                loaded = json.loads(settings.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                print(
                    "[om] settings.json unreadable; hf_endpoint mirror skipped",
                    file=sys.stderr, flush=True,
                )
                return
            if isinstance(loaded, dict):
                data = loaded

        embedding = data.get("embedding")
        if not isinstance(embedding, dict):
            embedding = {}
        if endpoint == embedding.get("hf_endpoint", ""):
            return  # no change — also covers "both empty, no file"

        if endpoint:
            embedding["hf_endpoint"] = endpoint
        else:
            embedding.pop("hf_endpoint", None)
        if embedding:
            data["embedding"] = embedding
        else:
            data.pop("embedding", None)

        cfg_dir.mkdir(parents=True, exist_ok=True)
        tmp = cfg_dir / "settings.json.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, settings)
    except OSError as e:
        print(f"[om] settings.json hf_endpoint write skipped: {e}", file=sys.stderr, flush=True)


def main() -> int:
    data_dir = os.environ.get("CLAUDE_PLUGIN_DATA")
    if not data_dir:
        return 0

    kb_root = None
    if len(sys.argv) >= 2 and sys.argv[1].strip():
        kb_root = sys.argv[1].strip()
    else:
        # CC exports CLAUDE_PLUGIN_OPTION_kb_root from userConfig; Codex has no
        # userConfig, so a Codex-side bootstrap reads kb_root from KB_ROOT.
        for _env_key in ("CLAUDE_PLUGIN_OPTION_kb_root", "KB_ROOT"):
            _val = os.environ.get(_env_key, "").strip()
            if _val:
                kb_root = _val
                break

    out_dir = Path(data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if kb_root:
        (out_dir / "kb_root").write_text(kb_root + "\n", encoding="utf-8")
        _write_active_kb(kb_root)

    _write_hf_endpoint()

    env_file = os.environ.get("CLAUDE_ENV_FILE")
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env_file and plugin_root:
        lines = [
            f"export CLAUDE_PLUGIN_ROOT={shlex.quote(plugin_root)}",
            f"export CLAUDE_PLUGIN_DATA={shlex.quote(data_dir)}",
        ]
        if kb_root:
            lines.append(f"export KB_ROOT={shlex.quote(kb_root)}")
        with open(env_file, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
