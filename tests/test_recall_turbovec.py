"""Regression coverage for TurboVec-backed Recall v2."""

from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import numpy as np

from pluribus.config import settings
from pluribus.db import get_db, init_db
from pluribus.recall import _semantic_candidates
from pluribus.vector_index import VectorIndex


class VectorIndexMultiScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "multi-scope.db"
        self.settings_patch = patch.object(settings, "DB_PATH", str(self.db_path))
        self.settings_patch.start()
        await init_db()
        self.index = VectorIndex()

    async def asyncTearDown(self) -> None:
        self.settings_patch.stop()
        self.temp_dir.cleanup()

    def _unit(self, coordinate: int = 0) -> np.ndarray:
        vec = np.zeros(settings.EMBED_DIM, dtype=np.float32)
        vec[coordinate] = 1.0
        return vec

    async def _insert(
        self,
        fact_id: str,
        chunk_id: str,
        scope: str,
        category: str,
        vector: np.ndarray,
    ) -> None:
        async with get_db() as db:
            await db.execute(
                """INSERT INTO facts(id, scope, category, content)
                   VALUES (?, ?, ?, ?)""",
                (fact_id, scope, category, fact_id),
            )
            await db.execute(
                """INSERT INTO chunks(id, fact_id, chunk_text, embedding_blob)
                   VALUES (?, ?, ?, ?)""",
                (chunk_id, fact_id, chunk_id, vector.tobytes()),
            )
            await db.commit()

    async def test_multi_scope_allowlist_excludes_other_scopes(self) -> None:
        vec = self._unit()
        await self._insert("f-shared", "c-shared", "shared", "events", vec)
        await self._insert("f-private", "c-private", "private", "events", vec)
        await self._insert("f-secret", "c-secret", "secret", "events", vec)

        results = await self.index.search(
            vec,
            scope_filters=["shared", "private"],
            top_k=10,
        )
        ids = {chunk_id for chunk_id, _ in results}
        self.assertEqual(ids, {"c-shared", "c-private"})

    async def test_multi_scope_and_category_filters_intersect(self) -> None:
        vec = self._unit()
        await self._insert("f-event", "c-event", "shared", "events", vec)
        await self._insert("f-profile", "c-profile", "private", "profile", vec)

        results = await self.index.search(
            vec,
            scope_filters=["shared", "private"],
            category_filter="profile",
            top_k=10,
        )
        self.assertEqual([chunk_id for chunk_id, _ in results], ["c-profile"])

    async def test_single_and_multi_scope_filters_fail_closed_on_disagreement(self) -> None:
        vec = self._unit()
        await self._insert("f1", "c1", "shared", "events", vec)
        results = await self.index.search(
            vec,
            scope_filter="shared",
            scope_filters=["private"],
            top_k=10,
        )
        self.assertEqual(results, [])

    async def test_index_rebuilds_when_database_path_changes_even_same_generation(self) -> None:
        vec = self._unit()
        await self._insert("f-first", "c-first", "shared", "events", vec)
        self.assertEqual(
            {chunk_id for chunk_id, _ in await self.index.search(vec, top_k=10)},
            {"c-first"},
        )

        second_dir = tempfile.TemporaryDirectory()
        try:
            second_path = Path(second_dir.name) / "second.db"
            with patch.object(settings, "DB_PATH", str(second_path)):
                await init_db()
                async with get_db() as db:
                    await db.execute(
                        """INSERT INTO facts(id, scope, category, content)
                           VALUES ('f-second', 'shared', 'events', 'second')"""
                    )
                    await db.execute(
                        """INSERT INTO chunks(id, fact_id, chunk_text, embedding_blob)
                           VALUES ('c-second', 'f-second', 'second', ?)""",
                        (vec.tobytes(),),
                    )
                    await db.commit()
                results = await self.index.search(vec, top_k=10)
                self.assertEqual({chunk_id for chunk_id, _ in results}, {"c-second"})
        finally:
            second_dir.cleanup()


class RecallTurboVecTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "recall-ann.db"
        self.settings_patch = patch.object(settings, "DB_PATH", str(self.db_path))
        self.settings_patch.start()
        await init_db()
        async with get_db() as db:
            facts = [
                ("f-shared", "shared", "events", "shared fact"),
                ("f-private", "private", "profile", "private profile"),
                ("f-secret", "secret", "events", "secret fact"),
                ("f-second", "shared", "events", "second shared"),
            ]
            for fact_id, scope, category, content in facts:
                await db.execute(
                    """INSERT INTO facts(id, scope, category, content)
                       VALUES (?, ?, ?, ?)""",
                    (fact_id, scope, category, content),
                )
            chunks = [
                ("c-shared-a", "f-shared", "shared snippet A"),
                ("c-shared-b", "f-shared", "shared snippet B"),
                ("c-private", "f-private", "private snippet"),
                ("c-secret", "f-secret", "secret snippet"),
                ("c-second", "f-second", "second snippet"),
            ]
            for chunk_id, fact_id, text in chunks:
                await db.execute(
                    "INSERT INTO chunks(id, fact_id, chunk_text) VALUES (?, ?, ?)",
                    (chunk_id, fact_id, text),
                )
            await db.commit()

    async def asyncTearDown(self) -> None:
        self.settings_patch.stop()
        self.temp_dir.cleanup()

    def _unit(self) -> np.ndarray:
        vec = np.zeros(settings.EMBED_DIM, dtype=np.float32)
        vec[0] = 1.0
        return vec

    async def test_recall_uses_ann_and_sql_rechecks_scopes(self) -> None:
        search = AsyncMock(
            return_value=[
                ("c-secret", 0.99),
                ("c-shared-a", 0.95),
                ("c-private", 0.90),
                ("c-second", 0.85),
            ]
        )
        with patch(
            "pluribus.recall.embedding_service.get_embedding_async",
            new=AsyncMock(return_value=self._unit()),
        ), patch(
            "pluribus.recall.vector_index.ensure_loaded",
            new=AsyncMock(return_value=True),
        ), patch(
            "pluribus.recall.vector_index.get_stats",
            new=AsyncMock(return_value={"size": 5}),
        ), patch(
            "pluribus.recall.vector_index.search",
            new=search,
        ):
            candidates, available = await _semantic_candidates(
                "meaning",
                ["shared", "private"],
                None,
                10,
            )

        self.assertTrue(available)
        self.assertNotIn("f-secret", candidates)
        self.assertEqual(set(candidates), {"f-shared", "f-private", "f-second"})
        self.assertEqual(search.await_args.kwargs["scope_filters"], ["shared", "private"])

    async def test_recall_rechecks_category_after_ann(self) -> None:
        with patch(
            "pluribus.recall.embedding_service.get_embedding_async",
            new=AsyncMock(return_value=self._unit()),
        ), patch(
            "pluribus.recall.vector_index.ensure_loaded",
            new=AsyncMock(return_value=True),
        ), patch(
            "pluribus.recall.vector_index.get_stats",
            new=AsyncMock(return_value={"size": 5}),
        ), patch(
            "pluribus.recall.vector_index.search",
            new=AsyncMock(return_value=[("c-shared-a", 0.95), ("c-private", 0.90)]),
        ):
            candidates, available = await _semantic_candidates(
                "meaning",
                ["shared", "private"],
                "profile",
                10,
            )
        self.assertTrue(available)
        self.assertEqual(set(candidates), {"f-private"})

    async def test_multiple_chunks_of_same_fact_are_deduplicated(self) -> None:
        with patch(
            "pluribus.recall.embedding_service.get_embedding_async",
            new=AsyncMock(return_value=self._unit()),
        ), patch(
            "pluribus.recall.vector_index.ensure_loaded",
            new=AsyncMock(return_value=True),
        ), patch(
            "pluribus.recall.vector_index.get_stats",
            new=AsyncMock(return_value={"size": 5}),
        ), patch(
            "pluribus.recall.vector_index.search",
            new=AsyncMock(
                return_value=[
                    ("c-shared-a", 0.95),
                    ("c-shared-b", 0.94),
                    ("c-second", 0.90),
                ]
            ),
        ):
            candidates, _ = await _semantic_candidates(
                "meaning", ["shared"], None, 10
            )
        self.assertEqual(list(candidates), ["f-shared", "f-second"])
        self.assertEqual(candidates["f-shared"][0], 1)
        self.assertEqual(candidates["f-second"][0], 2)

    async def test_unavailable_index_preserves_fts_fallback_signal(self) -> None:
        with patch(
            "pluribus.recall.embedding_service.get_embedding_async",
            new=AsyncMock(return_value=self._unit()),
        ), patch(
            "pluribus.recall.vector_index.ensure_loaded",
            new=AsyncMock(return_value=False),
        ):
            candidates, available = await _semantic_candidates(
                "meaning", ["shared"], None, 10
            )
        self.assertEqual(candidates, {})
        self.assertFalse(available)

    def test_semantic_candidate_code_does_not_scan_embedding_blobs(self) -> None:
        source = inspect.getsource(_semantic_candidates)
        self.assertNotIn("embedding_blob", source)
        self.assertIn("vector_index.search", source)


if __name__ == "__main__":
    unittest.main()
