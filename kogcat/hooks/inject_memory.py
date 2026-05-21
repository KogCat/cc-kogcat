#!/usr/bin/env python3
"""SessionStart hook — inject `<KB>/memory/MEMORY.md` into the system prompt.

Spec: om-client-spec 15 §6.1.

Best-effort. If the om-core sidecar isn't running yet (very first session
after install, before any MCP tool call has spawned it), this hook silently
emits an empty hook output — subsequent sessions will pick up the index
once the sidecar is alive.

Bootstrap status banner:
  When the binary fetcher is still in flight (first-session-after-install
  scenario), inject a short banner into the same SessionStart output so
  the LLM can surface a "正在准备" notice instead of letting the user
  stare at a hung-feeling session. The embedding model is downloaded by
  the sidecar after the binary is ready; its live progress surfaces via
  the MCP search gate and `/kogcat:status`, not this banner. Banner stays
  silent once the binary is ready — zero noise on steady-state startups.

Pure stdlib: we don't import the `om_mcp` package — keeps this hook's
dependency surface minimal and identical on every python3.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path


def _emit(ctx: str = "") -> int:
    """Write hook JSON to stdout. Empty ctx → noop output."""
    if not ctx.strip():
        sys.stdout.write("{}")
        return 0
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": ctx,
        }
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    return 0


def _config_dir() -> Path:
    """Mirror om_core.infra.paths.config_dir() without importing it."""
    if env := os.environ.get("OM_CONFIG_HOME", "").strip():
        return Path(env).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "om"
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "om"
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "om"


def _read_server_json() -> dict | None:
    p = _config_dir() / "server.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


class _UDSConnection(http.client.HTTPConnection):
    """http.client connection that dials the sidecar's AF_UNIX socket."""

    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


def _format_index(items: list) -> str:
    """Assemble the index text — mirrors om-core `memory/index.py::rewrite`."""
    rows = sorted(
        (it for it in items if isinstance(it, dict)),
        key=lambda it: (str(it.get("type", "")), str(it.get("name", ""))),
    )
    lines = [
        f"- [{it.get('name', '')}]({it.get('name', '')}.md) "
        f"— {str(it.get('description', ''))[:120]}"
        for it in rows
    ]
    return "\n".join(lines) + ("\n" if lines else "")


