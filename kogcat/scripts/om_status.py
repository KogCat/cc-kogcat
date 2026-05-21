#!/usr/bin/env python3
"""User-facing diagnostic — show om-core local state without mutating anything.

Skill entry point: `/kogcat:status` — skills/status/SKILL.md runs the
skills/status/scripts/status.py wrapper, which locates the plugin root and
execs this file.

Reads statefiles + probes the sidecar:
  - binary fetcher : <plugin>/bin/om-core-<target>/manifest.json
                     ~/.claude/om-core-cache/<v>/<target>.status.json
                     ~/.claude/om-core-cache/<v>/<target>/om-core-bin (existence)
  - embedding model: sidecar GET /v1/embedding/status (warmup is sidecar-owned)
  - sidecar service: ~/Library/Application Support/om/server.json + /healthz probe
  - KB binding     : <CLAUDE_PLUGIN_DATA>/kb_root + ~/.config/om/active_kb.json

Design rules:
  - stdlib only; runs even during first-install.
  - never mutates anything — pure read.
  - never imports the om_mcp package; talks to the sidecar over a minimal
    inline UDS HTTP client.
  - --json switch for machine-readable output (support tickets).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


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


def _fmt_mb(n: int | None) -> str:
    if n is None:
        return "?"
    return f"{n / (1 << 20):.1f}MB"


def _fmt_duration(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _config_dir() -> Path:
    if env := os.environ.get("OM_CONFIG_HOME", "").strip():
        return Path(env).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "om"
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "om"
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "om"


# ---------------------------------------------------------------------------
# Subsystem probes — each returns a dict with at least {"state": "..."}.
# ---------------------------------------------------------------------------


def probe_binary() -> dict[str, Any]:
    """Resolve binary fetcher state from manifest + statefile + cache filesystem."""
    plugin_root_env = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if not plugin_root_env:
        return {"state": "no_plugin_root", "hint": "CLAUDE_PLUGIN_ROOT not set"}

    try:
        sys.path.insert(0, str(Path(plugin_root_env) / "scripts"))
        import om_core_paths  # type: ignore[import-not-found]
    except ImportError as e:
        return {"state": "unknown", "error": f"om_core_paths import failed: {e}"}

    try:
        target = om_core_paths.target_triple()
    except om_core_paths.UnsupportedTarget as e:
        return {"state": "unsupported_target", "error": str(e)}

    try:
        manifest = om_core_paths.read_manifest(Path(plugin_root_env), target)
    except (FileNotFoundError, OSError, json.JSONDecodeError) as e:
        return {"state": "no_manifest", "target": target, "error": str(e)}

    version = manifest.get("version")
    if not version:
        return {"state": "no_version_in_manifest", "target": target}

    out: dict[str, Any] = {"version": version, "target": target}
    cached_bin = om_core_paths.cache_bin_path(version, target)
    if cached_bin.is_file():
        try:
            out["bin_path"] = str(cached_bin)
            out["bin_size_mb"] = cached_bin.stat().st_size / (1 << 20)
        except OSError:
            pass
        out["state"] = "ready"
        return out

    statefile = om_core_paths.bin_status_path(version, target)
    state = _read_json(statefile)
    if state is None:
        out["state"] = "pending"
        out["statefile"] = str(statefile)
        return out

    out["state"] = state.get("state", "unknown")
    for k in ("bytes_downloaded", "total_bytes_hint", "started_at",
              "last_progress_at", "reason", "pid"):
        if (v := state.get(k)) is not None:
            out[k] = v
    if (sa := state.get("started_at")):
        out["elapsed_seconds"] = _seconds_since(sa)
    if (lpa := state.get("last_progress_at")):
        out["seconds_since_progress"] = _seconds_since(lpa)
    return out


def _uds_get(
    socket_path: str, path: str, token: str | None = None, timeout: float = 1.5,
) -> tuple[bool, str | None]:
    """Minimal HTTP/1.0 GET over an AF_UNIX socket — stdlib only.

    Returns (ok, body) on a 200; (False, diagnostic) otherwise.
    """
    if not os.path.exists(socket_path):
        return False, "socket file missing"
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(socket_path)
        req = f"GET {path} HTTP/1.0\r\nHost: localhost\r\n"
        if token:
            req += f"Authorization: Bearer {token}\r\n"
        req += "\r\n"
        s.sendall(req.encode("latin-1"))
        buf = b""
        while len(buf) < 65536:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.close()
        head, _, body = buf.partition(b"\r\n\r\n")
        first = head.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
        ok = " 200 " in first
        return ok, (body.decode("utf-8", errors="replace") if ok else first)
    except (OSError, TimeoutError) as e:
        return False, f"{type(e).__name__}: {e}"


def probe_embedding() -> dict[str, Any]:
    """Embedding warmup state — sidecar-owned (GET /v1/embedding/status)."""
    cfg = _read_json(_config_dir() / "server.json")
    if not cfg:
        return {"state": "unknown", "reason": "sidecar not started (no server.json)"}
    sp = cfg.get("socket_path")
    if cfg.get("transport") != "uds" or not sp:
        return {"state": "unknown", "reason": "non-UDS transport unsupported"}

    ok, body = _uds_get(sp, "/v1/embedding/status", token=cfg.get("token"))
    if not ok or not body:
        return {"state": "unknown", "reason": body or "sidecar unreachable"}
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return {"state": "unknown", "reason": "malformed /v1/embedding/status response"}
    if not isinstance(data, dict):
        return {"state": "unknown"}

    out: dict[str, Any] = {"state": data.get("state", "unknown")}
    if out["state"] == "idle":
        out["state"] = "pending"  # warmup not yet advanced — render as pending
    for k in ("bytes_downloaded", "total_bytes_hint", "started_at",
              "last_progress_at", "reason", "model"):
        if (v := data.get(k)) is not None:
            out[k] = v
    if (sa := data.get("started_at")):
        out["elapsed_seconds"] = _seconds_since(sa)
    if (lpa := data.get("last_progress_at")):
        out["seconds_since_progress"] = _seconds_since(lpa)
    return out


# ---------------------------------------------------------------------------
# Sidecar probe — supports both UDS and TCP transports via stdlib.
# ---------------------------------------------------------------------------


def _probe_healthz_tcp(host: str, port: int, timeout: float = 1.5) -> tuple[bool, str | None]:
    url = f"http://{host}:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            ok = resp.status == 200
            body = resp.read(256).decode("utf-8", errors="replace") if ok else None
            return ok, body
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return False, f"{type(e).__name__}: {e}"


def _probe_healthz_uds(socket_path: str, timeout: float = 1.5) -> tuple[bool, str | None]:
    """Minimal HTTP/1.0 GET /healthz over an AF_UNIX socket — stdlib only."""
    if not os.path.exists(socket_path):
        return False, "socket file missing"
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(socket_path)
        s.sendall(b"GET /healthz HTTP/1.0\r\nHost: localhost\r\n\r\n")
        buf = b""
        while len(buf) < 4096:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.close()
        head, _, body = buf.partition(b"\r\n\r\n")
        first = head.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
        ok = " 200 " in first
        return ok, body[:256].decode("utf-8", errors="replace") if ok else first
    except (OSError, TimeoutError) as e:
        return False, f"{type(e).__name__}: {e}"


def probe_sidecar() -> dict[str, Any]:
    cfg_path = _config_dir() / "server.json"
    out: dict[str, Any] = {"server_json": str(cfg_path)}
    cfg = _read_json(cfg_path)
    if not cfg:
        out["state"] = "no_server_json"
        return out

    transport = cfg.get("transport") or "tcp"
    out["transport"] = transport
    if (pid := cfg.get("pid")):
        out["pid"] = pid

    ok = False
    body: str | None = None
    if transport == "uds":
        sp = cfg.get("socket_path")
        out["socket_path"] = sp
        if sp:
            ok, body = _probe_healthz_uds(sp)
    elif transport in ("tcp", "pipe"):
        port = cfg.get("port")
        out["port"] = port
        if port:
            ok, body = _probe_healthz_tcp("127.0.0.1", int(port))
    else:
        out["state"] = "unknown_transport"
        return out

    out["state"] = "ready" if ok else "unreachable"
    if body:
        out["healthz_body"] = body[:120]
    return out


def probe_kb() -> dict[str, Any]:
    out: dict[str, Any] = {}
    data_dir = os.environ.get("CLAUDE_PLUGIN_DATA", "").strip()
    if data_dir:
        kb_root_file = Path(data_dir) / "kb_root"
        if kb_root_file.is_file():
            try:
                out["kb_root_plugin"] = kb_root_file.read_text(encoding="utf-8").strip()
            except OSError:
                pass

    active = _read_json(_config_dir() / "active_kb.json")
    if active:
        if (p := active.get("path")):
            out["kb_root_om"] = p
        if (src := active.get("source")):
            out["source"] = src

    if not out:
        out["state"] = "no_binding"
    else:
        out["state"] = "ready" if out.get("kb_root_om") or out.get("kb_root_plugin") else "no_binding"
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


_ICON = {
    "ready": "✅",
    "pending": "⏳",
    "downloading": "⏳",
    "unreachable": "⚠️ ",
    "failed": "❌",
}


def _icon(state: str) -> str:
    return _ICON.get(state, "·")


def _render_progress(d: dict[str, Any]) -> str:
    bd = d.get("bytes_downloaded")
    th = d.get("total_bytes_hint")
    if isinstance(bd, int) and isinstance(th, int) and th > 0:
        pct = min(100, bd * 100 // th)
        return f"{_fmt_mb(bd)} / {_fmt_mb(th)} ({pct}%)"
    if isinstance(bd, int):
        return _fmt_mb(bd)
    return ""


def render_human(report: dict[str, Any]) -> str:
    b = report["binary"]
    e = report["embedding"]
    s = report["sidecar"]
    k = report["kb"]

    lines = ["om-core 状态", "─" * 30]

    # binary
    b_state = b.get("state", "?")
    extra = ""
    if b_state == "ready":
        size = b.get("bin_size_mb")
        v = b.get("version", "?")
        extra = f"v{v}" + (f", {size:.1f}MB" if size else "")
    elif b_state in ("downloading", "pending"):
        prog = _render_progress(b)
        elapsed = _fmt_duration(b.get("elapsed_seconds"))
        extra = (prog + (f", 已等 {elapsed}" if elapsed != "?" else "")).strip(", ")
        if b.get("seconds_since_progress") is not None and b["seconds_since_progress"] > 30:
            extra += f"  ⚠️ {b['seconds_since_progress']}s 无新进度"
    elif b_state == "failed":
        extra = f"原因: {b.get('reason', '?')}"
    lines.append(f"[A] binary       {_icon(b_state)} {b_state:<12} {extra}")

    # sidecar
    s_state = s.get("state", "?")
    extra = ""
    if s_state == "ready":
        tp = s.get("transport", "?")
        pid = s.get("pid")
        extra = f"transport={tp}" + (f", pid={pid}" if pid else "")
    elif s_state == "unreachable":
        extra = f"server.json 存在但 /healthz 不通; transport={s.get('transport', '?')}"
    elif s_state == "no_server_json":
        extra = "sidecar 还未起来(binary ready 后 launchd 应自动起)"
    lines.append(f"[B] sidecar      {_icon(s_state)} {s_state:<12} {extra}")

    # embedding
    e_state = e.get("state", "?")
    extra = ""
    if e_state == "ready":
        extra = "已就绪"
        if (m := e.get("model")):
            extra += f", model={m}"
    elif e_state in ("downloading", "pending"):
        prog = _render_progress(e)
        elapsed = _fmt_duration(e.get("elapsed_seconds"))
        extra = (prog + (f", 已等 {elapsed}" if elapsed != "?" else "")).strip(", ")
        if e.get("seconds_since_progress") is not None and e["seconds_since_progress"] > 30:
            extra += f"  ⚠️ {e['seconds_since_progress']}s 无新进度"
    elif e_state == "failed":
        extra = f"原因: {e.get('reason', '?')}; 检索将降级到 BM25-only"
    elif e_state == "unknown":
        extra = e.get("reason", "sidecar 未就绪")
    lines.append(f"[C] 嵌入模型     {_icon(e_state)} {e_state:<12} {extra}")

    # kb
    k_state = k.get("state", "?")
    extra = ""
    if k_state == "ready":
        path = k.get("kb_root_om") or k.get("kb_root_plugin", "?")
        extra = path
        if k.get("kb_root_plugin") and k.get("kb_root_om") and k["kb_root_plugin"] != k["kb_root_om"]:
            extra += f"  ⚠️ plugin 端为 {k['kb_root_plugin']} (不一致)"
    elif k_state == "no_binding":
        extra = "未配置 kb_root；在插件设置里填知识库根目录"
    lines.append(f"[D] KB 绑定      {_icon(k_state)} {k_state:<12} {extra}")

    # tail hints
    all_ready = all(report[k_]["state"] == "ready" for k_ in ("binary", "sidecar", "embedding", "kb"))
    if not all_ready:
        lines.append("")
        if b_state in ("pending", "downloading") or e_state in ("pending", "downloading"):
            lines.append("提示: 后台正在首次下载，无需操作；om 工具会自动重试。")
        if b_state == "failed" or e_state == "failed":
            lines.append("提示: 重启 Claude Code 会触发重试；详情看 ~/Library/Logs/om/。")

    return "\n".join(lines)


def collect_report() -> dict[str, Any]:
    return {
        "binary": probe_binary(),
        "sidecar": probe_sidecar(),
        "embedding": probe_embedding(),
        "kb": probe_kb(),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="om-core local status diagnostic")
    ap.add_argument("--json", action="store_true", help="machine-readable JSON output")
    args = ap.parse_args()

    report = collect_report()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_human(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
