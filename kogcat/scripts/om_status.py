#!/usr/bin/env python3
"""User-facing diagnostic — show om-core local state without mutating anything.

Skill entry point: `/kogcat:status` — skills/status/SKILL.md runs the
skills/status/scripts/status.py wrapper, which locates the plugin root and
execs this file.

Reads statefiles + probes the sidecar:
  - binary fetcher : ~/.claude/om-core-cache/current/<target>/om-core-bin (active
                     version — channel-first, decoupled from the plugin's bundled
                     manifest floor); channel resolve + statefile when none active
                     ~/.claude/om-core-cache/<v>/<target>.status.json (fetch state)
  - embedding model: sidecar GET /v1/embedding/status (warmup is sidecar-owned)
  - sidecar service: ~/Library/Application Support/om/server.json + /healthz probe
  - KB binding     : sidecar GET /v1/kb/active

Design rules:
  - stdlib only; runs even during first-install.
  - never mutates anything — pure read.
  - never imports the om_mcp package; talks to the sidecar over minimal
    inline UDS / npipe HTTP clients.
  - --json switch for machine-readable output (support tickets).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import socket
import sys
import time
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
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(local) / "om"
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "om"


# ---------------------------------------------------------------------------
# Subsystem probes — each returns a dict with at least {"state": "..."}.
# ---------------------------------------------------------------------------


def _current_active_version(om_core_paths, target: str) -> str | None:
    """Version `current/<target>` resolves to — the binary launchd actually runs.
    Offline ground truth, decoupled from the bundled manifest floor (channel-first
    means floor ≠ running version). None if the pointer is absent/broken."""
    try:
        real = Path(os.path.realpath(om_core_paths.current_bin_path(target)))
        if not real.is_file():
            return None
        rel = real.relative_to(om_core_paths.cache_root().resolve())
    except (OSError, ValueError, AttributeError):
        return None
    # layout: <cache_root>/<version>/<target>/om-core-bin
    return rel.parts[0] if rel.parts else None


def probe_binary() -> dict[str, Any]:
    """Binary state: active `current` pointer first (offline ground truth), else
    channel-first resolve + fetch statefile. NOT the bundled manifest floor."""
    plugin_root_env = (
        os.environ.get("CODEX_PLUGIN_ROOT", "").strip()
        or os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    )
    if not plugin_root_env:
        return {"state": "no_plugin_root", "hint": "plugin root env not set"}

    try:
        sys.path.insert(0, str(Path(plugin_root_env) / "scripts"))
        import om_core_paths  # type: ignore[import-not-found]
    except ImportError as e:
        return {"state": "unknown", "error": f"om_core_paths import failed: {e}"}

    try:
        target = om_core_paths.target_triple()
    except om_core_paths.UnsupportedTarget as e:
        return {"state": "unsupported_target", "error": str(e)}

    # Ground truth: the version `current/<target>` points at is what launchd runs.
    # Report it directly — offline, instant, channel-first-correct.
    active = _current_active_version(om_core_paths, target)
    if active:
        out: dict[str, Any] = {"version": active, "target": target, "state": "ready"}
        cur = om_core_paths.cache_bin_path(active, target)
        try:
            if cur.is_file():
                out["bin_path"] = str(cur)
                out["bin_size_mb"] = cur.stat().st_size / (1 << 20)
        except OSError:
            pass
        return out

    # No active binary yet (fresh install / first fetch). Resolve the fetch target
    # the same way bootstrap does — channel-first, bundled manifest as offline floor
    # — then surface its fetch state.
    try:
        manifest = om_core_paths.resolve_release(Path(plugin_root_env), target)
    except (FileNotFoundError, OSError, json.JSONDecodeError) as e:
        return {"state": "no_manifest", "target": target, "error": str(e)}

    version = manifest.get("version")
    if not version:
        return {"state": "no_version_in_manifest", "target": target}

    out = {"version": version, "target": target}
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


def _local_get(
    cfg: dict[str, Any], path: str, timeout: float = 1.5,
) -> tuple[bool, str | None]:
    transport = cfg.get("transport")
    if transport == "uds":
        socket_path = cfg.get("socket_path")
        if not isinstance(socket_path, str):
            return False, "server.json missing socket_path"
        return _uds_get(socket_path, path, token=cfg.get("token"), timeout=timeout)
    if transport == "npipe":
        pipe_name = cfg.get("pipe_name")
        if not isinstance(pipe_name, str):
            return False, "server.json missing pipe_name"
        return _npipe_get(pipe_name, path, token=cfg.get("token"), timeout=timeout)
    return False, f"unsupported transport: {transport!r}"


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


class _PipeHandleIO(io.RawIOBase):
    def __init__(self, handle: int):
        self._handle = handle
        self._closed = False

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return True

    def readinto(self, b: bytearray | memoryview) -> int:
        data = _npipe_read(self._handle, len(b))
        if not data:
            return 0
        b[: len(data)] = data
        return len(data)

    def write(self, b: bytes | bytearray | memoryview) -> int:
        data = bytes(b)
        _npipe_write_all(self._handle, data)
        return len(data)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            _npipe_close(self._handle)
        super().close()


def _npipe_get(
    pipe_name: str, path: str, token: str | None = None, timeout: float = 1.5,
) -> tuple[bool, str | None]:
    if sys.platform != "win32":
        return False, "npipe transport only available on Windows"
    try:
        handle = _npipe_open(pipe_name, timeout=timeout)
        raw = _PipeHandleIO(handle)
        reader = io.BufferedReader(raw)
        writer = io.BufferedWriter(raw)
        req = f"GET {path} HTTP/1.0\r\nHost: localhost\r\n"
        if token:
            req += f"Authorization: Bearer {token}\r\n"
        req += "\r\n"
        writer.write(req.encode("latin-1"))
        writer.flush()
        buf = b""
        while len(buf) < 65536:
            chunk = reader.read(4096)
            if not chunk:
                break
            buf += chunk
        raw.close()
        head, _, body = buf.partition(b"\r\n\r\n")
        first = head.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
        ok = " 200 " in first
        return ok, (body.decode("utf-8", errors="replace") if ok else first)
    except (OSError, TimeoutError) as e:
        return False, f"{type(e).__name__}: {e}"


def _npipe_open(pipe_name: str, *, timeout: float) -> int:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    OPEN_EXISTING = 3
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
    ERROR_PIPE_BUSY = 231
    deadline = time.monotonic() + timeout
    while True:
        handle = kernel32.CreateFileW(
            pipe_name,
            GENERIC_READ | GENERIC_WRITE,
            0,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if handle != INVALID_HANDLE_VALUE:
            return int(handle)
        err = ctypes.get_last_error()
        if err != ERROR_PIPE_BUSY or time.monotonic() >= deadline:
            raise OSError(err, f"CreateFileW failed for {pipe_name}")
        kernel32.WaitNamedPipeW(pipe_name, max(1, int((deadline - time.monotonic()) * 1000)))


def _npipe_read(handle: int, size: int) -> bytes:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    buf = ctypes.create_string_buffer(size)
    read = wintypes.DWORD(0)
    ok = kernel32.ReadFile(handle, buf, size, ctypes.byref(read), None)
    if not ok and read.value == 0:
        err = ctypes.get_last_error()
        # Server closing the pipe after its HTTP/1.0 response is normal EOF,
        # not a fault: BROKEN_PIPE(109)/PIPE_NOT_CONNECTED(233)/NO_DATA(232)/EOF(38).
        if err in (109, 233, 232, 38):
            return b""
        raise OSError(err, "ReadFile failed")
    return buf.raw[: read.value]


def _npipe_write_all(handle: int, data: bytes) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    view = memoryview(data)
    while view:
        written = wintypes.DWORD(0)
        chunk = view.tobytes()
        ok = kernel32.WriteFile(
            handle,
            ctypes.c_char_p(chunk),
            len(chunk),
            ctypes.byref(written),
            None,
        )
        if not ok:
            raise OSError(ctypes.get_last_error(), "WriteFile failed")
        view = view[written.value :]


def _npipe_close(handle: int) -> None:
    if sys.platform == "win32":
        import ctypes

        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)


def probe_embedding() -> dict[str, Any]:
    """Embedding warmup state — sidecar-owned (GET /v1/embedding/status)."""
    cfg = _read_json(_config_dir() / "server.json")
    if not cfg:
        return {"state": "unknown", "reason": "sidecar not started (no server.json)"}
    ok, body = _local_get(cfg, "/v1/embedding/status")
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
# Sidecar probe — supports UDS / npipe transports via stdlib.
# ---------------------------------------------------------------------------


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

    transport = cfg.get("transport")
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
    elif transport == "npipe":
        pipe_name = cfg.get("pipe_name")
        out["pipe_name"] = pipe_name
        if pipe_name:
            ok, body = _npipe_get(str(pipe_name), "/healthz", timeout=1.5)
    else:
        out["state"] = "unknown_transport"
        return out

    out["state"] = "ready" if ok else "unreachable"
    if body:
        out["healthz_body"] = body[:120]
    return out


def probe_kb() -> dict[str, Any]:
    out: dict[str, Any] = {}
    cfg = _read_json(_config_dir() / "server.json")
    if not cfg:
        return {"state": "unknown", "reason": "sidecar not started (no server.json)"}

    ok, body = _local_get(cfg, "/v1/kb/active")
    if not ok or not body:
        return {"state": "unknown", "reason": body or "sidecar unreachable"}

    try:
        active = json.loads(body)
    except (ValueError, TypeError):
        return {"state": "unknown", "reason": "malformed /v1/kb/active response"}
    if not isinstance(active, dict):
        return {"state": "unknown", "reason": "malformed /v1/kb/active response"}

    if path := active.get("path"):
        out["kb_root_om"] = str(path)
    if db_path := active.get("db_path"):
        out["db_path"] = str(db_path)
    for key in ("id", "source", "schema_version", "ontology_base_version", "writable_by_ingest"):
        if key in active:
            out[key] = active[key]

    data_dir = os.environ.get("CLAUDE_PLUGIN_DATA", "").strip()
    if data_dir:
        kb_root_file = Path(data_dir) / "kb_root"
        if kb_root_file.is_file():
            try:
                legacy = kb_root_file.read_text(encoding="utf-8").strip()
                if legacy:
                    out["kb_root_legacy"] = legacy
            except OSError:
                pass

    out["state"] = "ready" if out.get("kb_root_om") else "no_binding"
    return out


def probe_base_image() -> dict[str, Any]:
    """Free base knowledge image state. Read-only: cached status only."""
    cfg = _read_json(_config_dir() / "server.json")
    if not cfg:
        return {"state": "unknown", "reason": "sidecar not started (no server.json)"}
    ok, body = _local_get(cfg, "/v1/kb/base-image/status")
    if not ok or not body:
        return {"state": "unknown", "reason": body or "sidecar unreachable"}
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return {"state": "unknown", "reason": "malformed /v1/kb/base-image/status response"}
    if not isinstance(data, dict):
        return {"state": "unknown"}
    return {
        "state": data.get("state", "unknown"),
        "installed_version": data.get("installed_version"),
        "channel_version": data.get("channel_version"),
        "swap_state": data.get("swap_state"),
        "swap_progress": data.get("swap_progress"),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


_ICON = {
    "ready": "✅",
    "installed": "✅",
    "attention": "⚠️ ",
    "blocked": "❌",
    "pending": "⏳",
    "downloading": "⏳",
    "swapping": "⏳",
    "integrity": "⏳",
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
        path = k.get("kb_root_om", "?")
        extra = path
        if k.get("schema_version") is not None:
            extra += f", schema={k['schema_version']}"
        if k.get("kb_root_legacy") and k["kb_root_legacy"] != path:
            extra += f"  ⚠️ legacy plugin root={k['kb_root_legacy']}"
    elif k_state == "no_binding":
        extra = "sidecar 未返回 active KB"
    elif k_state == "unknown":
        extra = k.get("reason", "sidecar 未就绪")
    lines.append(f"[D] KB 绑定      {_icon(k_state)} {k_state:<12} {extra}")

    # base knowledge image (free base KB; absent is normal when you use your own KB)
    bi = report.get("base_image", {"state": "unknown"})
    bi_state = bi.get("state", "?")
    extra = ""
    if bi_state == "installed":
        extra = f"v{bi.get('installed_version', '?')}"
    elif bi_state == "absent":
        extra = "未安装（空 KB 首次启动自动拉取；或你在用自己的 KB）"
    elif bi_state == "unknown":
        extra = bi.get("reason", "sidecar 未就绪")
    lines.append(f"[E] base 知识库  {_icon(bi_state)} {bi_state:<12} {extra}")

    # live swap phase — only while a library change is actually applying on this device
    swap = bi.get("swap_state")
    if swap in ("downloading", "swapping", "integrity", "failed"):
        phase = {"downloading": "下载中", "swapping": "切换中",
                 "integrity": "校验中", "failed": "切换失败"}[swap]
        prog = bi.get("swap_progress")
        pct = f" {round(prog * 100)}%" if swap == "downloading" and isinstance(prog, (int, float)) and prog else ""
        lines.append(f"    └ 应用进度    {_icon(swap)} {phase}{pct}")

    # tail hints
    all_ready = all(report[k_]["state"] == "ready" for k_ in (
        "binary", "sidecar", "embedding", "kb",
    ))
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
        "base_image": probe_base_image(),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def main() -> int:
    # Force UTF-8 stdout/stderr: the human render uses ✅/⏳ and Chinese, which
    # crash on a GBK (cp936) Windows console. No-op where stdout is already UTF-8.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

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
