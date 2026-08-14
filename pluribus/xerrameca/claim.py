"""Claim estricte i atòmic de torns Xerrameca."""

from __future__ import annotations

import json
import secrets
from typing import Any

from fastapi import HTTPException

from pluribus.db import get_db

from .service import (
    _audit,
    _clean_identifier,
    _conversation,
    _lease_until,
    _now,
    _require_participant,
    _require_system_enabled,
)


async def claim_turn(agent: dict[str, Any], turn_id: str) -> dict[str, Any]:
    """Reclama un torn una sola vegada durant la vida de la lease.

    Fins i tot una segona instància del mateix agent rep 409 mentre la lease és
    vigent. Això evita doble execució amb credencials compartides. Una lease
    caducada torna a ser reclamable de forma atòmica.
    """
    turn_id = _clean_identifier(turn_id, "turn_id")
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        await _require_system_enabled(db)
        cursor = await db.execute(
            """SELECT t.*, c.scope, c.status AS conversation_status,
                      c.enabled AS conversation_enabled, c.turn_timeout_seconds
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
        if turn["status"] == "claimed" and turn["lease_until"] and turn["lease_until"] > now:
            raise HTTPException(status_code=409, detail="El torn ja està reclamat")
        if turn["status"] not in {"ready", "claimed"}:
            raise HTTPException(status_code=409, detail="El torn no està disponible")

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
            """SELECT id, from_agent_id, to_agent_id, message_type,
                      content, metadata, created_at
               FROM xerrameca_messages WHERE id = ?""",
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
