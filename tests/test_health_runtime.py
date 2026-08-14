"""Regression tests for health probes and embedding readiness."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from pluribus.config import settings
from pluribus.embedding import EmbeddingService
import pluribus.main as main


class EmbeddingReadinessTests(unittest.IsolatedAsyncioTestCase):
    def test_is_ready_property_never_performs_network_io(self) -> None:
        service = EmbeddingService()
        with patch("pluribus.embedding.requests.get") as get:
            self.assertFalse(service.is_ready)
        get.assert_not_called()

    async def test_explicit_check_ready_uses_check_function(self) -> None:
        service = EmbeddingService()
        with patch.object(service, "_check_ollama", return_value=True) as check:
            self.assertTrue(await service.check_ready(force=True))
        check.assert_called_once_with(True)

    def test_semantic_search_rejects_zero_query_and_zero_chunks(self) -> None:
        service = EmbeddingService()
        zero = np.zeros(3, dtype=np.float32)
        unit = np.array([1.0, 0.0, 0.0], dtype=np.float32)

        self.assertEqual(service.semantic_search(zero, [("a", unit)], 5), [])
        self.assertEqual(service.semantic_search(unit, [("zero", zero)], 5), [])


class HealthEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_sqlite_probe_executes_real_query(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(temp_dir.name) / "health.db"
        try:
            with patch.object(settings, "DB_PATH", str(db_path)):
                self.assertTrue(await main._sqlite_is_healthy())
        finally:
            temp_dir.cleanup()

    async def test_health_returns_503_when_sqlite_fails(self) -> None:
        with patch.object(main, "_sqlite_is_healthy", return_value=False), patch.object(
            main.embedding_service, "check_ready", return_value=True
        ):
            response = await main.health()

        self.assertEqual(response.status_code, 503)
        payload = json.loads(response.body)
        self.assertEqual(payload["status"], "error")
        self.assertFalse(payload["sqlite"])

    async def test_health_is_degraded_when_only_ollama_is_unavailable(self) -> None:
        with patch.object(main, "_sqlite_is_healthy", return_value=True), patch.object(
            main.embedding_service, "check_ready", return_value=False
        ):
            response = await main.health()

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.body)
        self.assertEqual(payload["status"], "degraded")
        self.assertTrue(payload["sqlite"])
        self.assertFalse(payload["embedding_ready"])


if __name__ == "__main__":
    unittest.main()
