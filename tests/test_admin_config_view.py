"""Regression tests for the hardened dashboard config read path."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from pluribus.admin_config_view import _read_safe_env
from pluribus.config import settings
import pluribus.main as main


class ConfigViewTests(unittest.IsolatedAsyncioTestCase):
    def test_safe_env_reader_filters_secrets_and_non_pluribus_keys(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        try:
            path = Path(temp_dir.name) / "pluribus.env"
            path.write_text(
                "PLURIBUS_RATE_LIMIT=123\n"
                "PLURIBUS_NOTION_API_KEY=secret\n"
                "OTHER_VALUE=hidden\n",
                encoding="utf-8",
            )
            result = _read_safe_env(str(path))
        finally:
            temp_dir.cleanup()

        self.assertEqual(result["PLURIBUS_RATE_LIMIT"], "123")
        self.assertNotIn("PLURIBUS_NOTION_API_KEY", result)
        self.assertNotIn("OTHER_VALUE", result)

    async def test_api_config_reads_settings_env_path_not_legacy_path(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        path = Path(temp_dir.name) / "pluribus.env"
        path.write_text("PLURIBUS_RATE_LIMIT=321\n", encoding="utf-8")
        agent = {
            "id": "admin-config-view",
            "name": "admin",
            "permissions": '{"read":true,"write":true,"delete":true,"admin":true}',
            "allowed_scopes": '["shared"]',
        }
        try:
            with patch.object(settings, "ENV_PATH", str(path)), patch(
                "pluribus.security._authenticate_agent",
                new=AsyncMock(return_value=agent),
            ):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=main.app),
                    base_url="http://testserver",
                    headers={"X-API-Key": "test-admin-key-long-enough"},
                ) as client:
                    response = await client.get("/api/config")
        finally:
            temp_dir.cleanup()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["PLURIBUS_RATE_LIMIT"], "321")
        self.assertEqual(payload["_ENV_PATH"], str(path))


if __name__ == "__main__":
    unittest.main()
