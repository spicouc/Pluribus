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
    def _payload(lease_token: str = "lease-token-1234567890") -> dict:
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
                "lease_token": lease_token,
                "lease_until": "2099-01-01T00:00:00Z",
            },
            "input_message": {"content": "fes la feina"},
            "reply": {
                "rest_path": "/v1/xerrameca/turns/turn-123/reply",
                "mcp_tool": "xerrameca_reply",
            },
        }

    def _headers(self, body: bytes) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Pluribus-Signature": _signature(self.settings.runner_secret, body),
            "X-Pluribus-Idempotency-Key": "turn-123",
        }

    def test_signature_contract_matches_runner_hmac(self) -> None:
        body = _serialize_payload(self._payload())
        signature = _signature(self.settings.runner_secret, body)
        self.assertTrue(verify_signature(self.settings.runner_secret, body, signature))
        self.assertFalse(verify_signature(self.settings.runner_secret, body + b"x", signature))
        self.assertFalse(verify_signature(self.settings.runner_secret, body, None))

    def test_valid_delivery_is_processed_once_and_duplicate_same_lease_is_safe(self) -> None:
        async def handler(payload):
            return {
                "content": f"processat {payload['turn']['id']}",
                "result": "continue",
                "metadata": {"test": True},
            }

        app = create_receiver_app(self.settings, handler)
        body = _serialize_payload(self._payload())

        with patch(
            "pluribus.xerrameca.receiver._reply_to_pluribus",
            new=AsyncMock(),
        ) as reply:
            with TestClient(app) as client:
                first = client.post("/xerrameca/turn", content=body, headers=self._headers(body))
                duplicate = client.post("/xerrameca/turn", content=body, headers=self._headers(body))

        self.assertEqual(first.status_code, 202)
        self.assertFalse(first.json()["duplicate"])
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.json()["duplicate"])
        self.assertEqual(reply.await_count, 1)
        result = reply.await_args.args[2]
        self.assertEqual(result["content"], "processat turn-123")
        self.assertEqual(result["result"], "continue")

    def test_same_turn_with_new_lease_is_a_legitimate_recovery_attempt(self) -> None:
        async def handler(payload):
            return {"content": "ok", "result": "continue"}

        app = create_receiver_app(self.settings, handler)
        first_body = _serialize_payload(self._payload("lease-token-first-123456"))
        second_body = _serialize_payload(self._payload("lease-token-second-12345"))

        # Simulate an accepted attempt whose callback processing did not finish.
        # Avoid completing the local row so the new Pluribus lease may supersede it.
        with patch(
            "pluribus.xerrameca.receiver._process_delivery",
            new=AsyncMock(),
        ) as process:
            with TestClient(app) as client:
                first = client.post(
                    "/xerrameca/turn", content=first_body, headers=self._headers(first_body)
                )
                second = client.post(
                    "/xerrameca/turn", content=second_body, headers=self._headers(second_body)
                )

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertFalse(second.json()["duplicate"])
        self.assertEqual(process.await_count, 2)

    def test_completed_turn_stays_deduplicated_even_if_a_new_lease_arrives(self) -> None:
        async def handler(payload):
            return {"content": "ok", "result": "continue"}

        app = create_receiver_app(self.settings, handler)
        first_body = _serialize_payload(self._payload("lease-token-complete-1234"))
        second_body = _serialize_payload(self._payload("lease-token-new-after-complete"))
        with patch(
            "pluribus.xerrameca.receiver._reply_to_pluribus",
            new=AsyncMock(),
        ) as reply:
            with TestClient(app) as client:
                first = client.post(
                    "/xerrameca/turn", content=first_body, headers=self._headers(first_body)
                )
                second = client.post(
                    "/xerrameca/turn", content=second_body, headers=self._headers(second_body)
                )
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["duplicate"])
        self.assertEqual(reply.await_count, 1)

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
        oversized = b"x" * (1024 * 1024 + 1)
        with TestClient(app) as client:
            response = client.post(
                "/xerrameca/turn",
                content=oversized,
                headers={"X-Pluribus-Signature": "sha256=anything"},
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
