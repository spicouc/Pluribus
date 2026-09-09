"""Safe agent-to-agent directive control plane for Pluribus.

Facts remain passive memory. Directives are explicit, structured work requests
with delegation/execution grants, scope checks, atomic claim leases and audit.
Pluribus coordinates directives but never executes shell/code itself.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from pluribus.audit import log_audit
from pluribus.db import get_db
from pluribus.validation import (
    validate_identifier,
    validate_metadata,
    validate_scope,
)

router = APIRouter(prefix="/v1/directives", tags=["directives"])


class DirectiveCreateRequest(BaseModel):
    target_agent_id: str
    scope: str = "shared"
    action: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    required_capability: str
    ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    idempotency_key: str | None = None

    @field_validator("target_agent_id")
    @classmethod
    def validate_target(cls, value: str) -> str:
        return validate_identifier(value, "target_agent_id")

    _scope = field_validator("scope")(validate_scope)

    @field_validator("action")
    @classmethod
    def validate_action(cls, value: str) -> str:
        return validate_identifier(value, "action")

    @field_validator("required_capability")
    @classmethod
    def validate_capability(cls, value: str) -> str:
        return validate_identifier(value, "required_capability")

    @field_validator("arguments")
    @classmethod
    def validate_arguments(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_metadata(value) or {}

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_identifier(value, "idempotency_key")


class DirectiveGrantRequest(BaseModel):
    can_execute: bool = False
    can_delegate: bool = False


class DirectiveClaimRequest(BaseModel):
    lease_seconds: int = Field(default=300, ge=30, le=1800)


class DirectiveCompleteRequest(BaseModel):
    result: dict[str, Any] = Field(default_factory=dict)

    @field_validator("result")
    @classmethod
    def validate_result(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_metadata(value) or {}


class DirectiveFailRequest(BaseModel):
    error: str = Field(min_length=1, max_length=4096)


class DirectiveRejectRequest(BaseModel):
    reason: str = Field(default="rejected", min_length=1, max_length=4096)


class DirectiveResponse(BaseModel):
    id: str
    issuer_agent_id: str
    target_agent_id: str
    scope: str
    action: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    required_capability: str
    status: str
    idempotency_key: str | None = None
    created_at: str
    expires_at: str
    claimed_at: str | None = None
    claimed_by_agent_id: str | None = None
    lease_until: str | None = None
    completed_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


class DirectiveGrantResponse(BaseModel):
    agent_id: str
    capability: str
    can_execute: bool
    can_delegate: bool
    updated_at: str


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return [item for item in parsed if isinstance(item, str)] if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _caller(request: Request) -> dict[str, Any]:
    agent = getattr(request.state, "agent", None)
    if not agent or not agent.get("id"):
        raise HTTPException(status_code=401, detail="Autenticació requerida")
    return agent


def _permissions(agent: dict[str, Any]) -> dict[str, Any]:
    return _json_dict(agent.get("permissions", {}))


def _is_admin(agent: dict[str, Any]) -> bool:
    return bool(_permissions(agent).get("admin", False))


def _assert_scope(agent: dict[str, Any], scope: str) -> None:
    if _is_admin(agent):
        return
    if scope not in _json_list(agent.get("allowed_scopes", [])):
        raise HTTPException(status_code=403, detail=f"Àmbit '{scope}' no permès per a aquest agent")


def _now_sql() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _future_sql(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


def _ttl_times(seconds: int) -> tuple[str, str]:
    """Return (created_at, expires_at) from the SAME server clock.

    Both are stored as ``%Y-%m-%d %H:%M:%S`` UTC text so that
    ``expires_at - created_at == ttl_seconds`` EXACTLY for new rows.
    Exactness is what makes the effective TTL a deterministic part of
    the idempotency signature (D3-B corrective).
    """
    now = datetime.now(timezone.utc)
    created_at = now.strftime("%Y-%m-%d %H:%M:%S")
    expires_at = (now + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")
    return created_at, expires_at


def _row_ttl_seconds(row: Any) -> int | None:
    """Deterministic effective TTL (seconds) of a stored directive row.

    Computed as ``expires_at - created_at`` on the stored UTC text
    timestamps. Rows written by the current code are exact; unparseable
    legacy timestamps yield None (never equal to any requested TTL).
    """
    try:
        created = datetime.strptime(str(row["created_at"]), "%Y-%m-%d %H:%M:%S")
        expires = datetime.strptime(str(row["expires_at"]), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return int(round((expires - created).total_seconds()))


def _same_directive_semantics(
    row: Any,
    body: DirectiveCreateRequest,
    arguments_json: str,
) -> bool:
    """Semantic equality between a stored row and a request payload.

    Compares target/scope/action/required_capability/arguments AND the
    effective TTL (expires_at - created_at), so a retry that asks for a
    different ttl_seconds is a CONFLICT, never a silent replay (D3-B).
    """
    ttl_original = _row_ttl_seconds(row)
    return (
        ttl_original is not None
        and ttl_original == body.ttl_seconds
        and row["target_agent_id"] == body.target_agent_id
        and row["scope"] == body.scope
        and row["action"] == body.action
        and row["required_capability"] == body.required_capability
        and row["arguments"] == arguments_json
    )


def _row_to_response(row: Any) -> DirectiveResponse:
    data = dict(row)
    raw_result = data.get("result")
    return DirectiveResponse(
        id=data["id"],
        issuer_agent_id=data["issuer_agent_id"],
        target_agent_id=data["target_agent_id"],
        scope=data["scope"],
        action=data["action"],
        arguments=_json_dict(data.get("arguments")),
        required_capability=data["required_capability"],
        status=data["status"],
        idempotency_key=data.get("idempotency_key"),
        created_at=data["created_at"],
        expires_at=data["expires_at"],
        claimed_at=data.get("claimed_at"),
        claimed_by_agent_id=data.get("claimed_by_agent_id"),
        lease_until=data.get("lease_until"),
        completed_at=data.get("completed_at"),
        result=_json_dict(raw_result) if raw_result is not None else None,
        error=data.get("error"),
    )


async def _agent_record(agent_id: str) -> dict[str, Any]:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, permissions, allowed_scopes, is_active FROM agents WHERE id = ?",
            (agent_id,),
        )
        row = await cursor.fetchone()
    if not row or not row["is_active"]:
        raise HTTPException(status_code=404, detail="Agent destinatari no trobat o inactiu")
    return dict(row)


async def _grant(agent_id: str, capability: str) -> dict[str, Any] | None:
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT agent_id, capability, can_execute, can_delegate, updated_at
               FROM directive_grants WHERE agent_id = ? AND capability = ?""",
            (agent_id, capability),
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


