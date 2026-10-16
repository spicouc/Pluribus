"""Router d'agents: registre, heartbeat, estat."""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

import bcrypt
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from pluribus.agent_telemetry import (
    normalize_work_state,
    validate_heartbeat_field,
)
from pluribus.api_keys import fingerprint_api_key, generate_api_key
from pluribus.db import get_db
from pluribus.models import (
    AgentRegisterRequest,
    AgentRegisterResponse,
    AgentResponse,
    AgentUpdateRequest,
)

router = APIRouter(prefix="/v1/agents", tags=["agents"])


def _agent_to_response(row: dict[str, Any], fact_count: int = 0) -> AgentResponse:
    """Converteix una fila d'agent a AgentResponse."""
    try:
        perms = json.loads(row["permissions"]) if isinstance(row["permissions"], str) else row["permissions"]
    except (json.JSONDecodeError, TypeError):
        perms = {"read": False, "write": False, "delete": False, "admin": False}
    try:
        scopes = json.loads(row["allowed_scopes"]) if isinstance(row["allowed_scopes"], str) else row["allowed_scopes"]
    except (json.JSONDecodeError, TypeError):
        scopes = []
    try:
        caps = json.loads(row["capabilities"]) if isinstance(row["capabilities"], str) else {}
    except (json.JSONDecodeError, TypeError):
        caps = {}
    try:
        meta = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else {}
    except (json.JSONDecodeError, TypeError):
        meta = {}
    return AgentResponse(
        id=row["id"],
        name=row["name"],
        permissions=perms,
        allowed_scopes=scopes,
        capabilities=caps,
        metadata=meta,
        last_active_at=row.get("last_active_at"),
        last_ip=row.get("last_ip"),
        is_active=bool(row.get("is_active", 1)),
        created_at=row["created_at"],
        fact_count=fact_count,
    )


def _is_admin(agent: dict[str, Any]) -> bool:
    return bool((agent.get("permissions") or {}).get("admin", False))


@router.get("", response_model=list[AgentResponse])
async def list_agents(request: Request) -> list[AgentResponse]:
    """Retorna l'inventari global només a administradors."""
    agent: dict[str, Any] = request.state.agent
    if not _is_admin(agent):
        raise HTTPException(
            status_code=403,
            detail="L'inventari global d'agents requereix permís admin",
        )

    async with get_db() as db:
        cursor = await db.execute("""
            SELECT a.*, COUNT(f.id) as fact_count
            FROM agents a
            LEFT JOIN facts f ON f.agent_id = a.id AND f.deleted_at IS NULL
            GROUP BY a.id
            ORDER BY a.last_active_at DESC NULLS LAST
        """)
        rows = await cursor.fetchall()
        return [_agent_to_response(dict(r), r["fact_count"]) for r in rows]


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(request: Request, agent_id: str) -> AgentResponse:
    """Permet a un agent consultar-se a si mateix; admin pot consultar qualsevol agent."""
    agent: dict[str, Any] = request.state.agent
    if agent.get("id") != agent_id and not _is_admin(agent):
        raise HTTPException(
            status_code=403,
            detail="Només pots consultar el teu propi agent",
        )

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT a.*, (SELECT COUNT(*) FROM facts WHERE agent_id = a.id AND deleted_at IS NULL) as fact_count FROM agents a WHERE a.id = ?",
            (agent_id,),
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Agent no trobat")
        return _agent_to_response(dict(row), row["fact_count"])


