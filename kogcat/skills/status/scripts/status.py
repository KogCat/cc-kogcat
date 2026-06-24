#!/usr/bin/env python3
"""Skill helper — locate and run the plugin's om_status.py on any host (CC / Codex).

This file sits at <plugin_root>/skills/status/scripts/status.py, so it resolves
the plugin root from its own location and does not depend on a host-injected
env var (`CLAUDE_PLUGIN_ROOT` is not guaranteed inside a skill's Bash shell on
Codex). It exports CLAUDE_PLUGIN_ROOT for the child — om_status.py's binary
probe reads it — then execs scripts/om_status.py, forwarding all arguments.
A value already set by the host (Claude Code) is left untouched.
"""
import os
import sys
from pathlib import Path

plugin_root = Path(__file__).resolve().parents[3]
target = plugin_root / "scripts" / "om_status.py"
if not target.is_file():
    sys.exit(f"[kogcat:status] not found: {target}")
os.environ.setdefault("CLAUDE_PLUGIN_ROOT", str(plugin_root))
os.execv(sys.executable, [sys.executable, str(target), *sys.argv[1:]])
