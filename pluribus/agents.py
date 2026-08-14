"""Router d'agents: registre, heartbeat, estat."""

from __future__ import annotations

import json
import uuid
from typing import Any

import bcrypt
from fastapi import APIRouter, HTTPException, Request

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


@router.get("", response_model=list[AgentResponse])
async def list_agents(request: Request) -> list[AgentResponse]:
    agent: dict[str, Any] = request.state.agent
    if not agent.get("permissions", {}).get("read", False):
        raise HTTPException(status_code=403, detail="Sense permís per llistar agents")

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
    agent: dict[str, Any] = request.state.agent
    if not agent.get("permissions", {}).get("read", False):
        raise HTTPException(status_code=403, detail="Sense permís")

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
    agent: dict[str, Any] = request.state.agent
    if agent["id"] != agent_id:
        raise HTTPException(status_code=403, detail="Només pots fer heartbeat del teu propi agent")

    client_host = request.client.host if request.client else "unknown"
    async with get_db() as db:
        await db.execute(
            "UPDATE agents SET last_active_at = datetime('now'), last_ip = ? WHERE id = ? AND is_active = 1",
            (client_host, agent_id),
        )
        await db.commit()


@router.put("/{agent_id}", response_model=AgentResponse)
async def update_agent(request: Request, agent_id: str, body: AgentUpdateRequest) -> AgentResponse:
    agent: dict[str, Any] = request.state.agent
    if agent["id"] != agent_id and not agent.get("permissions", {}).get("admin", False):
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
        if not agent.get("permissions", {}).get("admin", False):
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
    if not caller.get("permissions", {}).get("admin", False):
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
    """Elimina un agent sense violar foreign keys ni perdre els seus fets."""
    agent: dict[str, Any] = request.state.agent
    if not agent.get("permissions", {}).get("admin", False):
        raise HTTPException(status_code=403, detail="Es requereixen permisos admin per eliminar agents")

    async with get_db() as db:
        cursor = await db.execute("SELECT id FROM agents WHERE id = ?", (agent_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Agent no trobat")
        if row["id"] == agent.get("id"):
            raise HTTPException(status_code=400, detail="No pots eliminar el teu propi agent")

        await db.execute("UPDATE facts SET agent_id = NULL WHERE agent_id = ?", (agent_id,))
        await db.execute("UPDATE audit_log SET agent_id = NULL WHERE agent_id = ?", (agent_id,))
        await db.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        await db.commit()
