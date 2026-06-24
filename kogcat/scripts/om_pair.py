#!/usr/bin/env python3
"""kogcat:pair — bind this machine to a KogCat account using a pairing code from
kogcat.com/account, by running the bootstrapped om-core binary's `id pair`. CC + Codex
use this via the skill helper; standalone uses `om id pair` directly.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_BASE_URL = "https://id.kogcat.com"


def _binary() -> Path:
    "Resolve the active stable-pointer om-core binary (same probe as om_status.py)."
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    root = Path(plugin_root) if plugin_root else Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    try:
        import om_core_paths  # type: ignore[import-not-found]
    except Exception as e:  # noqa: BLE001
        sys.exit(f"[kogcat:pair] om_core_paths import failed: {e}")
    binp = Path(os.path.realpath(om_core_paths.current_bin_path(om_core_paths.target_triple())))
    if not binp.is_file():
        sys.exit("[kogcat:pair] om-core binary 尚未就绪——先运行 /kogcat:status 等下载完成再重试。")
    return binp


def main() -> int:
    ap = argparse.ArgumentParser(description="Pair this device with a kogcat-id account")
    ap.add_argument("code", help="配对码（账户控制台 kogcat.com/account 显示，短时单次）")
    ap.add_argument("--base-url", default=os.environ.get("OM_ID_BASE_URL", DEFAULT_BASE_URL),
                    help=f"kogcat-id base URL (default {DEFAULT_BASE_URL} / OM_ID_BASE_URL)")
    args = ap.parse_args()
    binp = _binary()
    return subprocess.run([str(binp), "id", "pair", args.code, "--base-url", args.base_url]).returncode


if __name__ == "__main__":
    sys.exit(main())
