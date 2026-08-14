"""Regressions for third-wave scope, Notion, backup and agent hardening."""

from __future__ import annotations

import gzip
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import numpy as np
from fastapi import HTTPException

from pluribus import agents, notion
from pluribus.backup import backup_database
from pluribus.config import settings
from pluribus.contradiction import _check_contradictions_impl
from pluribus.db import get_db, init_db
from pluribus.worker import compute_semantic_relations


class TemporaryPluribusDb(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "pluribus.db"
        self.db_patch = patch.object(settings, "DB_PATH", str(self.db_path))
        self.dim_patch = patch.object(settings, "EMBED_DIM", 4)
        self.db_patch.start()
        self.dim_patch.start()
        await init_db()

    async def asyncTearDown(self) -> None:
        self.dim_patch.stop()
        self.db_patch.stop()
        self.temp_dir.cleanup()

    async def _insert_fact(self, fact_id: str, scope: str, content: str = "fact") -> None:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO facts(id, scope, category, content) VALUES (?, ?, 'events', ?)",
                (fact_id, scope, content),
            )
            await db.commit()

    async def _insert_chunk(self, chunk_id: str, fact_id: str, vector: np.ndarray) -> None:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO chunks(id, fact_id, chunk_text, embedding_blob) VALUES (?, ?, ?, ?)",
                (chunk_id, fact_id, f"chunk-{chunk_id}", vector.astype(np.float32).tobytes()),
            )
            await db.commit()


class GraphScopeTests(TemporaryPluribusDb):
    async def test_worker_never_creates_cross_scope_relations(self) -> None:
        vector = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        await self._insert_fact("alpha-1", "alpha")
        await self._insert_fact("beta-1", "beta")
        await self._insert_chunk("c-alpha-1", "alpha-1", vector)
        await self._insert_chunk("c-beta-1", "beta-1", vector)

        async with get_db() as db:
            first = await compute_semantic_relations(db)
            count = (await (await db.execute("SELECT COUNT(*) AS n FROM fact_relations")).fetchone())["n"]
        self.assertEqual(first["relations_created"], 0)
        self.assertEqual(count, 0)

        await self._insert_fact("alpha-2", "alpha")
        await self._insert_chunk("c-alpha-2", "alpha-2", vector)
        async with get_db() as db:
            second = await compute_semantic_relations(db)
            rows = await (await db.execute(
                "SELECT source_fact_id, target_fact_id FROM fact_relations"
            )).fetchall()
        self.assertEqual(second["relations_created"], 1)
        self.assertEqual(
            {frozenset((row["source_fact_id"], row["target_fact_id"])) for row in rows},
            {frozenset(("alpha-1", "alpha-2"))},
        )

    async def test_contradiction_search_and_sql_candidates_are_scope_bound(self) -> None:
        vector = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        await self._insert_fact("alpha-fact", "alpha", "service is enabled")
        await self._insert_fact("beta-fact", "beta", "service is not enabled")
        await self._insert_chunk("beta-chunk", "beta-fact", vector)

        with patch(
            "pluribus.embedding.embedding_service.get_embedding_async",
            new=AsyncMock(return_value=vector),
        ), patch(
            "pluribus.vector_index.vector_index.search",
            new=AsyncMock(return_value=[("beta-chunk", 0.95)]),
        ) as search:
            await _check_contradictions_impl(
                "alpha-fact", "service is enabled", "agent-1"
            )

        search.assert_awaited_once()
        self.assertEqual(search.await_args.kwargs["scope_filter"], "alpha")
        async with get_db() as db:
            count = (await (await db.execute("SELECT COUNT(*) AS n FROM fact_relations")).fetchone())["n"]
        self.assertEqual(count, 0)


class NotionTests(TemporaryPluribusDb):
    async def test_text_search_uses_named_rows_and_returns_cached_page(self) -> None:
        async with get_db() as db:
            await db.execute(
                """INSERT INTO notion_cache(id, title, markdown, url)
                   VALUES ('p1', 'Page one', 'hello from notion', 'https://notion.so/p1')"""
            )
            await db.commit()

        zero = np.zeros(settings.EMBED_DIM, dtype=np.float32)
        with patch(
            "pluribus.embedding.embedding_service.get_embedding_async",
            new=AsyncMock(return_value=zero),
        ):
            rows = await notion.search_notion("hello", top_k=5)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "p1")
        self.assertEqual(rows[0]["title"], "Page one")

    async def test_link_and_context_paths_work_with_row_factory(self) -> None:
        await self._insert_fact("fact-1", "shared", "notion related fact")
        async with get_db() as db:
            await db.execute(
                """INSERT INTO notion_cache(id, title, markdown, url)
                   VALUES ('p1', 'Page one', 'body', 'https://notion.so/p1')"""
            )
            await db.commit()

        with patch(
            "pluribus.notion.search_notion",
            new=AsyncMock(return_value=[{
                "id": "p1", "title": "Page one", "markdown": "body",
                "url": "https://notion.so/p1", "score": 0.9,
            }]),
        ):
            created = await notion.link_fact_to_notion("fact-1")

        self.assertEqual(created, 1)
        context = await notion.get_notion_context("fact-1")
        self.assertEqual(context[0]["id"], "p1")
        self.assertAlmostEqual(context[0]["relevance"], 0.9)


class AgentInventoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_admin_cannot_list_global_agent_inventory(self) -> None:
        request = SimpleNamespace(
            state=SimpleNamespace(
                agent={"id": "a1", "permissions": {"read": True, "admin": False}}
            )
        )
        with self.assertRaises(HTTPException) as ctx:
            await agents.list_agents(request)
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_non_admin_cannot_read_another_agent_record(self) -> None:
        request = SimpleNamespace(
            state=SimpleNamespace(
                agent={"id": "a1", "permissions": {"read": True, "admin": False}}
            )
        )
        with self.assertRaises(HTTPException) as ctx:
            await agents.get_agent(request, "a2")
        self.assertEqual(ctx.exception.status_code, 403)


class BackupTests(unittest.TestCase):
    def test_backup_captures_committed_wal_data_and_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "live.db"
            backups = root / "backups"
            writer = sqlite3.connect(source)
            try:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, value TEXT)")
                writer.execute("INSERT INTO items(value) VALUES ('committed-in-wal')")
                writer.commit()

                result = backup_database(str(source), str(backups), retention_days=14)
                archive = Path(result.path)
                self.assertTrue(archive.is_file())
                self.assertEqual(archive.stat().st_mode & 0o777, 0o600)

                restored = root / "restored.db"
                with gzip.open(archive, "rb") as source_gz, restored.open("wb") as out:
                    out.write(source_gz.read())
                with sqlite3.connect(restored) as db:
                    row = db.execute("SELECT value FROM items").fetchone()
                self.assertEqual(row[0], "committed-in-wal")
            finally:
                writer.close()

    def test_shell_wrapper_does_not_vacuum_or_copy_turbovec(self) -> None:
        root = Path(__file__).resolve().parent.parent
        script = (root / "scripts" / "backup.sh").read_text(encoding="utf-8")
        self.assertNotIn("VACUUM", script)
        self.assertNotIn("turbovec", script.lower())
        self.assertIn("python\" -m pluribus.backup", script)


if __name__ == "__main__":
    unittest.main()
