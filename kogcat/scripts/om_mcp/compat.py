"""HTTP → MCP shape adapters."""
from __future__ import annotations

from typing import Any


def search(http_resp: dict[str, Any]) -> dict[str, Any]:
    """POST /v1/search → MCP shape."""
    return {
        "results": http_resp.get("results") or [],
    }


def node(http_resp: dict[str, Any] | None, *, title: str | None, stable_id: str | None) -> dict[str, Any]:
    """GET /v1/nodes/{id} → MCP shape. Returns ``{found: False, ...}`` on miss."""
    if http_resp is None:
        return {"found": False, "title": title, "stable_id": stable_id}
    return http_resp


def edges(http_resp: dict[str, Any]) -> dict[str, Any]:
    """GET /v1/nodes/{id}/edges → MCP shape."""
    return {
        "hop1": http_resp.get("hop1") or [],
        **({"hop2": http_resp["hop2"]} if http_resp.get("hop2") is not None else {}),
    }