@router.patch("/{agent_id}/heartbeat", status_code=204)
async def agent_heartbeat(request: Request, agent_id: str) -> None:
    """Update the agent's current-state telemetry.

    Self-only: the authenticated agent.id must equal path agent_id,
    otherwise 403. The server always updates last_active_at with
    SERVER time (datetime('now')) — never trust a client timestamp
    for presence classification.

    All body fields are optional. If a field is omitted, the
    existing value is preserved. If a field is explicitly null OR
    an empty string, the value is cleared (set to NULL).

    Heartbeat does NOT create a fact in Memory. Telemetry is
    ephemeral; authoritative history lives in Directives.
    """
    agent: dict[str, Any] = request.state.agent
    if agent["id"] != agent_id:
        raise HTTPException(status_code=403, detail="Només pots fer heartbeat del teu propi agent")

    body: dict[str, Any] = {}
    try:
        if request.headers.get("content-type", "").startswith("application/json"):
            raw = await request.body()
            if not raw:
                # Empty body is allowed (heartbeat with no payload).
                body = {}
            else:
                import json as _json
                try:
                    body = _json.loads(raw.decode("utf-8"))
                except (ValueError, _json.JSONDecodeError) as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Malformed JSON body: {exc}",
                    )
                if not isinstance(body, dict):
                    raise HTTPException(
                        status_code=422,
                        detail="body must be a JSON object",
                    )
    except HTTPException:
        raise
    except Exception:
        # Genuine error parsing the request — also reject, do not
        # silently swallow.
        raise HTTPException(status_code=400, detail="Could not parse request body")

    work_state_raw = body.get("work_state", "__OMIT__")
    if work_state_raw == "__OMIT__":
        ws_value: Optional[str] = None  # skip
        ws_set = False
    else:
        ws_value = normalize_work_state(work_state_raw)
        if ws_value is None and work_state_raw is not None and work_state_raw != "":
            raise HTTPException(
                status_code=422,
                detail="work_state invalid; allowed: IDLE, WORKING, BLOCKED, WAITING, ERROR, UNKNOWN",
            )
        # empty string or null -> clear to UNKNOWN (set to NULL handled
        # by update). Omit -> skip (preserves prior).
        ws_set = True

    # current_blocker requires a separate "reported" flag so the
    # dashboard can distinguish "agent never reported" (UNKNOWN)
    # from "agent explicitly reported no blocker" (NONE).
    cb_raw = body.get("current_blocker", "__OMIT__")
    cb_set = False
    cb_value: Optional[str] = None
    if cb_raw == "__OMIT__":
        pass  # skip — preserve both blocker and reported flag
    else:
        ok, normalized = validate_heartbeat_field("current_blocker", cb_raw)
        if not ok:
            raise HTTPException(
                status_code=422,
                detail="current_blocker invalid (type or length)",
            )
        cb_value = normalized  # None for empty string / null
        cb_set = True

    extra: dict[str, str] = {}
    for fld in ("current_task_id", "current_project"):
        raw = body.get(fld, "__OMIT__")
        if raw == "__OMIT__":
            continue  # skip -> preserve prior
        ok, normalized = validate_heartbeat_field(fld, raw)
        if not ok:
            raise HTTPException(status_code=422, detail=f"{fld} invalid (type or length)")
        extra[fld] = "" if normalized is None else normalized

    client_host = request.client.host if request.client else "unknown"
    async with get_db() as db:
        # Always update last_active_at + last_ip with SERVER time.
        updates = ["last_active_at = datetime('now')", "last_ip = ?"]
        params: list[Any] = [client_host]
        if ws_set:
            updates.append("work_state = ?")
            # Empty string and None both mean "clear" -> write NULL.
            params.append(None if ws_value is None else ws_value)
        for k, v in extra.items():
            updates.append(f"{k} = ?")
            params.append(None if v == "" else v)
        if cb_set:
            # We always set both fields together: when the field is
            # present in the request, the agent has explicitly
            # reported (regardless of value).
            updates.append("current_blocker = ?")
            params.append(cb_value)
            updates.append("current_blocker_reported = 1")
        params.append(agent_id)
        sql = (f"UPDATE agents SET {', '.join(updates)} "
               "WHERE id = ? AND is_active = 1")
        await db.execute(sql, params)
        await db.commit()


