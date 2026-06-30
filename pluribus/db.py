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
    """Aplica migracions addicionals a l'esquema existent."""
    async with get_db() as db:
        # Migració 1: ttl_days a facts
        try:
            await db.execute("ALTER TABLE facts ADD COLUMN ttl_days INTEGER DEFAULT NULL")
            print("Columna ttl_days afegida a facts")
        except Exception:
            pass  # Ja existeix
        # Migració 2: expires_at a facts
        try:
            await db.execute("ALTER TABLE facts ADD COLUMN expires_at TEXT DEFAULT NULL")
            print("Columna expires_at afegida a facts")
        except Exception:
            pass  # Ja existeix
        # Migració 3: entities table for graph traversal
        try:
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
            print("Taula entities creada per graph traversal")
        except Exception:
            pass  # Ja existeix
        # Migració 4: triples table for graph traversal
        try:
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
            print("Taula triples creada per graph traversal")
        except Exception:
            pass  # Ja existeix
        await db.commit()


async def init_db() -> None:
    """Inicialitza l'esquema de la base de dades executant init_db.sql."""
    sql_path = Path(__file__).resolve().parent.parent / "scripts" / "init_db.sql"
    if not sql_path.exists():
        raise FileNotFoundError(f"No es troba l'script SQL: {sql_path}")
    sql = sql_path.read_text(encoding="utf-8")
    async with get_db() as db:
        # Dividim en sentències separades per ';'
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        for stmt in statements:
            try:
                await db.execute(stmt)
            except Exception as exc:
                # Ignorem errors si la taula ja existeix (idempotent)
                if "already exists" not in str(exc).lower():
                    raise exc
        await db.commit()

    # Aplica migracions addicionals
    await _migrate_db()
