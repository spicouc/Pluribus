"""Regression tests for centralized authorization guards."""

from __future__ import annotations

import json
import unittest

from fastapi import HTTPException
from starlette.requests import Request

from pluribus.authorization import _require, dashboard_authorize, mcp_authorize, memory_authorize


def make_request(path: str, method: str = "GET", query: bytes = b"", body: dict | None = None) -> Request:
    payload = json.dumps(body or {}).encode()
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
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
    }
    return Request(scope, receive)


class RequireTests(unittest.TestCase):
    def test_scope_is_enforced(self) -> None:
        agent = {"permissions": {"read": True}, "allowed_scopes": ["shared"]}
        with self.assertRaises(HTTPException) as ctx:
            _require(agent, "read", "local")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_admin_bypasses_scope_and_permission(self) -> None:
        agent = {"permissions": {"admin": True}, "allowed_scopes": []}
        _require(agent, "delete", "private")


class GuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_admin_list_requires_explicit_scope(self) -> None:
        request = make_request("/v1/memory", "GET")
        request.state.agent = {
            "permissions": {"read": True, "admin": False},
            "allowed_scopes": ["shared"],
        }
        with self.assertRaises(HTTPException) as ctx:
            await memory_authorize(request)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_mcp_write_rejects_forbidden_scope(self) -> None:
        request = make_request(
            "/mcp/",
            "POST",
            body={
                "method": "tools/call",
                "params": {"name": "memory_write", "arguments": {"scope": "local", "content": "x"}},
            },
        )
        request.state.agent = {
            "permissions": {"write": True, "admin": False},
            "allowed_scopes": ["shared"],
        }
        with self.assertRaises(HTTPException) as ctx:
            await mcp_authorize(request)
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_dashboard_api_is_admin_only(self) -> None:
        request = make_request("/api/search", "GET")
        request.state.agent = {
            "permissions": {"read": True, "admin": False},
            "allowed_scopes": ["shared"],
        }
        with self.assertRaises(HTTPException) as ctx:
            await dashboard_authorize(request)
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
