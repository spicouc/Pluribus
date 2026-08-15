"""Idempotent schema extensions for Xerrameca Dialogue Protocol v1."""

from __future__ import annotations

from pluribus.db import get_db


async def _columns(db, table: str) -> set[str]:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    return {row["name"] for row in await cursor.fetchall()}


async def _add_column(db, table: str, name: str, ddl: str) -> None:
    if name not in await _columns(db, table):
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


async def init_xerrameca_dialogue_db() -> None:
    """Add protocol metadata without changing semantics of existing conversations."""
    async with get_db() as db:
        await _add_column(
            db,
            "xerrameca_conversations",
            "protocol_version",
            "TEXT NOT NULL DEFAULT 'legacy-v0'",
        )
        await _add_column(
            db,
            "xerrameca_conversations",
            "completion_proposed_by_agent_id",
            "TEXT REFERENCES agents(id) ON DELETE SET NULL",
        )
        await _add_column(
            db,
            "xerrameca_conversations",
            "completion_proposed_at",
            "TEXT",
        )
        await _add_column(
            db,
            "xerrameca_conversations",
            "completion_proposal_turn_id",
            "TEXT",
        )
        await _add_column(
            db,
            "xerrameca_turns",
            "dialogue_round",
            "INTEGER",
        )
        await _add_column(
            db,
            "xerrameca_turns",
            "turn_in_round",
            "INTEGER",
        )
        await _add_column(
            db,
            "xerrameca_turns",
            "phase",
            "TEXT NOT NULL DEFAULT 'dialogue'",
        )

        # Existing turns remain legacy and are mapped 1:1 for observability only.
        await db.execute(
            """UPDATE xerrameca_turns
               SET dialogue_round = COALESCE(dialogue_round, round_no),
                   turn_in_round = COALESCE(turn_in_round, 1)
               WHERE dialogue_round IS NULL OR turn_in_round IS NULL"""
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_xerrameca_turns_dialogue_round "
            "ON xerrameca_turns(conversation_id, dialogue_round, turn_in_round)"
        )
        await db.commit()
