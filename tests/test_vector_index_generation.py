"""Regression tests for generation-based TurboVec invalidation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from pluribus.config import settings
from pluribus.db import get_db, init_db
from pluribus.vector_index import VectorIndex


class VectorIndexGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "vectors.db"
        self.settings_patch = patch.object(settings, "DB_PATH", str(self.db_path))
        self.settings_patch.start()
        await init_db()
        self.index = VectorIndex()

    async def asyncTearDown(self) -> None:
        self.settings_patch.stop()
        self.temp_dir.cleanup()

    def _vector(self, coordinate: int = 0) -> np.ndarray:
        vec = np.zeros(settings.EMBED_DIM, dtype=np.float32)
        vec[coordinate] = 1.0
        return vec

    async def _insert_fact_chunk(
        self,
        fact_id: str,
        chunk_id: str,
        vector: np.ndarray,
        category: str = "events",
    ) -> None:
        async with get_db() as db:
            await db.execute(
                """INSERT INTO facts(id, scope, category, content)
                   VALUES (?, 'shared', ?, ?)""",
                (fact_id, category, fact_id),
            )
            await db.execute(
                """INSERT INTO chunks(id, fact_id, chunk_text, embedding_blob)
                   VALUES (?, ?, ?, ?)""",
                (chunk_id, fact_id, chunk_id, vector.tobytes()),
            )
            await db.commit()

    async def _generation(self) -> int:
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT generation FROM vector_index_state WHERE singleton = 1"
            )
            row = await cursor.fetchone()
            return int(row["generation"])

    async def test_zero_placeholder_is_excluded_then_embedding_update_rebuilds(self) -> None:
        zero = np.zeros(settings.EMBED_DIM, dtype=np.float32)
        await self._insert_fact_chunk("f1", "c1", zero)

        self.assertTrue(await self.index.rebuild())
        stats_before = await self.index.get_stats()
        self.assertEqual(stats_before["size"], 0)
        generation_before = stats_before["generation"]

        real_vector = self._vector(0)
        async with get_db() as db:
            await db.execute(
                "UPDATE chunks SET embedding_blob = ? WHERE id = 'c1'",
                (real_vector.tobytes(),),
            )
            await db.commit()

        self.assertGreater(await self._generation(), generation_before)
        results = await self.index.search(real_vector, scope_filter="shared", top_k=5)
        self.assertEqual([item[0] for item in results], ["c1"])

    async def test_new_chunk_invalidates_loaded_snapshot(self) -> None:
        v0 = self._vector(0)
        v1 = self._vector(1)
        await self._insert_fact_chunk("f1", "c1", v0)
        self.assertTrue(await self.index.rebuild())
        first_generation = (await self.index.get_stats())["generation"]

        await self._insert_fact_chunk("f2", "c2", v1)
        results = await self.index.search(v1, scope_filter="shared", top_k=5)
        self.assertIn("c2", [item[0] for item in results])
        self.assertGreater((await self.index.get_stats())["generation"], first_generation)

    async def test_fact_filter_metadata_change_invalidates_index(self) -> None:
        vec = self._vector(0)
        await self._insert_fact_chunk("f1", "c1", vec, category="events")
        self.assertTrue(await self.index.rebuild())
        self.assertEqual(
            [r[0] for r in await self.index.search(vec, category_filter="events")],
            ["c1"],
        )

        async with get_db() as db:
            await db.execute("UPDATE facts SET category = 'profile' WHERE id = 'f1'")
            await db.commit()

        self.assertEqual(await self.index.search(vec, category_filter="events"), [])
        self.assertEqual(
            [r[0] for r in await self.index.search(vec, category_filter="profile")],
            ["c1"],
        )


if __name__ == "__main__":
    unittest.main()
