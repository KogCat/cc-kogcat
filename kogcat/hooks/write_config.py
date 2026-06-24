#!/usr/bin/env python3
"""SessionStart hook — mirror wrapper settings that om-core cannot inherit."""
import json
import os
import shlex
import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_ROOT / "scripts"))
from plugin_env import export_compat_env, plugin_data, plugin_root  # noqa: E402


def _om_config_dir() -> Path:
    """Mirror om_core.infra.paths.config_dir() — keep stdlib-only here."""
    override = os.environ.get("OM_CONFIG_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "om"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "om"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "om"


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
    root = plugin_root(__file__)
    data = plugin_data()
    export_compat_env(root, data)
    if data is None:
        return 0

    out_dir = data
    out_dir.mkdir(parents=True, exist_ok=True)

    _write_hf_endpoint()

    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if env_file:
        lines = [
            f"export CLAUDE_PLUGIN_ROOT={shlex.quote(str(root))}",
            f"export CODEX_PLUGIN_ROOT={shlex.quote(str(root))}",
            f"export CLAUDE_PLUGIN_DATA={shlex.quote(str(data))}",
            f"export CODEX_PLUGIN_DATA={shlex.quote(str(data))}",
        ]
        with open(env_file, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
