"""Controls administratius segurs de Xerrameca."""

from __future__ import annotations

from typing import Any

from pluribus.db import get_db

from .models import ParticipantUpdate, XerramecaSystemUpdate
from .service import (
    pause_conversation,
    update_participant,
    update_system_state,
)


async def update_system_state_safe(
    agent: dict[str, Any], body: XerramecaSystemUpdate
) -> dict[str, Any]:
    """Actualitza runtime i revoca leases quan es desactiva globalment."""
    result = await update_system_state(agent, body)
    if body.enabled is False:
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """UPDATE xerrameca_turns
                   SET status = 'ready', claimed_by = NULL, lease_token = NULL,
                       claimed_at = NULL, lease_until = NULL
                   WHERE status = 'claimed'
                     AND conversation_id IN (
                         SELECT id FROM xerrameca_conversations
                         WHERE status = 'active'
                     )"""
            )
            await db.commit()
    return result


async def update_participant_safe(
    agent: dict[str, Any],
    conversation_id: str,
    participant_agent_id: str,
    body: ParticipantUpdate,
) -> dict[str, Any]:
    """Desactivar qualsevol participant pausa sempre una conversa activa."""
    result = await update_participant(
        agent, conversation_id, participant_agent_id, body
    )
    if body.enabled is False and result["status"] == "active":
        result = await pause_conversation(
            agent, conversation_id, "participant_disabled"
        )
    return result
