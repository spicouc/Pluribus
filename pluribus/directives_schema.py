"""Database schema for Pluribus Directive Control Plane v1.

D3-C adds the CANONICAL SAFE CANCEL lifecycle to the ``directives``
table: the ``'cancelled'`` status plus the ``cancelled_at`` /
``cancelled_by_agent_id`` / ``cancellation_reason`` columns.

SQLite cannot alter a CHECK constraint in place, so a database created
before D3-C is rebuilt with the canonical 12-step procedure (create a
new table, copy EVERY row, drop, rename). The rebuild preserves all
rows, ids, status values and the four original indexes — including the
partial UNIQUE index that implements idempotency semantics. The whole
migration is idempotent: it is a no-op the second time it runs.
"""

from __future__ import annotations

from pluribus.db import get_db


# --- Directives table: canonical (D3-C) shape ----------------------------
# Used for FRESH databases via CREATE TABLE IF NOT EXISTS. Databases that
# already have the pre-D3-C CHECK constraint are rebuilt by
# ``_migrate_directives_table`` (the IF NOT EXISTS clause leaves them
# untouched, which is exactly why the rebuild below is required).
_DIRECTIVES_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS directives (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    issuer_agent_id TEXT NOT NULL,
    target_agent_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    action TEXT NOT NULL,
    arguments TEXT NOT NULL DEFAULT '{}',
    required_capability TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','claimed','completed','failed','rejected','expired','cancelled')),
    idempotency_key TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    claimed_at TEXT,
    claimed_by_agent_id TEXT,
    lease_until TEXT,
    completed_at TEXT,
    result TEXT,
    error TEXT,
    cancelled_at TEXT,
    cancelled_by_agent_id TEXT,
    cancellation_reason TEXT
);
"""

# Same columns/CHECK as above, but with the transient table name used by
# the 12-step rebuild. Kept as a separate literal on purpose: it must NOT
# contain ``IF NOT EXISTS`` (a stale ``directives_migrated`` would
# otherwise silently absorb the row copy).
_DIRECTIVES_MIGRATED_CREATE_SQL = """
CREATE TABLE directives_migrated (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    issuer_agent_id TEXT NOT NULL,
    target_agent_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    action TEXT NOT NULL,
    arguments TEXT NOT NULL DEFAULT '{}',
    required_capability TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','claimed','completed','failed','rejected','expired','cancelled')),
    idempotency_key TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    claimed_at TEXT,
    claimed_by_agent_id TEXT,
    lease_until TEXT,
    completed_at TEXT,
    result TEXT,
    error TEXT,
    cancelled_at TEXT,
    cancelled_by_agent_id TEXT,
    cancellation_reason TEXT
);
"""

# The EXACT four indexes of the pre-D3-C schema, recreated identically
# after a rebuild. The partial UNIQUE index on (issuer_agent_id,
# idempotency_key) is the storage-level guarantee of idempotency: it must
# survive the table reconstruction untouched.
_DIRECTIVES_INDEX_SQL = """
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

# Additive column guards: covers the (unlikely) case of a table that
# already accepts 'cancelled' in its CHECK but predates the three new
# columns.
_DIRECTIVES_COLUMN_GUARDS: tuple[tuple[str, str], ...] = (
    ("cancelled_at", "ALTER TABLE directives ADD COLUMN cancelled_at TEXT"),
    ("cancelled_by_agent_id", "ALTER TABLE directives ADD COLUMN cancelled_by_agent_id TEXT"),
    ("cancellation_reason", "ALTER TABLE directives ADD COLUMN cancellation_reason TEXT"),
)


async def _migrate_directives_table(db) -> None:
    """Rebuild ``directives`` when it predates the 'cancelled' status.

    Idempotent detection: the stored ``sqlite_master.sql`` of the table
    is inspected for the literal ``cancelled``. If it is already there
    the CHECK constraint is current and NOTHING is done. A second guard
    (``PRAGMA table_info``) then adds any missing additive column.

    The row copy is atomic: exactly one transaction, ids/status/scores
    copied verbatim, new columns left NULL, and the four indexes are
    recreated afterwards by ``_DIRECTIVES_INDEX_SQL``. No other table
    has a foreign key pointing at ``directives``, so dropping and
    renaming cannot cascade.
    """
    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='directives'"
    )
    row = await cursor.fetchone()
    table_sql = (row[0] if row else "") or ""
    if not table_sql:
        # No table at all (should not happen: the caller creates it
        # first). Nothing to preserve, nothing to migrate.
        return

    if "cancelled" not in table_sql:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute(_DIRECTIVES_MIGRATED_CREATE_SQL)
            await db.execute(
                """
                INSERT INTO directives_migrated (
                    id, issuer_agent_id, target_agent_id, scope, action, arguments,
                    required_capability, status, idempotency_key, created_at, expires_at,
                    claimed_at, claimed_by_agent_id, lease_until, completed_at, result, error
                ) SELECT id, issuer_agent_id, target_agent_id, scope, action, arguments,
                         required_capability, status, idempotency_key, created_at, expires_at,
                         claimed_at, claimed_by_agent_id, lease_until, completed_at, result, error
                    FROM directives
                """
            )
            await db.execute("DROP TABLE directives")
            await db.execute("ALTER TABLE directives_migrated RENAME TO directives")
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    cursor = await db.execute("PRAGMA table_info(directives)")
    existing_columns = {r["name"] for r in await cursor.fetchall()}
    for column, sql in _DIRECTIVES_COLUMN_GUARDS:
        if column not in existing_columns:
            await db.execute(sql)
            existing_columns.add(column)


async def init_directives_db() -> None:
    """Create directive/grant tables idempotently (D3-C canonical schema)."""
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
            """
            + _DIRECTIVES_CREATE_SQL
        )
        await _migrate_directives_table(db)
        await db.executescript(_DIRECTIVES_INDEX_SQL)
        await db.commit()
