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
    """Aplica migracions idempotents a l'esquema existent.

    Qualsevol error inesperat es propaga perquè el servei no arrenqui amb un
    esquema parcial o inconsistent.
    """
    async with get_db() as db:
        cursor = await db.execute("PRAGMA table_info(facts)")
        fact_columns = {row["name"] for row in await cursor.fetchall()}

        # Migració 1: category a facts
        if "category" not in fact_columns:
            await db.execute(
                "ALTER TABLE facts ADD COLUMN category TEXT NOT NULL DEFAULT 'events'"
            )
            print("Columna category afegida a facts")

        # Migració 2: ttl_days a facts
        if "ttl_days" not in fact_columns:
            await db.execute("ALTER TABLE facts ADD COLUMN ttl_days INTEGER DEFAULT NULL")
            print("Columna ttl_days afegida a facts")

        # Migració 3: expires_at a facts
        if "expires_at" not in fact_columns:
            await db.execute("ALTER TABLE facts ADD COLUMN expires_at TEXT DEFAULT NULL")
            print("Columna expires_at afegida a facts")

        # L'índex s'ha de crear després de migrar category, perquè una BD antiga
        # encara no té aquesta columna quan s'executa init_db.sql.
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category)"
        )

        # Repara definicions antigues dels triggers FTS5. facts_fts és una taula
        # FTS5 normal, així que les baixes s'han de fer amb DELETE FROM.
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

        # Si una inicialització antiga va quedar a mitges, facts pot contenir
        # dades anteriors a la creació de facts_fts. Reindexem només les que
        # encara no hi són, de forma idempotent.
        await db.execute("""
            INSERT INTO facts_fts(fact_id, content, scope)
            SELECT f.id, f.content, f.scope
            FROM facts AS f
            WHERE f.deleted_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM facts_fts AS ft WHERE ft.fact_id = f.id
              )
        """)

        # Migració 4: entities table for graph traversal
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

        # Migració 5: triples table for graph traversal
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
        # executescript preserva blocs BEGIN ... END dels triggers. Fer split(';')
        # corromp aquests blocs i pot deixar la base de dades a mig inicialitzar.
        await db.executescript(sql)
        await db.commit()

    # Aplica migracions addicionals. Els errors es propaguen (fail-fast).
    await _migrate_db()
