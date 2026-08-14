"""Regression tests del motor Xerrameca v1."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from pluribus.config import settings
from pluribus.db import get_db, init_db
from pluribus.xerrameca.models import (
    AssignTurnRequest,
    ConversationCreateRequest,
    ConversationSettingsUpdate,
    ReplyRequest,
    ResumeRequest,
    XerramecaSystemUpdate,
)
from pluribus.xerrameca.schema import init_xerrameca_db
from pluribus.xerrameca.service import (
    assign_turn,
    claim_turn,
    create_conversation,
    get_conversation,
    inbox,
    pause_conversation,
    reply_turn,
    resume_conversation,
    start_conversation,
    update_conversation_settings,
    update_system_state,
)


class XerramecaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = settings.DB_PATH
        settings.DB_PATH = str(Path(self.tmp.name) / "pluribus.db")
        await init_db()
        await init_xerrameca_db()

        self.admin = {
            "id": "admin",
            "name": "Admin",
            "permissions": {"read": True, "write": True, "delete": True, "admin": True},
            "allowed_scopes": ["shared", "private"],
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

    async def _conversation(
        self,
        *,
        max_rounds: int = 4,
        policy: str = "alternating",
        persist_summary: bool = False,
    ) -> dict:
        body = ConversationCreateRequest(
            name="Prova",
            objective="Agent A revisa la proposta i Agent B respon.",
            scope="shared",
            participant_agent_ids=["agent-a", "agent-b"],
            turn_policy=policy,
            supervisor_agent_id="agent-a" if policy == "supervisor" else None,
            first_agent_id="agent-a",
            max_rounds=max_rounds,
            turn_timeout_seconds=60,
            persist_summary=persist_summary,
        )
        return await create_conversation(self.admin, body)

    async def test_alternating_claim_reply_and_complete_persists_summary(self) -> None:
        conv = await self._conversation(persist_summary=True)
        conv = await start_conversation(self.admin, conv["id"])
        self.assertEqual(conv["current_turn"]["assigned_agent_id"], "agent-a")

        a_inbox = await inbox(self.agent_a)
        self.assertEqual(len(a_inbox["turns"]), 1)
        first_turn = a_inbox["turns"][0]["turn_id"]

        first_claim = await claim_turn(self.agent_a, first_turn)
        again = await claim_turn(self.agent_a, first_turn)
        self.assertEqual(again["lease_token"], first_claim["lease_token"])

        conv = await reply_turn(
            self.agent_a,
            first_turn,
            ReplyRequest(
                content="Revisió feta; passa-ho a B.",
                result="continue",
                lease_token=first_claim["lease_token"],
            ),
        )
        self.assertEqual(conv["current_round"], 2)
        self.assertEqual(conv["current_turn"]["assigned_agent_id"], "agent-b")

        second_turn = conv["current_turn_id"]
        second_claim = await claim_turn(self.agent_b, second_turn)
        with patch(
            "pluribus.xerrameca.service._generate_embeddings_background",
            new=AsyncMock(),
        ):
            conv = await reply_turn(
                self.agent_b,
                second_turn,
                ReplyRequest(
                    content="Validat. Tasca completada.",
                    result="complete",
                    lease_token=second_claim["lease_token"],
                ),
            )
            await asyncio.sleep(0)

        self.assertEqual(conv["status"], "completed")
        self.assertIsNotNone(conv["summary_fact_id"])
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT scope, category, content FROM facts WHERE id = ?",
                (conv["summary_fact_id"],),
            )
            fact = await cursor.fetchone()
        self.assertEqual(fact["scope"], "shared")
        self.assertEqual(fact["category"], "x-xerrameca")
        self.assertIn("Validat. Tasca completada.", fact["content"])

    async def test_global_disable_blocks_claim_without_destroying_turn(self) -> None:
        conv = await self._conversation()
        conv = await start_conversation(self.admin, conv["id"])
        turn_id = conv["current_turn_id"]

        await update_system_state(self.admin, XerramecaSystemUpdate(enabled=False))
        with self.assertRaises(HTTPException) as ctx:
            await claim_turn(self.agent_a, turn_id)
        self.assertEqual(ctx.exception.status_code, 423)

        await update_system_state(self.admin, XerramecaSystemUpdate(enabled=True))
        claim = await claim_turn(self.agent_a, turn_id)
        self.assertEqual(claim["turn_id"], turn_id)

    async def test_pause_revokes_claim_and_manual_assign_changes_owner(self) -> None:
        conv = await self._conversation()
        conv = await start_conversation(self.admin, conv["id"])
        turn_id = conv["current_turn_id"]
        await claim_turn(self.agent_a, turn_id)

        conv = await pause_conversation(self.admin, conv["id"], "intervenció")
        self.assertEqual(conv["status"], "paused")
        self.assertEqual(conv["current_turn"]["status"], "ready")
        self.assertIsNone(conv["current_turn"]["claimed_by"])

        conv = await assign_turn(
            self.admin,
            conv["id"],
            AssignTurnRequest(agent_id="agent-b", force=False),
        )
        self.assertEqual(conv["current_turn"]["assigned_agent_id"], "agent-b")
        conv = await resume_conversation(self.admin, conv["id"], ResumeRequest())
        self.assertEqual(conv["status"], "active")
        claim = await claim_turn(self.agent_b, turn_id)
        self.assertEqual(claim["conversation_id"], conv["id"])

    async def test_max_rounds_blocks_then_can_be_extended_and_resumed(self) -> None:
        conv = await self._conversation(max_rounds=1)
        conv = await start_conversation(self.admin, conv["id"])
        turn_id = conv["current_turn_id"]
        claim = await claim_turn(self.agent_a, turn_id)
        conv = await reply_turn(
            self.agent_a,
            turn_id,
            ReplyRequest(
                content="Necessito una altra ronda.",
                result="continue",
                lease_token=claim["lease_token"],
            ),
        )
        self.assertEqual(conv["status"], "blocked")
        self.assertEqual(conv["block_reason"], "max_rounds")

        conv = await update_conversation_settings(
            self.admin,
            conv["id"],
            ConversationSettingsUpdate(max_rounds=3),
        )
        conv = await resume_conversation(
            self.admin, conv["id"], ResumeRequest(next_agent_id="agent-b")
        )
        self.assertEqual(conv["status"], "active")
        self.assertEqual(conv["current_round"], 2)
        self.assertEqual(conv["current_turn"]["assigned_agent_id"], "agent-b")

    async def test_supervisor_can_select_next_agent_but_worker_cannot(self) -> None:
        conv = await self._conversation(policy="supervisor")
        conv = await start_conversation(self.admin, conv["id"])
        claim = await claim_turn(self.agent_a, conv["current_turn_id"])
        conv = await reply_turn(
            self.agent_a,
            conv["current_turn_id"],
            ReplyRequest(
                content="Em quedo un altre torn per completar el pla.",
                result="continue",
                lease_token=claim["lease_token"],
                next_agent_id="agent-a",
            ),
        )
        self.assertEqual(conv["current_turn"]["assigned_agent_id"], "agent-a")

        claim2 = await claim_turn(self.agent_a, conv["current_turn_id"])
        conv = await reply_turn(
            self.agent_a,
            conv["current_turn_id"],
            ReplyRequest(
                content="Ara sí, passa al worker.",
                result="continue",
                lease_token=claim2["lease_token"],
                next_agent_id="agent-b",
            ),
        )
        worker_turn = conv["current_turn_id"]
        worker_claim = await claim_turn(self.agent_b, worker_turn)
        with self.assertRaises(HTTPException) as ctx:
            await reply_turn(
                self.agent_b,
                worker_turn,
                ReplyRequest(
                    content="Intento decidir el següent torn.",
                    result="continue",
                    lease_token=worker_claim["lease_token"],
                    next_agent_id="agent-b",
                ),
            )
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_non_participant_cannot_read_conversation(self) -> None:
        conv = await self._conversation()
        outsider = {
            "id": "outsider",
            "name": "Outsider",
            "permissions": {"read": True, "write": True, "delete": False, "admin": False},
            "allowed_scopes": ["shared"],
        }
        with self.assertRaises(HTTPException) as ctx:
            await get_conversation(outsider, conv["id"])
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
