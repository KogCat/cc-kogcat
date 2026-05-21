"""Tool dispatch — entrypoint imported by mcp_server.py.

Synchronous, standard-library only. Routes the 13 MCP tools to the
om-core sidecar over the stdlib UDS HTTP backend.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import compat
from .errors import OmApiError, OmWrapperError
from .http_backend import TRANSPORT_ERRORS, raise_for_response, request, reset

_HTTP_COVERED = frozenset({
    "search",
    "node",
    "edges",
    "calibrate",
    "memory_save",
    "memory_delete",
    "memory_list",
    "memory_get",
    "pack_list",
    "pack_info",
    "pack_install",
    "pack_uninstall",
    "pack_upgrade",
})

# Safe to retry once on transport failure.
_IDEMPOTENT_TOOLS = frozenset({
    "search",
    "node",
    "edges",
    "calibrate",
    "memory_get",
    "memory_list",
    "pack_list",
    "pack_info",
})

_STALL_THRESHOLD_S = 30


def _read_status(path: Path) -> dict | None:
    """Read a JSON statefile, returning None on missing / malformed."""
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


def _build_progress_details(state: dict | None) -> dict[str, Any]:
    """Pull progress fields out of a runner statefile, ignoring missing pieces."""
    details: dict[str, Any] = {}
    if not state:
        return details
    if (s := state.get("state")):
        details["state"] = s
    if (sa := state.get("started_at")):
        details["started_at"] = sa
        if (elapsed := _seconds_since(sa)) is not None:
            details["elapsed_seconds"] = elapsed
    if (bd := state.get("bytes_downloaded")) is not None:
        details["bytes_downloaded"] = int(bd)
        if (hint := state.get("total_bytes_hint")):
            details["total_bytes_hint"] = int(hint)
            details["progress_pct"] = min(100, int(bd) * 100 // max(int(hint), 1))
    if (lpa := state.get("last_progress_at")):
        details["last_progress_at"] = lpa
        if (since_progress := _seconds_since(lpa)) is not None:
            details["seconds_since_progress"] = since_progress
            if state.get("state") == "downloading" and since_progress > _STALL_THRESHOLD_S:
                details["stalled"] = True
                details["stall_seconds"] = since_progress
    if (reason := state.get("reason")):
        details["last_error"] = reason
    return details


def _format_progress_zh(details: dict[str, Any]) -> str:
    parts: list[str] = []
    if "bytes_downloaded" in details:
        mb = details["bytes_downloaded"] / (1 << 20)
        if "total_bytes_hint" in details:
            total_mb = details["total_bytes_hint"] / (1 << 20)
            pct = details.get("progress_pct", 0)
            parts.append(f"已下载 {mb:.0f}MB / 约 {total_mb:.0f}MB ({pct}%)")
        else:
            parts.append(f"已下载 {mb:.0f}MB")
    if "elapsed_seconds" in details:
        parts.append(f"已等 {details['elapsed_seconds']}s")
    return "；".join(parts)


_GATE_FAIL_OPEN = (OmWrapperError, *TRANSPORT_ERRORS)
_embedding_ready_seen = False


def _embedding_ready_or_raise() -> None:
    """Block dense-search calls while the embedding model is downloading.

    Queries the sidecar's ``GET /v1/embedding/status`` — the sidecar owns
    the warmup. Fails open: any transport / wrapper error here is left for
    the binary gate and HTTP dispatch to surface. Once the sidecar reports
    ``ready`` the gate is satisfied for the process lifetime.
    """
    global _embedding_ready_seen
    if _embedding_ready_seen:
        return
    try:
        resp = request("GET", "/v1/embedding/status", timeout=5.0)
    except _GATE_FAIL_OPEN:
        return
    if not resp.is_success:
        return
    try:
        state = resp.json()
    except (ValueError, json.JSONDecodeError):
        return
    if not isinstance(state, dict):
        return

    stage = state.get("state")
    if stage == "ready":
        _embedding_ready_seen = True
        return
    if stage != "downloading":
        # idle / failed / unknown — search degrades to BM25, don't gate.
        return

    details: dict[str, Any] = {"expected_seconds": "60-180"}
    details.update(_build_progress_details(state))

    progress = _format_progress_zh(details)
    if details.get("stalled"):
        hint = (
            f"嵌入模型下载疑似卡住（{progress}；{details['stall_seconds']}s 无进度）。"
            "可重启会话或检查网络后重试；不需要 KB 检索的工作可继续。"
        )
    elif progress:
        hint = (
            f"首次启动正在后台下载嵌入模型 (~90MB)：{progress}。"
            "通常需 1-3 分钟,请稍候后重发本次 query；或先做其他不需要 KB 检索的事情。"
        )
    else:
        hint = (
            "首次启动正在后台下载嵌入模型 (~90MB),通常需 1-3 分钟。"
            "请稍候后重新发起本次 query；或先做其他不需要 KB 检索的事情。"
        )

    raise OmApiError(
        code="EMBEDDING_MODEL_WARMING_UP",
        message="Embedding model is downloading in the background.",
        hint=hint,
        details=details,
        status_code=503,
    )


# Test asserts these are exported by om_core_paths.
_BIN_GATE_OM_CORE_PATHS_ATTRS = (
    "target_triple",
    "current_bin_path",
    "read_expected_release",
    "bin_status_path",
    "UnsupportedTarget",
)


def _bin_gate_impl() -> None:
    """Real body of the binary readiness gate.

    Blocks tool calls only during a genuine first install — when no usable
    om-core binary exists on the machine at all. An *upgrade* download never
    blocks: the stable `current` symlink still points at the working old
    binary, so tool calls keep flowing while the new version downloads in
    the background and swaps in on completion.
    """
    try:
        import om_core_paths  # type: ignore[import-not-found]  # noqa: PLC0415
    except ImportError:
        return

    try:
        target = om_core_paths.target_triple()
    except om_core_paths.UnsupportedTarget:
        return

    # Fast path — a usable binary already exists. `current` is the stable
    # symlink the OS supervisor runs; if it resolves to a real file there is
    # a sidecar to serve this call. Covers both a warm cache and an upgrade
    # downloading in the background. Never block here.
    try:
        if om_core_paths.current_bin_path(target).is_file():
            return
    except OSError:
        return

    # No usable binary → genuine first install. Surface download progress.
    # The version under download was recorded by the SessionStart bootstrap
    # in the expected-release pointer — read locally, no network.
    expected = om_core_paths.read_expected_release()
    expected_version = (expected or {}).get("version")
    if not expected_version:
        return

    status_path = om_core_paths.bin_status_path(expected_version, target)
    state = _read_status(status_path)
    if not state:
        return

    s = state.get("state")
    if s == "ready":
        return

    details: dict[str, Any] = {
        "version": expected_version,
        "target": target,
        "expected_seconds": "10-60",
    }
    details.update(_build_progress_details(state))
    progress = _format_progress_zh(details)

    if s == "failed":
        raise OmApiError(
            code="OM_CORE_BIN_DOWNLOAD_FAILED",
            message="om-core binary download failed.",
            hint=(
                f"上次下载二进制失败：{details.get('last_error', '未知原因')}。"
                "请重启会话以触发重试；或检查网络/磁盘,必要时设置 OM_CORE_BIN 指向本地二进制。"
            ),
            details=details,
            status_code=503,
        )

    if details.get("stalled"):
        hint = (
            f"om-core 二进制下载疑似卡住（{progress}；{details['stall_seconds']}s 无进度）。"
            "可重启会话或检查网络后重试。"
        )
    elif progress:
        hint = (
            f"首次启动正在后台下载 om-core 二进制 (~64MB)：{progress}。"
            "通常需 10-60 秒,请稍候后重发本次工具调用。"
        )
    else:
        hint = (
            "首次启动正在后台下载 om-core 二进制 (~64MB),通常需 10-60 秒。"
            "请稍候后重新发起本次工具调用。"
        )

    raise OmApiError(
        code="OM_CORE_BIN_DOWNLOADING",
        message="om-core binary is downloading in the background.",
        hint=hint,
        details=details,
        status_code=503,
    )


def _om_core_bin_ready_or_raise() -> None:
    """Fail-open binary readiness gate. Only OmApiError propagates."""
    try:
        _bin_gate_impl()
    except OmApiError:
        raise
    except Exception:  # noqa: BLE001 — fail-open
        return


def _http_dispatch(name: str, args: dict[str, Any]) -> Any:
    if name == "search":
        body = {
            "text": args["text"],
            "top": args.get("top", 5),
            "no_log": args.get("no_log", False),
        }
        v = args.get("vertical")
        if v and v != "all":
            body["vertical"] = v
        if (scope := args.get("scope")) is not None:
            body["scope"] = scope
        if (origin := args.get("origin")) is not None:
            body["origin"] = origin
        if args.get("include_details"):
            body["include_details"] = True
        r = request("POST", "/v1/search", json_body=body)
        raise_for_response(r)
        return compat.search(r.json())

    if name == "node":
        ident = args.get("stable_id") or args.get("title")
        if not ident:
            raise OmApiError("INVALID_INPUT", "node requires title or stable_id", status_code=400)
        params: dict[str, str] = {}
        if (scope := args.get("scope")):
            params["scope"] = scope
        r = request("GET", f"/v1/nodes/{quote(str(ident), safe='')}", params=params)
        if r.status_code == 404:
            return compat.node(None, title=args.get("title"), stable_id=args.get("stable_id"))
        raise_for_response(r)
        return compat.node(r.json(), title=args.get("title"), stable_id=args.get("stable_id"))

    if name == "edges":
        ident = args["title"]
        params = {"hops": args.get("hops", 1)}
        if (scope := args.get("scope")):
            params["scope"] = scope
        r = request("GET", f"/v1/nodes/{quote(str(ident), safe='')}/edges", params=params)
        raise_for_response(r)
        return compat.edges(r.json())

    if name == "calibrate":
        body: dict[str, Any] = {
            "text": args["text"],
            "top_k": args.get("top_k", 5),
        }
        source: dict[str, Any] = {}
        if (kind := args.get("source_kind")):
            source["kind"] = kind
        if (cid := args.get("client_id")):
            source["client_id"] = cid
        if source:
            body["source"] = source
        r = request("POST", "/v1/calibrate", json_body=body)
        raise_for_response(r)
        return r.json()

    if name == "memory_save":
        for required in ("name", "description", "type"):
            if required not in args:
                raise OmApiError(
                    "INVALID_INPUT", f"memory_save requires `{required}`", status_code=400,
                )
        body = {
            "name": args["name"],
            "description": args["description"],
            "type": args["type"],
            "source": args.get("source", "user_explicit"),
            "body": args.get("body", ""),
        }
        r = request("POST", "/v1/memory/upsert", json_body=body)
        raise_for_response(r)
        return r.json()

    if name == "memory_delete":
        if "name" not in args:
            raise OmApiError("INVALID_INPUT", "memory_delete requires `name`", status_code=400)
        r = request("DELETE", f"/v1/memory/{quote(str(args['name']), safe='')}")
        raise_for_response(r)
        return r.json()

    if name == "memory_list":
        r = request("GET", "/v1/memory/list")
        raise_for_response(r)
        return r.json()

    if name == "memory_get":
        if "name" not in args:
            raise OmApiError("INVALID_INPUT", "memory_get requires `name`", status_code=400)
        r = request("GET", f"/v1/memory/{quote(str(args['name']), safe='')}")
        raise_for_response(r)
        return r.json()

    if name == "pack_list":
        r = request("GET", "/v1/packs")
        raise_for_response(r)
        return r.json()

    if name == "pack_info":
        if "name" not in args:
            raise OmApiError("INVALID_INPUT", "pack_info requires `name`", status_code=400)
        r = request("GET", f"/v1/packs/{quote(str(args['name']), safe='')}")
        raise_for_response(r)
        return r.json()

    if name == "pack_install":
        if "archive_path" not in args:
            raise OmApiError(
                "INVALID_INPUT",
                "pack_install requires `archive_path` (local file path)",
                status_code=400,
            )
        r = request(
            "POST", "/v1/packs/install",
            json_body={"archive_path": str(args["archive_path"])},
        )
        raise_for_response(r)
        return r.json()

    if name == "pack_uninstall":
        if "name" not in args:
            raise OmApiError(
                "INVALID_INPUT",
                "pack_uninstall requires `name` (@scope/name)",
                status_code=400,
            )
        r = request(
            "POST", "/v1/packs/uninstall",
            json_body={
                "name": str(args["name"]),
                "yes": bool(args.get("yes", False)),
            },
        )
        raise_for_response(r)
        return r.json()

    if name == "pack_upgrade":
        if "archive_path" not in args:
            raise OmApiError(
                "INVALID_INPUT",
                "pack_upgrade requires `archive_path` (local file path)",
                status_code=400,
            )
        r = request(
            "POST", "/v1/packs/upgrade",
            json_body={
                "archive_path": str(args["archive_path"]),
                "dry_run": bool(args.get("dry_run", False)),
                "yes": bool(args.get("yes", False)),
                "force": bool(args.get("force", False)),
            },
        )
        raise_for_response(r)
        return r.json()

    raise OmApiError("UNKNOWN_TOOL", f"unknown tool: {name}", status_code=400)


def _http_dispatch_with_retry(name: str, args: dict[str, Any]) -> Any:
    """Call `_http_dispatch` with stale-cfg invalidation + one retry for idempotent tools."""
    try:
        return _http_dispatch(name, args)
    except TRANSPORT_ERRORS:
        reset()
        if name not in _IDEMPOTENT_TOOLS:
            raise OmApiError(
                code="om.sidecar_unreachable",
                message=f"sidecar transport failure on non-idempotent tool {name!r}",
                hint=(
                    "The sidecar likely died or restarted mid-request. The "
                    "mutation may or may not have applied — re-check state "
                    "before retrying."
                ),
                status_code=503,
            )
        return _http_dispatch(name, args)
    except OmApiError as e:
        if 500 <= e.status_code < 600:
            reset()
            if name in _IDEMPOTENT_TOOLS:
                return _http_dispatch(name, args)
        raise


def dispatch(name: str, args: dict[str, Any]) -> Any:
    """Route a tool call to the om-core HTTP sidecar."""
    if name == "search":
        _embedding_ready_or_raise()

    _om_core_bin_ready_or_raise()
    return _http_dispatch_with_retry(name, args)
