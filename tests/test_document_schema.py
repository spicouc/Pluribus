"""Regression + migration test for the document library schema (L0)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aiosqlite

from pluribus.config import settings
from pluribus.db import get_db, init_db


class DocumentLibrarySchemaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "pluribus-test.db"
        self.settings_patch = patch.object(settings, "DB_PATH", str(self.db_path))
        self.settings_patch.start()

    async def asyncTearDown(self) -> None:
        self.settings_patch.stop()
        self.temp_dir.cleanup()

    async def assert_tables_exist(self, db, tables) -> None:
        cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        names = {row["name"] for row in await cursor.fetchall()}
        cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='virtual'")
        names |= {row["name"] for row in await cursor.fetchall()}
        for table in tables:
            self.assertIn(table, names, f"missing table {table}")

    async def test_fresh_bootstrap_creates_document_schema(self) -> None:
        await init_db()
        async with get_db() as db:
            await self.assert_tables_exist(
                db,
                {
                    "documents",
                    "document_versions",
                    "document_chunks",
                    "documents_fts",
                    "document_vector_index_state",
                    "document_fact_provenance",
                },
            )
            # Documents do NOT share the facts table
            cursor = await db.execute("PRAGMA table_info(facts)")
            fact_cols = {row["name"] for row in await cursor.fetchall()}
            self.assertNotIn("document_id", fact_cols)
            self.assertNotIn("content_original", fact_cols)

            # The document vector generation counter exists and is initialised.
            cursor = await db.execute(
                "SELECT generation FROM document_vector_index_state WHERE singleton = 1"
            )
            self.assertEqual((await cursor.fetchone())["generation"], 0)

            # quick_check is clean after a fresh bootstrap
            cursor = await db.execute("PRAGMA quick_check")
            self.assertEqual((await cursor.fetchone())[0], "ok")

    async def test_document_schema_does_not_break_facts_semantics(self) -> None:
        """Writing a document row must not create/link any fact or chunk."""
        await init_db()
        async with get_db() as db:
            await db.execute(
                """INSERT INTO documents (id, title, scope) VALUES ('doc1', 'Tit', 'shared')"""
            )
            await db.execute(
                """INSERT INTO document_versions
                   (id, document_id, version, title, content, content_hash)
                   VALUES ('v1', 'doc1', 1, 'Tit', '# Hello', 'abc')"""
            )
            await db.execute(
                """INSERT INTO document_chunks
                   (id, version_id, document_id, chunk_index, section, chunk_text)
                   VALUES ('c1', 'v1', 'doc1', 0, 'Hello', 'Hello world')"""
            )
            await db.commit()

            cursor = await db.execute("SELECT COUNT(*) AS total FROM facts")
            self.assertEqual((await cursor.fetchone())["total"], 0)
            cursor = await db.execute("SELECT COUNT(*) AS total FROM facts_fts")
            self.assertEqual((await cursor.fetchone())["total"], 0)
            cursor = await db.execute("SELECT COUNT(*) AS total FROM chunks")
            self.assertEqual((await cursor.fetchone())["total"], 0)
            # But the document vector generation advanced (our own counter),
            # because inserting a document_chunk bumps the docvec generation.
            cursor = await db.execute(
                "SELECT generation FROM document_vector_index_state WHERE singleton = 1"
            )
            self.assertGreaterEqual((await cursor.fetchone())["generation"], 1)

    async def test_facts_vector_index_state_untouched_by_documents(self) -> None:
        """The *facts* generation counter must NOT advance when only documents
        are inserted. This proves the DocumentVectorIndex is decoupled from the
        Fact VectorIndex."""
        await init_db()
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT generation FROM vector_index_state WHERE singleton = 1"
            )
            before = (await cursor.fetchone())["generation"]

            await db.execute(
                """INSERT INTO documents (id, title) VALUES ('doc1', 'Tit')"""
            )
            await db.execute(
                """INSERT INTO document_versions
                   (id, document_id, version, title, content, content_hash)
                   VALUES ('v1', 'doc1', 1, 'Tit', 'body', 'h')"""
            )
            await db.execute(
                """INSERT INTO document_chunks
                   (id, version_id, document_id, chunk_index, chunk_text)
                   VALUES ('c1', 'v1', 'doc1', 0, 'body')"""
            )
            await db.commit()

            cursor = await db.execute(
                "SELECT generation FROM vector_index_state WHERE singleton = 1"
            )
            after = (await cursor.fetchone())["generation"]
            self.assertEqual(before, after, "facts vector_index_state must not advance")

    async def test_legacy_db_migrates_and_second_init_is_idempotent(self) -> None:
        """"Migration test" for L0: an existing pre-document DB gains the new
        tables without losing facts/FTS data, and a second init is a no-op."""
        # Build a legacy DB that predates the document library
        async with aiosqlite.connect(str(self.db_path)) as db:
            await db.executescript("""
                CREATE TABLE agents (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, api_key_hash TEXT NOT NULL
                );
                CREATE TABLE facts (
                    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
                    scope TEXT NOT NULL DEFAULT 'shared', agent_id TEXT,
                    category TEXT NOT NULL DEFAULT 'events', key TEXT,
                    content TEXT NOT NULL, metadata TEXT DEFAULT '{}',
                    version INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now')), deleted_at TEXT
                );
                CREATE VIRTUAL TABLE facts_fts USING fts5(
                    fact_id UNINDEXED, content, scope UNINDEXED,
                    tokenize='unicode61 categories ''L* N*'''
                );
                CREATE TABLE chunks (
                    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
                    fact_id TEXT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
                    chunk_text TEXT NOT NULL, embedding_blob BLOB,
                    metadata TEXT DEFAULT '{}',
                    created_at TEXT DEFAULT (datetime('now'))
                );
                INSERT INTO facts(content) VALUES ('legacy fact');
            """)
            await db.commit()

        await init_db()

        async with get_db() as db:
            await self.assert_tables_exist(
                db,
                {
                    "documents",
                    "document_versions",
                    "document_chunks",
                    "documents_fts",
                    "document_vector_index_state",
                    "document_fact_provenance",
                },
            )
            # facts + facts_fts intact
            cursor = await db.execute(
                "SELECT id FROM facts WHERE content = ?", ("legacy fact",)
            )
            fact_id = (await cursor.fetchone())["id"]
            cursor = await db.execute(
                "SELECT content FROM facts_fts WHERE fact_id = ?", (fact_id,)
            )
            self.assertEqual((await cursor.fetchone())["content"], "legacy fact")
            cursor = await db.execute("SELECT COUNT(*) AS total FROM chunks")
            self.assertEqual((await cursor.fetchone())["total"], 0)

        # Second init must be clean and idempotent
        await init_db()
        async with get_db() as db:
            cursor = await db.execute("PRAGMA quick_check")
            self.assertEqual((await cursor.fetchone())[0], "ok")


if __name__ == "__main__":
    unittest.main()
