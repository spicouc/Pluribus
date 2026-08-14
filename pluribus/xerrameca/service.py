"""Lògica transaccional del motor Agent-to-Agent Xerrameca v1."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import secrets
import uuid
from typing import Any

from fastapi import HTTPException

from pluribus.audit import log_audit
from pluribus.config import settings
from pluribus.db import get_db
from pluribus.embedding import embedding_service
from pluribus.memory import _generate_embeddings_background
from pluribus.validation import (
    validate_content,
    validate_identifier,
    validate_metadata,
    validate_scope,
)

from .models import (
    AssignTurnRequest,
    ConversationCreateRequest,
    ConversationSettingsUpdate,
    FinishRequest,
    ParticipantUpdate,
    ReplyRequest,
    ResumeRequest,
    SkipTurnRequest,
    XerramecaSystemUpdate,
)


TERMINAL_STATUSES = {"completed", "cancelled"}
RESUMABLE_STATUSES = {"paused", "blocked", "error"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _lease_until(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def _is_admin(agent: dict[str, Any]) -> bool:
    return bool((agent.get("permissions") or {}).get("admin", False))


def _require_admin(agent: dict[str, Any]) -> None:
    if not _is_admin(agent):
        raise HTTPException(status_code=403, detail="Xerrameca: permís admin requerit")


def _require_permission(agent: dict[str, Any], permission: str, scope: str) -> None:
    if _is_admin(agent):
        return
    permissions = agent.get("permissions") or {}
    if not permissions.get(permission, False):
        raise HTTPException(
            status_code=403, detail=f"Xerrameca: falta permís '{permission}'"
        )
    if scope not in (agent.get("allowed_scopes") or []):
        raise HTTPException(
            status_code=403, detail=f"Xerrameca: scope '{scope}' no permès"
        )


def _clean_identifier(value: str, field: str) -> str:
    try:
        return validate_identifier(value, field)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _clean_scope(value: str) -> str:
    try:
        return validate_scope(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _clean_content(value: str) -> str:
    try:
        return validate_content(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _clean_metadata(value: dict[str, Any]) -> dict[str, Any]:
    try:
        return validate_metadata(value) or {}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _audit(
    db: Any,
    agent_id: str,
    action: str,
    resource_id: str | None,
    payload: dict[str, Any] | None = None,
) -> None:
    await log_audit(
        db,
        agent_id,
        action,
        "xerrameca",
        resource_id=resource_id,
        payload=json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
    )


async def _runtime_row(db: Any) -> Any:
    cursor = await db.execute(
        """SELECT enabled, default_max_rounds, default_turn_timeout_seconds, updated_at
           FROM xerrameca_runtime WHERE singleton = 1"""
    )
    row = await cursor.fetchone()
    if row:
        return row
    await db.execute(
        """INSERT OR IGNORE INTO xerrameca_runtime
           (singleton, enabled, default_max_rounds, default_turn_timeout_seconds)
           VALUES (1, 1, 20, 300)"""
    )
    cursor = await db.execute(
        """SELECT enabled, default_max_rounds, default_turn_timeout_seconds, updated_at
           FROM xerrameca_runtime WHERE singleton = 1"""
    )
    return await cursor.fetchone()


async def _require_system_enabled(db: Any) -> None:
    row = await _runtime_row(db)
    if not bool(row["enabled"]):
        raise HTTPException(status_code=423, detail="Xerrameca està desactivada globalment")


async def get_system_state(agent: dict[str, Any]) -> dict[str, Any]:
    if not _is_admin(agent) and not (agent.get("permissions") or {}).get("read", False):
        raise HTTPException(status_code=403, detail="Xerrameca: falta permís 'read'")
    async with get_db() as db:
        row = await _runtime_row(db)
        return {
            "enabled": bool(row["enabled"]),
            "default_max_rounds": row["default_max_rounds"],
            "default_turn_timeout_seconds": row["default_turn_timeout_seconds"],
            "updated_at": row["updated_at"],
        }


async def update_system_state(
    agent: dict[str, Any], body: XerramecaSystemUpdate
) -> dict[str, Any]:
    _require_admin(agent)
    updates: list[str] = []
    params: list[Any] = []
    if body.enabled is not None:
        updates.append("enabled = ?")
        params.append(1 if body.enabled else 0)
    if body.default_max_rounds is not None:
        updates.append("default_max_rounds = ?")
        params.append(body.default_max_rounds)
    if body.default_turn_timeout_seconds is not None:
        updates.append("default_turn_timeout_seconds = ?")
        params.append(body.default_turn_timeout_seconds)
    if not updates:
        raise HTTPException(status_code=400, detail="No hi ha canvis")
    updates.append("updated_at = ?")
    params.append(_now())
    async with get_db() as db:
        await _runtime_row(db)
        await db.execute(
            f"UPDATE xerrameca_runtime SET {', '.join(updates)} WHERE singleton = 1",
            params,
        )
        await _audit(
            db,
            agent["id"],
            "XERRAMECA_SYSTEM_UPDATE",
            "system",
            body.model_dump(exclude_none=True),
        )
        await db.commit()
        row = await _runtime_row(db)
        return {
            "enabled": bool(row["enabled"]),
            "default_max_rounds": row["default_max_rounds"],
            "default_turn_timeout_seconds": row["default_turn_timeout_seconds"],
            "updated_at": row["updated_at"],
        }


async def _target_agent(db: Any, agent_id: str, scope: str) -> dict[str, Any]:
    agent_id = _clean_identifier(agent_id, "agent_id")
    cursor = await db.execute(
        "SELECT id, name, allowed_scopes, is_active FROM agents WHERE id = ?",
        (agent_id,),
    )
    row = await cursor.fetchone()
    if not row or not bool(row["is_active"]):
        raise HTTPException(status_code=400, detail=f"Agent '{agent_id}' no existeix o està inactiu")
    try:
        scopes = json.loads(row["allowed_scopes"]) if isinstance(row["allowed_scopes"], str) else row["allowed_scopes"]
    except (json.JSONDecodeError, TypeError):
        scopes = []
    if scope not in (scopes or []):
        raise HTTPException(
            status_code=400,
            detail=f"Agent '{agent_id}' no té accés al scope '{scope}'",
        )
    return {"id": row["id"], "name": row["name"], "allowed_scopes": scopes}


async def _conversation(db: Any, conversation_id: str) -> Any:
    conversation_id = _clean_identifier(conversation_id, "conversation_id")
    cursor = await db.execute(
        "SELECT * FROM xerrameca_conversations WHERE id = ?",
        (conversation_id,),
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Xerrameca no trobada")
    return row


async def _participants(db: Any, conversation_id: str) -> list[dict[str, Any]]:
    cursor = await db.execute(
        """SELECT p.agent_id, p.role, p.position, p.enabled, a.name, a.is_active
           FROM xerrameca_participants p
           JOIN agents a ON a.id = p.agent_id
           WHERE p.conversation_id = ?
           ORDER BY p.position""",
        (conversation_id,),
    )
    return [dict(row) for row in await cursor.fetchall()]


async def _participant_ids(db: Any, conversation_id: str, enabled_only: bool = False) -> list[str]:
    sql = "SELECT agent_id FROM xerrameca_participants WHERE conversation_id = ?"
    if enabled_only:
        sql += " AND enabled = 1"
    sql += " ORDER BY position"
    cursor = await db.execute(sql, (conversation_id,))
    return [row["agent_id"] for row in await cursor.fetchall()]


async def _can_view(db: Any, agent: dict[str, Any], conv: Any) -> None:
    _require_permission(agent, "read", conv["scope"])
    if _is_admin(agent):
        return
    cursor = await db.execute(
        """SELECT 1 FROM xerrameca_participants
           WHERE conversation_id = ? AND agent_id = ? LIMIT 1""",
        (conv["id"], agent["id"]),
    )
    if not await cursor.fetchone():
        raise HTTPException(status_code=403, detail="No ets participant d'aquesta Xerrameca")


async def _require_participant(
    db: Any, agent: dict[str, Any], conv: Any, write: bool = False
) -> None:
    _require_permission(agent, "write" if write else "read", conv["scope"])
    cursor = await db.execute(
        """SELECT enabled FROM xerrameca_participants
           WHERE conversation_id = ? AND agent_id = ?""",
        (conv["id"], agent["id"]),
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=403, detail="No ets participant d'aquesta Xerrameca")
    if not bool(row["enabled"]):
        raise HTTPException(status_code=423, detail="Participant desactivat en aquesta Xerrameca")


async def _conversation_payload(db: Any, conv: Any) -> dict[str, Any]:
    participants = await _participants(db, conv["id"])
    current_turn = None
    if conv["current_turn_id"]:
        cursor = await db.execute(
            """SELECT id, round_no, assigned_agent_id, status, claimed_by,
                      claimed_at, lease_until, created_at
               FROM xerrameca_turns WHERE id = ?""",
            (conv["current_turn_id"],),
        )
        row = await cursor.fetchone()
        if row:
            current_turn = dict(row)
    return {
        "id": conv["id"],
        "name": conv["name"],
        "objective": conv["objective"],
        "scope": conv["scope"],
        "status": conv["status"],
        "enabled": bool(conv["enabled"]),
        "turn_policy": conv["turn_policy"],
        "supervisor_agent_id": conv["supervisor_agent_id"],
        "first_agent_id": conv["first_agent_id"],
        "max_rounds": conv["max_rounds"],
        "turn_timeout_seconds": conv["turn_timeout_seconds"],
        "current_round": conv["current_round"],
        "current_turn_id": conv["current_turn_id"],
        "block_reason": conv["block_reason"],
        "persist_summary": bool(conv["persist_summary"]),
        "summary_fact_id": conv["summary_fact_id"],
        "created_by_agent_id": conv["created_by_agent_id"],
        "created_at": conv["created_at"],
        "updated_at": conv["updated_at"],
        "started_at": conv["started_at"],
        "finished_at": conv["finished_at"],
        "participants": participants,
        "current_turn": current_turn,
    }


async def create_conversation(
    agent: dict[str, Any], body: ConversationCreateRequest
) -> dict[str, Any]:
    _require_admin(agent)
    scope = _clean_scope(body.scope)
    name = body.name.strip()
    objective = _clean_content(body.objective)
    participant_ids = [_clean_identifier(v, "participant_agent_id") for v in body.participant_agent_ids]
    if len(set(participant_ids)) != 2:
        raise HTTPException(status_code=422, detail="Calen exactament 2 agents diferents")

    async with get_db() as db:
        runtime = await _runtime_row(db)
        for participant_id in participant_ids:
            await _target_agent(db, participant_id, scope)

        supervisor_id = body.supervisor_agent_id
        if body.turn_policy == "supervisor":
            supervisor_id = supervisor_id or participant_ids[0]
            supervisor_id = _clean_identifier(supervisor_id, "supervisor_agent_id")
            if supervisor_id not in participant_ids:
                raise HTTPException(status_code=422, detail="El supervisor ha de ser participant")
        elif supervisor_id is not None:
            raise HTTPException(
                status_code=422,
                detail="supervisor_agent_id només és vàlid amb turn_policy='supervisor'",
            )

        first_agent_id = body.first_agent_id or supervisor_id or participant_ids[0]
        first_agent_id = _clean_identifier(first_agent_id, "first_agent_id")
        if first_agent_id not in participant_ids:
            raise HTTPException(status_code=422, detail="first_agent_id ha de ser participant")

        conversation_id = str(uuid.uuid4())
        max_rounds = body.max_rounds or runtime["default_max_rounds"]
        timeout = body.turn_timeout_seconds or runtime["default_turn_timeout_seconds"]
        now = _now()
        await db.execute(
            """INSERT INTO xerrameca_conversations
               (id, name, objective, scope, status, enabled, turn_policy,
                supervisor_agent_id, first_agent_id, max_rounds,
                turn_timeout_seconds, persist_summary, created_by_agent_id,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, 'draft', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                conversation_id,
                name,
                objective,
                scope,
                body.turn_policy,
                supervisor_id,
                first_agent_id,
                max_rounds,
                timeout,
                1 if body.persist_summary else 0,
                agent["id"],
                now,
                now,
            ),
        )
        for position, participant_id in enumerate(participant_ids):
            role = "supervisor" if participant_id == supervisor_id else "participant"
            await db.execute(
                """INSERT INTO xerrameca_participants
                   (conversation_id, agent_id, role, position, enabled)
                   VALUES (?, ?, ?, ?, 1)""",
                (conversation_id, participant_id, role, position),
            )
        await _audit(
            db,
            agent["id"],
            "XERRAMECA_CREATE",
            conversation_id,
            {"scope": scope, "participants": participant_ids, "turn_policy": body.turn_policy},
        )
        await db.commit()
        conv = await _conversation(db, conversation_id)
        return await _conversation_payload(db, conv)


