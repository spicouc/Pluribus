"""Regression tests for incremental Memory Sync and MCP directive exposure."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

from pluribus.config import settings
from pluribus.db import get_db, init_db
from pluribus.mcp_async import ALL_TOOLS, mcp_handle_async
from pluribus.memory_sync import (
    MemorySyncPolicy,
    MemorySyncPolicyResponse,
    MemorySyncResponse,
    get_memory_sync_policy,
    init_memory_sync_db,
    memory_sync_service,
    set_memory_sync_policy,
)


def make_json_request(body: dict) -> Request:
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
        "path": "/mcp/",
        "raw_path": b"/mcp/",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
    }
    return Request(scope, receive)


class MemorySyncServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "sync.db"
        self.settings_patch = patch.object(settings, "DB_PATH", str(self.db_path))
        self.settings_patch.start()
        await init_db()
        await init_memory_sync_db()

        self.reader = {
            "id": "reader-1",
            "permissions": {"read": True, "write": True, "delete": False, "admin": False},
            "allowed_scopes": ["shared"],
        }
        self.private_reader = {
            "id": "reader-2",
            "permissions": {"read": True, "write": False, "delete": False, "admin": False},
            "allowed_scopes": ["private"],
        }
        self.admin = {
            "id": "admin-1",
            "permissions": {"read": True, "write": True, "delete": True, "admin": True},
            "allowed_scopes": [],
        }

        async with get_db() as db:
            for agent, name in (
                (self.reader, "reader"),
                (self.private_reader, "private-reader"),
                (self.admin, "admin"),
            ):
                await db.execute(
                    """INSERT INTO agents(
                           id, name, api_key_hash, permissions, allowed_scopes
                       ) VALUES (?, ?, 'unused', ?, ?)""",
                    (
                        agent["id"],
                        name,
                        json.dumps(agent["permissions"]),
                        json.dumps(agent["allowed_scopes"]),
                    ),
                )
            await db.commit()

    async def asyncTearDown(self) -> None:
        self.settings_patch.stop()
        self.temp_dir.cleanup()

    async def _insert_fact(self, fact_id: str, scope: str, content: str) -> None:
        async with get_db() as db:
            await db.execute(
                """INSERT INTO facts(id, scope, category, content, metadata)
                   VALUES (?, ?, 'events', ?, '{}')""",
                (fact_id, scope, content),
            )
            await db.commit()

    async def test_sync_filters_scopes_but_advances_global_cursor(self) -> None:
        await self._insert_fact("f-shared", "shared", "shared memory")
        await self._insert_fact("f-private", "private", "private memory")

        first = await memory_sync_service(self.reader, cursor=0, limit=100)
        self.assertEqual([c.fact_id for c in first.changes], ["f-shared"])
        self.assertGreater(first.next_cursor, 0)
        self.assertEqual(first.recommended_poll_seconds, 5)

        second = await memory_sync_service(
            self.reader,
            cursor=first.next_cursor,
            limit=100,
        )
        self.assertEqual(second.changes, [])
        self.assertEqual(second.next_cursor, first.next_cursor)
        self.assertEqual(second.recommended_poll_seconds, 30)

    async def test_soft_delete_emits_tombstone(self) -> None:
        await self._insert_fact("f-delete", "shared", "temporary")
        initial = await memory_sync_service(self.reader, cursor=0, limit=100)

        async with get_db() as db:
            await db.execute(
                "UPDATE facts SET deleted_at = datetime('now') WHERE id = ?",
                ("f-delete",),
            )
            await db.commit()

        delta = await memory_sync_service(
            self.reader,
            cursor=initial.next_cursor,
            limit=100,
        )
        self.assertEqual(len(delta.changes), 1)
        self.assertEqual(delta.changes[0].fact_id, "f-delete")
        self.assertEqual(delta.changes[0].change_type, "delete")
        self.assertIsNone(delta.changes[0].content)

    async def test_scope_move_sends_delete_to_old_scope_and_upsert_to_new_scope(self) -> None:
        await self._insert_fact("f-move", "shared", "movable")
        shared_initial = await memory_sync_service(self.reader, cursor=0, limit=100)
        private_initial = await memory_sync_service(
            self.private_reader,
            cursor=0,
            limit=100,
        )

        async with get_db() as db:
            await db.execute(
                "UPDATE facts SET scope = 'private', updated_at = datetime('now') WHERE id = ?",
                ("f-move",),
            )
            await db.commit()

        shared_delta = await memory_sync_service(
            self.reader,
            cursor=shared_initial.next_cursor,
            limit=100,
        )
        private_delta = await memory_sync_service(
            self.private_reader,
            cursor=private_initial.next_cursor,
            limit=100,
        )

        self.assertEqual(
            [(c.fact_id, c.change_type) for c in shared_delta.changes],
            [("f-move", "delete")],
        )
        self.assertEqual(
            [(c.fact_id, c.change_type, c.scope) for c in private_delta.changes],
            [("f-move", "upsert", "private")],
        )

    async def test_policy_defaults_and_admin_override(self) -> None:
        default = await get_memory_sync_policy(self.reader["id"])
        self.assertEqual(default.active_poll_seconds, 5)
        self.assertEqual(default.idle_poll_seconds, 30)
        self.assertEqual(default.write_debounce_seconds, 2)
        self.assertEqual(default.max_write_delay_seconds, 5)

        updated = await set_memory_sync_policy(
            self.admin,
            self.reader["id"],
            MemorySyncPolicy(
                active_poll_seconds=3,
                idle_poll_seconds=15,
                write_debounce_seconds=1,
                max_write_delay_seconds=3,
            ),
        )
        self.assertEqual(updated.active_poll_seconds, 3)
        self.assertEqual(updated.idle_poll_seconds, 15)

    async def test_policy_rejects_inverted_cadence(self) -> None:
        with self.assertRaises(ValueError):
            MemorySyncPolicy(active_poll_seconds=30, idle_poll_seconds=10)
        with self.assertRaises(ValueError):
            MemorySyncPolicy(write_debounce_seconds=10, max_write_delay_seconds=5)


class McpCoreToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalogue_contains_sync_and_directive_tools(self) -> None:
        names = {tool["name"] for tool in ALL_TOOLS}
        self.assertIn("memory_sync", names)
        self.assertIn("directive_inbox", names)
        self.assertIn("directive_create", names)
        self.assertIn("directive_claim", names)
        self.assertIn("directive_complete", names)

    async def test_memory_sync_mcp_uses_authenticated_agent(self) -> None:
        request = make_json_request(
            {
                "method": "tools/call",
                "params": {
                    "name": "memory_sync",
                    "arguments": {"cursor": 7, "limit": 25},
                },
                "id": 1,
            }
        )
        request.state.agent = {
            "id": "reader-1",
            "permissions": {"read": True, "admin": False},
            "allowed_scopes": ["shared"],
        }
        result = MemorySyncResponse(
            cursor=7,
            next_cursor=9,
            has_more=False,
            changes=[],
            recommended_poll_seconds=30,
            policy=MemorySyncPolicyResponse(agent_id="reader-1"),
        )
        service = AsyncMock(return_value=result)
        with patch("pluribus.mcp_async.memory_sync_service", new=service):
            response = await mcp_handle_async(request)

        payload = json.loads(response.body)
        self.assertEqual(payload["id"], 1)
        self.assertEqual(payload["result"]["next_cursor"], 9)
        agent_arg = service.await_args.args[0]
        self.assertEqual(agent_arg["id"], "reader-1")
        self.assertEqual(service.await_args.kwargs, {"cursor": 7, "limit": 25})

    async def test_directive_inbox_mcp_reuses_rest_handler(self) -> None:
        request = make_json_request(
            {
                "method": "tools/call",
                "params": {
                    "name": "directive_inbox",
                    "arguments": {"limit": 12},
                },
                "id": 2,
            }
        )
        request.state.agent = {
            "id": "worker-1",
            "permissions": {"read": True, "admin": False},
            "allowed_scopes": ["shared"],
        }
        inbox = AsyncMock(return_value=[])
        with patch("pluribus.mcp_async.directive_inbox", new=inbox):
            response = await mcp_handle_async(request)

        payload = json.loads(response.body)
        self.assertEqual(payload["id"], 2)
        self.assertEqual(payload["result"], [])
        inbox.assert_awaited_once()
        self.assertIs(inbox.await_args.args[0], request)
        self.assertEqual(inbox.await_args.kwargs, {"limit": 12})


if __name__ == "__main__":
    unittest.main()
