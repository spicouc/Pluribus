"""Async-safe MCP wrapper for recall, semantic search and Xerrameca tools."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from pluribus.mcp import TOOLS, _error, _handle_tool_call, _success
from pluribus.recall import RecallRequest, recall_service
from pluribus.semantic_async import _audit_search, semantic_lookup
from pluribus.xerrameca.mcp import TOOL_NAMES as XERRAMECA_TOOL_NAMES, TOOLS as XERRAMECA_TOOLS, handle_tool as handle_xerrameca_tool

router = APIRouter(prefix="/mcp", tags=["mcp"])

MEMORY_RECALL_TOOL = {
    "name": "memory_recall",
    "description": "Recupera records complets amb ranking híbrid i només dins els scopes autoritzats de l'agent.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "scope": {"type": "string", "description": "Opcional; per defecte usa tots els scopes autoritzats"},
            "category": {"type": "string", "description": "Opcional; per defecte busca totes les categories"},
            "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
        },
        "required": ["query"],
    },
}

ALL_TOOLS = [*TOOLS, MEMORY_RECALL_TOOL, *XERRAMECA_TOOLS]


@router.get("/")
async def mcp_list_async_tools() -> JSONResponse:
    return _success({"tools": ALL_TOOLS, "protocol": "model-context-protocol", "version": "1.2.0"})


@router.post("/")
async def mcp_handle_async(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return _error(-32700, "Parse error: invalid JSON")

    method = body.get("method", "")
    params = body.get("params", {}) or {}
    id_: Any = body.get("id")

    if method == "tools/list":
        return _success({"tools": ALL_TOOLS}, id_)
    if method != "tools/call":
        return _error(-32601, f"Method not found: {method}", id_)

    tool_name = params.get("name", "")
    arguments = params.get("arguments", {}) or {}

    if tool_name == "memory_recall":
        try:
            recall_request = RecallRequest.model_validate(arguments)
        except ValidationError:
            return _error(-32602, "Invalid memory_recall arguments", id_)
        try:
            agent = getattr(request.state, "agent", None) or {}
            result = await recall_service(agent, recall_request)
            return _success(result.model_dump(), id_)
        except HTTPException as exc:
            return _error(exc.status_code, str(exc.detail), id_)
        except Exception:
            return _error(-32603, "Memory recall failed", id_)

    if tool_name in XERRAMECA_TOOL_NAMES:
        try:
            result = await handle_xerrameca_tool(request, tool_name, arguments)
            return _success(result, id_)
        except HTTPException as exc:
            return _error(exc.status_code, str(exc.detail), id_)
        except Exception:
            return _error(-32603, "Xerrameca tool failed", id_)

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
        rows, fallback = await semantic_lookup(query, scope, category, None, top_k)
        agent = getattr(request.state, "agent", None) or {}
        if agent.get("id"):
            await _audit_search(agent["id"], query, len(rows), semantic=True, fallback=fallback)
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
        return _success({"results": results, "total": len(results), "query": query, "fallback": fallback}, id_)
    except Exception:
        return _error(-32603, "Semantic search failed", id_)