async def list_conversations(agent: dict[str, Any]) -> list[dict[str, Any]]:
    async with get_db() as db:
        if _is_admin(agent):
            cursor = await db.execute(
                "SELECT * FROM xerrameca_conversations ORDER BY created_at DESC"
            )
        else:
            if not (agent.get("permissions") or {}).get("read", False):
                raise HTTPException(status_code=403, detail="Xerrameca: falta permís 'read'")
            cursor = await db.execute(
                """SELECT c.*
                   FROM xerrameca_conversations c
                   JOIN xerrameca_participants p ON p.conversation_id = c.id
                   WHERE p.agent_id = ?
                   ORDER BY c.created_at DESC""",
                (agent["id"],),
            )
        rows = await cursor.fetchall()
        result: list[dict[str, Any]] = []
        for conv in rows:
            try:
                await _can_view(db, agent, conv)
            except HTTPException:
                continue
            result.append(await _conversation_payload(db, conv))
        return result


async def get_conversation(agent: dict[str, Any], conversation_id: str) -> dict[str, Any]:
    async with get_db() as db:
        conv = await _conversation(db, conversation_id)
        await _can_view(db, agent, conv)
        return await _conversation_payload(db, conv)


async def _create_turn(
    db: Any,
    conversation_id: str,
    round_no: int,
    assigned_agent_id: str,
    input_message_id: str,
) -> str:
    turn_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO xerrameca_turns
           (id, conversation_id, round_no, assigned_agent_id, input_message_id,
            status, created_at)
           VALUES (?, ?, ?, ?, ?, 'ready', ?)""",
        (
            turn_id,
            conversation_id,
            round_no,
            assigned_agent_id,
            input_message_id,
            _now(),
        ),
    )
    return turn_id


async def start_conversation(agent: dict[str, Any], conversation_id: str) -> dict[str, Any]:
    _require_admin(agent)
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        await _require_system_enabled(db)
        conv = await _conversation(db, conversation_id)
        if conv["status"] != "draft":
            raise HTTPException(status_code=409, detail="Només es pot iniciar una Xerrameca en draft")
        if not bool(conv["enabled"]):
            raise HTTPException(status_code=423, detail="Aquesta Xerrameca està desactivada")
        participants = await _participant_ids(db, conv["id"], enabled_only=True)
        if len(participants) != 2 or conv["first_agent_id"] not in participants:
            raise HTTPException(status_code=409, detail="Participants no disponibles")

        message_id = str(uuid.uuid4())
        now = _now()
        await db.execute(
            """INSERT INTO xerrameca_messages
               (id, conversation_id, round_no, from_agent_id, to_agent_id,
                message_type, content, metadata, created_at)
               VALUES (?, ?, 1, NULL, ?, 'task', ?, '{}', ?)""",
            (message_id, conv["id"], conv["first_agent_id"], conv["objective"], now),
        )
        turn_id = await _create_turn(
            db, conv["id"], 1, conv["first_agent_id"], message_id
        )
        await db.execute(
            """UPDATE xerrameca_conversations
               SET status = 'active', current_round = 1, current_turn_id = ?,
                   block_reason = NULL, started_at = COALESCE(started_at, ?),
                   updated_at = ?
               WHERE id = ?""",
            (turn_id, now, now, conv["id"]),
        )
        await _audit(db, agent["id"], "XERRAMECA_START", conv["id"])
        await db.commit()
        conv = await _conversation(db, conv["id"])
        return await _conversation_payload(db, conv)


async def pause_conversation(
    agent: dict[str, Any], conversation_id: str, reason: str | None = None
) -> dict[str, Any]:
    _require_admin(agent)
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        conv = await _conversation(db, conversation_id)
        if conv["status"] != "active":
            raise HTTPException(status_code=409, detail="Només es pot pausar una Xerrameca activa")
        now = _now()
        if conv["current_turn_id"]:
            await db.execute(
                """UPDATE xerrameca_turns
                   SET status = 'ready', claimed_by = NULL, lease_token = NULL,
                       claimed_at = NULL, lease_until = NULL
                   WHERE id = ? AND status = 'claimed'""",
                (conv["current_turn_id"],),
            )
        await db.execute(
            """UPDATE xerrameca_conversations
               SET status = 'paused', block_reason = ?, updated_at = ?
               WHERE id = ?""",
            (reason or "paused_by_admin", now, conv["id"]),
        )
        await _audit(db, agent["id"], "XERRAMECA_PAUSE", conv["id"], {"reason": reason})
        await db.commit()
        conv = await _conversation(db, conv["id"])
        return await _conversation_payload(db, conv)


async def _last_message(db: Any, conversation_id: str) -> Any:
    cursor = await db.execute(
        """SELECT * FROM xerrameca_messages
           WHERE conversation_id = ?
           ORDER BY created_at DESC, rowid DESC LIMIT 1""",
        (conversation_id,),
    )
    return await cursor.fetchone()


def _next_agent(
    participants: list[str],
    policy: str,
    supervisor_id: str | None,
    last_agent_id: str | None,
    requested_next: str | None = None,
) -> str:
    if requested_next is not None:
        if requested_next not in participants:
            raise HTTPException(status_code=422, detail="El següent agent no és participant")
        return requested_next
    if len(participants) != 2:
        raise HTTPException(status_code=409, detail="Xerrameca v1 requereix 2 participants")
    if policy == "supervisor":
        if not supervisor_id or supervisor_id not in participants:
            raise HTTPException(status_code=409, detail="Supervisor invàlid")
        if last_agent_id == supervisor_id:
            return participants[1] if participants[0] == supervisor_id else participants[0]
        return supervisor_id
    if last_agent_id in participants:
        return participants[1] if participants[0] == last_agent_id else participants[0]
    return participants[0]


async def resume_conversation(
    agent: dict[str, Any], conversation_id: str, body: ResumeRequest
) -> dict[str, Any]:
    _require_admin(agent)
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        await _require_system_enabled(db)
        conv = await _conversation(db, conversation_id)
        if conv["status"] not in RESUMABLE_STATUSES:
            raise HTTPException(status_code=409, detail="L'estat actual no es pot reprendre")
        if not bool(conv["enabled"]):
            raise HTTPException(status_code=423, detail="Aquesta Xerrameca està desactivada")

        participants = await _participant_ids(db, conv["id"], enabled_only=True)
        if len(participants) != 2:
            raise HTTPException(status_code=409, detail="Calen 2 participants actius")

        if conv["status"] == "paused" and conv["current_turn_id"]:
            cursor = await db.execute(
                "SELECT status FROM xerrameca_turns WHERE id = ?",
                (conv["current_turn_id"],),
            )
            current = await cursor.fetchone()
            if current and current["status"] == "ready":
                await db.execute(
                    """UPDATE xerrameca_conversations
                       SET status = 'active', block_reason = NULL, updated_at = ?
                       WHERE id = ?""",
                    (_now(), conv["id"]),
                )
                await _audit(db, agent["id"], "XERRAMECA_RESUME", conv["id"])
                await db.commit()
                conv = await _conversation(db, conv["id"])
                return await _conversation_payload(db, conv)

        if conv["current_round"] >= conv["max_rounds"]:
            raise HTTPException(
                status_code=409,
                detail="S'ha assolit max_rounds; amplia'l abans de reprendre",
            )
        last = await _last_message(db, conv["id"])
        if not last:
            raise HTTPException(status_code=409, detail="No hi ha missatge per reprendre")
        requested = (
            _clean_identifier(body.next_agent_id, "next_agent_id")
            if body.next_agent_id
            else None
        )
        next_agent = _next_agent(
            participants,
            conv["turn_policy"],
            conv["supervisor_agent_id"],
            last["from_agent_id"],
            requested,
        )
        next_round = conv["current_round"] + 1
        turn_id = await _create_turn(
            db, conv["id"], next_round, next_agent, last["id"]
        )
        await db.execute(
            """UPDATE xerrameca_conversations
               SET status = 'active', current_round = ?, current_turn_id = ?,
                   block_reason = NULL, updated_at = ?
               WHERE id = ?""",
            (next_round, turn_id, _now(), conv["id"]),
        )
        await _audit(
            db,
            agent["id"],
            "XERRAMECA_RESUME",
            conv["id"],
            {"next_agent_id": next_agent},
        )
        await db.commit()
        conv = await _conversation(db, conv["id"])
        return await _conversation_payload(db, conv)


async def update_conversation_settings(
    agent: dict[str, Any],
    conversation_id: str,
    body: ConversationSettingsUpdate,
) -> dict[str, Any]:
    _require_admin(agent)
    values = body.model_dump(exclude_none=True)
    if not values:
        raise HTTPException(status_code=400, detail="No hi ha canvis")
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        conv = await _conversation(db, conversation_id)
        if conv["status"] in TERMINAL_STATUSES:
            raise HTTPException(status_code=409, detail="La Xerrameca ja és terminal")
        participants = await _participant_ids(db, conv["id"])
        policy = values.get("turn_policy", conv["turn_policy"])
        supervisor_id = values.get("supervisor_agent_id", conv["supervisor_agent_id"])
        if body.supervisor_agent_id is not None and policy != "supervisor":
            raise HTTPException(
                status_code=422,
                detail="supervisor_agent_id només és vàlid amb política supervisor",
            )
        if policy == "supervisor":
            supervisor_id = supervisor_id or participants[0]
            supervisor_id = _clean_identifier(supervisor_id, "supervisor_agent_id")
            if supervisor_id not in participants:
                raise HTTPException(status_code=422, detail="Supervisor no participant")
        else:
            supervisor_id = None

        updates: list[str] = []
        params: list[Any] = []
        mapping = {
            "max_rounds": body.max_rounds,
            "turn_timeout_seconds": body.turn_timeout_seconds,
            "persist_summary": None if body.persist_summary is None else (1 if body.persist_summary else 0),
            "enabled": None if body.enabled is None else (1 if body.enabled else 0),
        }
        for key, value in mapping.items():
            if value is not None:
                updates.append(f"{key} = ?")
                params.append(value)
        if body.turn_policy is not None:
            updates.extend(["turn_policy = ?", "supervisor_agent_id = ?"])
            params.extend([policy, supervisor_id])
            await db.execute(
                """UPDATE xerrameca_participants
                   SET role = CASE WHEN agent_id = ? THEN 'supervisor' ELSE 'participant' END
                   WHERE conversation_id = ?""",
                (supervisor_id, conv["id"]),
            )
        elif body.supervisor_agent_id is not None:
            updates.append("supervisor_agent_id = ?")
            params.append(supervisor_id)
            await db.execute(
                """UPDATE xerrameca_participants
                   SET role = CASE WHEN agent_id = ? THEN 'supervisor' ELSE 'participant' END
                   WHERE conversation_id = ?""",
                (supervisor_id, conv["id"]),
            )

        if body.enabled is False and conv["status"] == "active":
            updates.extend(["status = 'paused'", "block_reason = 'conversation_disabled'"])
            if conv["current_turn_id"]:
                await db.execute(
                    """UPDATE xerrameca_turns
                       SET status = 'ready', claimed_by = NULL, lease_token = NULL,
                           claimed_at = NULL, lease_until = NULL
                       WHERE id = ? AND status = 'claimed'""",
                    (conv["current_turn_id"],),
                )

        updates.append("updated_at = ?")
        params.append(_now())
        params.append(conv["id"])
        await db.execute(
            f"UPDATE xerrameca_conversations SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        await _audit(db, agent["id"], "XERRAMECA_SETTINGS", conv["id"], values)
        await db.commit()
        conv = await _conversation(db, conv["id"])
        return await _conversation_payload(db, conv)


async def update_participant(
    agent: dict[str, Any],
    conversation_id: str,
    participant_agent_id: str,
    body: ParticipantUpdate,
) -> dict[str, Any]:
    _require_admin(agent)
    participant_agent_id = _clean_identifier(participant_agent_id, "participant_agent_id")
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        conv = await _conversation(db, conversation_id)
        cursor = await db.execute(
            """UPDATE xerrameca_participants SET enabled = ?
               WHERE conversation_id = ? AND agent_id = ?""",
            (1 if body.enabled else 0, conv["id"], participant_agent_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Participant no trobat")
        if not body.enabled and conv["current_turn_id"]:
            cursor = await db.execute(
                "SELECT assigned_agent_id FROM xerrameca_turns WHERE id = ?",
                (conv["current_turn_id"],),
            )
            turn = await cursor.fetchone()
            if turn and turn["assigned_agent_id"] == participant_agent_id:
                await db.execute(
                    """UPDATE xerrameca_turns
                       SET status = 'ready', claimed_by = NULL, lease_token = NULL,
                           claimed_at = NULL, lease_until = NULL
                       WHERE id = ? AND status = 'claimed'""",
                    (conv["current_turn_id"],),
                )
                await db.execute(
                    """UPDATE xerrameca_conversations
                       SET status = 'paused', block_reason = 'participant_disabled',
                           updated_at = ?
                       WHERE id = ?""",
                    (_now(), conv["id"]),
                )
        await _audit(
            db,
            agent["id"],
            "XERRAMECA_PARTICIPANT_UPDATE",
            conv["id"],
            {"agent_id": participant_agent_id, "enabled": body.enabled},
        )
        await db.commit()
        conv = await _conversation(db, conv["id"])
        return await _conversation_payload(db, conv)


async def inbox(agent: dict[str, Any]) -> dict[str, Any]:
    if not (agent.get("permissions") or {}).get("read", False) and not _is_admin(agent):
        raise HTTPException(status_code=403, detail="Xerrameca: falta permís 'read'")
    now = _now()
    async with get_db() as db:
        runtime = await _runtime_row(db)
        cursor = await db.execute(
            """SELECT t.id AS turn_id, t.conversation_id, t.round_no,
                      t.status, t.claimed_by, t.lease_until,
                      c.name, c.objective, c.scope, c.turn_policy,
                      c.turn_timeout_seconds, c.max_rounds,
                      m.id AS input_message_id, m.from_agent_id, m.message_type,
                      m.content, m.metadata, m.created_at AS message_created_at
               FROM xerrameca_turns t
               JOIN xerrameca_conversations c ON c.id = t.conversation_id
               JOIN xerrameca_participants p
                 ON p.conversation_id = c.id AND p.agent_id = ?
               JOIN xerrameca_messages m ON m.id = t.input_message_id
               WHERE t.assigned_agent_id = ?
                 AND p.enabled = 1
                 AND c.enabled = 1
                 AND c.status = 'active'
                 AND (t.status = 'ready'
                      OR (t.status = 'claimed' AND t.lease_until <= ?))
               ORDER BY t.created_at ASC""",
            (agent["id"], agent["id"], now),
        )
        rows = []
        for row in await cursor.fetchall():
            if row["scope"] not in (agent.get("allowed_scopes") or []) and not _is_admin(agent):
                continue
            item = dict(row)
            try:
                item["metadata"] = json.loads(item["metadata"] or "{}")
            except (json.JSONDecodeError, TypeError):
                item["metadata"] = {}
            rows.append(item)
        return {"system_enabled": bool(runtime["enabled"]), "turns": rows}


async def claim_turn(agent: dict[str, Any], turn_id: str) -> dict[str, Any]:
    turn_id = _clean_identifier(turn_id, "turn_id")
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        await _require_system_enabled(db)
        cursor = await db.execute(
            """SELECT t.*, c.scope, c.status AS conversation_status, c.enabled AS conversation_enabled,
                      c.turn_timeout_seconds
               FROM xerrameca_turns t
               JOIN xerrameca_conversations c ON c.id = t.conversation_id
               WHERE t.id = ?""",
            (turn_id,),
        )
        turn = await cursor.fetchone()
        if not turn:
            raise HTTPException(status_code=404, detail="Torn no trobat")
        conv = await _conversation(db, turn["conversation_id"])
        await _require_participant(db, agent, conv, write=True)
        if turn["assigned_agent_id"] != agent["id"]:
            raise HTTPException(status_code=403, detail="Aquest torn correspon a un altre agent")
        if conv["status"] != "active" or not bool(conv["enabled"]):
            raise HTTPException(status_code=423, detail="La Xerrameca no està activa")

        now = _now()
        if turn["status"] == "claimed" and turn["claimed_by"] == agent["id"] and turn["lease_until"] and turn["lease_until"] > now:
            token = turn["lease_token"]
            lease_until = turn["lease_until"]
        else:
            if turn["status"] not in {"ready", "claimed"}:
                raise HTTPException(status_code=409, detail="El torn no està disponible")
            if turn["status"] == "claimed" and turn["lease_until"] and turn["lease_until"] > now:
                raise HTTPException(status_code=409, detail="El torn ja està reclamat")
            token = secrets.token_urlsafe(32)
            lease_until = _lease_until(conv["turn_timeout_seconds"])
            cursor = await db.execute(
                """UPDATE xerrameca_turns
                   SET status = 'claimed', claimed_by = ?, lease_token = ?,
                       claimed_at = ?, lease_until = ?
                   WHERE id = ?
                     AND (status = 'ready'
                          OR (status = 'claimed' AND lease_until <= ?))""",
                (agent["id"], token, now, lease_until, turn_id, now),
            )
            if cursor.rowcount != 1:
                raise HTTPException(status_code=409, detail="El torn acaba de ser reclamat")
            await _audit(
                db,
                agent["id"],
                "XERRAMECA_CLAIM",
                turn["conversation_id"],
                {"turn_id": turn_id, "round": turn["round_no"]},
            )
            await db.commit()

        cursor = await db.execute(
            """SELECT m.id, m.from_agent_id, m.to_agent_id, m.message_type,
                      m.content, m.metadata, m.created_at
               FROM xerrameca_messages m WHERE m.id = ?""",
            (turn["input_message_id"],),
        )
        message = await cursor.fetchone()
        payload = dict(message) if message else None
        if payload:
            try:
                payload["metadata"] = json.loads(payload["metadata"] or "{}")
            except (json.JSONDecodeError, TypeError):
                payload["metadata"] = {}
        return {
            "turn_id": turn_id,
            "conversation_id": turn["conversation_id"],
            "round": turn["round_no"],
            "lease_token": token,
            "lease_until": lease_until,
            "input_message": payload,
        }


async def _persist_summary(
    db: Any,
    conv: Any,
    final_content: str,
    status: str,
) -> tuple[str, list[str]] | tuple[None, list[str]]:
    if not bool(conv["persist_summary"]):
        return None, []
    fact_id = str(uuid.uuid4())
    summary = (
        f"Xerrameca: {conv['name']}\n"
        f"Objectiu: {conv['objective']}\n"
        f"Estat final: {status}\n"
        f"Rondes: {conv['current_round']}\n"
        f"Resultat final: {final_content}"
    )
    metadata = json.dumps(
        {
            "xerrameca_conversation_id": conv["id"],
            "status": status,
            "rounds": conv["current_round"],
        },
        ensure_ascii=False,
    )
    await db.execute(
        """INSERT INTO facts
           (id, scope, category, agent_id, key, content, metadata)
           VALUES (?, ?, 'x-xerrameca', NULL, ?, ?, ?)""",
        (
            fact_id,
            conv["scope"],
            f"xerrameca:{conv['id']}",
            summary,
            metadata,
        ),
    )
    chunks = embedding_service.split_into_chunks(summary)
    empty_blob = b"\x00" * (settings.EMBED_DIM * 4)
    for chunk in chunks:
        await db.execute(
            """INSERT INTO chunks (fact_id, chunk_text, embedding_blob)
               VALUES (?, ?, ?)""",
            (fact_id, chunk, empty_blob),
        )
    await db.execute(
        """UPDATE xerrameca_conversations
           SET summary_fact_id = ? WHERE id = ?""",
        (fact_id, conv["id"]),
    )
    return fact_id, chunks


async def reply_turn(
    agent: dict[str, Any], turn_id: str, body: ReplyRequest
) -> dict[str, Any]:
    turn_id = _clean_identifier(turn_id, "turn_id")
    content = _clean_content(body.content)
    metadata = _clean_metadata(body.metadata)
    requested_next = (
        _clean_identifier(body.next_agent_id, "next_agent_id")
        if body.next_agent_id
        else None
    )
    summary_task: tuple[str, list[str]] | None = None

    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        await _require_system_enabled(db)
        cursor = await db.execute(
            """SELECT t.id, t.conversation_id, t.round_no, t.assigned_agent_id,
                      t.input_message_id, t.status, t.claimed_by, t.lease_token,
                      t.claimed_at, t.lease_until, t.completed_at, t.created_at
               FROM xerrameca_turns t
               WHERE t.id = ?""",
            (turn_id,),
        )
        joined = await cursor.fetchone()
        if not joined:
            raise HTTPException(status_code=404, detail="Torn no trobat")
        conv = await _conversation(db, joined["conversation_id"])
        await _require_participant(db, agent, conv, write=True)
        if conv["status"] != "active" or not bool(conv["enabled"]):
            raise HTTPException(status_code=423, detail="La Xerrameca no està activa")
        if joined["assigned_agent_id"] != agent["id"]:
            raise HTTPException(status_code=403, detail="Aquest torn correspon a un altre agent")

        now = _now()
        if joined["status"] != "claimed" or joined["claimed_by"] != agent["id"]:
            raise HTTPException(status_code=409, detail="Cal reclamar el torn abans de respondre")
        if not secrets.compare_digest(joined["lease_token"] or "", body.lease_token):
            raise HTTPException(status_code=409, detail="Lease token invàlid")
        if not joined["lease_until"] or joined["lease_until"] <= now:
            raise HTTPException(status_code=409, detail="La lease del torn ha caducat")

        participants = await _participant_ids(db, conv["id"], enabled_only=True)
        if len(participants) != 2:
            raise HTTPException(status_code=409, detail="Participants no disponibles")

        next_agent: str | None = None
        terminal_status: str | None = None
        block_reason: str | None = None
        if body.result == "continue":
            if conv["current_round"] >= conv["max_rounds"]:
                terminal_status = "blocked"
                block_reason = "max_rounds"
            else:
                if requested_next is not None:
                    if conv["turn_policy"] != "supervisor" or agent["id"] != conv["supervisor_agent_id"]:
                        raise HTTPException(
                            status_code=403,
                            detail="Només el supervisor pot escollir next_agent_id",
                        )
                next_agent = _next_agent(
                    participants,
                    conv["turn_policy"],
                    conv["supervisor_agent_id"],
                    agent["id"],
                    requested_next,
                )
        elif body.result == "complete":
            terminal_status = "completed"
        elif body.result in {"blocked", "needs_human"}:
            terminal_status = "blocked"
            block_reason = body.result
        elif body.result == "error":
            terminal_status = "error"
            block_reason = "agent_error"

        message_id = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO xerrameca_messages
               (id, conversation_id, turn_id, round_no, from_agent_id, to_agent_id,
                message_type, content, metadata, turn_result, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'result', ?, ?, ?, ?)""",
            (
                message_id,
                conv["id"],
                turn_id,
                joined["round_no"],
                agent["id"],
                next_agent,
                content,
                json.dumps(metadata, ensure_ascii=False),
                body.result,
                now,
            ),
        )
        await db.execute(
            """UPDATE xerrameca_turns
               SET status = 'completed', completed_at = ?
               WHERE id = ?""",
            (now, turn_id),
        )

        next_turn_id = None
        if terminal_status is not None:
            finished_at = now if terminal_status == "completed" else None
            await db.execute(
                """UPDATE xerrameca_conversations
                   SET status = ?, current_turn_id = NULL, block_reason = ?,
                       finished_at = COALESCE(?, finished_at), updated_at = ?
                   WHERE id = ?""",
                (terminal_status, block_reason, finished_at, now, conv["id"]),
            )
            if terminal_status == "completed":
                conv = await _conversation(db, conv["id"])
                fact_id, chunks = await _persist_summary(
                    db, conv, content, terminal_status
                )
                if fact_id:
                    summary_task = (fact_id, chunks)
        elif next_agent is not None:
            next_round = conv["current_round"] + 1
            next_turn_id = await _create_turn(
                db, conv["id"], next_round, next_agent, message_id
            )
            await db.execute(
                """UPDATE xerrameca_conversations
                   SET current_round = ?, current_turn_id = ?,
                       block_reason = NULL, updated_at = ?
                   WHERE id = ?""",
                (next_round, next_turn_id, now, conv["id"]),
            )

        await _audit(
            db,
            agent["id"],
            "XERRAMECA_REPLY",
            conv["id"],
            {
                "turn_id": turn_id,
                "result": body.result,
                "next_agent_id": next_agent,
            },
        )
        await db.commit()
        conv = await _conversation(db, conv["id"])
        payload = await _conversation_payload(db, conv)

    if summary_task is not None:
        asyncio.create_task(
            _generate_embeddings_background(summary_task[0], summary_task[1])
        )
    payload["reply_message_id"] = message_id
    payload["next_turn_id"] = next_turn_id
    return payload