@router.put("/{agent_id}", response_model=AgentResponse)
async def update_agent(request: Request, agent_id: str, body: AgentUpdateRequest) -> AgentResponse:
    agent: dict[str, Any] = request.state.agent
    if agent["id"] != agent_id and not _is_admin(agent):
        raise HTTPException(status_code=403, detail="No tens permís per modificar aquest agent")

    updates: list[str] = []
    params: list[Any] = []
    if body.capabilities is not None:
        updates.append("capabilities = ?")
        params.append(json.dumps(body.capabilities))
    if body.metadata is not None:
        updates.append("metadata = ?")
        params.append(json.dumps(body.metadata))
    if body.is_active is not None:
        if not _is_admin(agent):
            raise HTTPException(status_code=403, detail="Només un admin pot canviar is_active")
        updates.append("is_active = ?")
        params.append(1 if body.is_active else 0)

    if not updates:
        raise HTTPException(status_code=400, detail="No s'ha proporcionat cap camp per actualitzar")

    updates.append("last_active_at = datetime('now')")
    params.append(agent_id)

    async with get_db() as db:
        cursor = await db.execute(f"UPDATE agents SET {', '.join(updates)} WHERE id = ?", params)
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Agent no trobat")
        await db.commit()
        cursor = await db.execute(
            "SELECT a.*, (SELECT COUNT(*) FROM facts WHERE agent_id = a.id AND deleted_at IS NULL) as fact_count FROM agents a WHERE a.id = ?",
            (agent_id,),
        )
        row = await cursor.fetchone()
        return _agent_to_response(dict(row), row["fact_count"])


@router.post("/register", status_code=201, response_model=AgentRegisterResponse)
async def register_agent(request: Request, body: AgentRegisterRequest) -> AgentRegisterResponse:
    """Registra un agent. Només un administrador autenticat ho pot fer."""
    caller: dict[str, Any] = request.state.agent
    if not _is_admin(caller):
        raise HTTPException(status_code=403, detail="Permís admin requerit per registrar agents")

    api_key = generate_api_key()
    api_key_hash = bcrypt.hashpw(api_key.encode(), bcrypt.gensalt()).decode()
    api_key_fingerprint = fingerprint_api_key(api_key)
    agent_id = str(uuid.uuid4())

    async with get_db() as db:
        await db.execute(
            """INSERT INTO agents
               (id, name, api_key_hash, api_key_fingerprint, permissions, allowed_scopes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                agent_id,
                body.name,
                api_key_hash,
                api_key_fingerprint,
                json.dumps(body.permissions),
                json.dumps(body.allowed_scopes),
            ),
        )
        await db.commit()

    return AgentRegisterResponse(agent_id=agent_id, name=body.name, api_key=api_key)


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(request: Request, agent_id: str) -> None:
    """Elimina un agent sense violar foreign keys ni perdre els seus fets.

    Els agents que ja formen part de l'historial de Xerrameca es conserven com
    identitat auditable: s'han de desactivar (`is_active=false`) en lloc
    d'eliminar-los.
    """
    agent: dict[str, Any] = request.state.agent
    if not _is_admin(agent):
        raise HTTPException(status_code=403, detail="Es requereixen permisos admin per eliminar agents")

    async with get_db() as db:
        cursor = await db.execute("SELECT id FROM agents WHERE id = ?", (agent_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Agent no trobat")
        if row["id"] == agent.get("id"):
            raise HTTPException(status_code=400, detail="No pots eliminar el teu propi agent")

        cursor = await db.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type = 'table' AND name = 'xerrameca_participants'"""
        )
        if await cursor.fetchone():
            cursor = await db.execute(
                """SELECT 1 FROM xerrameca_participants
                   WHERE agent_id = ? LIMIT 1""",
                (agent_id,),
            )
            if await cursor.fetchone():
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Agent amb historial Xerrameca: desactiva'l en lloc "
                        "d'eliminar-lo per preservar l'auditoria"
                    ),
                )

        await db.execute("UPDATE facts SET agent_id = NULL WHERE agent_id = ?", (agent_id,))
        await db.execute("UPDATE audit_log SET agent_id = NULL WHERE agent_id = ?", (agent_id,))
        await db.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        await db.commit()
