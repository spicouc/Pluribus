"""Regression coverage for Xerrameca Dialogue Protocol v1 and Monitor."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from pluribus.config import settings
from pluribus.db import get_db, init_db
from pluribus.xerrameca.claim import claim_turn
from pluribus.xerrameca.dialogue import (
    PROTOCOL_VERSION,
    create_conversation,
    get_conversation,
    reply_turn,
    start_conversation,
)
from pluribus.xerrameca.dialogue_schema import init_xerrameca_dialogue_db
from pluribus.xerrameca.models import ConversationCreateRequest, ReplyRequest
from pluribus.xerrameca.monitor import MonitorUpdate, list_alerts, monitor_once, update_monitor_state
from pluribus.xerrameca.monitor_schema import init_xerrameca_monitor_db
from pluribus.xerrameca.schema import init_xerrameca_db
from pluribus.xerrameca.service import create_conversation as legacy_create_conversation


class DialogueMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = settings.DB_PATH
        settings.DB_PATH = str(Path(self.tmp.name) / "pluribus.db")
        await init_db()
        await init_xerrameca_db()
        await init_xerrameca_dialogue_db()
        await init_xerrameca_monitor_db()

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
            "name": "Agent B",
            "permissions": {"read": True, "write": True, "delete": False, "admin": False},
            "allowed_scopes": ["shared"],
        }
        async with get_db() as db:
            for agent in (self.admin, self.a, self.b):
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
        settings.DB_PATH = self.old_db
        self.tmp.cleanup()

    def body(self, *, policy: str = "alternating", max_rounds: int = 4) -> ConversationCreateRequest:
        return ConversationCreateRequest(
            name="Protocol",
            objective="Arribar a una conclusió compartida.",
            scope="shared",
            participant_agent_ids=["agent-a", "agent-b"],
            turn_policy=policy,
            supervisor_agent_id="agent-a" if policy == "supervisor" else None,
            first_agent_id="agent-a",
            max_rounds=max_rounds,
            turn_timeout_seconds=60,
            persist_summary=False,
        )

    async def create_started(self, **kwargs) -> dict:
        conv = await create_conversation(self.admin, self.body(**kwargs))
        return await start_conversation(self.admin, conv["id"])

    async def answer(self, agent: dict, conv: dict, content: str, result: str) -> dict:
        turn_id = conv["current_turn_id"]
        claim = await claim_turn(agent, turn_id)
        return await reply_turn(
            agent,
            turn_id,
            ReplyRequest(content=content, result=result, lease_token=claim["lease_token"]),
        )

    async def test_new_conversation_uses_dialogue_protocol_but_legacy_stays_legacy(self) -> None:
        legacy = await legacy_create_conversation(self.admin, self.body())
        async with get_db() as db:
            row = await db.execute_fetchall(
                "SELECT protocol_version FROM xerrameca_conversations WHERE id = ?", (legacy["id"],)
            )
        self.assertEqual(row[0]["protocol_version"], "legacy-v0")

        conv = await create_conversation(self.admin, self.body())
        self.assertEqual(conv["protocol_version"], PROTOCOL_VERSION)

    async def test_kickoff_and_two_agent_round_semantics(self) -> None:
        conv = await self.create_started()
        self.assertEqual(conv["current_round"], 1)
        self.assertEqual(conv["current_turn"]["dialogue_round"], 1)
        self.assertEqual(conv["current_turn"]["turn_in_round"], 1)

        claim = await claim_turn(self.a, conv["current_turn_id"])
        self.assertEqual(claim["round"], 1)
        self.assertEqual(claim["dialogue_context"]["objective"], "Arribar a una conclusió compartida.")
        self.assertEqual(claim["dialogue_context"]["protocol_version"], PROTOCOL_VERSION)
        self.assertIn("XERRAMECA DIALOGUE PROTOCOL v1", claim["input_message"]["content"])

        conv = await reply_turn(
            self.a,
            conv["current_turn_id"],
            ReplyRequest(content="A aporta la primera part.", result="continue", lease_token=claim["lease_token"]),
        )
        self.assertEqual(conv["current_round"], 1)
        self.assertEqual(conv["current_turn"]["assigned_agent_id"], "agent-b")
        self.assertEqual(conv["current_turn"]["turn_in_round"], 2)

        conv = await self.answer(self.b, conv, "B completa la ronda.", "continue")
        self.assertEqual(conv["current_round"], 2)
        self.assertEqual(conv["current_turn"]["assigned_agent_id"], "agent-a")
        self.assertEqual(conv["current_turn"]["turn_in_round"], 1)

    async def test_alternating_complete_requires_confirmation(self) -> None:
        conv = await self.create_started()
        conv = await self.answer(self.a, conv, "Crec que ja està resolt.", "complete")
        self.assertEqual(conv["status"], "active")
        self.assertTrue(conv["completion_pending"])
        self.assertEqual(conv["completion_proposed_by_agent_id"], "agent-a")
        self.assertEqual(conv["current_turn"]["assigned_agent_id"], "agent-b")
        self.assertEqual(conv["current_turn"]["phase"], "completion_confirmation")

        conv = await self.answer(self.b, conv, "Confirmo la conclusió.", "complete")
        self.assertEqual(conv["status"], "completed")
        self.assertFalse(conv["completion_pending"])

    async def test_rejecting_completion_continues_next_round(self) -> None:
        conv = await self.create_started()
        conv = await self.answer(self.a, conv, "Proposo acabar.", "complete")
        conv = await self.answer(self.b, conv, "Encara falta aquest punt.", "continue")
        self.assertEqual(conv["status"], "active")
        self.assertFalse(conv["completion_pending"])
        self.assertEqual(conv["current_round"], 2)
        self.assertEqual(conv["current_turn"]["assigned_agent_id"], "agent-a")

    async def test_supervisor_can_complete_unilaterally(self) -> None:
        conv = await self.create_started(policy="supervisor")
        conv = await self.answer(self.a, conv, "Com a supervisor dono l'objectiu per resolt.", "complete")
        self.assertEqual(conv["status"], "completed")

    async def test_monitor_detects_stalled_without_auto_pause_then_can_pause(self) -> None:
        conv = await self.create_started()
        async with get_db() as db:
            await db.execute(
                "UPDATE xerrameca_turns SET created_at = '2000-01-01T00:00:00.000000Z' WHERE id = ?",
                (conv["current_turn_id"],),
            )
            await db.commit()

        await update_monitor_state(MonitorUpdate(stalled_after_seconds=30, auto_pause_stalled=False))
        result = await monitor_once(persist=True)
        snap = next(x for x in result["conversations"] if x["conversation_id"] == conv["id"])
        self.assertEqual(snap["health"], "critical")
        self.assertIn("stalled_ready", {x["alert_type"] for x in snap["live_alerts"]})
        conv = await get_conversation(self.admin, conv["id"])
        self.assertEqual(conv["status"], "active")

        await update_monitor_state(MonitorUpdate(auto_pause_stalled=True))
        result = await monitor_once(persist=True)
        self.assertEqual(result["auto_paused"], 1)
        conv = await get_conversation(self.admin, conv["id"])
        self.assertEqual(conv["status"], "paused")
        self.assertEqual(conv["block_reason"], "monitor_stalled")
        alerts = await list_alerts()
        self.assertTrue(any(a["alert_type"] == "stalled_ready" for a in alerts))

    async def test_monitor_detects_exact_two_agent_loop_pattern(self) -> None:
        conv = await self.create_started(max_rounds=10)
        async with get_db() as db:
            for idx, (agent, text) in enumerate(
                [
                    ("agent-a", "mateixa proposta A"),
                    ("agent-b", "mateixa resposta B"),
                    ("agent-a", "mateixa proposta A"),
                    ("agent-b", "mateixa resposta B"),
                ],
                start=1,
            ):
                await db.execute(
                    """INSERT INTO xerrameca_messages
                       (id, conversation_id, round_no, from_agent_id, to_agent_id,
                        message_type, content, metadata, turn_result, created_at)
                       VALUES (?, ?, ?, ?, NULL, 'result', ?, '{}', 'continue', ?)""",
                    (f"loop-{idx}", conv["id"], idx, agent, text, f"2099-01-01T00:00:0{idx}.000000Z"),
                )
            await db.commit()
        result = await monitor_once(persist=False)
        snap = next(x for x in result["conversations"] if x["conversation_id"] == conv["id"])
        self.assertIn("possible_loop", {x["alert_type"] for x in snap["live_alerts"]})


if __name__ == "__main__":
    unittest.main()
