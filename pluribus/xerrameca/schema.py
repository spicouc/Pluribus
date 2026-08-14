"""Esquema idempotent de Xerrameca v1."""

from __future__ import annotations

from pluribus.db import get_db


SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS xerrameca_runtime (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    default_max_rounds INTEGER NOT NULL DEFAULT 20 CHECK (default_max_rounds BETWEEN 1 AND 200),
    default_turn_timeout_seconds INTEGER NOT NULL DEFAULT 300 CHECK (default_turn_timeout_seconds BETWEEN 10 AND 86400),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT OR IGNORE INTO xerrameca_runtime
(singleton, enabled, default_max_rounds, default_turn_timeout_seconds)
VALUES (1, 1, 20, 300);

CREATE TABLE IF NOT EXISTS xerrameca_conversations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    objective TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'shared',
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','active','paused','blocked','completed','cancelled','error')),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    turn_policy TEXT NOT NULL DEFAULT 'alternating'
        CHECK (turn_policy IN ('alternating','supervisor')),
    supervisor_agent_id TEXT REFERENCES agents(id),
    first_agent_id TEXT NOT NULL REFERENCES agents(id),
    max_rounds INTEGER NOT NULL DEFAULT 20 CHECK (max_rounds BETWEEN 1 AND 200),
    turn_timeout_seconds INTEGER NOT NULL DEFAULT 300 CHECK (turn_timeout_seconds BETWEEN 10 AND 86400),
    current_round INTEGER NOT NULL DEFAULT 0 CHECK (current_round >= 0),
    current_turn_id TEXT,
    block_reason TEXT,
    persist_summary INTEGER NOT NULL DEFAULT 1 CHECK (persist_summary IN (0, 1)),
    summary_fact_id TEXT REFERENCES facts(id) ON DELETE SET NULL,
    created_by_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_xerrameca_conversations_status
    ON xerrameca_conversations(status);
CREATE INDEX IF NOT EXISTS idx_xerrameca_conversations_scope
    ON xerrameca_conversations(scope);

CREATE TABLE IF NOT EXISTS xerrameca_participants (
    conversation_id TEXT NOT NULL REFERENCES xerrameca_conversations(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE RESTRICT,
    role TEXT NOT NULL DEFAULT 'participant'
        CHECK (role IN ('participant','supervisor')),
    position INTEGER NOT NULL CHECK (position IN (0, 1)),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    PRIMARY KEY (conversation_id, agent_id),
    UNIQUE (conversation_id, position)
);
CREATE INDEX IF NOT EXISTS idx_xerrameca_participants_agent
    ON xerrameca_participants(agent_id, enabled);

CREATE TABLE IF NOT EXISTS xerrameca_turns (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES xerrameca_conversations(id) ON DELETE CASCADE,
    round_no INTEGER NOT NULL CHECK (round_no >= 1),
    assigned_agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE RESTRICT,
    input_message_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready'
        CHECK (status IN ('ready','claimed','completed','skipped','cancelled')),
    claimed_by TEXT REFERENCES agents(id) ON DELETE SET NULL,
    lease_token TEXT,
    claimed_at TEXT,
    lease_until TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (conversation_id, round_no)
);
CREATE INDEX IF NOT EXISTS idx_xerrameca_turns_inbox
    ON xerrameca_turns(assigned_agent_id, status, lease_until);
CREATE INDEX IF NOT EXISTS idx_xerrameca_turns_conversation
    ON xerrameca_turns(conversation_id, round_no);

CREATE TABLE IF NOT EXISTS xerrameca_messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES xerrameca_conversations(id) ON DELETE CASCADE,
    turn_id TEXT,
    round_no INTEGER NOT NULL CHECK (round_no >= 0),
    from_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    to_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    message_type TEXT NOT NULL
        CHECK (message_type IN ('task','message','question','answer','result','error','control')),
    content TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    turn_result TEXT
        CHECK (turn_result IS NULL OR turn_result IN ('continue','complete','blocked','needs_human','error')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_xerrameca_messages_conversation
    ON xerrameca_messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_xerrameca_messages_recipient
    ON xerrameca_messages(to_agent_id, created_at);
"""


async def init_xerrameca_db() -> None:
    """Crea/migra l'esquema Xerrameca de forma idempotent."""
    async with get_db() as db:
        await db.executescript(SCHEMA_SQL)
        await db.commit()
