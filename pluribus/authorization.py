"""Central authorization guards for REST, MCP, agents and dashboard routes."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from pluribus.db import get_db


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


async def _fact_scope_category(fact_id: str) -> tuple[str, str] | None:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT scope, COALESCE(category, '') AS category FROM facts WHERE id = ? AND deleted_at IS NULL",
            (fact_id,),
        )
        row = await cursor.fetchone()
        return None if not row else (row["scope"], row["category"])


async def memory_authorize(request: Request) -> None:
    """Apply permission and scope checks before memory handlers run."""
    agent = _request_agent(request)
    path = request.url.path.rstrip("/") or "/"
    method = request.method.upper()

    if path == "/v1/memory/write" and method == "POST":
        body = await request.json()
        _require(agent, "write", body.get("scope", "shared"))
        return
    if path == "/v1/memory/query" and method == "GET":
        _require(agent, "read", request.query_params.get("scope", "shared"))
        return
    if path == "/v1/memory/search/semantic" and method == "POST":
        body = await request.json()
        _require(agent, "read", body.get("scope", "shared"))
        return
    if path == "/v1/memory/search" and method == "GET":
        _require(agent, "read", request.query_params.get("scope", "shared"))
        return
    if path == "/v1/memory/ls" and method == "GET":
        _require(agent, "read", request.query_params.get("scope", "shared"))
        return
    if path in {"/v1/memory/expire", "/v1/memory/audit"}:
        _require(agent, "admin")
        return
    if path == "/v1/memory" and method == "GET":
        if agent.get("permissions", {}).get("admin", False):
            return
        scope = request.query_params.get("scope")
        if not scope:
            raise HTTPException(status_code=400, detail="Els agents no-admin han d'indicar un scope explícit")
        _require(agent, "read", scope)
        return

    prefix = "/v1/memory/"
    if path.startswith(prefix) and method in {"PUT", "DELETE"}:
        fact_id = path[len(prefix):]
        if not fact_id or "/" in fact_id:
            return
        fact = await _fact_scope_category(fact_id)
        if fact is None:
            return
        scope, category = fact
        _require(agent, "write" if method == "PUT" else "delete", scope)
        if method == "DELETE" and category in _PROTECTED_CATEGORIES and not agent.get("permissions", {}).get("admin", False):
            raise HTTPException(status_code=403, detail="Fet de categoria persistent; requereix permís admin")


async def mcp_authorize(request: Request) -> None:
    """Authorize MCP tool calls before legacy handlers execute."""
    if request.method.upper() == "GET":
        return
    agent = _request_agent(request)
    body = await request.json()
    if body.get("method") == "tools/list":
        _require(agent, "read")
        return
    if body.get("method") != "tools/call":
        return

    params = body.get("params") or {}
    tool = params.get("name", "")
    args = params.get("arguments") or {}
    scoped = {
        "memory_write": "write",
        "memory_query": "read",
        "memory_search_semantic": "read",
        "memory_ls": "read",
    }
    if tool in scoped:
        _require(agent, scoped[tool], args.get("scope", "shared"))
        return

    if tool in {"memory_get_fact", "memory_delete"}:
        fact_id = args.get("fact_id", "")
        if not fact_id:
            return
        fact = await _fact_scope_category(fact_id)
        if fact is None:
            return
        scope, category = fact
        permission = "delete" if tool == "memory_delete" else "read"
        _require(agent, permission, scope)
        if tool == "memory_delete" and category in _PROTECTED_CATEGORIES and not agent.get("permissions", {}).get("admin", False):
            raise HTTPException(status_code=403, detail="Fet de categoria persistent; requereix permís admin")
        return

    # These legacy tools expose global non-scope-filtered state.
    if tool in {"memory_stats", "knowledge_traverse"}:
        _require(agent, "admin")


async def agents_authorize(request: Request) -> None:
    """Protect privileged agent-management operations."""
    path = request.url.path.rstrip("/")
    method = request.method.upper()
    if path == "/v1/agents/register" and method == "POST":
        _require(_request_agent(request), "admin")
    elif path.startswith("/v1/agents/") and method == "DELETE":
        _require(_request_agent(request), "admin")


async def dashboard_authorize(request: Request) -> None:
    """Keep the HTML shell public but protect all dashboard data/config APIs."""
    path = request.url.path.rstrip("/") or "/"
    if path == "/dashboard":
        return
    if path.startswith("/api/"):
        _require(_request_agent(request), "admin")
