"""Regression tests for centralized authorization guards."""

from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

from pluribus.authorization import (
    _require,
    dashboard_authorize,
    knowledge_authorize,
    mcp_authorize,
    memory_authorize,
)


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

    async def test_knowledge_graph_is_admin_only_until_scope_aware(self) -> None:
        request = make_request("/v1/knowledge/graph", "GET")
        request.state.agent = {
            "permissions": {"read": True, "admin": False},
            "allowed_scopes": ["shared"],
        }
        with self.assertRaises(HTTPException) as ctx:
            await knowledge_authorize(request)
        self.assertEqual(ctx.exception.status_code, 403)

        request.state.agent = {
            "permissions": {"admin": True},
            "allowed_scopes": [],
        }
        await knowledge_authorize(request)


class PersistentCategoryGuardTests(unittest.IsolatedAsyncioTestCase):
    """PUT /v1/memory/{id} must not let a non-admin un-protect a fact."""

    def _agent(self, *, admin: bool = False) -> dict:
        return {
            "permissions": {"read": True, "write": True, "delete": True, "admin": admin},
            "allowed_scopes": ["shared"],
        }

    def _request(self, body: dict) -> Request:
        request = make_request("/v1/memory/fact-1", "PUT", body=body)
        return request

    async def test_non_admin_cannot_unprotect_a_persistent_fact(self) -> None:
        request = self._request({"content": "x", "category": "events"})
        request.state.agent = self._agent()
        with patch(
            "pluribus.authorization._fact_scope_category",
            new=AsyncMock(return_value=("shared", "system")),
        ):
            with self.assertRaises(HTTPException) as ctx:
                await memory_authorize(request)
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_non_admin_can_edit_content_of_a_persistent_fact(self) -> None:
        request = self._request({"content": "x"})
        request.state.agent = self._agent()
        with patch(
            "pluribus.authorization._fact_scope_category",
            new=AsyncMock(return_value=("shared", "system")),
        ):
            await memory_authorize(request)

    async def test_non_admin_may_repeat_the_same_persistent_category(self) -> None:
        request = self._request({"content": "x", "category": "system"})
        request.state.agent = self._agent()
        with patch(
            "pluribus.authorization._fact_scope_category",
            new=AsyncMock(return_value=("shared", "system")),
        ):
            await memory_authorize(request)

    async def test_admin_can_recategorize_a_persistent_fact(self) -> None:
        request = self._request({"content": "x", "category": "events"})
        request.state.agent = self._agent(admin=True)
        with patch(
            "pluribus.authorization._fact_scope_category",
            new=AsyncMock(return_value=("shared", "system")),
        ):
            await memory_authorize(request)

    async def test_non_admin_may_tag_a_standard_fact(self) -> None:
        request = self._request({"content": "x", "category": "system"})
        request.state.agent = self._agent()
        with patch(
            "pluribus.authorization._fact_scope_category",
            new=AsyncMock(return_value=("shared", "events")),
        ):
            await memory_authorize(request)


if __name__ == "__main__":
    unittest.main()
