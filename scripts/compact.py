"""Safe, idempotent SQLite archival and compaction for Pluribus."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger("pluribus.compact")

_DEFAULT_DB_PATH = "/opt/pluribus/data/pluribus.db"


def configured_db_path() -> str:
    """Resolve the current DB path without hardcoding it into function defaults."""
    return os.environ.get("PLURIBUS_DB_PATH", _DEFAULT_DB_PATH)


def default_archive_path(db_path: str) -> str:
    return str(Path(db_path).with_name("archive.db"))


def get_db_size(path: str) -> int:
    """Return SQLite footprint including WAL and shared-memory sidecars."""
    total = 0
    for candidate in (path, f"{path}-wal", f"{path}-shm"):
        try:
            total += os.path.getsize(candidate)
        except OSError:
            pass
    return total


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _ensure_archive_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS archived_facts (
            id TEXT PRIMARY KEY,
            scope TEXT,
            category TEXT,
            agent_id TEXT,
            key TEXT,
            content TEXT,
            metadata TEXT,
            version INTEGER,
            created_at TEXT,
            updated_at TEXT,
            deleted_at TEXT,
            ttl_days INTEGER,
            expires_at TEXT,
            archived_at TEXT DEFAULT (datetime('now'))
        )"""
    )


def _archive_rows(archive_path: str, rows: list[sqlite3.Row]) -> None:
    """Durably archive every selected row before the primary DB is modified."""
    archive_parent = Path(archive_path).parent
    archive_parent.mkdir(parents=True, exist_ok=True)
    archive = _connect(archive_path)
    try:
        _ensure_archive_schema(archive)
        archive.execute("BEGIN IMMEDIATE")
        archive.executemany(
            """INSERT INTO archived_facts
               (id, scope, category, agent_id, key, content, metadata,
                version, created_at, updated_at, deleted_at, ttl_days, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   scope=excluded.scope,
                   category=excluded.category,
                   agent_id=excluded.agent_id,
                   key=excluded.key,
                   content=excluded.content,
                   metadata=excluded.metadata,
                   version=excluded.version,
                   created_at=excluded.created_at,
                   updated_at=excluded.updated_at,
                   deleted_at=excluded.deleted_at,
                   ttl_days=excluded.ttl_days,
                   expires_at=excluded.expires_at,
                   archived_at=datetime('now')""",
            [
                (
                    row["id"],
                    row["scope"],
                    row["category"],
                    row["agent_id"],
                    row["key"],
                    row["content"],
                    row["metadata"],
                    row["version"],
                    row["created_at"],
                    row["updated_at"],
                    row["deleted_at"],
                    row["ttl_days"],
                    row["expires_at"],
                )
                for row in rows
            ],
        )
        ids = [row["id"] for row in rows]
        placeholders = ",".join("?" for _ in ids)
        archived_count = archive.execute(
            f"SELECT COUNT(*) FROM archived_facts WHERE id IN ({placeholders})",
            ids,
        ).fetchone()[0]
        if archived_count != len(ids):
            raise RuntimeError(
                f"Archive verification failed: expected {len(ids)}, found {archived_count}"
            )
        archive.commit()
    except Exception:
        archive.rollback()
        raise
    finally:
        archive.close()


def compact_database(
    db_path: str | None = None,
    archive_path: str | None = None,
    retention_days: int = 30,
) -> dict[str, Any]:
    """Archive old soft-deleted facts and VACUUM the configured DB.

    Critical failures propagate. Archival is completed and verified before any
    fact is physically deleted, making retries idempotent and preventing data
    loss if deletion or VACUUM later fails.
    """
    if retention_days < 1:
        raise ValueError("retention_days must be >= 1")

    db_path = str(db_path or configured_db_path())
    archive_path = str(archive_path or default_archive_path(db_path))
    if Path(db_path).resolve() == Path(archive_path).resolve():
        raise ValueError("archive_path must differ from db_path")
    if not Path(db_path).exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    result: dict[str, Any] = {
        "db_path": db_path,
        "archive_path": archive_path,
        "retention_days": retention_days,
        "archived_facts": 0,
        "space_before": get_db_size(db_path),
        "space_after": 0,
        "space_saved": 0,
        "vacuum_done": False,
    }

    conn = _connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """SELECT id, scope, category, agent_id, key, content, metadata,
                      version, created_at, updated_at, deleted_at,
                      ttl_days, expires_at
               FROM facts
               WHERE deleted_at IS NOT NULL
                 AND deleted_at < datetime('now', ?)
               ORDER BY deleted_at ASC""",
            (f"-{retention_days} days",),
        ).fetchall()

        if rows:
            # Keep the primary write lock while the archive copy is committed.
            # If archiving fails, rollback leaves all primary rows untouched.
            _archive_rows(archive_path, rows)
            ids = [row["id"] for row in rows]
            placeholders = ",".join("?" for _ in ids)
            conn.execute(f"DELETE FROM facts WHERE id IN ({placeholders})", ids)
            remaining = conn.execute(
                f"SELECT COUNT(*) FROM facts WHERE id IN ({placeholders})", ids
            ).fetchone()[0]
            if remaining != 0:
                raise RuntimeError("Primary deletion verification failed")
            result["archived_facts"] = len(rows)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # VACUUM must run outside an explicit transaction. Failure is critical and
    # is intentionally propagated to callers/monitoring.
    vacuum_conn = _connect(db_path)
    try:
        vacuum_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        vacuum_conn.execute("VACUUM")
        vacuum_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        result["vacuum_done"] = True
    finally:
        vacuum_conn.close()

    result["space_after"] = get_db_size(db_path)
    result["space_saved"] = result["space_before"] - result["space_after"]
    logger.info(
        "Compaction complete: archived=%d before=%d after=%d",
        result["archived_facts"],
        result["space_before"],
        result["space_after"],
    )
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(compact_database(), indent=2))
