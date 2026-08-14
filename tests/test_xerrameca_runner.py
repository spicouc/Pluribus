"""Regression tests for Xerrameca Runner v1."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from pluribus.config import settings
from pluribus.db import get_db, init_db
from pluribus.xerrameca.models import ConversationCreateRequest
from pluribus.xerrameca.runner import (
    RunnerConfigUpsert,
    RunnerSystemUpdate,
    get_runner_system,
    runner_tick,
    update_runner_system,
    upsert_runner_config,
)
from pluribus.xerrameca.runner_schema import init_xerrameca_runner_db
from pluribus.xerrameca.schema import init_xerrameca_db
from pluribus.xerrameca.service import create_conversation, start_conversation
from pluribus.webhooks import ResolvedWebhookTarget


class XerramecaRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = settings.DB_PATH
        settings.DB_PATH = str(Path(self.tmp.name) / "pluribus.db")
        await init_db()
        await init_xerrameca_db()
        await init_xerrameca_runner_db()

        self.admin = {
            "id": "admin",
            "name": "Admin",
            "permissions": {"read": True, "write": True, "delete": True, "admin": True},
            "allowed_scopes": ["shared"],
        }
        self.agent_a = {
            "id": "agent-a",
            "name": "Agent A",
            "permissions": {"read": True, "write": True, "delete": False, "admin": False},
            "allowed_scopes": ["shared"],
        }
        self.agent_b = {
            "id": "agent-b",
            "name": "Agent B",
            "permissions": {"read": True, "write": True, "delete": False, "admin": False},
            "allowed_scopes": ["shared"],
        }
        async with get_db() as db:
            for agent in (self.admin, self.agent_a, self.agent_b):
                await db.execute(
                    """INSERT INTO agents
                       (id, name, api_key_hash, permissions, allowed_scopes, is_active)
                       VALUES (?, ?, 'test', ?, ?, 1)""",
                    (
                        agent["id"],
                        agent["name"],
                        json.dumps(agent["permissions"]),
                        json.dumps(agent["allowed_scopes"]),
                    ),
                )
            await db.commit()

    async def asyncTearDown(self) -> None:
        settings.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    async def _start_for_a(self, name: str = "Runner test") -> dict:
        conv = await create_conversation(
            self.admin,
            ConversationCreateRequest(
                name=name,
                objective="Agent A processa el primer torn i Agent B continua.",
                scope="shared",
                participant_agent_ids=["agent-a", "agent-b"],
                turn_policy="alternating",
                first_agent_id="agent-a",
                max_rounds=4,
                turn_timeout_seconds=60,
                persist_summary=False,
            ),
        )
        return await start_conversation(self.admin, conv["id"])

    async def _configure_a(
        self,
        *,
        max_failures: int = 3,
        cooldown_seconds: int = 60,
    ) -> dict:
        body = RunnerConfigUpsert(
            endpoint_url="https://agent-a.example/xerrameca",
            request_timeout_seconds=10,
            max_failures=max_failures,
            cooldown_seconds=cooldown_seconds,
        )
        with patch(
            "pluribus.xerrameca.runner._validate_webhook_url",
            new=AsyncMock(return_value=body.endpoint_url),
        ):
            return await upsert_runner_config(self.admin, "agent-a", body)

    async def _enable(self, max_dispatches: int = 4) -> None:
        await update_runner_system(
            self.admin,
            RunnerSystemUpdate(
                enabled=True,
                poll_interval_seconds=1.0,
                max_dispatches_per_tick=max_dispatches,
            ),
        )

    def _target(self) -> ResolvedWebhookTarget:
        return ResolvedWebhookTarget(
            url="https://agent-a.example/xerrameca",
            scheme="https",
            hostname="agent-a.example",
            port=443,
            address="203.0.113.10",
            request_target="/xerrameca",
            host_header="agent-a.example",
        )

    async def test_runner_is_disabled_by_default_and_requires_admin(self) -> None:
        state = await get_runner_system(self.admin)
        self.assertFalse(state["enabled"])
        with self.assertRaises(HTTPException) as ctx:
            await get_runner_system(self.agent_a)
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_config_secret_is_only_returned_on_create(self) -> None:
        first = await self._configure_a()
        self.assertIn("secret", first)
        second = await self._configure_a()
        self.assertNotIn("secret", second)

    async def test_loopback_endpoint_is_rejected(self) -> None:
        body = RunnerConfigUpsert(endpoint_url="http://127.0.0.1:9999/wake")
        with self.assertRaises(HTTPException) as ctx:
            await upsert_runner_config(self.admin, "agent-a", body)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_successful_tick_claims_and_dispatches_signed_turn(self) -> None:
        await self._configure_a()
        await self._enable()
        conv = await self._start_for_a()
        post = AsyncMock(return_value=204)
        with patch(
            "pluribus.xerrameca.runner._resolve_webhook_target",
            new=AsyncMock(return_value=self._target()),
        ), patch("pluribus.xerrameca.runner._post_pinned", new=post):
            result = await runner_tick(self.admin)

        self.assertEqual(result["attempted"], 1)
        self.assertEqual(result["results"][0]["status"], "dispatched")
        args = post.await_args.args
        headers = args[2]
        self.assertEqual(headers["X-Pluribus-Idempotency-Key"], conv["current_turn_id"])
        self.assertTrue(headers["X-Pluribus-Signature"].startswith("sha256="))

        async with get_db() as db:
            cursor = await db.execute(
                "SELECT status, claimed_by, lease_token FROM xerrameca_turns WHERE id = ?",
                (conv["current_turn_id"],),
            )
            turn = await cursor.fetchone()
        self.assertEqual(turn["status"], "claimed")
        self.assertEqual(turn["claimed_by"], "agent-a")
        self.assertTrue(turn["lease_token"])

    async def test_failed_dispatch_releases_lease_and_opens_circuit(self) -> None:
        await self._configure_a(max_failures=1, cooldown_seconds=60)
        await self._enable()
        conv = await self._start_for_a()
        with patch(
            "pluribus.xerrameca.runner._resolve_webhook_target",
            new=AsyncMock(return_value=self._target()),
        ), patch(
            "pluribus.xerrameca.runner._post_pinned",
            new=AsyncMock(return_value=503),
        ):
            result = await runner_tick(self.admin)

        self.assertEqual(result["results"][0]["status"], "failed")
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT status, claimed_by, lease_token FROM xerrameca_turns WHERE id = ?",
                (conv["current_turn_id"],),
            )
            turn = await cursor.fetchone()
            cursor = await db.execute(
                "SELECT consecutive_failures, circuit_open_until FROM xerrameca_runners WHERE agent_id = 'agent-a'"
            )
            runner = await cursor.fetchone()
        self.assertEqual(turn["status"], "ready")
        self.assertIsNone(turn["claimed_by"])
        self.assertIsNone(turn["lease_token"])
        self.assertEqual(runner["consecutive_failures"], 1)
        self.assertIsNotNone(runner["circuit_open_until"])

        # Circuit-open runner is not selected again immediately.
        second = await runner_tick(self.admin)
        self.assertEqual(second["attempted"], 0)

    async def test_expired_successful_lease_is_recovered_and_redispatched(self) -> None:
        await self._configure_a()
        await self._enable()
        conv = await self._start_for_a()
        with patch(
            "pluribus.xerrameca.runner._resolve_webhook_target",
            new=AsyncMock(return_value=self._target()),
        ), patch(
            "pluribus.xerrameca.runner._post_pinned",
            new=AsyncMock(return_value=204),
        ):
            first = await runner_tick(self.admin)
            self.assertEqual(first["attempted"], 1)
            async with get_db() as db:
                await db.execute(
                    "UPDATE xerrameca_turns SET lease_until = '2000-01-01T00:00:00.000000Z' WHERE id = ?",
                    (conv["current_turn_id"],),
                )
                await db.commit()
            second = await runner_tick(self.admin)
        self.assertEqual(second["attempted"], 1)
        self.assertEqual(second["results"][0]["status"], "dispatched")

    async def test_only_one_turn_per_agent_is_dispatched_per_tick(self) -> None:
        await self._configure_a()
        await self._enable(max_dispatches=10)
        await self._start_for_a("One")
        await self._start_for_a("Two")
        with patch(
            "pluribus.xerrameca.runner._resolve_webhook_target",
            new=AsyncMock(return_value=self._target()),
        ), patch(
            "pluribus.xerrameca.runner._post_pinned",
            new=AsyncMock(return_value=204),
        ):
            result = await runner_tick(self.admin)
        self.assertEqual(result["attempted"], 1)

    async def test_disabled_runtime_never_claims(self) -> None:
        await self._configure_a()
        conv = await self._start_for_a()
        result = await runner_tick(self.admin)
        self.assertFalse(result["enabled"])
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT status FROM xerrameca_turns WHERE id = ?",
                (conv["current_turn_id"],),
            )
            turn = await cursor.fetchone()
        self.assertEqual(turn["status"], "ready")


if __name__ == "__main__":
    unittest.main()
