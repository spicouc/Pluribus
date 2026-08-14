"""Gestor de base de dades SQLite amb aiosqlite i suport per FTS5."""

import aiosqlite
from contextlib import asynccontextmanager
from pathlib import Path

from pluribus.config import settings


@asynccontextmanager
async def get_db() -> aiosqlite.Connection:
    """Obté una connexió a SQLite dins d'un context manager asíncron."""
    db_path = Path(settings.DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA busy_timeout=5000")
    await conn.execute("PRAGMA cache_size=-4000")
    try:
        yield conn
    finally:
        await conn.close()


async def _migrate_db() -> None:
    """Aplica migracions idempotents; qualsevol error inesperat es propaga."""
    async with get_db() as db:
        cursor = await db.execute("PRAGMA table_info(facts)")
        fact_columns = {row["name"] for row in await cursor.fetchall()}

        if "category" not in fact_columns:
            await db.execute(
                "ALTER TABLE facts ADD COLUMN category TEXT NOT NULL DEFAULT 'events'"
            )
        if "ttl_days" not in fact_columns:
            await db.execute("ALTER TABLE facts ADD COLUMN ttl_days INTEGER DEFAULT NULL")
        if "expires_at" not in fact_columns:
            await db.execute("ALTER TABLE facts ADD COLUMN expires_at TEXT DEFAULT NULL")

        await db.execute("CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category)")

        # API-key lookup migration. Existing agents remain NULL until they
        # authenticate once with their legacy key or are rotated/recreated.
        cursor = await db.execute("PRAGMA table_info(agents)")
        agent_columns = {row["name"] for row in await cursor.fetchall()}
        if "api_key_fingerprint" not in agent_columns:
            await db.execute("ALTER TABLE agents ADD COLUMN api_key_fingerprint TEXT")
        await db.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_api_key_fingerprint
               ON agents(api_key_fingerprint)
               WHERE api_key_fingerprint IS NOT NULL"""
        )

        await db.executescript("""
            DROP TRIGGER IF EXISTS facts_ai;
            DROP TRIGGER IF EXISTS facts_ad;
            DROP TRIGGER IF EXISTS facts_au;

            CREATE TRIGGER facts_ai AFTER INSERT ON facts BEGIN
                INSERT INTO facts_fts(fact_id, content, scope)
                VALUES (new.id, new.content, new.scope);
            END;

            CREATE TRIGGER facts_ad AFTER DELETE ON facts BEGIN
                DELETE FROM facts_fts WHERE fact_id = old.id;
            END;

            CREATE TRIGGER facts_au AFTER UPDATE ON facts
            WHEN old.content != new.content BEGIN
                DELETE FROM facts_fts WHERE fact_id = old.id;
                INSERT INTO facts_fts(fact_id, content, scope)
                VALUES (new.id, new.content, new.scope);
            END;
        """)

        cursor = await db.execute(
            "SELECT COUNT(*) AS total FROM facts WHERE deleted_at IS NULL"
        )
        active_fact_count = (await cursor.fetchone())["total"]
        cursor = await db.execute("SELECT COUNT(*) AS total FROM facts_fts")
        fts_fact_count = (await cursor.fetchone())["total"]

        if active_fact_count != fts_fact_count:
            await db.execute("DELETE FROM facts_fts")
            await db.execute("""
                INSERT INTO facts_fts(fact_id, content, scope)
                SELECT id, content, scope
                FROM facts
                WHERE deleted_at IS NULL
            """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS entities (
                id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
                name TEXT NOT NULL,
                type TEXT DEFAULT '',
                aliases TEXT DEFAULT '[]',
                description TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                deleted_at TEXT
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type)")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS triples (
                id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
                subject_id TEXT NOT NULL REFERENCES entities(id),
                predicate TEXT NOT NULL,
                object_id TEXT NOT NULL REFERENCES entities(id),
                confidence REAL DEFAULT 1.0,
                source_agent_id TEXT,
                metadata TEXT DEFAULT '{}',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                expires_at TEXT,
                deleted_at TEXT
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_triples_subject ON triples(subject_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_triples_object ON triples(object_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_triples_predicate ON triples(predicate)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_triples_deleted ON triples(deleted_at)")

        await db.commit()


async def init_db() -> None:
    """Inicialitza l'esquema de la base de dades executant init_db.sql."""
    sql_path = Path(__file__).resolve().parent.parent / "scripts" / "init_db.sql"
    if not sql_path.exists():
        raise FileNotFoundError(f"No es troba l'script SQL: {sql_path}")

    sql = sql_path.read_text(encoding="utf-8")
    async with get_db() as db:
        await db.executescript(sql)
        await db.commit()

    await _migrate_db()