async def assign_turn(
    agent: dict[str, Any],
    conversation_id: str,
    body: AssignTurnRequest,
) -> dict[str, Any]:
    _require_admin(agent)
    target = _clean_identifier(body.agent_id, "agent_id")
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        conv = await _conversation(db, conversation_id)
        if conv["status"] not in {"active", "paused"} or not conv["current_turn_id"]:
            raise HTTPException(status_code=409, detail="No hi ha un torn assignable")
        participants = await _participant_ids(db, conv["id"], enabled_only=True)
        if target not in participants:
            raise HTTPException(status_code=422, detail="Agent no participant o desactivat")
        cursor = await db.execute(
            "SELECT status FROM xerrameca_turns WHERE id = ?",
            (conv["current_turn_id"],),
        )
        turn = await cursor.fetchone()
        if not turn or turn["status"] not in {"ready", "claimed"}:
            raise HTTPException(status_code=409, detail="El torn no és reassignable")
        if turn["status"] == "claimed" and not body.force:
            raise HTTPException(
                status_code=409,
                detail="El torn està reclamat; usa force=true per revocar la lease",
            )
        await db.execute(
            """UPDATE xerrameca_turns
               SET assigned_agent_id = ?, status = 'ready', claimed_by = NULL,
                   lease_token = NULL, claimed_at = NULL, lease_until = NULL
               WHERE id = ?""",
            (target, conv["current_turn_id"]),
        )
        await _audit(
            db,
            agent["id"],
            "XERRAMECA_ASSIGN_TURN",
            conv["id"],
            {"agent_id": target, "force": body.force, "reason": body.reason},
        )
        await db.commit()
        conv = await _conversation(db, conv["id"])
        return await _conversation_payload(db, conv)


