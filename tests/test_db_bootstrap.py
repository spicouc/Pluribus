"""Regression tests for database bootstrap and schema migrations."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aiosqlite

from pluribus.config import settings
from pluribus.db import get_db, init_db


class DatabaseBootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "pluribus-test.db"
        self.settings_patch = patch.object(settings, "DB_PATH", str(self.db_path))
        self.settings_patch.start()

    async def asyncTearDown(self) -> None:
        self.settings_patch.stop()
        self.temp_dir.cleanup()

    async def test_fresh_bootstrap_creates_schema_and_working_fts_triggers(self) -> None:
        await init_db()

        async with get_db() as db:
            cursor = await db.execute("PRAGMA table_info(facts)")
            columns = {row["name"] for row in await cursor.fetchall()}
            self.assertIn("category", columns)
            self.assertIn("ttl_days", columns)
            self.assertIn("expires_at", columns)

            await db.execute("INSERT INTO facts(content) VALUES (?)", ("hola món",))
            await db.commit()
            cursor = await db.execute("SELECT id FROM facts WHERE content = ?", ("hola món",))
            fact_id = (await cursor.fetchone())["id"]

            cursor = await db.execute(
                "SELECT content FROM facts_fts WHERE fact_id = ?", (fact_id,)
            )
            self.assertEqual((await cursor.fetchone())["content"], "hola món")

            await db.execute(
                "UPDATE facts SET content = ? WHERE id = ?", ("adeu món", fact_id)
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT content FROM facts_fts WHERE fact_id = ?", (fact_id,)
            )
            self.assertEqual((await cursor.fetchone())["content"], "adeu món")

            await db.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
            await db.commit()
            cursor = await db.execute(
                "SELECT COUNT(*) AS total FROM facts_fts WHERE fact_id = ?", (fact_id,)
            )
            self.assertEqual((await cursor.fetchone())["total"], 0)

    async def test_legacy_database_is_migrated_without_losing_fts_data(self) -> None:
        async with aiosqlite.connect(str(self.db_path)) as db:
            await db.executescript("""
                CREATE TABLE agents (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    api_key_hash TEXT NOT NULL
                );

                CREATE TABLE facts (
                    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
                    scope TEXT NOT NULL DEFAULT 'shared',
                    agent_id TEXT REFERENCES agents(id),
                    key TEXT,
                    content TEXT NOT NULL,
                    metadata TEXT DEFAULT '{}',
                    version INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now')),
                    deleted_at TEXT
                );

                CREATE VIRTUAL TABLE facts_fts USING fts5(
                    fact_id UNINDEXED,
                    content,
                    scope UNINDEXED,
                    tokenize='unicode61'
                );

                CREATE TRIGGER facts_ai AFTER INSERT ON facts BEGIN
                    INSERT INTO facts_fts(fact_id, content, scope)
                    VALUES (new.id, new.content, new.scope);
                END;

                CREATE TRIGGER facts_ad AFTER DELETE ON facts BEGIN
                    INSERT INTO facts_fts(facts_fts, fact_id, content, scope)
                    VALUES ('delete', old.id, old.content, old.scope);
                END;

                CREATE TRIGGER facts_au AFTER UPDATE ON facts
                WHEN old.content != new.content BEGIN
                    INSERT INTO facts_fts(facts_fts, fact_id, content, scope)
                    VALUES ('delete', old.id, old.content, old.scope);
                    INSERT INTO facts_fts(fact_id, content, scope)
                    VALUES (new.id, new.content, new.scope);
                END;

                INSERT INTO facts(content) VALUES ('legacy fact');
            """)
            await db.commit()

        await init_db()

        async with get_db() as db:
            cursor = await db.execute("PRAGMA table_info(facts)")
            columns = {row["name"] for row in await cursor.fetchall()}
            self.assertIn("category", columns)
            self.assertIn("ttl_days", columns)
            self.assertIn("expires_at", columns)

            cursor = await db.execute(
                "SELECT id, category FROM facts WHERE content = ?", ("legacy fact",)
            )
            row = await cursor.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["category"], "events")
            fact_id = row["id"]

            cursor = await db.execute(
                "SELECT content FROM facts_fts WHERE fact_id = ?", (fact_id,)
            )
            self.assertEqual((await cursor.fetchone())["content"], "legacy fact")

            await db.execute(
                "UPDATE facts SET content = ? WHERE id = ?", ("legacy updated", fact_id)
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT content FROM facts_fts WHERE fact_id = ?", (fact_id,)
            )
            self.assertEqual((await cursor.fetchone())["content"], "legacy updated")

            await db.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
            await db.commit()
            cursor = await db.execute(
                "SELECT COUNT(*) AS total FROM facts_fts WHERE fact_id = ?", (fact_id,)
            )
            self.assertEqual((await cursor.fetchone())["total"], 0)


if __name__ == "__main__":
    unittest.main()
