"""Agent inbox that respects command-level inter-turn delay."""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException

from pluribus.db import get_db

from .service import _is_admin, _now, _runtime_row


async def inbox(agent: dict[str, Any]) -> dict[str, Any]:
    """Return only turns whose not-before timestamp has elapsed."""
    if not (agent.get("permissions") or {}).get("read", False) and not _is_admin(agent):
        raise HTTPException(status_code=403, detail="Xerrameca: falta permís 'read'")
    now = _now()
    async with get_db() as db:
        runtime = await _runtime_row(db)
        cursor = await db.execute(
            """SELECT t.id AS turn_id, t.conversation_id, t.round_no,
                      t.status, t.claimed_by, t.lease_until,
                      t.created_at AS ready_at,
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
                 AND t.created_at <= ?
                 AND (t.status = 'ready'
                      OR (t.status = 'claimed' AND t.lease_until <= ?))
               ORDER BY t.created_at ASC""",
            (agent["id"], agent["id"], now, now),
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
