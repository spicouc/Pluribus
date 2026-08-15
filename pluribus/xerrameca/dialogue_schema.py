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
            "xerrameca_conversations",
            "turn_delay_seconds",
            "INTEGER NOT NULL DEFAULT 0",
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

        # For command-created dialogues, created_at on successor turns is the
        # not-before timestamp. The first kickoff turn is not delayed because
        # its input message is task/control rather than a result.
        await db.execute(
            """CREATE TRIGGER IF NOT EXISTS xerrameca_turn_delay_ready
               AFTER INSERT ON xerrameca_turns
               WHEN EXISTS (
                   SELECT 1
                     FROM xerrameca_conversations c
                     JOIN xerrameca_messages m ON m.id = NEW.input_message_id
                    WHERE c.id = NEW.conversation_id
                      AND c.turn_delay_seconds > 0
                      AND m.message_type = 'result'
               )
               BEGIN
                   UPDATE xerrameca_turns
                      SET created_at = strftime(
                          '%Y-%m-%dT%H:%M:%S.000000Z',
                          julianday(NEW.created_at) + (
                              SELECT c.turn_delay_seconds / 86400.0
                                FROM xerrameca_conversations c
                               WHERE c.id = NEW.conversation_id
                          )
                      )
                    WHERE id = NEW.id;
               END"""
        )
        await db.commit()
