"""Regression tests for shared request validation."""

from __future__ import annotations

import json
import unittest

from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from pluribus.authorization import mcp_authorize, memory_authorize
from pluribus.models import (
    AgentRegisterRequest,
    QueryParams,
    SemanticSearchRequest,
    WriteRequest,
)
from pluribus.validation import MAX_CONTENT_LENGTH, MAX_METADATA_BYTES


def make_request(
    path: str,
    method: str = "GET",
    query: bytes = b"",
    body: dict | None = None,
) -> Request:
    payload = json.dumps(body or {}).encode("utf-8")
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
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
    }
    return Request(scope, receive)


def standard_agent() -> dict:
    return {
        "id": "agent-1",
        "permissions": {"read": True, "write": True, "delete": True, "admin": False},
        "allowed_scopes": ["shared"],
    }


class ModelValidationTests(unittest.TestCase):
    def test_write_accepts_protected_and_explicit_extension_categories(self) -> None:
        self.assertEqual(
            WriteRequest(content="x", category="system").category,
            "system",
        )
        self.assertEqual(
            WriteRequest(content="x", category="x-my-domain").category,
            "x-my-domain",
        )

    def test_unknown_category_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            WriteRequest(content="x", category="totally-custom")

    def test_scope_is_bounded_and_syntax_checked(self) -> None:
        with self.assertRaises(ValidationError):
            WriteRequest(content="x", scope="bad scope")
        with self.assertRaises(ValidationError):
            WriteRequest(content="x", scope="a" * 65)

    def test_ttl_must_be_positive_and_bounded(self) -> None:
        with self.assertRaises(ValidationError):
            WriteRequest(content="x", ttl_days=0)
        with self.assertRaises(ValidationError):
            WriteRequest(content="x", ttl_days=3651)

    def test_content_and_metadata_have_size_limits(self) -> None:
        with self.assertRaises(ValidationError):
            WriteRequest(content="x" * (MAX_CONTENT_LENGTH + 1))
        with self.assertRaises(ValidationError):
            WriteRequest(content="x", metadata={"blob": "y" * MAX_METADATA_BYTES})

    def test_legacy_fts_query_quote_and_controls_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            QueryParams(q='alpha" OR beta')
        with self.assertRaises(ValidationError):
            SemanticSearchRequest(query="alpha\nbeta")

    def test_agent_permissions_and_scopes_are_strict(self) -> None:
        with self.assertRaises(ValidationError):
            AgentRegisterRequest(
                name="agent",
                permissions={"read": True, "sudo": True},
            )
        with self.assertRaises(ValidationError):
            AgentRegisterRequest(
                name="agent",
                allowed_scopes=["shared", "shared"],
            )
        request = AgentRegisterRequest(
            name=" agent ",
            permissions={"read": True},
            allowed_scopes=["shared"],
        )
        self.assertEqual(request.name, "agent")
        self.assertEqual(
            request.permissions,
            {"read": True, "write": False, "delete": False, "admin": False},
        )


class RawGuardValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_ls_query_params_are_validated_before_handler(self) -> None:
        request = make_request(
            "/v1/memory/ls",
            "GET",
            query=b"scope=bad%20scope&category=events",
        )
        request.state.agent = standard_agent()
        with self.assertRaises(HTTPException) as ctx:
            await memory_authorize(request)
        self.assertEqual(ctx.exception.status_code, 422)

    async def test_manual_list_sort_is_allowlisted(self) -> None:
        request = make_request(
            "/v1/memory",
            "GET",
            query=b"scope=shared&sort=created_at%3Bdrop%20table%20facts",
        )
        request.state.agent = standard_agent()
        with self.assertRaises(HTTPException) as ctx:
            await memory_authorize(request)
        self.assertEqual(ctx.exception.status_code, 422)

    async def test_mcp_write_rejects_oversized_content_before_handler(self) -> None:
        request = make_request(
            "/mcp/",
            "POST",
            body={
                "method": "tools/call",
                "params": {
                    "name": "memory_write",
                    "arguments": {
                        "scope": "shared",
                        "category": "events",
                        "content": "x" * (MAX_CONTENT_LENGTH + 1),
                    },
                },
            },
        )
        request.state.agent = standard_agent()
        with self.assertRaises(HTTPException) as ctx:
            await mcp_authorize(request)
        self.assertEqual(ctx.exception.status_code, 422)

    async def test_mcp_query_rejects_unescaped_legacy_fts_quote(self) -> None:
        request = make_request(
            "/mcp/",
            "POST",
            body={
                "method": "tools/call",
                "params": {
                    "name": "memory_query",
                    "arguments": {"scope": "shared", "q": 'alpha" OR beta'},
                },
            },
        )
        request.state.agent = standard_agent()
        with self.assertRaises(HTTPException) as ctx:
            await mcp_authorize(request)
        self.assertEqual(ctx.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