async def _cleanup_queue(target_agent_id: str) -> None:
    async with get_db() as db:
        await db.execute(
            """UPDATE directives
               SET status = 'expired', completed_at = datetime('now'),
                   error = COALESCE(error, 'directive expired')
               WHERE target_agent_id = ?
                 AND status IN ('pending','claimed')
                 AND expires_at <= datetime('now')""",
            (target_agent_id,),
        )
        await db.execute(
            """UPDATE directives
               SET status = 'pending', claimed_at = NULL,
                   claimed_by_agent_id = NULL, lease_until = NULL
               WHERE target_agent_id = ?
                 AND status = 'claimed'
                 AND lease_until IS NOT NULL
                 AND lease_until <= datetime('now')
                 AND expires_at > datetime('now')""",
            (target_agent_id,),
        )
        await db.commit()


async def _fetch_directive(directive_id: str) -> Any:
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM directives WHERE id = ?", (directive_id,))
        row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Directiva no trobada")
    return row


@router.put("/grants/{agent_id}/{capability}", response_model=DirectiveGrantResponse)
async def set_directive_grant(
    request: Request,
    agent_id: str,
    capability: str,
    body: DirectiveGrantRequest,
) -> DirectiveGrantResponse:
    caller = _caller(request)
    if not _is_admin(caller):
        raise HTTPException(status_code=403, detail="Només un admin pot modificar grants")
    agent_id = validate_identifier(agent_id, "agent_id")
    capability = validate_identifier(capability, "capability")
    await _agent_record(agent_id)

    async with get_db() as db:
        await db.execute(
            """INSERT INTO directive_grants(agent_id, capability, can_execute, can_delegate)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(agent_id, capability) DO UPDATE SET
                   can_execute = excluded.can_execute,
                   can_delegate = excluded.can_delegate,
                   updated_at = datetime('now')""",
            (agent_id, capability, int(body.can_execute), int(body.can_delegate)),
        )
        await log_audit(
            db,
            caller["id"],
            "UPDATE",
            "directive_grant",
            resource_id=f"{agent_id}:{capability}",
            payload=json.dumps(body.model_dump()),
        )
        await db.commit()
        cursor = await db.execute(
            "SELECT * FROM directive_grants WHERE agent_id = ? AND capability = ?",
            (agent_id, capability),
        )
        row = await cursor.fetchone()

    return DirectiveGrantResponse(
        agent_id=row["agent_id"],
        capability=row["capability"],
        can_execute=bool(row["can_execute"]),
        can_delegate=bool(row["can_delegate"]),
        updated_at=row["updated_at"],
    )