def _fetch_index(cfg: dict) -> str:
    """Fetch the memory index from the sidecar over its AF_UNIX socket.

    The sidecar exposes no single "index" route; we list every memory
    entry via `GET /v1/memory/list` and assemble the same index text
    that om-core's `memory/index.py::rewrite` writes to `MEMORY.md`.
    """
    if cfg.get("transport") != "uds":
        return ""
    socket_path = cfg.get("socket_path")
    if not isinstance(socket_path, str) or not socket_path:
        return ""
    headers: dict[str, str] = {}
    if token := cfg.get("token"):
        headers["Authorization"] = f"Bearer {token}"
    conn = _UDSConnection(socket_path, timeout=2.0)
    try:
        conn.request("GET", "/v1/memory/list", headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            return ""
    except (OSError, http.client.HTTPException, EOFError):
        return ""
    finally:
        try:
            conn.close()
        except OSError:
            pass
    try:
        items = json.loads(body.decode("utf-8")).get("items", [])
    except (json.JSONDecodeError, ValueError, AttributeError):
        return ""
    if not isinstance(items, list):
        return ""
    return _format_index(items)


# ---------------------------------------------------------------------------
# Bootstrap status banner (0.33+ first-session UX)
# ---------------------------------------------------------------------------
#
# Read three statefiles + emit a banner only when at least one subsystem is
# not yet `ready`. Failure-mode philosophy: any error → no banner (don't
# block memory injection over a missing statefile).


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _seconds_since(iso_ts: str | None) -> int | None:
    if not iso_ts:
        return None
    try:
        t = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None
    return int((datetime.now(timezone.utc) - t).total_seconds())


def _bin_status() -> dict | None:
    """Look up the binary fetcher statefile via om_core_paths.

    Returns None when:
      - the helper module can't be imported (CC sandboxing / very early
        SessionStart before `sys.path` is set up),
      - the per-target manifest file isn't bundled with the plugin (dev
        checkouts without `bin/<target>/manifest.json`),
      - the target triple isn't supported on this host,
      - the statefile path resolves but doesn't yet exist on disk.
    """
    plugin_root_env = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if not plugin_root_env:
        return None
    try:
        sys.path.insert(0, str(Path(plugin_root_env) / "scripts"))
        import om_core_paths  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        target = om_core_paths.target_triple()
        manifest = om_core_paths.read_manifest(Path(plugin_root_env), target)
    except (om_core_paths.UnsupportedTarget, FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    version = manifest.get("version")
    if not version:
        return None
    # If the cached binary already exists at the expected version path, the
    # subsystem is ready regardless of what the statefile says — short-
    # circuit so we don't show a stale "downloading" banner after a fast
    # download completed and the statefile was never updated to ready.
    if om_core_paths.cache_bin_path(version, target).is_file():
        return {"state": "ready", "version": version, "target": target}
    state = _read_json(om_core_paths.bin_status_path(version, target))
    if state is None:
        # Manifest declares a version but no statefile yet → fetcher hasn't
        # even started. Flag as "pending" so the banner can mention it.
        return {"state": "pending", "version": version, "target": target}
    state.setdefault("version", version)
    state.setdefault("target", target)
    return state


def _format_mb(n: int | None) -> str:
    if n is None:
        return "?"
    return f"{n / (1 << 20):.0f}MB"


def _bin_line(state: dict | None) -> str | None:
    """One status line for the binary subsystem, or None when already ready."""
    if not state or state.get("state") == "ready":
        return None
    s = state.get("state")
    version = state.get("version") or "?"
    if s == "pending":
        return f"  - 二进制 (~64MB): 等待 fetcher 启动 (v{version})"
    if s == "failed":
        reason = state.get("reason", "未知原因")
        return f"  - 二进制 (~64MB): ❌ 上次下载失败 ({reason})；重启会话重试"
    # downloading / unknown
    bd = state.get("bytes_downloaded")
    th = state.get("total_bytes_hint")
    progress = ""
    if isinstance(bd, int) and isinstance(th, int) and th > 0:
        pct = min(100, bd * 100 // th)
        progress = f" {_format_mb(bd)} / {_format_mb(th)} ({pct}%)"
    elif isinstance(bd, int):
        progress = f" {_format_mb(bd)}"
    elapsed = _seconds_since(state.get("started_at"))
    elapsed_s = f"，已等 {elapsed}s" if elapsed is not None else ""
    return f"  - 二进制 (~64MB): 下载中{progress}{elapsed_s}"


def _build_bootstrap_banner() -> str:
    """Return a banner string when the binary fetcher is still in flight, else ''.

    The embedding model is sidecar-owned and downloads only after the
    binary is ready, so it is not a banner line — its live progress comes
    from the MCP search gate and `/kogcat:status`. The banner is gated
    solely on the binary fetcher, which has a client-side statefile that
    is accurate at SessionStart time.
    """
    bin_line = _bin_line(_bin_status())
    if not bin_line:
        return ""
    return (
        "<om-bootstrap-status>\n"
        "om-core 后台正在首次准备（一次性）。期间 om 工具会返回\n"
        "「正在准备」结构化提示并自动重试；你可以继续聊天 / 做其他工作。\n"
        "\n"
        f"{bin_line}\n"
        "  - 嵌入模型 (~90MB): 二进制就绪后由 sidecar 自动后台下载\n"
        "\n"
        "实时进度：运行 `/kogcat:status`。\n"
        "</om-bootstrap-status>"
    )


_INSTRUCTIONS = (
    "Persistent user memory (spec 15) loaded from this KB. Treat as "
    "authoritative background context: who the user is, work-style "
    "preferences, ongoing projects, external system pointers. "
    "When the user expresses a fact worth remembering across sessions — "
    "especially after triggers like 记住/以后/下次/always/never/remember — "
    "call `memory_save` with `source='user_explicit'`. "
    "Type guide: user=identity, feedback=work-style preference, "
    "project=current decisions, reference=external system pointer; "
    "lens=synthesized user-perspective profile, (re)built only via the "
    "memory-consolidate skill — never memory_save type=lens ad-hoc. "
    "Do NOT save code blocks, file paths, commit shas, PR refs, or "
    "session-anchored phrasing — these are auto-rejected for "
    "client_inferred and warned for user_explicit."
)


def main() -> int:
    # Bootstrap banner is independent of sidecar reachability — it can
    # (and should) appear even when the sidecar isn't up yet, which is
    # exactly the first-install case we're optimizing for.
    banner = _build_bootstrap_banner()

    cfg = _read_server_json()
    memory_block = ""
    if cfg:
        text = _fetch_index(cfg).strip()
        if text:
            memory_block = (
                "<om-memory>\n"
                f"{_INSTRUCTIONS}\n"
                "\n"
                f"{text}\n"
                "</om-memory>"
            )

    combined = "\n\n".join(b for b in (banner, memory_block) if b)
    return _emit(combined)


if __name__ == "__main__":
    sys.exit(main())
