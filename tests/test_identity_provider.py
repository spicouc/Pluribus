"""Regression tests for the generic authenticated identity-provider API."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
import tempfile
import unittest
from unittest.mock import patch

import aiosqlite
from fastapi import HTTPException
from starlette.requests import Request

from pluribus.identity_provider import identity_me, identity_peers


def make_request(agent: dict) -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/v1/identity/me",
        "raw_path": b"/v1/identity/me",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
    }
    request = Request(scope)
    request.state.agent = agent
    return request


class IdentityProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db_path = tmp.name
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """CREATE TABLE agents (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    permissions TEXT NOT NULL,
                    allowed_scopes TEXT NOT NULL,
                    capabilities TEXT NOT NULL,
                    is_active INTEGER NOT NULL,
                    api_key_hash TEXT,
                    api_key_fingerprint TEXT,
                    last_ip TEXT,
                    metadata TEXT
                )"""
            )
            rows = [
                (
                    "caller", "Caller",
                    json.dumps({"read": True, "write": True, "admin": False}),
                    json.dumps(["shared"]), json.dumps({"xerrameca": True}), 1,
                    "HASH-CALLER", "FP-CALLER", "10.0.0.1", "PRIVATE-CALLER",
                ),
                (
                    "peer-ok", "Peer OK",
                    json.dumps({"read": True, "write": True, "admin": False}),
                    json.dumps(["shared"]), json.dumps({"xerrameca": True}), 1,
                    "HASH-PEER", "FP-PEER", "10.0.0.2", "PRIVATE-PEER",
                ),
                (
                    "peer-read", "Peer Read Only",
                    json.dumps({"read": True, "write": False, "admin": False}),
                    json.dumps(["shared"]), json.dumps({}), 1,
                    "HASH-READ", "FP-READ", "10.0.0.3", "PRIVATE-READ",
                ),
                (
                    "peer-other", "Peer Other Scope",
                    json.dumps({"read": True, "write": True, "admin": False}),
                    json.dumps(["private"]), json.dumps({}), 1,
                    "HASH-OTHER", "FP-OTHER", "10.0.0.4", "PRIVATE-OTHER",
                ),
                (
                    "peer-off", "Peer Inactive",
                    json.dumps({"read": True, "write": True, "admin": False}),
                    json.dumps(["shared"]), json.dumps({}), 0,
                    "HASH-OFF", "FP-OFF", "10.0.0.5", "PRIVATE-OFF",
                ),
            ]
            await db.executemany(
                "INSERT INTO agents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
            )
            await db.commit()

        @asynccontextmanager
        async def test_get_db():
            db = await aiosqlite.connect(self.db_path)
            db.row_factory = aiosqlite.Row
            try:
                yield db
            finally:
                await db.close()

        self.get_db_patch = patch(
            "pluribus.identity_provider.get_db", new=test_get_db
        )
        self.get_db_patch.start()

    async def asyncTearDown(self) -> None:
        self.get_db_patch.stop()
        import os

        os.unlink(self.db_path)

    async def test_me_returns_public_identity_only(self) -> None:
        request = make_request(
            {
                "id": "caller",
                "permissions": {"read": True, "write": True, "admin": False},
                "allowed_scopes": ["shared"],
            }
        )
        result = await identity_me(request)
        self.assertEqual(result["id"], "caller")
        self.assertEqual(result["capabilities"], {"xerrameca": True})
        encoded = json.dumps(result)
        self.assertNotIn("HASH-CALLER", encoded)
        self.assertNotIn("FP-CALLER", encoded)
        self.assertNotIn("10.0.0.1", encoded)
        self.assertNotIn("PRIVATE-CALLER", encoded)

    async def test_peers_filters_permission_scope_and_activity(self) -> None:
        request = make_request(
            {
                "id": "caller",
                "permissions": {"read": True, "write": True, "admin": False},
                "allowed_scopes": ["shared"],
            }
        )
        result = await identity_peers(request, scope="shared")
        self.assertEqual([peer["id"] for peer in result], ["peer-ok"])
        encoded = json.dumps(result)
        self.assertNotIn("HASH-", encoded)
        self.assertNotIn("FP-", encoded)
        self.assertNotIn("PRIVATE-", encoded)

    async def test_peers_rejects_scope_not_allowed_to_caller(self) -> None:
        request = make_request(
            {
                "id": "caller",
                "permissions": {"read": True, "write": True, "admin": False},
                "allowed_scopes": ["shared"],
            }
        )
        with self.assertRaises(HTTPException) as ctx:
            await identity_peers(request, scope="private")
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
