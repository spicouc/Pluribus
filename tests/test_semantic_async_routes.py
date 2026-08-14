"""Regression tests for async REST/MCP semantic route overrides."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import numpy as np
from starlette.requests import Request

from pluribus.config import settings
from pluribus.db import get_db, init_db
from pluribus.mcp_async import mcp_handle_async
from pluribus.semantic_async import semantic_lookup
import pluribus.main as main


def make_json_request(path: str, body: dict) -> Request:
    payload = json.dumps(body).encode("utf-8")
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": payload, "more_body": False}

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
    }
    return Request(scope, receive)


class SemanticLookupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "semantic.db"
        self.settings_patch = patch.object(settings, "DB_PATH", str(self.db_path))
        self.settings_patch.start()
        await init_db()
        async with get_db() as db:
            await db.execute(
                """INSERT INTO facts(id, scope, category, content, metadata)
                   VALUES ('f1', 'shared', 'events', 'alpha beta', '{}')"""
            )
            await db.commit()

    async def asyncTearDown(self) -> None:
        self.settings_patch.stop()
        self.temp_dir.cleanup()

    def _unit(self) -> np.ndarray:
        vec = np.zeros(settings.EMBED_DIM, dtype=np.float32)
        vec[0] = 1.0
        return vec

    async def test_zero_query_embedding_falls_back_to_fts(self) -> None:
        zero = np.zeros(settings.EMBED_DIM, dtype=np.float32)
        with patch(
            "pluribus.semantic_async.embedding_service.get_embedding_async",
            new=AsyncMock(return_value=zero),
        ):
            rows, fallback = await semantic_lookup(
                "alpha", "shared", "events", None, 5
            )

        self.assertTrue(fallback)
        self.assertEqual([row["fact_id"] for row in rows], ["f1"])
        self.assertEqual(rows[0]["score"], 0.0)

    async def test_valid_embedding_uses_semantic_path_and_skips_zero_chunks(self) -> None:
        unit = self._unit()
        zero = np.zeros(settings.EMBED_DIM, dtype=np.float32)
        async with get_db() as db:
            await db.execute(
                """INSERT INTO chunks(id, fact_id, chunk_text, embedding_blob)
                   VALUES ('c-zero', 'f1', 'placeholder', ?)""",
                (zero.tobytes(),),
            )
            await db.execute(
                """INSERT INTO chunks(id, fact_id, chunk_text, embedding_blob)
                   VALUES ('c-real', 'f1', 'semantic alpha', ?)""",
                (unit.tobytes(),),
            )
            await db.commit()

        with patch(
            "pluribus.semantic_async.embedding_service.get_embedding_async",
            new=AsyncMock(return_value=unit),
        ) as get_embedding:
            rows, fallback = await semantic_lookup(
                "meaning", "shared", "events", None, 5
            )

        self.assertFalse(fallback)
        self.assertEqual([row["content"] for row in rows], ["semantic alpha"])
        get_embedding.assert_awaited_once()


class SemanticRoutePrecedenceTests(unittest.TestCase):
    def _endpoint_index(self, endpoint_name: str) -> int:
        for index, route in enumerate(main.app.routes):
            endpoint = getattr(route, "endpoint", None)
            if getattr(endpoint, "__name__", None) == endpoint_name:
                return index
        self.fail(f"Endpoint {endpoint_name!r} no registrat")

    def test_async_rest_routes_precede_legacy_duplicates(self) -> None:
        self.assertLess(
            self._endpoint_index("semantic_search_async"),
            self._endpoint_index("semantic_search"),
        )
        self.assertLess(
            self._endpoint_index("search_memory_async"),
            self._endpoint_index("search_memory"),
        )

    def test_async_mcp_post_precedes_legacy_post(self) -> None:
        self.assertLess(
            self._endpoint_index("mcp_handle_async"),
            self._endpoint_index("mcp_handle"),
        )


class McpSemanticTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_semantic_call_uses_async_lookup(self) -> None:
        request = make_json_request(
            "/mcp/",
            {
                "method": "tools/call",
                "params": {
                    "name": "memory_search_semantic",
                    "arguments": {"query": "alpha", "scope": "shared", "top_k": 3},
                },
                "id": 7,
            },
        )
        request.state.agent = {"id": "agent-1"}
        lookup_rows = [
            {
                "fact_id": "f1",
                "content": "alpha",
                "scope": "shared",
                "category": "events",
                "agent_id": None,
                "key": None,
                "metadata": {},
                "score": 0.91,
            }
        ]
        with patch(
            "pluribus.mcp_async.semantic_lookup",
            new=AsyncMock(return_value=(lookup_rows, False)),
        ), patch(
            "pluribus.mcp_async._audit_search", new=AsyncMock()
        ):
            response = await mcp_handle_async(request)

        payload = json.loads(response.body)
        self.assertEqual(payload["id"], 7)
        self.assertFalse(payload["result"]["fallback"])
        self.assertEqual(payload["result"]["results"][0]["fact_id"], "f1")


if __name__ == "__main__":
    unittest.main()
