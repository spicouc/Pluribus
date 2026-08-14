"""Regression coverage for Xerrameca reference receiver and dashboard console."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from pluribus.xerrameca.receiver import (
    ReceiverSettings,
    create_receiver_app,
    verify_signature,
)
from pluribus.webhooks import _serialize_payload, _signature


class ReceiverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_db = str(Path(self.tmp.name) / "receiver.db")
        self.settings = ReceiverSettings(
            runner_secret="runner-secret-test",
            pluribus_url="http://pluribus.invalid",
            pluribus_api_key="plb_test_receiver_api_key_abcdefghijklmnopqrstuvwxyz",
            handler_spec=None,
            state_db=self.state_db,
            reply_timeout_seconds=2,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _payload() -> dict:
        return {
            "event": "xerrameca.turn.claimed",
            "delivery_id": "delivery-1",
            "idempotency_key": "turn-123",
            "agent": {"id": "agent-b", "name": "Agent B"},
            "conversation": {
                "id": "conv-1",
                "name": "Prova",
                "objective": "Resoldre la tasca",
                "scope": "shared",
                "turn_policy": "alternating",
                "max_rounds": 10,
            },
            "turn": {
                "id": "turn-123",
                "round": 2,
                "lease_token": "lease-token-1234567890",
                "lease_until": "2099-01-01T00:00:00Z",
            },
            "input_message": {"content": "fes la feina"},
            "reply": {
                "rest_path": "/v1/xerrameca/turns/turn-123/reply",
                "mcp_tool": "xerrameca_reply",
            },
        }

    def test_signature_contract_matches_runner_hmac(self) -> None:
        body = _serialize_payload(self._payload())
        signature = _signature(self.settings.runner_secret, body)
        self.assertTrue(verify_signature(self.settings.runner_secret, body, signature))
        self.assertFalse(verify_signature(self.settings.runner_secret, body + b"x", signature))
        self.assertFalse(verify_signature(self.settings.runner_secret, body, None))

    def test_valid_delivery_is_processed_once_and_duplicate_is_safe(self) -> None:
        async def handler(payload):
            return {
                "content": f"processat {payload['turn']['id']}",
                "result": "continue",
                "metadata": {"test": True},
            }

        app = create_receiver_app(self.settings, handler)
        payload = self._payload()
        body = _serialize_payload(payload)
        headers = {
            "Content-Type": "application/json",
            "X-Pluribus-Signature": _signature(self.settings.runner_secret, body),
            "X-Pluribus-Idempotency-Key": "turn-123",
        }

        with patch(
            "pluribus.xerrameca.receiver._reply_to_pluribus",
            new=AsyncMock(),
        ) as reply:
            with TestClient(app) as client:
                first = client.post("/xerrameca/turn", content=body, headers=headers)
                duplicate = client.post("/xerrameca/turn", content=body, headers=headers)

        self.assertEqual(first.status_code, 202)
        self.assertFalse(first.json()["duplicate"])
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.json()["duplicate"])
        self.assertEqual(reply.await_count, 1)
        result = reply.await_args.args[2]
        self.assertEqual(result["content"], "processat turn-123")
        self.assertEqual(result["result"], "continue")

    def test_mismatched_idempotency_key_is_rejected(self) -> None:
        app = create_receiver_app(self.settings, AsyncMock())
        payload = self._payload()
        body = _serialize_payload(payload)
        with TestClient(app) as client:
            response = client.post(
                "/xerrameca/turn",
                content=body,
                headers={
                    "X-Pluribus-Signature": _signature(self.settings.runner_secret, body),
                    "X-Pluribus-Idempotency-Key": "other-turn",
                },
            )
        self.assertEqual(response.status_code, 422)

    def test_bad_signature_fails_before_processing(self) -> None:
        app = create_receiver_app(self.settings, AsyncMock())
        with TestClient(app) as client:
            response = client.post(
                "/xerrameca/turn",
                content=b'{"turn":{"id":"x","lease_token":"1234567890123456"}}',
                headers={"X-Pluribus-Signature": "sha256=bad"},
            )
        self.assertEqual(response.status_code, 401)

    def test_payload_size_is_bounded(self) -> None:
        app = create_receiver_app(self.settings, AsyncMock())
        with TestClient(app) as client:
            response = client.post(
                "/xerrameca/turn",
                content=b"{}",
                headers={
                    "Content-Length": str(1024 * 1024 + 1),
                    "X-Pluribus-Signature": "sha256=anything",
                },
            )
        self.assertEqual(response.status_code, 413)


class ConsoleStaticTests(unittest.TestCase):
    def test_console_uses_authenticated_api_calls_and_never_embeds_runner_secret(self) -> None:
        from pluribus.xerrameca.console import _HTML

        self.assertIn("sessionStorage", _HTML)
        self.assertIn("X-API-Key", _HTML)
        self.assertIn("/v1/xerrameca/runner/system", _HTML)
        self.assertIn("/v1/xerrameca/conversations", _HTML)
        self.assertIn("rotate-secret", _HTML)
        self.assertNotIn("XERRAMECA_RUNNER_SECRET=", _HTML)

    def test_dashboard_switch_serves_xerrameca_view_without_starting_workers(self) -> None:
        from pluribus.xerrameca.console_entry import router

        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            response = client.get("/dashboard?view=xerrameca")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Xerrameca Console", response.text)
        self.assertIn("API key admin", response.text)

    def test_main_registers_console_before_legacy_dashboard(self) -> None:
        import inspect
        import pluribus.main as main

        source = inspect.getsource(main)
        console_pos = source.index("app.include_router(xerrameca_console_entry_router)")
        legacy_pos = source.index("app.include_router(dashboard_router")
        self.assertLess(console_pos, legacy_pos)
        self.assertIn('version="2.3.0"', source)


if __name__ == "__main__":
    unittest.main()
