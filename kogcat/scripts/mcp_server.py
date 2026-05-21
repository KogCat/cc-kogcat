#!/usr/bin/env python3
"""om MCP server — stdlib stdio JSON-RPC, zero third-party dependencies.

MCP stdio transport is newline-delimited JSON-RPC 2.0 on stdin/stdout.
This server hand-rolls that framing (no `mcp` SDK) so it runs on whatever
python3 is already on the machine — e.g. the macOS system interpreter —
with no bootstrap, no vendored `lib/`, no version floor beyond 3.6.
"""
import json
import sys
from pathlib import Path

# Make sibling packages importable (om_mcp + om_core_paths + project_config).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from om_mcp.errors import OmApiError, OmWrapperError  # noqa: E402
from om_mcp.tools import dispatch as _dispatch  # noqa: E402

_PROTOCOL_VERSION = "2025-06-18"
_SERVER_NAME = "om"

_TOOLS = [
    {
        "name": "search",
        "description": "Similarity search over KB concepts. Returns hits with source_pack/stable_id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "search query"},
                "vertical": {"type": "string", "description": "R|W|M|all (default all)"},
                "top": {"type": "integer", "default": 5},
                "no_log": {"type": "boolean", "default": False},
                "scope": {"type": "string",
                          "description": "'mine' | '@owner/name' | 'all' (default)"},
                "origin": {"type": "string",
                           "enum": ["user_query", "ingest_probe", "system_audit"],
                           "default": "user_query"},
                "include_details": {"type": "boolean", "default": False,
                                    "description": "Attach full node payload to each hit under `details`."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "node",
        "description": "Fetch full node (concept/source/synthesis) by title or stable_id, including edges.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "scope": {"type": "string",
                          "description": "'mine' | '@owner/name'"},
                "stable_id": {"type": "string",
                              "description": "UUID v4 — alternative to title"},
            },
        },
    },
    {
        "name": "edges",
        "description": "Graph traversal from a node. hops=1 or 2.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "hops": {"type": "integer", "default": 1, "enum": [1, 2]},
                "scope": {"type": "string",
                          "description": "'mine' | '@owner/name'"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "calibrate",
        "description": (
            "KB calibration of an LLM draft. Returns "
            "{directive, debug?} where directive carries should_emit / "
            "placement / phrasing / inline_refs / user_facing_note / extras. "
            "Client renders directive verbatim — no internal stance/signal "
            "fields exposed. Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string",
                         "description": "LLM answer to calibrate against KB"},
                "top_k": {"type": "integer", "default": 5},
                "source_kind": {"type": "string",
                                "description": "Caller namespace (e.g. 'cc.parallel_query')."},
                "client_id": {"type": "string"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "pack_list",
        "description": "List vendor packs installed in this KB.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "pack_info",
        "description": "Return the lock entry + manifest for an installed pack.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "@scope/name"}},
            "required": ["name"],
        },
    },
    {
        "name": "pack_install",
        "description": (
            "Install a vendor pack from a local .ompack file. URL fetching is "
            "the caller's responsibility — download first, then pass the path."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "archive_path": {
                    "type": "string",
                    "description": "Absolute or kb_root-relative path to the .ompack file.",
                },
            },
            "required": ["archive_path"],
        },
    },
    {
        "name": "pack_uninstall",
        "description": (
            "Remove an installed vendor pack. When inbound edges exist and "
            "yes=false, the call aborts with 409 PACK_USER_ABORTED."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "@scope/name of an installed pack",
                },
                "yes": {
                    "type": "boolean",
                    "default": False,
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "pack_upgrade",
        "description": (
            "Upgrade an installed pack to a new .ompack version. "
            "ALWAYS run with dry_run=true first; non-empty rewrite plans "
            "require yes=true. Pinned packs require force=true. Local paths only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "archive_path": {
                    "type": "string",
                    "description": "Path to the new .ompack file.",
                },
                "dry_run": {
                    "type": "boolean",
                    "default": False,
                },
                "yes": {
                    "type": "boolean",
                    "default": False,
                },
                "force": {
                    "type": "boolean",
                    "default": False,
                },
            },
            "required": ["archive_path"],
        },
    },
    {
        "name": "memory_save",
        "description": (
            "Persist a user memory across sessions. "
            "Use source='user_explicit' when the user explicitly asks to remember; "
            "source='client_inferred' otherwise. "
            "type ∈ {user, feedback, project, reference}; "
            "type='lens' is the synthesized user-perspective profile (single "
            "canonical 'user_lens') — write it only via the memory-consolidate "
            "skill, never ad-hoc."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "lowercase ascii identifier, [a-z][a-z0-9_]{0,79}",
                },
                "description": {
                    "type": "string",
                    "description": "≤200 chars; what this memory is about",
                },
                "type": {
                    "type": "string",
                    "enum": ["user", "feedback", "project", "reference", "lens"],
                },
                "source": {
                    "type": "string",
                    "enum": ["user_explicit", "client_inferred"],
                    "default": "user_explicit",
                },
                "body": {
                    "type": "string",
                    "description": "Markdown body (≤4KB utf-8).",
                },
            },
            "required": ["name", "description", "type"],
        },
    },
    {
        "name": "memory_delete",
        "description": "Remove a memory by name.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "memory_list",
        "description": "Return all memories' frontmatter (no body).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "memory_get",
        "description": "Fetch a single memory by name (frontmatter + body).",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
]


