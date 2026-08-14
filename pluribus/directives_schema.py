"""Database schema for Pluribus Directive Control Plane v1."""

from __future__ import annotations

from pluribus.db import get_db


async def init_directives_db() -> None:
    """Create directive/grant tables idempotently."""
    async with get_db() as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS directive_grants (
                agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                capability TEXT NOT NULL,
                can_execute INTEGER NOT NULL DEFAULT 0 CHECK (can_execute IN (0, 1)),
                can_delegate INTEGER NOT NULL DEFAULT 0 CHECK (can_delegate IN (0, 1)),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (agent_id, capability)
            );

            CREATE INDEX IF NOT EXISTS idx_directive_grants_capability
                ON directive_grants(capability);

            CREATE TABLE IF NOT EXISTS directives (
                id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
                issuer_agent_id TEXT NOT NULL,
                target_agent_id TEXT NOT NULL,
                scope TEXT NOT NULL,
                action TEXT NOT NULL,
                arguments TEXT NOT NULL DEFAULT '{}',
                required_capability TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','claimed','completed','failed','rejected','expired')),
                idempotency_key TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                expires_at TEXT NOT NULL,
                claimed_at TEXT,
                claimed_by_agent_id TEXT,
                lease_until TEXT,
                completed_at TEXT,
                result TEXT,
                error TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_directives_target_status
                ON directives(target_agent_id, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_directives_issuer
                ON directives(issuer_agent_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_directives_scope
                ON directives(scope, created_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_directives_idempotency
                ON directives(issuer_agent_id, idempotency_key)
                WHERE idempotency_key IS NOT NULL;
            """
        )
        await db.commit()
