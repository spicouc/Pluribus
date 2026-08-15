"""Adaptador MCP de Xerrameca per a agents."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from .claim import claim_turn
from .dialogue import get_conversation, list_conversations, reply_turn
from .models import ReplyRequest
from .service import inbox, list_messages


TOOLS = [
    {
        "name": "xerrameca_inbox",
        "description": "Llista els torns Xerrameca disponibles per a l'agent autenticat.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "xerrameca_claim",
        "description": "Reclama atòmicament un torn i obté lease + context Dialogue Protocol v1.",
        "input_schema": {
            "type": "object",
            "properties": {"turn_id": {"type": "string"}},
            "required": ["turn_id"],
        },
    },
    {
        "name": "xerrameca_reply",
        "description": "Respon un torn reclamat. En alternating, complete proposa tancament i l'altre agent l'ha de confirmar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "turn_id": {"type": "string"},
                "lease_token": {"type": "string"},
                "content": {"type": "string"},
                "result": {
                    "type": "string",
                    "enum": ["continue", "complete", "blocked", "needs_human", "error"],
                    "default": "continue",
                },
                "next_agent_id": {
                    "type": "string",
                    "description": "Només el supervisor; Dialogue v1 manté alternança entre els dos participants.",
                },
                "metadata": {"type": "object", "default": {}},
            },
            "required": ["turn_id", "lease_token", "content"],
        },
    },
    {
        "name": "xerrameca_list",
        "description": "Llista les Xerrameques visibles per a l'agent.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "xerrameca_get",
        "description": "Obté l'estat i protocol d'una Xerrameca on l'agent participa.",
        "input_schema": {
            "type": "object",
            "properties": {"conversation_id": {"type": "string"}},
            "required": ["conversation_id"],
        },
    },
    {
        "name": "xerrameca_messages",
        "description": "Obté l'historial estructurat d'una Xerrameca visible.",
        "input_schema": {
            "type": "object",
            "properties": {"conversation_id": {"type": "string"}},
            "required": ["conversation_id"],
        },
    },
]

TOOL_NAMES = {tool["name"] for tool in TOOLS}


def _required(arguments: dict[str, Any], key: str) -> Any:
    value = arguments.get(key)
    if value is None or value == "":
        raise HTTPException(status_code=422, detail=f"{key} és obligatori")
    return value


async def handle_tool(
    request: Request,
    tool_name: str,
    arguments: dict[str, Any],
) -> Any:
    agent = request.state.agent
    if tool_name == "xerrameca_inbox":
        return await inbox(agent)
    if tool_name == "xerrameca_claim":
        return await claim_turn(agent, str(_required(arguments, "turn_id")))
    if tool_name == "xerrameca_reply":
        body = ReplyRequest(
            content=_required(arguments, "content"),
            result=arguments.get("result", "continue"),
            lease_token=_required(arguments, "lease_token"),
            next_agent_id=arguments.get("next_agent_id"),
            metadata=arguments.get("metadata") or {},
        )
        return await reply_turn(
            agent,
            str(_required(arguments, "turn_id")),
            body,
        )
    if tool_name == "xerrameca_list":
        return await list_conversations(agent)
    if tool_name == "xerrameca_get":
        return await get_conversation(
            agent, str(_required(arguments, "conversation_id"))
        )
    if tool_name == "xerrameca_messages":
        return await list_messages(
            agent, str(_required(arguments, "conversation_id"))
        )
    raise HTTPException(status_code=404, detail="Eina Xerrameca desconeguda")