@router.get("/grants/{agent_id}", response_model=list[DirectiveGrantResponse])
async def list_directive_grants(request: Request, agent_id: str) -> list[DirectiveGrantResponse]:
    caller = _caller(request)
    agent_id = validate_identifier(agent_id, "agent_id")
    if caller["id"] != agent_id and not _is_admin(caller):
        raise HTTPException(status_code=403, detail="Només pots consultar els teus grants")
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM directive_grants WHERE agent_id = ? ORDER BY capability",
            (agent_id,),
        )
        rows = await cursor.fetchall()
    return [
        DirectiveGrantResponse(
            agent_id=row["agent_id"],
            capability=row["capability"],
            can_execute=bool(row["can_execute"]),
            can_delegate=bool(row["can_delegate"]),
            updated_at=row["updated_at"],
        )
        for row in rows
    ]


async def create_directive_for_actor(
    caller: dict[str, Any],
    body: DirectiveCreateRequest,
) -> DirectiveResponse:
    """Servei compartit i autoritatiu de creació de directives.

    Usat tant per POST /v1/directives (agent→agent) com per l'endpoint
    D3-B de control del dashboard (POST /v1/dashboard/control/assign),
    de manera que TOTA la lògica de negoci viu en un sol lloc:
    checks de scope de l'emissor, existència/activitat del destinatari,
    grants de delegació i d'execució, replay d'idempotència i audit.
    """
    _assert_scope(caller, body.scope)
    target = await _agent_record(body.target_agent_id)
    if not _json_dict(target.get("permissions")).get("admin", False):
        if body.scope not in _json_list(target.get("allowed_scopes")):
            raise HTTPException(status_code=403, detail="El destinatari no té accés a aquest scope")

    if not _is_admin(caller):
        issuer_grant = await _grant(caller["id"], body.required_capability)
        if not issuer_grant or not issuer_grant.get("can_delegate"):
            raise HTTPException(
                status_code=403,
                detail="L'emissor no té delegation grant per aquesta capability",
            )

    target_grant = await _grant(body.target_agent_id, body.required_capability)
    if not target_grant or not target_grant.get("can_execute"):
        raise HTTPException(
            status_code=403,
            detail="El destinatari no té execution grant per aquesta capability",
        )

    arguments_json = json.dumps(body.arguments, sort_keys=True, separators=(",", ":"))

    if body.idempotency_key:
        async with get_db() as db:
            cursor = await db.execute(
                """SELECT * FROM directives
                  WHERE issuer_agent_id = ? AND idempotency_key = ?""",
                (caller["id"], body.idempotency_key),
            )
            existing = await cursor.fetchone()
        if existing:
            if not _same_directive_semantics(existing, body, arguments_json):
                raise HTTPException(status_code=409, detail="idempotency_key reutilitzada amb una directiva diferent")
            return _row_to_response(existing)

    created_at, expires_at = _ttl_times(body.ttl_seconds)
    try:
        async with get_db() as db:
            cursor = await db.execute(
                """INSERT INTO directives(
                      issuer_agent_id, target_agent_id, scope, action, arguments,
                      required_capability, idempotency_key, created_at, expires_at
                  ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    caller["id"],
                    body.target_agent_id,
                    body.scope,
                    body.action,
                    arguments_json,
                    body.required_capability,
                    body.idempotency_key,
                    created_at,
                    expires_at,
                ),
            )
            rowid = cursor.lastrowid
            cursor = await db.execute("SELECT * FROM directives WHERE rowid = ?", (rowid,))
            row = await cursor.fetchone()
            await log_audit(
                db,
                caller["id"],
                "CREATE",
                "directive",
                resource_id=row["id"],
                payload=json.dumps(
                    {
                        "target_agent_id": body.target_agent_id,
                        "scope": body.scope,
                        "action": body.action,
                        "required_capability": body.required_capability,
                    }
                ),
            )
            await db.commit()
    except sqlite3.IntegrityError:
        # A real concurrent race on idx_directives_idempotency
        # (issuer_agent_id, idempotency_key): the fast-path SELECT above
        # was empty for BOTH requests, so both tried to INSERT and the
        # loser hit the partial UNIQUE index. The failed INSERT aborted
        # this connection's transaction and the context manager closed
        # the connection, so the re-check below runs on a FRESH, clean
        # connection that sees the winner's committed row.
        if not body.idempotency_key:
            # Partial UNIQUE index never matches NULL keys: this error
            # cannot be an idempotency race — surface the real failure.
            raise
        async with get_db() as db:
            cursor = await db.execute(
                """SELECT * FROM directives
                  WHERE issuer_agent_id = ? AND idempotency_key = ?""",
                (caller["id"], body.idempotency_key),
            )
            existing = await cursor.fetchone()
        if existing is None:
            # No row exists for this issuer+key → the IntegrityError did
            # NOT come from the idempotency race. Re-raise the original
            # error instead of hiding a genuine failure.
            raise
        if _same_directive_semantics(existing, body, arguments_json):
            # Atomic replay: the concurrent identical request won.
            return _row_to_response(existing)
        raise HTTPException(status_code=409, detail="idempotency_key reutilitzada amb una directiva diferent")
    return _row_to_response(row)


@router.post("", status_code=201, response_model=DirectiveResponse)
async def create_directive(request: Request, body: DirectiveCreateRequest) -> DirectiveResponse:
    """Endpoint REST original — comportament idèntic (backward-compatible)."""
    return await create_directive_for_actor(_caller(request), body)


@router.get("/inbox", response_model=list[DirectiveResponse])
async def directive_inbox(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[DirectiveResponse]:
    caller = _caller(request)
    await _cleanup_queue(caller["id"])
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT * FROM directives
               WHERE target_agent_id = ? AND status = 'pending'
                 AND expires_at > datetime('now')
               ORDER BY created_at ASC LIMIT ?""",
            (caller["id"], limit),
        )
        rows = await cursor.fetchall()
    return [_row_to_response(row) for row in rows]


