"""Regression tests for the semantic-search caller contract."""

from __future__ import annotations

import inspect
import unittest

import numpy as np

from pluribus.embedding import EmbeddingService


class SemanticSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = EmbeddingService()

    def test_semantic_search_returns_iterable_results_not_coroutine(self) -> None:
        query = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        chunks = [
            ("exact", np.array([1.0, 0.0, 0.0], dtype=np.float32)),
            ("partial", np.array([0.8, 0.6, 0.0], dtype=np.float32)),
            ("other", np.array([0.0, 1.0, 0.0], dtype=np.float32)),
        ]

        results = self.service.semantic_search(query, chunks, 2)

        self.assertFalse(inspect.isawaitable(results))
        self.assertEqual([chunk_id for chunk_id, _ in results], ["exact", "partial"])
        self.assertAlmostEqual(results[0][1], 1.0, places=6)
        self.assertAlmostEqual(results[1][1], 0.8, places=6)

    def test_semantic_search_respects_top_k_and_empty_input(self) -> None:
        query = np.array([1.0, 0.0], dtype=np.float32)
        chunks = [
            ("a", np.array([1.0, 0.0], dtype=np.float32)),
            ("b", np.array([0.5, 0.5], dtype=np.float32)),
        ]

        self.assertEqual(len(self.service.semantic_search(query, chunks, 1)), 1)
        self.assertEqual(self.service.semantic_search(query, [], 5), [])

    def test_turbovec_api_is_explicitly_async(self) -> None:
        self.assertTrue(inspect.iscoroutinefunction(self.service.semantic_search_index))


if __name__ == "__main__":
    unittest.main()
