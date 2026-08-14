"""Regression tests for background chunk embedding updates."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from pluribus.config import settings
from pluribus.db import get_db, init_db
from pluribus.memory import _generate_embeddings_background


class ChunkEmbeddingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "pluribus-test.db"
        self.settings_patch = patch.object(settings, "DB_PATH", str(self.db_path))
        self.settings_patch.start()
        await init_db()

    async def asyncTearDown(self) -> None:
        self.settings_patch.stop()
        self.temp_dir.cleanup()

    async def _create_fact(self) -> str:
        async with get_db() as db:
            cursor = await db.execute(
                "INSERT INTO facts (content) VALUES (?)",
                ("fact de prova",),
            )
            rowid = cursor.lastrowid
            cursor = await db.execute("SELECT id FROM facts WHERE rowid = ?", (rowid,))
            fact_id = (await cursor.fetchone())["id"]
            await db.commit()
            return fact_id

    async def test_background_updates_placeholders_without_duplicating_rows(self) -> None:
        fact_id = await self._create_fact()
        empty_blob = b"\x00" * (settings.EMBED_DIM * 4)

        async with get_db() as db:
            # Dos chunks idèntics són legítims i s'han de conservar tots dos.
            await db.execute(
                "INSERT INTO chunks (fact_id, chunk_text, embedding_blob) VALUES (?, ?, ?)",
                (fact_id, "mateix text", empty_blob),
            )
            await db.execute(
                "INSERT INTO chunks (fact_id, chunk_text, embedding_blob) VALUES (?, ?, ?)",
                (fact_id, "mateix text", empty_blob),
            )
            await db.commit()

        vector = np.ones(settings.EMBED_DIM, dtype=np.float32)
        with patch(
            "pluribus.memory.embedding_service.get_embedding",
            return_value=vector,
        ):
            await _generate_embeddings_background(
                fact_id,
                ["mateix text", "mateix text"],
            )

        async with get_db() as db:
            cursor = await db.execute(
                "SELECT embedding_blob FROM chunks WHERE fact_id = ? ORDER BY rowid",
                (fact_id,),
            )
            rows = await cursor.fetchall()

        self.assertEqual(len(rows), 2)
        expected_blob = vector.tobytes()
        self.assertTrue(all(row["embedding_blob"] == expected_blob for row in rows))

    async def test_stale_background_task_does_not_recreate_deleted_chunks(self) -> None:
        fact_id = await self._create_fact()
        empty_blob = b"\x00" * (settings.EMBED_DIM * 4)

        async with get_db() as db:
            await db.execute(
                "INSERT INTO chunks (fact_id, chunk_text, embedding_blob) VALUES (?, ?, ?)",
                (fact_id, "vell", empty_blob),
            )
            await db.commit()
            await db.execute("DELETE FROM chunks WHERE fact_id = ?", (fact_id,))
            await db.commit()

        vector = np.ones(settings.EMBED_DIM, dtype=np.float32)
        with patch(
            "pluribus.memory.embedding_service.get_embedding",
            return_value=vector,
        ):
            await _generate_embeddings_background(fact_id, ["vell"])

        async with get_db() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) AS total FROM chunks WHERE fact_id = ?",
                (fact_id,),
            )
            total = (await cursor.fetchone())["total"]

        self.assertEqual(total, 0)


if __name__ == "__main__":
    unittest.main()