@router.post("/{directive_id}/claim", response_model=DirectiveResponse)
async def claim_directive(
    request: Request,
    directive_id: str,
    body: DirectiveClaimRequest,
) -> DirectiveResponse:
    caller = _caller(request)
    directive_id = validate_identifier(directive_id, "directive_id")
    await _cleanup_queue(caller["id"])
    existing = await _fetch_directive(directive_id)
    if existing["target_agent_id"] != caller["id"]:
        raise HTTPException(status_code=403, detail="Només el destinatari pot reclamar la directiva")
    grant = await _grant(caller["id"], existing["required_capability"])
    if not grant or not grant.get("can_execute"):
        raise HTTPException(status_code=403, detail="Execution grant revocada")

    lease_until = _future_sql(body.lease_seconds)
    async with get_db() as db:
        cursor = await db.execute(
            """UPDATE directives
               SET status = 'claimed', claimed_at = datetime('now'),
                   claimed_by_agent_id = ?, lease_until = ?
               WHERE id = ? AND target_agent_id = ? AND status = 'pending'
                 AND expires_at > datetime('now')""",
            (caller["id"], lease_until, directive_id, caller["id"]),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            raise HTTPException(status_code=409, detail="Directiva no disponible per claim")
        await log_audit(db, caller["id"], "CLAIM", "directive", resource_id=directive_id)
        await db.commit()
        cursor = await db.execute("SELECT * FROM directives WHERE id = ?", (directive_id,))
        row = await cursor.fetchone()
    return _row_to_response(row)


async def _claimed_by_caller(directive_id: str, caller: dict[str, Any]) -> Any:
    row = await _fetch_directive(directive_id)
    if row["target_agent_id"] != caller["id"]:
        raise HTTPException(status_code=403, detail="Només el destinatari pot completar la directiva")
    if row["status"] != "claimed" or row["claimed_by_agent_id"] != caller["id"]:
        raise HTTPException(status_code=409, detail="La directiva no està reclamada per aquest agent")
    if not row["lease_until"] or row["lease_until"] <= _now_sql():
        await _cleanup_queue(caller["id"])
        raise HTTPException(status_code=409, detail="Lease caducada")
    return row


@router.post("/{directive_id}/complete", response_model=DirectiveResponse)
async def complete_directive(
    request: Request,
    directive_id: str,
    body: DirectiveCompleteRequest,
) -> DirectiveResponse:
    caller = _caller(request)
    directive_id = validate_identifier(directive_id, "directive_id")
    await _claimed_by_caller(directive_id, caller)
    async with get_db() as db:
        await db.execute(
            """UPDATE directives SET status = 'completed', completed_at = datetime('now'),
                   result = ?, error = NULL
               WHERE id = ? AND status = 'claimed' AND claimed_by_agent_id = ?""",
            (json.dumps(body.result), directive_id, caller["id"]),
        )
        await log_audit(db, caller["id"], "COMPLETE", "directive", resource_id=directive_id)
        await db.commit()
        cursor = await db.execute("SELECT * FROM directives WHERE id = ?", (directive_id,))
        row = await cursor.fetchone()
    return _row_to_response(row)


@router.post("/{directive_id}/fail", response_model=DirectiveResponse)
async def fail_directive(
    request: Request,
    directive_id: str,
    body: DirectiveFailRequest,
) -> DirectiveResponse:
    caller = _caller(request)
    directive_id = validate_identifier(directive_id, "directive_id")
    await _claimed_by_caller(directive_id, caller)
    async with get_db() as db:
        await db.execute(
            """UPDATE directives SET status = 'failed', completed_at = datetime('now'),
                   error = ?
               WHERE id = ? AND status = 'claimed' AND claimed_by_agent_id = ?""",
            (body.error, directive_id, caller["id"]),
        )
        await log_audit(db, caller["id"], "FAIL", "directive", resource_id=directive_id)
        await db.commit()
        cursor = await db.execute("SELECT * FROM directives WHERE id = ?", (directive_id,))
        row = await cursor.fetchone()
    return _row_to_response(row)


@router.post("/{directive_id}/reject", response_model=DirectiveResponse)
async def reject_directive(
    request: Request,
    directive_id: str,
    body: DirectiveRejectRequest,
) -> DirectiveResponse:
    caller = _caller(request)
    directive_id = validate_identifier(directive_id, "directive_id")
    await _cleanup_queue(caller["id"])
    row = await _fetch_directive(directive_id)
    if row["target_agent_id"] != caller["id"]:
        raise HTTPException(status_code=403, detail="Només el destinatari pot rebutjar la directiva")
    if row["status"] not in {"pending", "claimed"}:
        raise HTTPException(status_code=409, detail="Directiva ja finalitzada")
    async with get_db() as db:
        await db.execute(
            """UPDATE directives SET status = 'rejected', completed_at = datetime('now'),
                   error = ? WHERE id = ? AND target_agent_id = ?
                   AND status IN ('pending','claimed')""",
            (body.reason, directive_id, caller["id"]),
        )
        await log_audit(db, caller["id"], "REJECT", "directive", resource_id=directive_id)
        await db.commit()
        cursor = await db.execute("SELECT * FROM directives WHERE id = ?", (directive_id,))
        updated = await cursor.fetchone()
    return _row_to_response(updated)


@router.get("/{directive_id}", response_model=DirectiveResponse)
async def get_directive(request: Request, directive_id: str) -> DirectiveResponse:
    caller = _caller(request)
    directive_id = validate_identifier(directive_id, "directive_id")
    row = await _fetch_directive(directive_id)
    if not _is_admin(caller) and caller["id"] not in {row["issuer_agent_id"], row["target_agent_id"]}:
        raise HTTPException(status_code=403, detail="No tens accés a aquesta directiva")
    return _row_to_response(row)
