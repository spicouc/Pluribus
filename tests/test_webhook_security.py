"""Regression tests for webhook SSRF, DNS pinning and signing hardening."""

from __future__ import annotations

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
        fake_dns = [(2, 1, 6, "", ("10.0.0.10", 443))]
        with patch.object(settings, "WEBHOOK_ALLOW_PRIVATE", False), patch(
            "pluribus.webhooks.socket.getaddrinfo", return_value=fake_dns
        ):
            with self.assertRaises(HTTPException):
                await webhooks._validate_webhook_url("https://hook.example.test/path")

    async def test_url_credentials_are_rejected(self) -> None:
        with self.assertRaises(HTTPException):
            await webhooks._validate_webhook_url("https://user:pass@example.com/hook")

    async def test_control_characters_in_url_are_rejected(self) -> None:
        with self.assertRaises(HTTPException):
            await webhooks._validate_webhook_url("https://example.com/hook\nX-Test: evil")


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
        target = webhooks.ResolvedWebhookTarget(
            url="https://example.com/hook",
            scheme="https",
            hostname="example.com",
            port=443,
            address="93.184.216.34",
            request_target="/hook",
            host_header="example.com",
        )
        payload = {"event": "fact.created", "fact_id": "f1", "content": "hola"}
        secret = "test-secret"
        record = AsyncMock()
        post = AsyncMock(return_value=204)

        with patch(
            "pluribus.webhooks._resolve_webhook_target",
            new=AsyncMock(return_value=target),
        ), patch("pluribus.webhooks._post_pinned", new=post), patch(
            "pluribus.webhooks._record_delivery", new=record
        ):
            await webhooks._dispatch_webhook(
                "w1", "https://example.com/hook", secret, payload
            )

        post.assert_awaited_once()
        posted_target, body, headers = post.await_args.args
        self.assertIs(posted_target, target)
        expected_body = webhooks._serialize_payload(payload)
        self.assertEqual(body, expected_body)
        self.assertEqual(
            headers["X-Pluribus-Signature"],
            webhooks._signature(secret, expected_body),
        )
        record.assert_awaited_once_with("w1", 204, None, True)

    async def test_pinned_transport_connects_to_validated_ip_with_original_sni(self) -> None:
        class FakeReader:
            def __init__(self):
                self.lines = [b"HTTP/1.1 204 No Content\r\n", b"\r\n"]

            async def readline(self):
                return self.lines.pop(0) if self.lines else b""

        class FakeWriter:
            def __init__(self):
                self.buffer = b""
                self.closed = False

            def write(self, data):
                self.buffer += data

            async def drain(self):
                return None

            def close(self):
                self.closed = True

            async def wait_closed(self):
                return None

        target = webhooks.ResolvedWebhookTarget(
            url="https://example.com/hook?a=1",
            scheme="https",
            hostname="example.com",
            port=443,
            address="93.184.216.34",
            request_target="/hook?a=1",
            host_header="example.com",
        )
        reader = FakeReader()
        writer = FakeWriter()
        open_connection = AsyncMock(return_value=(reader, writer))

        with patch("pluribus.webhooks.asyncio.open_connection", new=open_connection):
            status = await webhooks._post_pinned(
                target,
                b"{}",
                {"Content-Type": "application/json"},
            )

        self.assertEqual(status, 204)
        kwargs = open_connection.await_args.kwargs
        self.assertEqual(kwargs["host"], "93.184.216.34")
        self.assertEqual(kwargs["port"], 443)
        self.assertEqual(kwargs["server_hostname"], "example.com")
        self.assertIn(b"Host: example.com\r\n", writer.buffer)
        self.assertIn(b"POST /hook?a=1 HTTP/1.1\r\n", writer.buffer)
        self.assertTrue(writer.closed)

    async def test_legacy_webhook_without_secret_is_not_sent(self) -> None:
        record = AsyncMock()
        with patch("pluribus.webhooks._record_delivery", new=record), patch(
            "pluribus.webhooks._post_pinned", new=AsyncMock()
        ) as post:
            await webhooks._dispatch_webhook(
                "legacy", "https://example.com/hook", None, {"event": "fact.created"}
            )
        post.assert_not_awaited()
        record.assert_awaited_once()
        args = record.await_args.args
        self.assertEqual(args[0], "legacy")
        self.assertFalse(args[3])


if __name__ == "__main__":
    unittest.main()
