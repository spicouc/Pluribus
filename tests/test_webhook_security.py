"""Regression tests for webhook SSRF and signing hardening."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from pluribus.config import settings
from pluribus.db import get_db
import pluribus.webhooks as webhooks


class WebhookUrlValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_literal_ip_is_allowed(self) -> None:
        url = "https://93.184.216.34/hook"
        self.assertEqual(await webhooks._validate_webhook_url(url), url)

    async def test_loopback_is_always_blocked(self) -> None:
        with self.assertRaises(HTTPException):
            await webhooks._validate_webhook_url("http://127.0.0.1/hook")

    async def test_private_ip_requires_explicit_opt_in(self) -> None:
        with patch.object(settings, "WEBHOOK_ALLOW_PRIVATE", False):
            with self.assertRaises(HTTPException):
                await webhooks._validate_webhook_url("http://10.10.0.5/hook")

        with patch.object(settings, "WEBHOOK_ALLOW_PRIVATE", True):
            self.assertEqual(
                await webhooks._validate_webhook_url("http://10.10.0.5/hook"),
                "http://10.10.0.5/hook",
            )

    async def test_dns_resolution_to_private_ip_is_blocked(self) -> None:
        fake_dns = [
            (2, 1, 6, "", ("10.0.0.10", 443)),
        ]
        with patch.object(settings, "WEBHOOK_ALLOW_PRIVATE", False), patch(
            "pluribus.webhooks.socket.getaddrinfo", return_value=fake_dns
        ):
            with self.assertRaises(HTTPException):
                await webhooks._validate_webhook_url("https://hook.example.test/path")

    async def test_url_credentials_are_rejected(self) -> None:
        with self.assertRaises(HTTPException):
            await webhooks._validate_webhook_url("https://user:pass@example.com/hook")


class WebhookSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "legacy-webhooks.db"
        self.settings_patch = patch.object(settings, "DB_PATH", str(self.db_path))
        self.settings_patch.start()
        async with get_db() as db:
            await db.execute(
                """CREATE TABLE webhooks (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    scope TEXT,
                    category TEXT,
                    events TEXT NOT NULL DEFAULT '[\"fact.created\"]',
                    created_at TEXT DEFAULT (datetime('now')),
                    last_triggered_at TEXT
                )"""
            )
            await db.commit()

    async def asyncTearDown(self) -> None:
        self.settings_patch.stop()
        self.temp_dir.cleanup()

    async def test_legacy_schema_gets_delivery_security_columns(self) -> None:
        async with get_db() as db:
            await webhooks._ensure_webhook_schema(db)
            cursor = await db.execute("PRAGMA table_info(webhooks)")
            columns = {row["name"] for row in await cursor.fetchall()}

        self.assertTrue(
            {"secret", "last_attempted_at", "last_status", "last_error"}.issubset(columns)
        )


class WebhookDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_delivery_is_signed_and_recorded_after_2xx(self) -> None:
        captured: dict = {}

        class FakeResponse:
            status_code = 204

        class FakeClient:
            def __init__(self, **kwargs):
                captured["client_kwargs"] = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, *, content, headers):
                captured["url"] = url
                captured["content"] = content
                captured["headers"] = headers
                return FakeResponse()

        payload = {"event": "fact.created", "fact_id": "f1", "content": "hola"}
        secret = "test-secret"
        record = AsyncMock()

        with patch(
            "pluribus.webhooks._validate_webhook_url",
            new=AsyncMock(return_value="https://example.com/hook"),
        ), patch("pluribus.webhooks.httpx.AsyncClient", FakeClient), patch(
            "pluribus.webhooks._record_delivery", new=record
        ):
            await webhooks._dispatch_webhook(
                "w1", "https://example.com/hook", secret, payload
            )

        expected_body = webhooks._serialize_payload(payload)
        self.assertEqual(captured["content"], expected_body)
        self.assertEqual(
            captured["headers"]["X-Pluribus-Signature"],
            webhooks._signature(secret, expected_body),
        )
        self.assertFalse(captured["client_kwargs"]["follow_redirects"])
        self.assertFalse(captured["client_kwargs"]["trust_env"])
        record.assert_awaited_once_with("w1", 204, None, True)

    async def test_legacy_webhook_without_secret_is_not_sent(self) -> None:
        record = AsyncMock()
        with patch("pluribus.webhooks._record_delivery", new=record), patch(
            "pluribus.webhooks.httpx.AsyncClient"
        ) as client:
            await webhooks._dispatch_webhook(
                "legacy", "https://example.com/hook", None, {"event": "fact.created"}
            )
        client.assert_not_called()
        record.assert_awaited_once()
        args = record.await_args.args
        self.assertEqual(args[0], "legacy")
        self.assertFalse(args[3])


if __name__ == "__main__":
    unittest.main()
