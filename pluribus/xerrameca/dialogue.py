"""Formal two-agent dialogue semantics for Xerrameca.

Dialogue v1 is opt-in per conversation. Existing conversations remain legacy-v0.
The existing turn.round_no remains a monotonically increasing turn sequence for
backwards compatibility; dialogue_round/turn_in_round carry human round semantics.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from typing import Any

from fastapi import HTTPException

from pluribus.db import get_db
from pluribus.memory import _generate_embeddings_background

from .models import ConversationCreateRequest, ReplyRequest
from . import service as legacy


PROTOCOL_VERSION = "dialogue-v1"
HISTORY_LIMIT = 8


async def _protocol_row(db: Any, conversation_id: str) -> Any:
    cursor = await db.execute(
        """SELECT protocol_version, completion_proposed_by_agent_id,
                  completion_proposed_at, completion_proposal_turn_id
           FROM xerrameca_conversations WHERE id = ?""",
        (conversation_id,),
    )
    return await cursor.fetchone()


async def _decorate(payload: dict[str, Any]) -> dict[str, Any]:
    async with get_db() as db:
        row = await _protocol_row(db, payload["id"])
        if row:
            payload["protocol_version"] = row["protocol_version"]
            payload["completion_proposed_by_agent_id"] = row[
                "completion_proposed_by_agent_id"
            ]
            payload["completion_proposed_at"] = row["completion_proposed_at"]
            payload["completion_pending"] = bool(
                row["completion_proposed_by_agent_id"]
            )
        turn = payload.get("current_turn")
        if turn and turn.get("id"):
            cursor = await db.execute(
                """SELECT dialogue_round, turn_in_round, phase
                   FROM xerrameca_turns WHERE id = ?""",
                (turn["id"],),
            )
            extra = await cursor.fetchone()
            if extra:
                turn["dialogue_round"] = extra["dialogue_round"]
                turn["turn_in_round"] = extra["turn_in_round"]
                turn["phase"] = extra["phase"]
    return payload


async def create_conversation(
    agent: dict[str, Any], body: ConversationCreateRequest
) -> dict[str, Any]:
    payload = await legacy.create_conversation(agent, body)
    async with get_db() as db:
        await db.execute(
            """UPDATE xerrameca_conversations
               SET protocol_version = ?, updated_at = ? WHERE id = ?""",
            (PROTOCOL_VERSION, legacy._now(), payload["id"]),
        )
        await db.commit()
    return await _decorate(await legacy.get_conversation(agent, payload["id"]))


async def get_conversation(agent: dict[str, Any], conversation_id: str) -> dict[str, Any]:
    return await _decorate(await legacy.get_conversation(agent, conversation_id))


async def list_conversations(agent: dict[str, Any]) -> list[dict[str, Any]]:
    rows = await legacy.list_conversations(agent)
    return [await _decorate(row) for row in rows]


def _kickoff_content(conv: Any, participants: list[dict[str, Any]]) -> str:
    names = "\n".join(
        f"- {p.get('name') or p['agent_id']} ({p['agent_id']})" for p in participants
    )
    completion = (
        "El supervisor pot finalitzar la conversa. Un altre agent pot proposar "
        "finalització, que el supervisor confirmarà."
        if conv["turn_policy"] == "supervisor"
        else "La finalització requereix consens: un agent proposa complete i "
        "l'altre ha de confirmar complete."
    )
    return (
        "XERRAMECA DIALOGUE PROTOCOL v1\n\n"
        f"Objectiu:\n{conv['objective']}\n\n"
        f"Participants:\n{names}\n\n"
        f"Política de torns: {conv['turn_policy']}\n"
        f"Màxim de rondes: {conv['max_rounds']}\n"
        f"Timeout per torn: {conv['turn_timeout_seconds']} s\n\n"
        "Regles:\n"
        "- Una ronda alternating és la intervenció dels dos agents.\n"
        "- Respon al missatge rebut i aporta progrés cap a l'objectiu.\n"
        "- No repeteixis arguments ja resolts.\n"
        "- Usa blocked/needs_human/error només quan correspongui.\n"
        f"- {completion}\n"
    )


async def start_conversation(agent: dict[str, Any], conversation_id: str) -> dict[str, Any]:
    async with get_db() as db:
        conv = await legacy._conversation(db, conversation_id)
        protocol = conv["protocol_version"] if "protocol_version" in conv.keys() else "legacy-v0"
    if protocol != PROTOCOL_VERSION:
        return await legacy.start_conversation(agent, conversation_id)

    payload = await legacy.start_conversation(agent, conversation_id)
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        conv = await legacy._conversation(db, conversation_id)
        participants = await legacy._participants(db, conversation_id)
        cursor = await db.execute(
            "SELECT input_message_id FROM xerrameca_turns WHERE id = ?",
            (conv["current_turn_id"],),
        )
        turn = await cursor.fetchone()
        if not turn:
            raise HTTPException(status_code=409, detail="Kickoff sense torn inicial")
        kickoff = _kickoff_content(conv, participants)
        kickoff_meta = json.dumps(
            {
                "protocol": PROTOCOL_VERSION,
                "kind": "kickoff",
                "objective": conv["objective"],
                "turn_policy": conv["turn_policy"],
                "max_rounds": conv["max_rounds"],
            },
            ensure_ascii=False,
        )
        await db.execute(
            """UPDATE xerrameca_messages
               SET message_type = 'control', content = ?, metadata = ?
               WHERE id = ?""",
            (kickoff, kickoff_meta, turn["input_message_id"]),
        )
        await db.execute(
            """UPDATE xerrameca_turns
               SET dialogue_round = 1, turn_in_round = 1, phase = 'dialogue'
               WHERE id = ?""",
            (conv["current_turn_id"],),
        )
        await db.execute(
            """UPDATE xerrameca_conversations
               SET current_round = 1,
                   completion_proposed_by_agent_id = NULL,
                   completion_proposed_at = NULL,
                   completion_proposal_turn_id = NULL,
                   updated_at = ?
               WHERE id = ?""",
            (legacy._now(), conversation_id),
        )
        await legacy._audit(
            db,
            agent["id"],
            "XERRAMECA_DIALOGUE_START",
            conversation_id,
            {"protocol": PROTOCOL_VERSION},
        )
        await db.commit()
    return await get_conversation(agent, conversation_id)


async def _turn_sequence(db: Any, conversation_id: str) -> int:
    cursor = await db.execute(
        "SELECT COALESCE(MAX(round_no), 0) + 1 AS seq FROM xerrameca_turns WHERE conversation_id = ?",
        (conversation_id,),
    )
    row = await cursor.fetchone()
    return int(row["seq"])


async def _create_dialogue_turn(
    db: Any,
    conv: Any,
    assigned_agent_id: str,
    input_message_id: str,
    dialogue_round: int,
    turn_in_round: int,
    phase: str = "dialogue",
) -> str:
    turn_id = str(uuid.uuid4())
    seq = await _turn_sequence(db, conv["id"])
    await db.execute(
        """INSERT INTO xerrameca_turns
           (id, conversation_id, round_no, assigned_agent_id, input_message_id,
            status, created_at, dialogue_round, turn_in_round, phase)
           VALUES (?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?)""",
        (
            turn_id,
            conv["id"],
            seq,
            assigned_agent_id,
            input_message_id,
            legacy._now(),
            dialogue_round,
            turn_in_round,
            phase,
        ),
    )
    return turn_id


def _ordered_participants(conv: Any, participants: list[str]) -> list[str]:
    if len(participants) != 2:
        raise HTTPException(status_code=409, detail="Dialogue v1 requereix 2 participants")
    first = conv["first_agent_id"]
    if first not in participants:
        raise HTTPException(status_code=409, detail="first_agent_id no disponible")
    other = participants[1] if participants[0] == first else participants[0]
    return [first, other]


def _normal_next(
    order: list[str], dialogue_round: int, turn_in_round: int
) -> tuple[str, int, int]:
    if turn_in_round == 1:
        return order[1], dialogue_round, 2
    if turn_in_round == 2:
        return order[0], dialogue_round + 1, 1
    raise HTTPException(status_code=409, detail="Posició de torn invàlida")


async def _finish_from_reply(
    db: Any,
    conv: Any,
    content: str,
    now: str,
) -> tuple[str, list[str]] | None:
    await db.execute(
        """UPDATE xerrameca_conversations
           SET status = 'completed', current_turn_id = NULL, block_reason = NULL,
               completion_proposed_by_agent_id = NULL,
               completion_proposed_at = NULL,
               completion_proposal_turn_id = NULL,
               finished_at = ?, updated_at = ?
           WHERE id = ?""",
        (now, now, conv["id"]),
    )
    refreshed = await legacy._conversation(db, conv["id"])
    fact_id, chunks = await legacy._persist_summary(db, refreshed, content, "completed")
    return (fact_id, chunks) if fact_id else None


async def _proposal_turn(db: Any, conv: Any) -> Any:
    proposal_id = conv["completion_proposal_turn_id"]
    if not proposal_id:
        return None
    cursor = await db.execute(
        """SELECT id, dialogue_round, turn_in_round, assigned_agent_id
           FROM xerrameca_turns WHERE id = ?""",
        (proposal_id,),
    )
    return await cursor.fetchone()


async def reply_turn(
    agent: dict[str, Any], turn_id: str, body: ReplyRequest
) -> dict[str, Any]:
    turn_id = legacy._clean_identifier(turn_id, "turn_id")
    content = legacy._clean_content(body.content)
    metadata = legacy._clean_metadata(body.metadata)
    requested_next = (
        legacy._clean_identifier(body.next_agent_id, "next_agent_id")
        if body.next_agent_id
        else None
    )
    summary_task: tuple[str, list[str]] | None = None

    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        await legacy._require_system_enabled(db)
        cursor = await db.execute(
            """SELECT * FROM xerrameca_turns WHERE id = ?""",
            (turn_id,),
        )
        turn = await cursor.fetchone()
        if not turn:
            raise HTTPException(status_code=404, detail="Torn no trobat")
        conv = await legacy._conversation(db, turn["conversation_id"])
        if conv["protocol_version"] != PROTOCOL_VERSION:
            await db.rollback()
            return await legacy.reply_turn(agent, turn_id, body)

        await legacy._require_participant(db, agent, conv, write=True)
        if conv["status"] != "active" or not bool(conv["enabled"]):
            raise HTTPException(status_code=423, detail="La Xerrameca no està activa")
        if turn["assigned_agent_id"] != agent["id"]:
            raise HTTPException(status_code=403, detail="Aquest torn correspon a un altre agent")

        now = legacy._now()
        if turn["status"] != "claimed" or turn["claimed_by"] != agent["id"]:
            raise HTTPException(status_code=409, detail="Cal reclamar el torn abans de respondre")
        if not secrets.compare_digest(turn["lease_token"] or "", body.lease_token):
            raise HTTPException(status_code=409, detail="Lease token invàlid")
        if not turn["lease_until"] or turn["lease_until"] <= now:
            raise HTTPException(status_code=409, detail="La lease del torn ha caducat")

        participants = await legacy._participant_ids(db, conv["id"], enabled_only=True)
        order = _ordered_participants(conv, participants)
        logical_round = int(turn["dialogue_round"] or conv["current_round"] or 1)
        slot = int(turn["turn_in_round"] or 1)
        phase = turn["phase"] or "dialogue"

        proposer = conv["completion_proposed_by_agent_id"]
        confirmation = bool(proposer) and phase == "completion_confirmation"
        if confirmation and proposer == agent["id"]:
            raise HTTPException(status_code=409, detail="El proposant no pot confirmar-se a si mateix")

        next_agent: str | None = None
        next_round: int | None = None
        next_slot: int | None = None
        next_phase = "dialogue"
        terminal_status: str | None = None
        block_reason: str | None = None

        if body.result == "complete":
            if conv["turn_policy"] == "supervisor" and agent["id"] == conv["supervisor_agent_id"]:
                terminal_status = "completed"
            elif confirmation:
                terminal_status = "completed"
            else:
                # First complete is a proposal, never unilateral in alternating mode.
                other = participants[1] if participants[0] == agent["id"] else participants[0]
                next_agent = (
                    conv["supervisor_agent_id"]
                    if conv["turn_policy"] == "supervisor"
                    else other
                )
                if not next_agent or next_agent == agent["id"]:
                    raise HTTPException(status_code=409, detail="No hi ha agent de confirmació")
                next_round = logical_round
                next_slot = 0
                next_phase = "completion_confirmation"
        elif body.result == "continue":
            if confirmation:
                proposal_turn = await _proposal_turn(db, conv)
                if not proposal_turn:
                    raise HTTPException(status_code=409, detail="Proposta de finalització inconsistent")
                p_round = int(proposal_turn["dialogue_round"] or logical_round)
                p_slot = int(proposal_turn["turn_in_round"] or 1)
                # The confirmation response counts as the natural next contribution.
                if p_round >= conv["max_rounds"]:
                    terminal_status = "blocked"
                    block_reason = "max_rounds"
                elif p_slot == 1:
                    next_agent, next_round, next_slot = order[0], p_round + 1, 1
                else:
                    # proposer was slot 2; confirmer is implicitly slot 1 of next round.
                    next_agent, next_round, next_slot = order[1], p_round + 1, 2
            else:
                if requested_next is not None:
                    if conv["turn_policy"] != "supervisor" or agent["id"] != conv["supervisor_agent_id"]:
                        raise HTTPException(status_code=403, detail="Només el supervisor pot escollir next_agent_id")
                natural_agent, natural_round, natural_slot = _normal_next(order, logical_round, slot)
                if requested_next is not None and requested_next != natural_agent:
                    raise HTTPException(
                        status_code=422,
                        detail="Dialogue v1 manté alternança entre els dos participants",
                    )
                if slot == 2 and logical_round >= conv["max_rounds"]:
                    terminal_status = "blocked"
                    block_reason = "max_rounds"
                else:
                    next_agent, next_round, next_slot = natural_agent, natural_round, natural_slot
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
                logical_round,
                agent["id"],
                next_agent,
                content,
                json.dumps(
                    {
                        **metadata,
                        "protocol": PROTOCOL_VERSION,
                        "dialogue_round": logical_round,
                        "turn_in_round": slot,
                        "phase": phase,
                    },
                    ensure_ascii=False,
                ),
                body.result,
                now,
            ),
        )
        await db.execute(
            "UPDATE xerrameca_turns SET status = 'completed', completed_at = ? WHERE id = ?",
            (now, turn_id),
        )

        next_turn_id = None
        if terminal_status == "completed":
            summary_task = await _finish_from_reply(db, conv, content, now)
        elif terminal_status is not None:
            await db.execute(
                """UPDATE xerrameca_conversations
                   SET status = ?, current_turn_id = NULL, block_reason = ?,
                       completion_proposed_by_agent_id = NULL,
                       completion_proposed_at = NULL,
                       completion_proposal_turn_id = NULL,
                       updated_at = ? WHERE id = ?""",
                (terminal_status, block_reason, now, conv["id"]),
            )
        elif next_agent is not None and next_round is not None and next_slot is not None:
            if body.result == "complete" and not confirmation:
                await db.execute(
                    """UPDATE xerrameca_conversations
                       SET completion_proposed_by_agent_id = ?,
                           completion_proposed_at = ?, completion_proposal_turn_id = ?,
                           updated_at = ? WHERE id = ?""",
                    (agent["id"], now, turn_id, now, conv["id"]),
                )
            elif confirmation and body.result == "continue":
                await db.execute(
                    """UPDATE xerrameca_conversations
                       SET completion_proposed_by_agent_id = NULL,
                           completion_proposed_at = NULL,
                           completion_proposal_turn_id = NULL
                       WHERE id = ?""",
                    (conv["id"],),
                )
            next_turn_id = await _create_dialogue_turn(
                db,
                conv,
                next_agent,
                message_id,
                next_round,
                next_slot,
                next_phase,
            )
            effective_round = logical_round if next_phase == "completion_confirmation" else next_round
            await db.execute(
                """UPDATE xerrameca_conversations
                   SET status = 'active', current_round = ?, current_turn_id = ?,
                       block_reason = NULL, updated_at = ? WHERE id = ?""",
                (effective_round, next_turn_id, now, conv["id"]),
            )

        await legacy._audit(
            db,
            agent["id"],
            "XERRAMECA_DIALOGUE_REPLY",
            conv["id"],
            {
                "turn_id": turn_id,
                "result": body.result,
                "dialogue_round": logical_round,
                "turn_in_round": slot,
                "phase": phase,
                "next_agent_id": next_agent,
            },
        )
        await db.commit()

    if summary_task:
        asyncio.create_task(_generate_embeddings_background(summary_task[0], summary_task[1]))
    payload = await get_conversation(agent, conv["id"])
    payload["reply_message_id"] = message_id
    payload["next_turn_id"] = next_turn_id
    return payload


async def turn_context(turn_id: str) -> dict[str, Any]:
    """Build bounded context for REST/MCP/Runner after a turn is claimed."""
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT t.id, t.conversation_id, t.dialogue_round, t.turn_in_round,
                      t.phase, c.name, c.objective, c.status, c.turn_policy,
                      c.max_rounds, c.current_round, c.protocol_version,
                      c.completion_proposed_by_agent_id
               FROM xerrameca_turns t
               JOIN xerrameca_conversations c ON c.id = t.conversation_id
               WHERE t.id = ?""",
            (turn_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return {}
        cursor = await db.execute(
            """SELECT id, round_no, from_agent_id, to_agent_id, message_type,
                      content, turn_result, created_at
               FROM xerrameca_messages
               WHERE conversation_id = ?
               ORDER BY created_at DESC, rowid DESC LIMIT ?""",
            (row["conversation_id"], HISTORY_LIMIT),
        )
        history = [dict(item) for item in await cursor.fetchall()]
        history.reverse()
        return {
            "protocol_version": row["protocol_version"],
            "objective": row["objective"],
            "conversation_status": row["status"],
            "turn_policy": row["turn_policy"],
            "dialogue_round": row["dialogue_round"] or row["current_round"],
            "turn_in_round": row["turn_in_round"],
            "phase": row["phase"],
            "max_rounds": row["max_rounds"],
            "completion_pending": bool(row["completion_proposed_by_agent_id"]),
            "completion_proposed_by_agent_id": row["completion_proposed_by_agent_id"],
            "history": history,
        }
