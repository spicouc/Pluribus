"""Central authorization and fail-closed raw-input validation guards."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from pluribus.db import get_db
from pluribus.validation import (
    validate_category,
    validate_content,
    validate_identifier,
    validate_key,
    validate_metadata,
    validate_query,
    validate_scope,
    validate_ttl,
)


_PROTECTED_CATEGORIES = {"system", "config", "entities"}


def _request_agent(request: Request) -> dict[str, Any]:
    agent = getattr(request.state, "agent", None)
    if not agent:
        raise HTTPException(status_code=401, detail="Autenticació requerida")
    return agent


def _require(agent: dict[str, Any], permission: str, scope: str | None = None) -> None:
    perms = agent.get("permissions", {}) or {}
    if perms.get("admin", False):
        return
    if not perms.get(permission, False):
        raise HTTPException(status_code=403, detail=f"L'agent no té permís '{permission}'")
    if scope is not None:
        allowed = agent.get("allowed_scopes", ["shared"]) or []
        if scope not in allowed:
            raise HTTPException(status_code=403, detail=f"Àmbit '{scope}' no permès per a aquest agent")


def _validated(callable_, value):
    try:
        return callable_(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _optional_category(value: str | None) -> str | None:
    if value in {None, ""}:
        return value
    return _validated(validate_category, value)


def _validate_write_body(body: dict[str, Any]) -> str:
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Cos JSON invàlid")
    scope = _validated(validate_scope, body.get("scope", "shared"))
    _validated(validate_content, body.get("content", ""))
    _validated(validate_category, body.get("category", "events"))
    if "key" in body:
        _validated(validate_key, body.get("key"))
    if "metadata" in body:
        _validated(validate_metadata, body.get("metadata"))
    if "ttl_days" in body:
        _validated(validate_ttl, body.get("ttl_days"))
    return scope


def _validate_query_args(query: str, scope: str, category: str | None = None) -> str:
    _validated(validate_query, query)
    normalized_scope = _validated(validate_scope, scope)
    _optional_category(category)
    return normalized_scope


async def _fact_scope_category(fact_id: str) -> tuple[str, str] | None:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT scope, COALESCE(category, '') AS category FROM facts WHERE id = ? AND deleted_at IS NULL",
            (fact_id,),
        )
        row = await cursor.fetchone()
        return None if not row else (row["scope"], row["category"])


async def memory_authorize(request: Request) -> None:
    """Apply permission, scope and raw validation before memory handlers run."""
    agent = _request_agent(request)
    path = request.url.path.rstrip("/") or "/"
    method = request.method.upper()

    if path == "/v1/memory/write" and method == "POST":
        body = await request.json()
        scope = _validate_write_body(body)
        _require(agent, "write", scope)
        return

    if path == "/v1/memory/query-save" and method == "POST":
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="Cos JSON invàlid")
        scope = _validated(validate_scope, body.get("scope", "shared"))
        _validated(validate_query, body.get("query", ""))
        _validated(validate_content, body.get("content", ""))
        if "metadata" in body:
            _validated(validate_metadata, body.get("metadata"))
        source_ids = body.get("source_fact_ids")
        if source_ids is not None:
            if not isinstance(source_ids, list) or len(source_ids) > 100:
                raise HTTPException(status_code=422, detail="source_fact_ids invàlid")
            for fact_id in source_ids:
                _validated(lambda value: validate_identifier(value, "source_fact_id"), fact_id)
        _require(agent, "write", scope)
        return

    if path == "/v1/memory/query" and method == "GET":
        scope = _validate_query_args(
            request.query_params.get("q", ""),
            request.query_params.get("scope", "shared"),
            request.query_params.get("category", "events"),
        )
        _require(agent, "read", scope)
        return

    if path == "/v1/memory/search/semantic" and method == "POST":
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="Cos JSON invàlid")
        scope = _validate_query_args(
            body.get("query", ""),
            body.get("scope", "shared"),
            body.get("category", "events"),
        )
        _require(agent, "read", scope)
        return

    if path == "/v1/memory/search" and method == "GET":
        scope = _validate_query_args(
            request.query_params.get("q", ""),
            request.query_params.get("scope", "shared"),
            request.query_params.get("category", "events"),
        )
        _require(agent, "read", scope)
        return

    if path == "/v1/memory/ls" and method == "GET":
        scope = _validated(validate_scope, request.query_params.get("scope", "shared"))
        _optional_category(request.query_params.get("category", ""))
        _require(agent, "read", scope)
        return

    if path in {"/v1/memory/expire", "/v1/memory/audit", "/v1/memory/lint"}:
        _require(agent, "admin")
        return

    if path == "/v1/memory" and method == "GET":
        scope_raw = request.query_params.get("scope")
        if scope_raw is not None:
            scope = _validated(validate_scope, scope_raw)
            _optional_category(request.query_params.get("category"))
        else:
            scope = None
        sort = request.query_params.get("sort", "created_at:desc")
        if sort not in {
            "created_at:asc",
            "created_at:desc",
            "updated_at:asc",
            "updated_at:desc",
        }:
            raise HTTPException(status_code=422, detail="sort invàlid")
        if agent.get("permissions", {}).get("admin", False):
            return
        if not scope:
            raise HTTPException(status_code=400, detail="Els agents no-admin han d'indicar un scope explícit")
        _require(agent, "read", scope)
        return

    prefix = "/v1/memory/"
    if path.startswith(prefix) and method in {"PUT", "DELETE"}:
        fact_id = path[len(prefix):]
        if not fact_id or "/" in fact_id:
            return
        _validated(lambda value: validate_identifier(value, "fact_id"), fact_id)
        if method == "PUT":
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(status_code=422, detail="Cos JSON invàlid")
            _validated(validate_content, body.get("content", ""))
            if "category" in body and body.get("category") is not None:
                _validated(validate_category, body.get("category"))
            if "metadata" in body:
                _validated(validate_metadata, body.get("metadata"))

        fact = await _fact_scope_category(fact_id)
        if fact is None:
            return
        scope, category = fact
        _require(agent, "write" if method == "PUT" else "delete", scope)
        if method == "DELETE" and category in _PROTECTED_CATEGORIES and not agent.get("permissions", {}).get("admin", False):
            raise HTTPException(status_code=403, detail="Fet de categoria persistent; requereix permís admin")


async def mcp_authorize(request: Request) -> None:
    """Authorize and validate MCP tool calls before legacy handlers execute."""
    if request.method.upper() == "GET":
        return
    agent = _request_agent(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="JSON-RPC invàlid")
    if body.get("method") == "tools/list":
        _require(agent, "read")
        return
    if body.get("method") != "tools/call":
        return

    params = body.get("params") or {}
    if not isinstance(params, dict):
        raise HTTPException(status_code=422, detail="params invàlid")
    tool = params.get("name", "")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        raise HTTPException(status_code=422, detail="arguments invàlid")

    scoped = {
        "memory_write": "write",
        "memory_query": "read",
        "memory_search_semantic": "read",
        "memory_ls": "read",
    }
    if tool in scoped:
        scope = _validated(validate_scope, args.get("scope", "shared"))
        _optional_category(args.get("category", ""))
        if tool == "memory_write":
            _validated(validate_content, args.get("content", ""))
            if "key" in args:
                _validated(validate_key, args.get("key"))
            if "metadata" in args:
                _validated(validate_metadata, args.get("metadata"))
        elif tool == "memory_query":
            _validated(validate_query, args.get("q", ""))
        elif tool == "memory_search_semantic":
            _validated(validate_query, args.get("query", ""))
        _require(agent, scoped[tool], scope)
        return

    if tool in {"memory_get_fact", "memory_delete"}:
        fact_id = args.get("fact_id", "")
        _validated(lambda value: validate_identifier(value, "fact_id"), fact_id)
        fact = await _fact_scope_category(fact_id)
        if fact is None:
            return
        scope, category = fact
        permission = "delete" if tool == "memory_delete" else "read"
        _require(agent, permission, scope)
        if tool == "memory_delete" and category in _PROTECTED_CATEGORIES and not agent.get("permissions", {}).get("admin", False):
            raise HTTPException(status_code=403, detail="Fet de categoria persistent; requereix permís admin")
        return

    if tool in {"memory_stats", "knowledge_traverse"}:
        _require(agent, "admin")


async def agents_authorize(request: Request) -> None:
    path = request.url.path.rstrip("/")
    method = request.method.upper()
    if path == "/v1/agents/register" and method == "POST":
        _require(_request_agent(request), "admin")
    elif path.startswith("/v1/agents/") and method == "DELETE":
        _require(_request_agent(request), "admin")


async def dashboard_authorize(request: Request) -> None:
    path = request.url.path.rstrip("/") or "/"
    if path == "/dashboard":
        return
    if path.startswith("/api/"):
        _require(_request_agent(request), "admin")


async def knowledge_authorize(request: Request) -> None:
    """Fail closed while the current knowledge graph remains global."""
    _require(_request_agent(request), "admin")
