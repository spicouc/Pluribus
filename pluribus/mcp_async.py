"""Async-safe MCP wrapper for memory, directives, semantic search and Xerrameca."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from pluribus.directives import (
    DirectiveClaimRequest,
    DirectiveCompleteRequest,
    DirectiveCreateRequest,
    DirectiveFailRequest,
    DirectiveGrantRequest,
    DirectiveRejectRequest,
    claim_directive,
    complete_directive,
    create_directive,
    directive_inbox,
    fail_directive,
    get_directive,
    list_directive_grants,
    reject_directive,
    set_directive_grant,
)
from pluribus.mcp import TOOLS, _error, _handle_tool_call, _success
from pluribus.memory_sync import memory_sync_service
from pluribus.recall import RecallRequest, recall_service
from pluribus.semantic_async import _audit_search, semantic_lookup
from pluribus.xerrameca.mcp import (
    TOOL_NAMES as XERRAMECA_TOOL_NAMES,
    TOOLS as XERRAMECA_TOOLS,
    handle_tool as handle_xerrameca_tool,
)

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

MEMORY_SYNC_TOOL = {
    "name": "memory_sync",
    "description": "Retorna només els records canviats des d'un cursor i indica quan convé revisar de nou.",
    "input_schema": {
        "type": "object",
        "properties": {
            "cursor": {"type": "integer", "default": 0, "minimum": 0},
            "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 200},
        },
    },
}

DIRECTIVE_TOOLS = [
    {
        "name": "directive_inbox",
        "description": "Llista directives pendents dirigides a l'agent autenticat.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200}},
        },
    },
    {
        "name": "directive_create",
        "description": "Crea una directiva estructurada si l'emissor pot delegar la capability i el destinatari la pot executar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "target_agent_id": {"type": "string"},
                "scope": {"type": "string", "default": "shared"},
                "action": {"type": "string"},
                "arguments": {"type": "object", "default": {}},
                "required_capability": {"type": "string"},
                "ttl_seconds": {"type": "integer", "default": 3600, "minimum": 60, "maximum": 86400},
                "idempotency_key": {"type": "string"},
            },
            "required": ["target_agent_id", "action", "required_capability"],
        },
    },
    {
        "name": "directive_get",
        "description": "Consulta una directiva visible per emissor, destinatari o admin.",
        "input_schema": {
            "type": "object",
            "properties": {"directive_id": {"type": "string"}},
            "required": ["directive_id"],
        },
    },
    {
        "name": "directive_claim",
        "description": "Reclama atòmicament una directiva pendent i obté una lease.",
        "input_schema": {
            "type": "object",
            "properties": {
                "directive_id": {"type": "string"},
                "lease_seconds": {"type": "integer", "default": 300, "minimum": 30, "maximum": 1800},
            },
            "required": ["directive_id"],
        },
    },
    {
        "name": "directive_complete",
        "description": "Completa una directiva reclamada per l'agent autenticat.",
        "input_schema": {
            "type": "object",
            "properties": {
                "directive_id": {"type": "string"},
                "result": {"type": "object", "default": {}},
            },
            "required": ["directive_id"],
        },
    },
    {
        "name": "directive_fail",
        "description": "Marca com fallida una directiva reclamada per l'agent autenticat.",
        "input_schema": {
            "type": "object",
            "properties": {
                "directive_id": {"type": "string"},
                "error": {"type": "string"},
            },
            "required": ["directive_id", "error"],
        },
    },
    {
        "name": "directive_reject",
        "description": "Rebutja una directiva dirigida a l'agent autenticat.",
        "input_schema": {
            "type": "object",
            "properties": {
                "directive_id": {"type": "string"},
                "reason": {"type": "string", "default": "rejected"},
            },
            "required": ["directive_id"],
        },
    },
    {
        "name": "directive_list_grants",
        "description": "Consulta grants de directives propis; admin pot consultar qualsevol agent.",
        "input_schema": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}},
        },
    },
    {
        "name": "directive_set_grant",
        "description": "Administra can_execute/can_delegate per capability. Només admin.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "capability": {"type": "string"},
                "can_execute": {"type": "boolean", "default": False},
                "can_delegate": {"type": "boolean", "default": False},
            },
            "required": ["agent_id", "capability"],
        },
    },
]

DIRECTIVE_TOOL_NAMES = {tool["name"] for tool in DIRECTIVE_TOOLS}
ALL_TOOLS = [*TOOLS, MEMORY_RECALL_TOOL, MEMORY_SYNC_TOOL, *DIRECTIVE_TOOLS, *XERRAMECA_TOOLS]


def _dump_result(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, list):
        return [_dump_result(item) for item in value]
    return value


async def _handle_directive_tool(request: Request, tool_name: str, arguments: dict[str, Any]) -> Any:
    if tool_name == "directive_inbox":
        limit = int(arguments.get("limit", 50))
        if not 1 <= limit <= 200:
            raise ValueError("limit fora de rang")
        return _dump_result(await directive_inbox(request, limit=limit))
    if tool_name == "directive_create":
        return _dump_result(await create_directive(request, DirectiveCreateRequest.model_validate(arguments)))

    directive_id = arguments.get("directive_id")
    if tool_name == "directive_get":
        if not isinstance(directive_id, str):
            raise ValueError("directive_id requerit")
        return _dump_result(await get_directive(request, directive_id))
    if tool_name == "directive_claim":
        if not isinstance(directive_id, str):
            raise ValueError("directive_id requerit")
        body = DirectiveClaimRequest.model_validate({"lease_seconds": arguments.get("lease_seconds", 300)})
        return _dump_result(await claim_directive(request, directive_id, body))
    if tool_name == "directive_complete":
        if not isinstance(directive_id, str):
            raise ValueError("directive_id requerit")
        body = DirectiveCompleteRequest.model_validate({"result": arguments.get("result", {})})
        return _dump_result(await complete_directive(request, directive_id, body))
    if tool_name == "directive_fail":
        if not isinstance(directive_id, str):
            raise ValueError("directive_id requerit")
        return _dump_result(await fail_directive(request, directive_id, DirectiveFailRequest.model_validate({"error": arguments.get("error")})))
    if tool_name == "directive_reject":
        if not isinstance(directive_id, str):
            raise ValueError("directive_id requerit")
        body = DirectiveRejectRequest.model_validate({"reason": arguments.get("reason", "rejected")})
        return _dump_result(await reject_directive(request, directive_id, body))
    if tool_name == "directive_list_grants":
        caller = getattr(request.state, "agent", None) or {}
        agent_id = arguments.get("agent_id") or caller.get("id")
        if not isinstance(agent_id, str):
            raise ValueError("agent_id requerit")
        return _dump_result(await list_directive_grants(request, agent_id))
    if tool_name == "directive_set_grant":
        agent_id = arguments.get("agent_id")
        capability = arguments.get("capability")
        if not isinstance(agent_id, str) or not isinstance(capability, str):
            raise ValueError("agent_id i capability requerits")
        body = DirectiveGrantRequest.model_validate({
            "can_execute": arguments.get("can_execute", False),
            "can_delegate": arguments.get("can_delegate", False),
        })
        return _dump_result(await set_directive_grant(request, agent_id, capability, body))
    raise ValueError("directive tool desconeguda")


@router.get("/")
async def mcp_list_async_tools() -> JSONResponse:
    return _success({"tools": ALL_TOOLS, "protocol": "model-context-protocol", "version": "1.3.0"})


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
    if not isinstance(params, dict):
        return _error(-32602, "params must be an object", id_)

    tool_name = params.get("name", "")
    arguments = params.get("arguments", {}) or {}
    if not isinstance(arguments, dict):
        return _error(-32602, "arguments must be an object", id_)

    if tool_name == "memory_recall":
        try:
            recall_request = RecallRequest.model_validate(arguments)
        except ValidationError:
            return _error(-32602, "Invalid memory_recall arguments", id_)
        try:
            result = await recall_service(getattr(request.state, "agent", None) or {}, recall_request)
            return _success(result.model_dump(), id_)
        except HTTPException as exc:
            return _error(exc.status_code, str(exc.detail), id_)
        except Exception:
            return _error(-32603, "Memory recall failed", id_)

    if tool_name == "memory_sync":
        try:
            cursor = int(arguments.get("cursor", 0))
            limit = int(arguments.get("limit", 100))
            result = await memory_sync_service(getattr(request.state, "agent", None) or {}, cursor=cursor, limit=limit)
            return _success(result.model_dump(), id_)
        except (TypeError, ValueError):
            return _error(-32602, "Invalid memory_sync arguments", id_)
        except HTTPException as exc:
            return _error(exc.status_code, str(exc.detail), id_)
        except Exception:
            return _error(-32603, "Memory sync failed", id_)

    if tool_name in DIRECTIVE_TOOL_NAMES:
        try:
            return _success(await _handle_directive_tool(request, tool_name, arguments), id_)
        except (ValidationError, TypeError, ValueError):
            return _error(-32602, "Invalid directive arguments", id_)
        except HTTPException as exc:
            return _error(exc.status_code, str(exc.detail), id_)
        except Exception:
            return _error(-32603, "Directive tool failed", id_)

    if tool_name in XERRAMECA_TOOL_NAMES:
        try:
            return _success(await handle_xerrameca_tool(request, tool_name, arguments), id_)
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
        results = [{
            "fact_id": row["fact_id"],
            "content": row["content"],
            "scope": row["scope"],
            "category": row["category"],
            "score": row["score"],
        } for row in rows]
        return _success({"results": results, "total": len(results), "query": query, "fallback": fallback}, id_)
    except Exception:
        return _error(-32603, "Semantic search failed", id_)