async def skip_turn(
    agent: dict[str, Any],
    conversation_id: str,
    body: SkipTurnRequest,
) -> dict[str, Any]:
    _require_admin(agent)
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        await _require_system_enabled(db)
        conv = await _conversation(db, conversation_id)
        if conv["status"] != "active" or not conv["current_turn_id"]:
            raise HTTPException(status_code=409, detail="No hi ha torn actiu")
        if conv["current_round"] >= conv["max_rounds"]:
            raise HTTPException(status_code=409, detail="S'ha assolit max_rounds")
        cursor = await db.execute(
            "SELECT * FROM xerrameca_turns WHERE id = ?",
            (conv["current_turn_id"],),
        )
        turn = await cursor.fetchone()
        if not turn or turn["status"] not in {"ready", "claimed"}:
            raise HTTPException(status_code=409, detail="El torn no es pot saltar")
        participants = await _participant_ids(db, conv["id"], enabled_only=True)
        next_agent = _next_agent(
            participants,
            conv["turn_policy"],
            conv["supervisor_agent_id"],
            turn["assigned_agent_id"],
        )
        now = _now()
        await db.execute(
            """UPDATE xerrameca_turns SET status = 'skipped', completed_at = ?
               WHERE id = ?""",
            (now, turn["id"]),
        )
        message_id = str(uuid.uuid4())
        content = "Torn saltat per administració"
        if body.reason:
            content += f": {body.reason}"
        await db.execute(
            """INSERT INTO xerrameca_messages
               (id, conversation_id, turn_id, round_no, from_agent_id, to_agent_id,
                message_type, content, metadata, turn_result, created_at)
               VALUES (?, ?, ?, ?, NULL, ?, 'control', ?, '{}', 'continue', ?)""",
            (
                message_id,
                conv["id"],
                turn["id"],
                conv["current_round"],
                next_agent,
                content,
                now,
            ),
        )
        next_round = conv["current_round"] + 1
        next_turn_id = await _create_turn(
            db, conv["id"], next_round, next_agent, message_id
        )
        await db.execute(
            """UPDATE xerrameca_conversations
               SET current_round = ?, current_turn_id = ?, updated_at = ?
               WHERE id = ?""",
            (next_round, next_turn_id, now, conv["id"]),
        )
        await _audit(
            db,
            agent["id"],
            "XERRAMECA_SKIP_TURN",
            conv["id"],
            {"reason": body.reason, "next_agent_id": next_agent},
        )
        await db.commit()
        conv = await _conversation(db, conv["id"])
        return await _conversation_payload(db, conv)


