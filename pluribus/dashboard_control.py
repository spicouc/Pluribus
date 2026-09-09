"""D3-B — Safe control endpoints for the Pluribus dashboard (directive ASSIGN).

The dashboard never creates a second task system: ASSIGN reuses the
EXISTING Directives control plane as the authoritative source. The
mutation endpoint shares the single authoritative service
``create_directive_for_actor`` with ``POST /v1/directives`` — it does
NOT duplicate business logic, does NOT write Memory facts, and does NOT
touch work_state / current_task / current_project / current_blocker /
presence. The created directive stays ``status='pending'`` and the
target agent claims it later (pending != current_task).

Security model:
  - POST /assign is guarded by ``dashboard_control_authorize``
    (read+write via the existing ``_require`` semantics; cookie OR
    X-API-Key, fresh agent row at request time, no implicit admin).
  - Auth-source binding: a VALID session cookie selects the cookie
    identity (``request.state.auth_method = "cookie"``); the X-API-Key
    is consulted ONLY when no valid cookie is present
    (``auth_method = "api_key"``).
  - Browser (cookie) mutations additionally require a same-origin
    ``Origin`` header (CSRF defence). The Origin exemption applies ONLY
    to VALIDATED API-key auth (``auth_method == "api_key"``) — never to
    the mere presence of an X-API-Key header, so a bogus key next to a
    valid cookie cannot convert cookie auth into an API-key exemption.
  - POST /assign requires an ``application/json`` content type; the
    check runs as a route dependency BEFORE body parsing so a non-JSON
    body answers 415 instead of a misleading 422.
  - GET /options is read-only and guarded by ``dashboard_session_authorize``;
    it returns an advisory capability/target model for the UI only.
    The server re-validates everything at mutation time.

The idempotency key is REQUIRED on dashboard assignments so an
operator double-click or a network retry can never create duplicates.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import field_validator

from pluribus.dashboard_session import (
    dashboard_control_authorize,
    dashboard_session_authorize,
)
from pluribus.db import get_db
from pluribus.directives import (
    DirectiveCreateRequest,
    DirectiveResponse,
    create_directive_for_actor,
)
from pluribus.validation import validate_identifier

router = APIRouter(prefix="/v1/dashboard/control", tags=["dashboard-control"])


class DashboardAssignRequest(DirectiveCreateRequest):
    """Directive creation body from the dashboard.

    ``idempotency_key`` is REQUIRED here (it overrides the parent's
    optional field). The validator is re-registered on the subclass so
    the format check is guaranteed to apply to the override in
    pydantic v2.
    """

    idempotency_key: str

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency(cls, value: str) -> str:
        return validate_identifier(value, "idempotency_key")


def _scope_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return [item for item in parsed if isinstance(item, str)] if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _require_json_content_type(request: Request) -> None:
    """Reject non-JSON bodies BEFORE FastAPI parses them (415, not 422).

    Runs as a route dependency: FastAPI resolves route dependencies
    before body reading/validation, so a ``text/plain`` body that is not
    valid JSON answers the documented 415 contract.
    """
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" not in content_type:
        raise HTTPException(status_code=415, detail="Content-Type ha de ser application/json")


def _assert_browser_origin(request: Request) -> None:
    """CSRF/origin protection for browser (cookie) mutations.

    Auth-source precedence (D3-B): a VALID session cookie selects the
    cookie identity and ``request.state.auth_method == "cookie"``; the
    X-API-Key is consulted only when no valid cookie is present
    (``auth_method == "api_key"``).

    The Origin exemption applies ONLY to validated API-key auth. A bogus
    X-API-Key header next to a valid cookie NEVER converts cookie auth
    into an API-key exemption — cookie requests always demand a
    same-origin ``Origin`` header (scheme AND netloc must match
    ``request.base_url``).
    """
    if getattr(request.state, "auth_method", None) == "api_key":
        return
    origin = request.headers.get("Origin")
    if not origin:
        raise HTTPException(status_code=403, detail="Origin requerit per a peticions de navegador")
    try:
        parsed_origin = urlsplit(origin)
        parsed_base = urlsplit(str(request.base_url))
    except ValueError:
        raise HTTPException(status_code=403, detail="Origin no permès")
    if parsed_origin.scheme != parsed_base.scheme or parsed_origin.netloc != parsed_base.netloc:
        raise HTTPException(status_code=403, detail="Origin no permès")


@router.post("/assign", status_code=201, response_model=DirectiveResponse)
async def dashboard_assign(
    request: Request,
    body: DashboardAssignRequest,
    agent: dict[str, Any] = Depends(dashboard_control_authorize),
    _content_type: None = Depends(_require_json_content_type),
) -> DirectiveResponse:
    """Assign a directive from the dashboard (safe, idempotent).

    The service function re-validates EVERYTHING at mutation time
    (target exists/active → 404, actor scope → 403, target scope → 403,
    issuer delegation grant → 403, target execution grant → 403,
    idempotency replay → 200/409, audit CREATE). The /options payload
    the UI renders is purely advisory.
    """
    _assert_browser_origin(request)
    return await create_directive_for_actor(agent, body)


@router.get("/options")
async def dashboard_assign_options(
    agent: dict[str, Any] = Depends(dashboard_session_authorize),
) -> dict[str, Any]:
    """Advisory read model for the assign form (no secrets, no alien perms).

    - is_admin / can_assign: derived from the caller's own permissions.
    - allowed_scopes: the caller's own scopes.
    - capabilities: capabilities the caller may delegate
      (admin → all known capabilities; otherwise only the caller's
      can_delegate grants).
    - targets: active agents sharing >=1 scope with the caller
      (admin → all active agents), each with the capabilities they can
      execute. The actor itself is excluded (self-assign is not a
      dashboard action).
    """
    permissions = agent.get("permissions", {}) or {}
    is_admin = bool(permissions.get("admin", False))
    can_assign = is_admin or bool(permissions.get("write", False))
    allowed_scopes = list(agent.get("allowed_scopes", []) or [])

    async with get_db() as db:
        if is_admin:
            cursor = await db.execute(
                "SELECT DISTINCT capability FROM directive_grants ORDER BY capability"
            )
        else:
            cursor = await db.execute(
                """SELECT capability FROM directive_grants
                   WHERE agent_id = ? AND can_delegate = 1
                   ORDER BY capability""",
                (agent["id"],),
            )
        capability_rows = await cursor.fetchall()
        cursor = await db.execute(
            """SELECT id, name, allowed_scopes FROM agents
               WHERE is_active = 1 ORDER BY name"""
        )
        agent_rows = await cursor.fetchall()
        cursor = await db.execute(
            """SELECT agent_id, capability FROM directive_grants
               WHERE can_execute = 1 ORDER BY capability"""
        )
        exec_rows = await cursor.fetchall()

    capabilities = [row["capability"] for row in capability_rows]
    execute_by_agent: dict[str, list[str]] = {}
    for row in exec_rows:
        execute_by_agent.setdefault(row["agent_id"], []).append(row["capability"])

    actor_scopes = set(allowed_scopes)
    targets: list[dict[str, Any]] = []
    for row in agent_rows:
        if row["id"] == agent["id"]:
            continue
        if not is_admin:
            target_scopes = set(_scope_list(row["allowed_scopes"]))
            if not (actor_scopes & target_scopes):
                continue
        targets.append(
            {
                "agent_id": row["id"],
                "name": row["name"],
                "capabilities": list(execute_by_agent.get(row["id"], [])),
            }
        )

    return {
        "actor_id": agent["id"],
        "actor_name": agent.get("name"),
        "can_assign": can_assign,
        "allowed_scopes": allowed_scopes,
        "capabilities": capabilities,
        "targets": targets,
    }
