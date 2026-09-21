"""Focused tests for scope persistence on entities and triples.

Verifies:
  - Fresh-schema: entities and triples created by init_db contain a
    ``scope TEXT NOT NULL DEFAULT 'shared'`` column and the expected
    ``idx_entities_scope`` / ``idx_triples_scope`` indexes.
  - Legacy migration: pre-existing entities/triples tables that lack the
    ``scope`` column are upgraded idempotently with a deterministic
    ``'shared'`` value on every row.
  - Idempotency: running init_db twice does not error and preserves rows.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aiosqlite

from pluribus.config import settings
from pluribus.db import get_db, init_db


class EntitiesTriplesScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "pluribus-scope-test.db"
        self.settings_patch = patch.object(settings, "DB_PATH", str(self.db_path))
        self.settings_patch.start()

    async def asyncTearDown(self) -> None:
        self.settings_patch.stop()
        self.temp_dir.cleanup()

    def _column_names(self, columns_rows) -> set[str]:
        return {row["name"] for row in columns_rows}

    def _index_names(self, index_rows) -> set[str]:
        return {row["name"] for row in index_rows}

    # ── Fresh schema ──────────────────────────────────────────────────

    async def test_fresh_schema_has_scope_column_on_entities_and_triples(self) -> None:
        await init_db()

        async with get_db() as db:
            cursor = await db.execute("PRAGMA table_info(entities)")
            entity_cols = self._column_names(await cursor.fetchall())
            self.assertIn("scope", entity_cols)

            cursor = await db.execute("PRAGMA table_info(triples)")
            triple_cols = self._column_names(await cursor.fetchall())
            self.assertIn("scope", triple_cols)

    async def test_fresh_schema_has_scope_indexes(self) -> None:
        await init_db()

        async with get_db() as db:
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='entities'"
            )
            entity_indexes = self._index_names(await cursor.fetchall())
            self.assertIn("idx_entities_scope", entity_indexes)

            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='triples'"
            )
            triple_indexes = self._index_names(await cursor.fetchall())
            self.assertIn("idx_triples_scope", triple_indexes)

    async def test_fresh_schema_scope_defaults_to_shared(self) -> None:
        await init_db()

        async with get_db() as db:
            await db.execute(
                "INSERT INTO entities (id, name) VALUES ('e1', 'Alice')"
            )
            await db.execute(
                "INSERT INTO entities (id, name) VALUES ('e2', 'Bob')"
            )
            await db.execute(
                "INSERT INTO triples (id, subject_id, predicate, object_id) "
                "VALUES ('t1', 'e1', 'knows', 'e2')"
            )
            await db.commit()

            cursor = await db.execute(
                "SELECT scope FROM entities WHERE id = 'e1'"
            )
            self.assertEqual((await cursor.fetchone())["scope"], "shared")

            cursor = await db.execute(
                "SELECT scope FROM triples WHERE id = 't1'"
            )
            self.assertEqual((await cursor.fetchone())["scope"], "shared")

    async def test_fresh_schema_scope_is_overridable(self) -> None:
        await init_db()

        async with get_db() as db:
            await db.execute(
                "INSERT INTO entities (id, name, scope) VALUES ('e1', 'Alice', 'private')"
            )
            await db.commit()

            cursor = await db.execute(
                "SELECT scope FROM entities WHERE id = 'e1'"
            )
            self.assertEqual((await cursor.fetchone())["scope"], "private")

    # ── Legacy migration ───────────────────────────────────────────────

    async def test_legacy_tables_gain_scope_deterministic_default(self) -> None:
        """Pre-existing entities/triples without scope must be migrated."""
        async with aiosqlite.connect(str(self.db_path)) as db:
            await db.executescript(
                """
                CREATE TABLE entities (
                    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
                    name TEXT NOT NULL,
                    type TEXT DEFAULT '',
                    aliases TEXT DEFAULT '[]',
                    description TEXT DEFAULT '',
                    metadata TEXT DEFAULT '{}'
                );
                CREATE TABLE triples (
                    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
                    subject_id TEXT NOT NULL REFERENCES entities(id),
                    predicate TEXT NOT NULL,
                    object_id TEXT NOT NULL REFERENCES entities(id),
                    confidence REAL DEFAULT 1.0,
                    source_agent_id TEXT,
                    metadata TEXT DEFAULT '{}'
                );
                INSERT INTO entities (id, name) VALUES ('e1', 'Alice');
                INSERT INTO entities (id, name) VALUES ('e2', 'Bob');
                INSERT INTO triples (id, subject_id, predicate, object_id)
                VALUES ('t1', 'e1', 'knows', 'e2');
                """
            )
            await db.commit()

        # init_db must migrate without error
        await init_db()

        async with get_db() as db:
            cursor = await db.execute("PRAGMA table_info(entities)")
            cols = self._column_names(await cursor.fetchall())
            self.assertIn("scope", cols)

            cursor = await db.execute("PRAGMA table_info(triples)")
            cols = self._column_names(await cursor.fetchall())
            self.assertIn("scope", cols)

            # All rows get deterministic default
            cursor = await db.execute("SELECT id, scope FROM entities ORDER BY id")
            rows = await cursor.fetchall()
            self.assertEqual(len(rows), 2)
            for row in rows:
                self.assertEqual(row["scope"], "shared")

            cursor = await db.execute("SELECT id, scope FROM triples ORDER BY id")
            rows = await cursor.fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["scope"], "shared")

    async def test_legacy_indexes_created_on_upgrade(self) -> None:
        async with aiosqlite.connect(str(self.db_path)) as db:
            await db.executescript(
                """
                CREATE TABLE entities (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    type TEXT DEFAULT ''
                );
                CREATE TABLE triples (
                    id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    object_id TEXT NOT NULL
                );
                """
            )
            await db.commit()

        await init_db()

        async with get_db() as db:
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='entities'"
            )
            self.assertIn("idx_entities_scope", self._index_names(await cursor.fetchall()))

            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='triples'"
            )
            self.assertIn("idx_triples_scope", self._index_names(await cursor.fetchall()))

    async def test_migrated_data_is_queryable_via_scope_filter(self) -> None:
        """After migration, scoped queries return expected rows."""
        async with aiosqlite.connect(str(self.db_path)) as db:
            await db.executescript(
                """
                CREATE TABLE entities (
                                id TEXT PRIMARY KEY,
                                name TEXT NOT NULL,
                                scope TEXT NOT NULL DEFAULT 'shared'
                            );
                            INSERT INTO entities (id, name, scope) VALUES ('e-shared', 'Shared', 'shared');
                            INSERT INTO entities (id, name, scope) VALUES ('e-private', 'Private', 'private');
                """
            )
            await db.commit()

        await init_db()

        async with get_db() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) AS n FROM entities WHERE scope = 'shared'"
            )
            self.assertEqual((await cursor.fetchone())["n"], 1)

            cursor = await db.execute(
                "SELECT COUNT(*) AS n FROM entities WHERE scope = 'private'"
            )
            self.assertEqual((await cursor.fetchone())["n"], 1)

    # ── Idempotency ───────────────────────────────────────────────────

    async def test_idempotent_run_preserves_rows(self) -> None:
        await init_db()

        async with get_db() as db:
            await db.execute(
                "INSERT INTO entities (id, name, scope) VALUES ('e1', 'Alice', 'shared')"
            )
            await db.commit()

        # Second init_db must not error
        await init_db()

        async with get_db() as db:
            cursor = await db.execute("SELECT COUNT(*) AS n FROM entities")
            self.assertEqual((await cursor.fetchone())["n"], 1)

            cursor = await db.execute("PRAGMA quick_check")
            self.assertEqual((await cursor.fetchone())[0], "ok")


if __name__ == "__main__":
    unittest.main()
