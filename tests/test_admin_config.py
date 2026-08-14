"""Regression tests for hardened admin configuration writes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from pluribus.admin_config import _atomic_update_env, _validate_updates
import pluribus.main as main


class ConfigValidationTests(unittest.TestCase):
    def test_rejects_newline_injection(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            _validate_updates(
                {"PLURIBUS_OLLAMA_MODEL": "safe-model\nPLURIBUS_API_PORT=9999"}
            )
        self.assertEqual(ctx.exception.status_code, 400)

    def test_rejects_non_allowlisted_environment_key(self) -> None:
        with self.assertRaises(HTTPException):
            _validate_updates({"PLURIBUS_NOTION_API_KEY": "secret"})

    def test_rejects_invalid_chunk_overlap(self) -> None:
        with self.assertRaises(HTTPException):
            _validate_updates(
                {
                    "PLURIBUS_MAX_CHUNK_SIZE": "100",
                    "PLURIBUS_CHUNK_OVERLAP": "100",
                }
            )

    def test_canonicalizes_integer_values(self) -> None:
        updates, restart = _validate_updates(
            {"PLURIBUS_RATE_LIMIT": "00100", "_restart": "false"}
        )
        self.assertEqual(updates["PLURIBUS_RATE_LIMIT"], "100")
        self.assertFalse(restart)


class AtomicEnvTests(unittest.TestCase):
    def test_atomic_update_preserves_unrelated_values_and_sets_mode(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        try:
            path = Path(temp_dir.name) / ".env"
            path.write_text(
                "# comment\nPLURIBUS_RATE_LIMIT=10\nSECRET_VALUE=keep-me\n",
                encoding="utf-8",
            )
            _atomic_update_env(
                str(path),
                {
                    "PLURIBUS_RATE_LIMIT": "200",
                    "PLURIBUS_API_PORT": "8791",
                },
            )
            content = path.read_text(encoding="utf-8")
            self.assertIn("# comment\n", content)
            self.assertIn("SECRET_VALUE=keep-me\n", content)
            self.assertIn("PLURIBUS_RATE_LIMIT=200\n", content)
            self.assertIn("PLURIBUS_API_PORT=8791\n", content)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        finally:
            temp_dir.cleanup()

    def test_refuses_symlink_target(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        try:
            root = Path(temp_dir.name)
            target = root / "real.env"
            target.write_text("X=1\n", encoding="utf-8")
            link = root / ".env"
            link.symlink_to(target)
            with self.assertRaises(RuntimeError):
                _atomic_update_env(str(link), {"PLURIBUS_RATE_LIMIT": "100"})
        finally:
            temp_dir.cleanup()


class RoutePrecedenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.agent = {
            "id": "admin-route-test",
            "name": "admin",
            "permissions": '{"read":true,"write":true,"delete":true,"admin":true}',
            "allowed_scopes": '["shared"]',
        }
        self.transport = httpx.ASGITransport(app=main.app)
        self.client = httpx.AsyncClient(
            transport=self.transport,
            base_url="http://testserver",
            headers={"X-API-Key": "test-admin-key-long-enough"},
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_safe_save_route_shadows_legacy_mutation(self) -> None:
        with patch(
            "pluribus.security._authenticate_agent",
            new=AsyncMock(return_value=self.agent),
        ):
            response = await self.client.post(
                "/api/config/save",
                json={"PLURIBUS_NOTION_API_KEY": "must-not-be-editable"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("no editables", response.json()["detail"])

    async def test_get_restart_is_blocked_before_legacy_get_handler(self) -> None:
        with patch(
            "pluribus.security._authenticate_agent",
            new=AsyncMock(return_value=self.agent),
        ):
            response = await self.client.get("/api/config/restart")
        self.assertEqual(response.status_code, 405)


if __name__ == "__main__":
    unittest.main()
