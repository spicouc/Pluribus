"""Regression tests for fail-safe database archival and compaction."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pluribus.compact import compact_database
from pluribus.config import settings
from pluribus.db import get_db, init_db
from scripts.compact import get_db_size


class CompactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "custom.db"
        self.settings_patch = patch.object(settings, "DB_PATH", str(self.db_path))
        self.settings_patch.start()
        await init_db()

    async def asyncTearDown(self) -> None:
        self.settings_patch.stop()
        self.temp_dir.cleanup()

    async def _seed(self) -> None:
        async with get_db() as db:
            await db.execute(
                """INSERT INTO facts(id, scope, category, content, deleted_at)
                   VALUES ('old', 'shared', 'events', 'archive me', datetime('now', '-40 days'))"""
            )
            await db.execute(
                """INSERT INTO facts(id, scope, category, content, deleted_at)
                   VALUES ('recent', 'shared', 'events', 'keep me', datetime('now', '-5 days'))"""
            )
            await db.execute(
                """INSERT INTO facts(id, scope, category, content)
                   VALUES ('active', 'shared', 'events', 'active')"""
            )
            await db.execute(
                """INSERT INTO chunks(id, fact_id, chunk_text, embedding_blob)
                   VALUES ('old-chunk', 'old', 'archive me', zeroblob(?))""",
                (settings.EMBED_DIM * 4,),
            )
            await db.commit()

    async def test_wrapper_uses_configured_database_and_archives_before_delete(self) -> None:
        await self._seed()
        archive_path = self.root / "archive.db"
        result = compact_database(archive_path=str(archive_path))

        self.assertEqual(result["db_path"], str(self.db_path))
        self.assertEqual(result["archive_path"], str(archive_path))
        self.assertEqual(result["archived_facts"], 1)
        self.assertTrue(result["vacuum_done"])

        async with get_db() as db:
            cursor = await db.execute("SELECT id FROM facts ORDER BY id")
            remaining = [row["id"] for row in await cursor.fetchall()]
            cursor = await db.execute("SELECT COUNT(*) AS n FROM chunks WHERE fact_id = 'old'")
            old_chunks = (await cursor.fetchone())["n"]
        self.assertEqual(remaining, ["active", "recent"])
        self.assertEqual(old_chunks, 0)

        archive = sqlite3.connect(archive_path)
        try:
            row = archive.execute(
                "SELECT content FROM archived_facts WHERE id = 'old'"
            ).fetchone()
        finally:
            archive.close()
        self.assertEqual(row[0], "archive me")

    async def test_archive_failure_does_not_delete_primary_fact(self) -> None:
        await self._seed()
        bad_archive = self.root / "not-a-db"
        bad_archive.mkdir()

        with self.assertRaises(sqlite3.Error):
            compact_database(archive_path=str(bad_archive))

        async with get_db() as db:
            cursor = await db.execute("SELECT COUNT(*) AS n FROM facts WHERE id = 'old'")
            self.assertEqual((await cursor.fetchone())["n"], 1)

    def test_size_includes_wal_and_shm_sidecars(self) -> None:
        base = self.root / "size.db"
        base.write_bytes(b"a" * 10)
        Path(f"{base}-wal").write_bytes(b"b" * 20)
        Path(f"{base}-shm").write_bytes(b"c" * 30)
        self.assertEqual(get_db_size(str(base)), 60)

    async def test_invalid_retention_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            compact_database(retention_days=0)


if __name__ == "__main__":
    unittest.main()