async def finish_conversation(
    agent: dict[str, Any], conversation_id: str, body: FinishRequest
) -> dict[str, Any]:
    _require_admin(agent)
    summary_task: tuple[str, list[str]] | None = None
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        conv = await _conversation(db, conversation_id)
        if conv["status"] in TERMINAL_STATUSES:
            raise HTTPException(status_code=409, detail="La Xerrameca ja és terminal")
        if conv["current_turn_id"]:
            await db.execute(
                """UPDATE xerrameca_turns
                   SET status = 'cancelled', completed_at = ?
                   WHERE id = ? AND status IN ('ready','claimed')""",
                (_now(), conv["current_turn_id"]),
            )
        final_content = body.summary
        if final_content:
            final_content = _clean_content(final_content)
        else:
            last = await _last_message(db, conv["id"])
            final_content = last["content"] if last else "Finalitzada per administració"
        now = _now()
        message_id = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO xerrameca_messages
               (id, conversation_id, round_no, from_agent_id, to_agent_id,
                message_type, content, metadata, turn_result, created_at)
               VALUES (?, ?, ?, NULL, NULL, 'control', ?, '{}', 'complete', ?)""",
            (message_id, conv["id"], conv["current_round"], final_content, now),
        )
        await db.execute(
            """UPDATE xerrameca_conversations
               SET status = 'completed', current_turn_id = NULL,
                   block_reason = NULL, finished_at = ?, updated_at = ?
               WHERE id = ?""",
            (now, now, conv["id"]),
        )
        conv = await _conversation(db, conv["id"])
        fact_id, chunks = await _persist_summary(db, conv, final_content, "completed")
        if fact_id:
            summary_task = (fact_id, chunks)
        await _audit(db, agent["id"], "XERRAMECA_FINISH", conv["id"])
        await db.commit()
        conv = await _conversation(db, conv["id"])
        payload = await _conversation_payload(db, conv)
    if summary_task is not None:
        asyncio.create_task(
            _generate_embeddings_background(summary_task[0], summary_task[1])
        )
    return payload


async def cancel_conversation(
    agent: dict[str, Any], conversation_id: str
) -> dict[str, Any]:
    _require_admin(agent)
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        conv = await _conversation(db, conversation_id)
        if conv["status"] in TERMINAL_STATUSES:
            raise HTTPException(status_code=409, detail="La Xerrameca ja és terminal")
        now = _now()
        if conv["current_turn_id"]:
            await db.execute(
                """UPDATE xerrameca_turns
                   SET status = 'cancelled', completed_at = ?
                   WHERE id = ? AND status IN ('ready','claimed')""",
                (now, conv["current_turn_id"]),
            )
        await db.execute(
            """UPDATE xerrameca_conversations
               SET status = 'cancelled', current_turn_id = NULL,
                   block_reason = 'cancelled_by_admin', finished_at = ?, updated_at = ?
               WHERE id = ?""",
            (now, now, conv["id"]),
        )
        await _audit(db, agent["id"], "XERRAMECA_CANCEL", conv["id"])
        await db.commit()
        conv = await _conversation(db, conv["id"])
        return await _conversation_payload(db, conv)


async def list_messages(
    agent: dict[str, Any], conversation_id: str
) -> list[dict[str, Any]]:
    async with get_db() as db:
        conv = await _conversation(db, conversation_id)
        await _can_view(db, agent, conv)
        cursor = await db.execute(
            """SELECT id, turn_id, round_no, from_agent_id, to_agent_id,
                      message_type, content, metadata, turn_result, created_at
               FROM xerrameca_messages
               WHERE conversation_id = ?
               ORDER BY created_at ASC, rowid ASC""",
            (conv["id"],),
        )
        result = []
        for row in await cursor.fetchall():
            item = dict(row)
            try:
                item["metadata"] = json.loads(item["metadata"] or "{}")
            except (json.JSONDecodeError, TypeError):
                item["metadata"] = {}
            result.append(item)
        return result
