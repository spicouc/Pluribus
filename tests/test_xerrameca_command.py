"""Regression coverage for the uniform Xerrameca slash command."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException

from pluribus.config import settings
from pluribus.db import get_db, init_db
from pluribus.xerrameca.claim import claim_turn
from pluribus.xerrameca.command import DEFAULT_DELAY_SECONDS, run_command
from pluribus.xerrameca.dialogue import create_conversation, reply_turn
from pluribus.xerrameca.dialogue_schema import init_xerrameca_dialogue_db
from pluribus.xerrameca.inbox import inbox
from pluribus.xerrameca.mcp import TOOL_NAMES
from pluribus.xerrameca.models import ConversationCreateRequest, ReplyRequest
from pluribus.xerrameca.runner_dialogue import _candidate_rows
from pluribus.xerrameca.runner_schema import init_xerrameca_runner_db
from pluribus.xerrameca.schema import init_xerrameca_db
from pluribus.xerrameca.service import _now


class XerramecaCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = settings.DB_PATH
        settings.DB_PATH = str(Path(self.tmp.name) / "pluribus.db")
        await init_db()
        await init_xerrameca_db()
        await init_xerrameca_dialogue_db()
        await init_xerrameca_runner_db()

        self.admin = {
            "id": "admin",
            "name": "Admin",
            "permissions": {"read": True, "write": True, "delete": True, "admin": True},
            "allowed_scopes": ["shared"],
        }
        self.a = {
            "id": "agent-a",
            "name": "Agent A",
            "permissions": {"read": True, "write": True, "delete": False, "admin": False},
            "allowed_scopes": ["shared"],
        }
        self.b = {
            "id": "agent-b",
            "name": "Babufrik",
            "permissions": {"read": True, "write": True, "delete": False, "admin": False},
            "allowed_scopes": ["shared"],
        }
        self.private = {
            "id": "agent-private",
            "name": "Private Agent",
            "permissions": {"read": True, "write": True, "delete": False, "admin": False},
            "allowed_scopes": ["private"],
        }
        async with get_db() as db:
            for agent in (self.admin, self.a, self.b, self.private):
                await db.execute(
                    """INSERT INTO agents
                       (id, name, api_key_hash, permissions, allowed_scopes,
                        capabilities, is_active)
                       VALUES (?, ?, 'test', ?, ?, '{}', 1)""",
                    (
                        agent["id"],
                        agent["name"],
                        json.dumps(agent["permissions"]),
                        json.dumps(agent["allowed_scopes"]),
                    ),
                )
            await db.commit()

    async def asyncTearDown(self) -> None:
        settings.DB_PATH = self.old_db
        self.tmp.cleanup()

    async def test_help_agents_and_mcp_discovery(self) -> None:
        help_result = await run_command(self.a, "/xerrameca help")
        self.assertEqual(help_result["kind"], "help")
        self.assertIn("--rounds", help_result["text"])
        self.assertIn("--timeout", help_result["text"])
        self.assertIn("--delay", help_result["text"])
        self.assertIn("xerrameca_command", TOOL_NAMES)

        agents = await run_command(self.a, "/xerrameca agents")
        self.assertEqual(agents["kind"], "agents")
        ids = {item["id"] for item in agents["agents"]}
        self.assertIn("agent-b", ids)
        self.assertNotIn("agent-a", ids)
        self.assertNotIn("agent-private", ids)

    async def test_self_service_start_is_scoped_and_raw_create_stays_admin_only(self) -> None:
        raw_body = ConversationCreateRequest(
            name="Raw",
            objective="No ha de funcionar sense admin.",
            scope="shared",
            participant_agent_ids=["agent-a", "agent-b"],
            first_agent_id="agent-a",
            max_rounds=3,
            turn_timeout_seconds=60,
            persist_summary=False,
        )
        with self.assertRaises(HTTPException) as raised:
            await create_conversation(self.a, raw_body)
        self.assertEqual(raised.exception.status_code, 403)

        result = await run_command(
            self.a,
            "/xerrameca Babufrik Revisa aquesta arquitectura --rounds 3 --timeout 60 --delay 5",
        )
        conv = result["conversation"]
        self.assertEqual(result["kind"], "started")
        self.assertEqual(conv["status"], "active")
        self.assertEqual(conv["protocol_version"], "dialogue-v1")
        self.assertEqual(conv["first_agent_id"], "agent-a")
        self.assertEqual(conv["max_rounds"], 3)
        self.assertEqual(conv["turn_timeout_seconds"], 60)
        self.assertEqual(conv["turn_delay_seconds"], 5)
        self.assertEqual(conv["current_turn"]["assigned_agent_id"], "agent-a")
        self.assertEqual(
            {item["agent_id"] for item in conv["participants"]},
            {"agent-a", "agent-b"},
        )
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT created_by_agent_id FROM xerrameca_conversations WHERE id = ?",
                (conv["id"],),
            )
            row = await cursor.fetchone()
        self.assertEqual(row["created_by_agent_id"], "agent-a")

    async def test_delay_hides_next_turn_and_does_not_consume_reply_timeout(self) -> None:
        result = await run_command(
            self.a,
            "/xerrameca Babufrik Contrasta la proposta --rounds 4 --timeout 60 --delay 30",
        )
        conv = result["conversation"]
        first_turn = conv["current_turn_id"]
        first_claim = await claim_turn(self.a, first_turn)
        conv = await reply_turn(
            self.a,
            first_turn,
            ReplyRequest(
                content="Primera aportació.",
                result="continue",
                lease_token=first_claim["lease_token"],
            ),
        )
        next_turn = conv["current_turn_id"]

        b_inbox = await inbox(self.b)
        self.assertNotIn(next_turn, {item["turn_id"] for item in b_inbox["turns"]})
        with self.assertRaises(HTTPException) as raised:
            await claim_turn(self.b, next_turn)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("ready_at", str(raised.exception.detail))

        async with get_db() as db:
            await db.execute(
                """INSERT INTO xerrameca_runners
                   (agent_id, endpoint_url, secret, enabled,
                    request_timeout_seconds, max_failures, cooldown_seconds,
                    consecutive_failures, created_at, updated_at)
                   VALUES (?, 'https://agent.invalid/xerrameca', 'secret', 1,
                           30, 3, 60, 0, ?, ?)""",
                (self.b["id"], _now(), _now()),
            )
            await db.commit()
        candidates = await _candidate_rows(10)
        self.assertNotIn(next_turn, {item["turn_id"] for item in candidates})

        async with get_db() as db:
            await db.execute(
                "UPDATE xerrameca_turns SET created_at = '2000-01-01T00:00:00.000000Z' WHERE id = ?",
                (next_turn,),
            )
            await db.commit()

        b_inbox = await inbox(self.b)
        self.assertIn(next_turn, {item["turn_id"] for item in b_inbox["turns"]})
        candidates = await _candidate_rows(10)
        self.assertIn(next_turn, {item["turn_id"] for item in candidates})
        second_claim = await claim_turn(self.b, next_turn)
        self.assertGreater(second_claim["lease_until"], second_claim["input_message"]["created_at"])

    async def test_status_get_stop_and_supervisor(self) -> None:
        normal = await run_command(
            self.a,
            "/xerrameca Babufrik Revisa això --delay 0",
        )
        conv_id = normal["conversation"]["id"]
        self.assertEqual(normal["conversation"]["turn_delay_seconds"], 0)

        status = await run_command(self.a, "/xerrameca status")
        self.assertIn(conv_id, {item["id"] for item in status["conversations"]})

        detail = await run_command(self.a, f"/xerrameca {conv_id}")
        self.assertEqual(detail["conversation"]["id"], conv_id)
        self.assertTrue(detail["recent_messages"])

        with self.assertRaises(HTTPException) as raised:
            await run_command(self.b, f"/xerrameca stop {conv_id}")
        self.assertEqual(raised.exception.status_code, 403)

        stopped = await run_command(self.a, f"/xerrameca stop {conv_id}")
        self.assertEqual(stopped["conversation"]["status"], "cancelled")

        supervised = await run_command(
            self.a,
            "/xerrameca Babufrik Valida aquesta decisió --supervisor",
        )
        conv = supervised["conversation"]
        self.assertEqual(conv["turn_policy"], "supervisor")
        self.assertEqual(conv["supervisor_agent_id"], "agent-a")
        self.assertEqual(conv["turn_delay_seconds"], DEFAULT_DELAY_SECONDS)


if __name__ == "__main__":
    unittest.main()
