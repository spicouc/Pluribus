"""Regression coverage for Recall v2."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

from pluribus.config import settings
from pluribus.db import get_db, init_db
from pluribus.mcp_async import ALL_TOOLS, mcp_handle_async
from pluribus.recall import RecallRequest, RecallResponse, recall_service


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


class RecallServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "recall.db"
        self.settings_patch = patch.object(settings, "DB_PATH", str(self.db_path))
        self.settings_patch.start()
        await init_db()

        self.agent = {
            "id": "reader-1",
            "permissions": {"read": True, "write": False, "delete": False, "admin": False},
            "allowed_scopes": ["shared"],
        }
        async with get_db() as db:
            await db.execute(
                """INSERT INTO agents(id, name, api_key_hash, permissions, allowed_scopes)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    self.agent["id"],
                    "reader",
                    "unused-test-hash",
                    json.dumps(self.agent["permissions"]),
                    json.dumps(self.agent["allowed_scopes"]),
                ),
            )
            await db.execute(
                """INSERT INTO facts(id, scope, category, content, metadata)
                   VALUES ('f-event', 'shared', 'events', 'alpha deployment happened',
                           '{"importance":0.4}')"""
            )
            await db.execute(
                """INSERT INTO facts(id, scope, category, content, metadata)
                   VALUES ('f-pref', 'shared', 'preferences', 'alpha prefers concise answers',
                           '{"importance":0.9,"confidence":0.8}')"""
            )
            await db.execute(
                """INSERT INTO facts(id, scope, category, content, metadata)
                   VALUES ('f-secret', 'private', 'events', 'alpha secret from another scope', '{}')"""
            )
            await db.commit()

    async def asyncTearDown(self) -> None:
        self.settings_patch.stop()
        self.temp_dir.cleanup()

    async def test_general_recall_searches_all_categories_but_only_allowed_scopes(self) -> None:
        with patch(
            "pluribus.recall.embedding_service.get_embedding_async",
            new=AsyncMock(side_effect=RuntimeError("embedding unavailable")),
        ):
            response = await recall_service(
                self.agent,
                RecallRequest(query="alpha", limit=10),
            )

        ids = {item.fact_id for item in response.results}
        self.assertEqual(response.scopes, ["shared"])
        self.assertIn("f-event", ids)
        self.assertIn("f-pref", ids)
        self.assertNotIn("f-secret", ids)
        self.assertEqual({item.category for item in response.results}, {"events", "preferences"})
        self.assertFalse(response.semantic_available)

    async def test_category_filter_is_optional_and_precise(self) -> None:
        with patch(
            "pluribus.recall.embedding_service.get_embedding_async",
            new=AsyncMock(side_effect=RuntimeError("embedding unavailable")),
        ):
            response = await recall_service(
                self.agent,
                RecallRequest(query="alpha", category="preferences", limit=10),
            )

        self.assertEqual([item.fact_id for item in response.results], ["f-pref"])
        self.assertEqual(response.results[0].content, "alpha prefers concise answers")

    async def test_direct_service_call_rejects_disallowed_scope(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            await recall_service(
                self.agent,
                RecallRequest(query="alpha", scope="private"),
            )
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_direct_service_call_rejects_agent_without_read_permission(self) -> None:
        blocked = {
            "id": "blocked",
            "permissions": {"read": False, "admin": False},
            "allowed_scopes": ["shared"],
        }
        with self.assertRaises(HTTPException) as ctx:
            await recall_service(blocked, RecallRequest(query="alpha"))
        self.assertEqual(ctx.exception.status_code, 403)


class RecallMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_catalogue_exposes_memory_recall(self) -> None:
        self.assertIn("memory_recall", {tool["name"] for tool in ALL_TOOLS})

    async def test_mcp_recall_uses_authenticated_agent(self) -> None:
        request = make_json_request(
            "/mcp/",
            {
                "method": "tools/call",
                "params": {
                    "name": "memory_recall",
                    "arguments": {"query": "alpha", "limit": 3},
                },
                "id": 17,
            },
        )
        request.state.agent = {
            "id": "reader-1",
            "permissions": {"read": True, "admin": False},
            "allowed_scopes": ["shared"],
        }
        expected = RecallResponse(
            query="alpha",
            scopes=["shared"],
            category=None,
            results=[],
            total=0,
            semantic_available=False,
        )
        service = AsyncMock(return_value=expected)
        with patch("pluribus.mcp_async.recall_service", new=service):
            response = await mcp_handle_async(request)

        payload = json.loads(response.body)
        self.assertEqual(payload["id"], 17)
        self.assertEqual(payload["result"]["scopes"], ["shared"])
        service.assert_awaited_once()
        agent_arg, request_arg = service.await_args.args
        self.assertEqual(agent_arg["id"], "reader-1")
        self.assertEqual(request_arg.query, "alpha")


if __name__ == "__main__":
    unittest.main()