class _MethodNotFound(Exception):
    """Raised for an unrecognized JSON-RPC method."""


def _server_version() -> str:
    try:
        root = Path(__file__).resolve().parent.parent
        data = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        return str(data.get("version", "0"))
    except Exception:  # noqa: BLE001 — informational only
        return "0"


def _call_tool(name: str, arguments: dict) -> dict:
    """Run a tool; errors are surfaced as result text (matches prior behavior)."""
    try:
        result = _dispatch(name, arguments or {})
    except OmApiError as e:
        text = json.dumps(
            {"error": e.code, "message": e.message, "hint": e.hint, "details": e.details},
            ensure_ascii=False,
        )
    except OmWrapperError as e:
        text = json.dumps(
            {"error": e.code, "message": str(e), "hint": e.hint},
            ensure_ascii=False,
        )
    except Exception as e:  # noqa: BLE001 — surface anything as tool error text
        text = json.dumps(
            {"error": type(e).__name__, "message": str(e)},
            ensure_ascii=False,
        )
    else:
        text = json.dumps(result, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}]}


def _handle(method: str, params: dict):
    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion") or _PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": _SERVER_NAME, "version": _server_version()},
        }
    if method == "tools/list":
        return {"tools": _TOOLS}
    if method == "tools/call":
        return _call_tool(params.get("name", ""), params.get("arguments") or {})
    if method == "ping":
        return {}
    raise _MethodNotFound(method)


def _write(out, obj: dict) -> None:
    out.write(json.dumps(obj, ensure_ascii=False) + "\n")
    out.flush()


def main() -> None:
    stdin = sys.stdin
    out = sys.stdout
    for stream in (stdin, out):
        try:
            stream.reconfigure(encoding="utf-8")  # py3.7+
        except Exception:  # noqa: BLE001
            pass

    while True:
        line = stdin.readline()
        if not line:  # EOF — client closed the pipe
            break
        line = line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _write(out, {"jsonrpc": "2.0", "id": None,
                         "error": {"code": -32700, "message": "parse error"}})
            continue

        method = msg.get("method")
        if method is None:
            continue  # a response/unknown frame — not addressed to this server
        msg_id = msg.get("id")
        is_notification = "id" not in msg

        try:
            result = _handle(method, msg.get("params") or {})
        except _MethodNotFound:
            if not is_notification:
                _write(out, {"jsonrpc": "2.0", "id": msg_id,
                             "error": {"code": -32601,
                                       "message": f"method not found: {method}"}})
            continue
        except Exception as e:  # noqa: BLE001
            if not is_notification:
                _write(out, {"jsonrpc": "2.0", "id": msg_id,
                             "error": {"code": -32603, "message": str(e)}})
            continue

        if not is_notification:
            _write(out, {"jsonrpc": "2.0", "id": msg_id, "result": result})


if __name__ == "__main__":
    main()
