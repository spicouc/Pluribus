"""Focused tests for knowledge_traverse input validation."""

from __future__ import annotations

import json
import unittest
from fastapi import HTTPException
from starlette.requests import Request

from pluribus.authorization import mcp_authorize
from pluribus.mcp import _tool_knowledge_traverse


def make_request(path: str, method: str = "POST", body: dict | None = None) -> Request:
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
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
    }
    return Request(scope, receive)


def _body_text(response) -> str:
    """Extract body text from JSONResponse."""
    if isinstance(response.body, bytes):
        return response.body.decode()
    if isinstance(response.body, str):
        return response.body
    return str(response.body)


class KnowledgeTraverseValidationTests(unittest.IsolatedAsyncioTestCase):
    """Test input validation for knowledge_traverse tool."""

    async def test_valid_call(self):
        """Valid call passes validation (may fail at DB layer but not validation)."""
        # entity is non-empty, hops in range, direction valid
        response = await _tool_knowledge_traverse(
            {"entity": "Alice", "hops": 2, "direction": "both"}, 1
        )
        # Should reach DB layer (not fail at validation), so status_code should be 500 (DB error)
        self.assertEqual(response.status_code, 500)

    async def test_hops_below_range(self):
        """hops=0 should be rejected."""
        response = await _tool_knowledge_traverse(
            {"entity": "Alice", "hops": 0, "direction": "both"}, 1
        )
        self.assertEqual(response.status_code, 500)
        body = json.loads(_body_text(response))
        self.assertIn("hops must be an integer between 1 and 3", body["error"]["message"])

    async def test_hops_above_range(self):
        """hops=4 should be rejected."""
        response = await _tool_knowledge_traverse(
            {"entity": "Alice", "hops": 4, "direction": "both"}, 1
        )
        self.assertEqual(response.status_code, 500)
        body = json.loads(_body_text(response))
        self.assertIn("hops must be an integer between 1 and 3", body["error"]["message"])

    async def test_invalid_direction(self):
        """Invalid direction should be rejected."""
        response = await _tool_knowledge_traverse(
            {"entity": "Alice", "hops": 2, "direction": "invalid"}, 1
        )
        self.assertEqual(response.status_code, 500)
        body = json.loads(_body_text(response))
        self.assertIn("direction must be one of: out, in, both", body["error"]["message"])

    async def test_missing_entity(self):
        """Missing entity should be rejected."""
        response = await _tool_knowledge_traverse({}, 1)
        self.assertEqual(response.status_code, 500)
        body = json.loads(_body_text(response))
        self.assertIn("entity must be a non-empty string", body["error"]["message"])

    async def test_non_string_entity_is_rejected(self):
        """Entity must be a non-empty string, not just a truthy value."""
        response = await _tool_knowledge_traverse({"entity": ["Alice"], "hops": 2}, 1)
        self.assertEqual(response.status_code, 500)
        body = json.loads(_body_text(response))
        self.assertIn("entity must be a non-empty string", body["error"]["message"])

    async def test_non_integer_hops_is_rejected(self):
        """Boolean is not a valid JSON integer for hops."""
        response = await _tool_knowledge_traverse(
            {"entity": "Alice", "hops": True, "direction": "both"}, 1
        )
        self.assertEqual(response.status_code, 500)
        body = json.loads(_body_text(response))
        self.assertIn("hops must be an integer between 1 and 3", body["error"]["message"])

    async def test_non_admin_denied(self):
        """Non-admin agents should be denied at authorization layer."""
        request = make_request(
            "/mcp/",
            "POST",
            body={
                "method": "tools/call",
                "params": {
                    "name": "knowledge_traverse",
                    "arguments": {"entity": "Alice", "hops": 2, "direction": "both"},
                },
            },
        )
        request.state.agent = {
            "permissions": {"read": True, "write": True, "delete": False, "admin": False},
            "allowed_scopes": ["shared"],
        }
        with self.assertRaises(HTTPException) as ctx:
            await mcp_authorize(request)
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_admin_allowed(self):
        """Admin agents should pass authorization."""
        request = make_request(
            "/mcp/",
            "POST",
            body={
                "method": "tools/call",
                "params": {
                    "name": "knowledge_traverse",
                    "arguments": {"entity": "Alice", "hops": 2, "direction": "both"},
                },
            },
        )
        request.state.agent = {
            "permissions": {"read": True, "write": True, "delete": True, "admin": True},
            "allowed_scopes": ["shared"],
        }
        # Should not raise
        await mcp_authorize(request)


if __name__ == "__main__":
    unittest.main()