"""
Compactació programada de la base de dades.

Executa VACUUM i arxiva facts marcats com deleted_at > 30 dies
a un fitxer SQLite separat.

Funció sincrona (sqlite3) per evitar conflictes amb aiosqlite.
Des de FastAPI, cridar amb: await asyncio.to_thread(compact_database)
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("pluribus.compact")

DB_PATH = "/opt/pluribus/data/pluribus.db"
ARCHIVE_PATH = "/opt/pluribus/data/archive.db"


def get_db_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def compact_database(db_path: str = DB_PATH, archive_path: str = ARCHIVE_PATH) -> dict:
    """Executa VACUUM i arxiva facts antics (SINCRON).

    Retorna dict amb estadistiques.
    Nota: Funcio sincrona. Cridar amb asyncio.to_thread() desde FastAPI.
    """
    result = {
        "archived_facts": 0,
        "space_before": 0,
        "space_after": 0,
        "space_saved": 0,
        "vacuum_done": False,
    }

    db_path = str(db_path)
    archive_path = str(archive_path)
    result["space_before"] = get_db_size(db_path)

    # Arxivar facts deleted > 30 dies
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")

        cursor = conn.execute("""
            SELECT id, scope, category, agent_id, key, content, metadata,
                   version, created_at, updated_at, deleted_at,
                   ttl_days, expires_at
            FROM facts
            WHERE deleted_at IS NOT NULL
              AND deleted_at < datetime('now', '-30 days')
        """)
        old_facts = cursor.fetchall()

        if old_facts:
            arch_conn = sqlite3.connect(archive_path)
            arch_conn.execute("""
                CREATE TABLE IF NOT EXISTS archived_facts (
                    id TEXT PRIMARY KEY,
                    scope TEXT, category TEXT, agent_id TEXT, key TEXT,
                    content TEXT, metadata TEXT, version INTEGER,
                    created_at TEXT, updated_at TEXT, deleted_at TEXT,
                    ttl_days INTEGER, expires_at TEXT,
                    archived_at TEXT DEFAULT (datetime('now'))
                )
            """)

            for fact in old_facts:
                arch_conn.execute(
                    """INSERT OR IGNORE INTO archived_facts
                       (id, scope, category, agent_id, key, content, metadata,
                        version, created_at, updated_at, deleted_at, ttl_days, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (fact["id"], fact["scope"], fact["category"],
                     fact["agent_id"], fact["key"], fact["content"],
                     fact["metadata"], fact["version"], fact["created_at"],
                     fact["updated_at"], fact["deleted_at"],
                     fact["ttl_days"], fact["expires_at"]),
                )
            arch_conn.commit()
            arch_conn.close()

            ids = [f["id"] for f in old_facts]
            placeholders = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM chunks WHERE fact_id IN ({placeholders})", ids)
            conn.execute(f"DELETE FROM facts WHERE id IN ({placeholders})", ids)
            conn.commit()

            result["archived_facts"] = len(old_facts)
            logger.info("Arxivats %d facts a %s", len(old_facts), archive_path)

        conn.close()
    except Exception as exc:
        logger.error("Error arxivant facts: %s", exc)

    # VACUUM
    try:
        conn2 = sqlite3.connect(db_path)
        conn2.execute("PRAGMA journal_mode=WAL")
        conn2.execute("VACUUM")
        conn2.close()
        result["vacuum_done"] = True
        logger.info("VACUUM completat a %s", db_path)
    except Exception as exc:
        logger.error("Error en VACUUM: %s", exc)

    result["space_after"] = get_db_size(db_path)
    result["space_saved"] = result["space_before"] - result["space_after"]

    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = compact_database()
    print(json.dumps(result, indent=2))
