"""MCP POST wrapper that replaces only semantic search with async-safe logic."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from pluribus.mcp import TOOLS, _error, _handle_tool_call, _success
from pluribus.semantic_async import _audit_search, semantic_lookup

router = APIRouter(prefix="/mcp", tags=["mcp"])


@router.post("/")
async def mcp_handle_async(request: Request) -> JSONResponse:
    """Preserve legacy MCP behavior while intercepting memory_search_semantic."""
    try:
        body = await request.json()
    except Exception:
        return _error(-32700, "Parse error: invalid JSON")

    method = body.get("method", "")
    params = body.get("params", {}) or {}
    id_: Any = body.get("id")

    if method == "tools/list":
        return _success({"tools": TOOLS}, id_)
    if method != "tools/call":
        return _error(-32601, f"Method not found: {method}", id_)

    tool_name = params.get("name", "")
    arguments = params.get("arguments", {}) or {}
    if tool_name != "memory_search_semantic":
        return await _handle_tool_call(request, tool_name, arguments, id_)

    query = arguments.get("query", "")
    if not isinstance(query, str) or not query.strip():
        return _error(-32602, "query is required", id_)

    scope = arguments.get("scope", "shared")
    category = arguments.get("category", "")
    try:
        top_k = max(1, min(int(arguments.get("top_k", 5)), 50))
    except (TypeError, ValueError):
        return _error(-32602, "top_k must be an integer", id_)

    try:
        rows, fallback = await semantic_lookup(
            query,
            scope,
            category,
            None,
            top_k,
        )
        agent = getattr(request.state, "agent", None) or {}
        if agent.get("id"):
            await _audit_search(
                agent["id"], query, len(rows), semantic=True, fallback=fallback
            )
        results = [
            {
                "fact_id": row["fact_id"],
                "content": row["content"],
                "scope": row["scope"],
                "category": row["category"],
                "score": row["score"],
            }
            for row in rows
        ]
        return _success(
            {
                "results": results,
                "total": len(results),
                "query": query,
                "fallback": fallback,
            },
            id_,
        )
    except Exception:
        # Keep transport error generic; internal exception text may expose DB or
        # network details and is already observable through server logs/audit.
        return _error(-32603, "Semantic search failed", id_)
