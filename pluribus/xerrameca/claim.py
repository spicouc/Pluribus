"""Claim estricte i atòmic de torns Xerrameca."""

from __future__ import annotations

import json
import secrets
from typing import Any

from fastapi import HTTPException

from pluribus.db import get_db

from .dialogue import turn_context
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
    vigent. Una lease caducada torna a ser reclamable de forma atòmica.
    Dialogue v1 afegeix context estructurat quan la seva migració és present,
    sense trencar instal·lacions/tests que només inicialitzen Xerrameca legacy.
    """
    turn_id = _clean_identifier(turn_id, "turn_id")
    result: dict[str, Any]
    has_dialogue_schema = False
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

        turn_keys = set(turn.keys())
        has_dialogue_schema = {
            "dialogue_round",
            "turn_in_round",
            "phase",
        }.issubset(turn_keys)
        logical_round = (
            turn["dialogue_round"]
            if has_dialogue_schema and turn["dialogue_round"] is not None
            else turn["round_no"]
        )
        turn_in_round = turn["turn_in_round"] if has_dialogue_schema else None
        phase = (turn["phase"] or "dialogue") if has_dialogue_schema else "dialogue"

        await _audit(
            db,
            agent["id"],
            "XERRAMECA_CLAIM",
            turn["conversation_id"],
            {"turn_id": turn_id, "round": logical_round},
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

        result = {
            "turn_id": turn_id,
            "conversation_id": turn["conversation_id"],
            "round": logical_round,
            "turn_sequence": turn["round_no"],
            "turn_in_round": turn_in_round,
            "phase": phase,
            "lease_token": token,
            "lease_until": lease_until,
            "input_message": payload,
        }

    if has_dialogue_schema:
        result["dialogue_context"] = await turn_context(turn_id)
    else:
        result["dialogue_context"] = {
            "protocol_version": "legacy-v0",
            "dialogue_round": result["round"],
            "turn_in_round": None,
            "phase": "dialogue",
            "history": [],
        }
    return result
