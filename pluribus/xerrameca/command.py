"""Uniform self-service `/xerrameca` command for authenticated agents.

The existing administrative REST surface stays admin-only. This module is a
narrow capability gateway: a normal agent may only start a dialogue where it is
itself a participant and first speaker, in the shared scope it can read/write.
"""

from __future__ import annotations

import json
import shlex
import uuid
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from pluribus.db import get_db

from .dialogue import (
    create_conversation,
    get_conversation,
    list_conversations,
    start_conversation,
)
from .models import ConversationCreateRequest
from .service import cancel_conversation, list_messages


DEFAULT_ROUNDS = 5
DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_DELAY_SECONDS = 2
MAX_DELAY_SECONDS = 3600
COMMAND_SCOPE = "shared"


class CommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str = Field(min_length=1, max_length=100_000)


HELP_TEXT = """XERRAMECA — Converses entre agents

Ús:
  /xerrameca <agent> <objectiu> [opcions]
      Inicia una conversa amb un altre agent.

  /xerrameca agents
      Llista els agents actius disponibles al scope shared.

  /xerrameca agents available
      Equivalent a `agents`; només mostra agents actius i compatibles.

  /xerrameca status
      Mostra les Xerrameques visibles per a l'agent.

  /xerrameca <conversation_id>
      Mostra estat i últims missatges d'una conversa.

  /xerrameca stop <conversation_id>
      Cancel·la una conversa iniciada per tu, si encara és cancel·lable.

  /xerrameca help
      Mostra aquesta ajuda.

Opcions:
  --rounds <n>       Màxim de rondes completes. Default: 5
  --timeout <sec>    Temps màxim de lease/resposta per torn. Default: 300
  --delay <sec>      Espera mínima entre una resposta i el torn següent. Default: 2
  --supervisor       L'iniciador és supervisor.

Exemples:
  /xerrameca agent2 Revisa aquesta arquitectura
  /xerrameca babufrik Busca errors --rounds 6 --timeout 120
  /xerrameca agent3 Debatiu aquesta proposta --rounds 8 --timeout 180 --delay 5
  /xerrameca agent2 Valida aquest canvi --supervisor
  /xerrameca agents
  /xerrameca status
"""


def _is_admin(agent: dict[str, Any]) -> bool:
    return bool((agent.get("permissions") or {}).get("admin", False))


def _require_command_access(agent: dict[str, Any]) -> None:
    if _is_admin(agent):
        return
    permissions = agent.get("permissions") or {}
    if not permissions.get("read", False) or not permissions.get("write", False):
        raise HTTPException(
            status_code=403,
            detail="Xerrameca command requereix permisos read + write",
        )
    if COMMAND_SCOPE not in (agent.get("allowed_scopes") or []):
        raise HTTPException(
            status_code=403,
            detail=f"Xerrameca command requereix accés al scope '{COMMAND_SCOPE}'",
        )


def _internal_admin(agent: dict[str, Any]) -> dict[str, Any]:
    """Elevate only after command-specific self-service checks have passed.

    Existing create/start/cancel services intentionally remain admin-only. The
    audit identity is preserved: only the permissions view is elevated inside
    this narrow command boundary.
    """
    permissions = dict(agent.get("permissions") or {})
    permissions["admin"] = True
    return {**agent, "permissions": permissions}


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    try:
        parsed = json.loads(value or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _can_participate(permissions: dict[str, Any]) -> bool:
    return bool(
        permissions.get("admin", False)
        or (permissions.get("read", False) and permissions.get("write", False))
    )


async def _available_agents(agent: dict[str, Any]) -> list[dict[str, Any]]:
    _require_command_access(agent)
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT id, name, permissions, allowed_scopes, capabilities,
                      last_active_at
               FROM agents
               WHERE is_active = 1 AND id != ?
               ORDER BY name COLLATE NOCASE, id""",
            (agent["id"],),
        )
        result: list[dict[str, Any]] = []
        for row in await cursor.fetchall():
            scopes = _json_list(row["allowed_scopes"])
            permissions = _json_object(row["permissions"])
            if COMMAND_SCOPE not in scopes or not _can_participate(permissions):
                continue
            result.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "status": "active",
                    "scope": COMMAND_SCOPE,
                    "capabilities": _json_object(row["capabilities"]),
                    "last_active_at": row["last_active_at"],
                }
            )
        return result


async def _resolve_target(agent: dict[str, Any], token: str) -> dict[str, Any]:
    agents = await _available_agents(agent)
    exact_id = [item for item in agents if item["id"] == token]
    if exact_id:
        return exact_id[0]
    exact_name = [item for item in agents if item["name"].casefold() == token.casefold()]
    if len(exact_name) == 1:
        return exact_name[0]
    if len(exact_name) > 1:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Nom d'agent ambigu; utilitza agent_id",
                "candidates": [item["id"] for item in exact_name],
            },
        )
    raise HTTPException(status_code=404, detail=f"Agent '{token}' no disponible")


def _parse_int(value: str, option: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{option} ha de ser un enter") from exc
    if not minimum <= parsed <= maximum:
        raise HTTPException(
            status_code=422,
            detail=f"{option} ha d'estar entre {minimum} i {maximum}",
        )
    return parsed


def _parse_start(tokens: list[str]) -> tuple[str, str, int, int, int, bool]:
    if len(tokens) < 2:
        raise HTTPException(
            status_code=422,
            detail="Falta l'objectiu. Ús: /xerrameca <agent> <objectiu> [opcions]",
        )
    target = tokens[0]
    rounds = DEFAULT_ROUNDS
    timeout = DEFAULT_TIMEOUT_SECONDS
    delay = DEFAULT_DELAY_SECONDS
    supervisor = False
    objective: list[str] = []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--supervisor":
            supervisor = True
            index += 1
            continue
        matched = False
        for option, minimum, maximum in (
            ("--rounds", 1, 200),
            ("--timeout", 10, 86400),
            ("--delay", 0, MAX_DELAY_SECONDS),
        ):
            if token == option:
                if index + 1 >= len(tokens):
                    raise HTTPException(status_code=422, detail=f"Falta valor per {option}")
                value = _parse_int(tokens[index + 1], option, minimum, maximum)
                if option == "--rounds":
                    rounds = value
                elif option == "--timeout":
                    timeout = value
                else:
                    delay = value
                index += 2
                matched = True
                break
            prefix = option + "="
            if token.startswith(prefix):
                value = _parse_int(token[len(prefix) :], option, minimum, maximum)
                if option == "--rounds":
                    rounds = value
                elif option == "--timeout":
                    timeout = value
                else:
                    delay = value
                index += 1
                matched = True
                break
        if matched:
            continue
        if token.startswith("--"):
            raise HTTPException(status_code=422, detail=f"Opció desconeguda: {token}")
        objective.append(token)
        index += 1

    text = " ".join(objective).strip()
    if not text:
        raise HTTPException(status_code=422, detail="L'objectiu no pot estar buit")
    return target, text, rounds, timeout, delay, supervisor


async def _set_turn_delay(conversation_id: str, delay: int) -> None:
    async with get_db() as db:
        await db.execute(
            """UPDATE xerrameca_conversations
               SET turn_delay_seconds = ? WHERE id = ?""",
            (delay, conversation_id),
        )
        await db.commit()


async def _delay_for(conversation_id: str) -> int:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT turn_delay_seconds FROM xerrameca_conversations WHERE id = ?",
            (conversation_id,),
        )
        row = await cursor.fetchone()
        return int(row["turn_delay_seconds"] if row else 0)


async def _annotate_kickoff(conversation_id: str, delay: int) -> None:
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT t.input_message_id, m.content, m.metadata
               FROM xerrameca_conversations c
               JOIN xerrameca_turns t ON t.id = c.current_turn_id
               JOIN xerrameca_messages m ON m.id = t.input_message_id
               WHERE c.id = ?""",
            (conversation_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return
        metadata = _json_object(row["metadata"])
        metadata["turn_delay_seconds"] = delay
        content = row["content"]
        marker = f"Espera mínima entre torns: {delay} s"
        if marker not in content:
            content = content.rstrip() + f"\n- {marker}.\n"
        await db.execute(
            "UPDATE xerrameca_messages SET content = ?, metadata = ? WHERE id = ?",
            (
                content,
                json.dumps(metadata, ensure_ascii=False),
                row["input_message_id"],
            ),
        )
        await db.commit()


async def _enrich_conversation(payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(payload)
    payload["turn_delay_seconds"] = await _delay_for(payload["id"])
    turn = payload.get("current_turn")
    if turn:
        turn = dict(turn)
        turn["ready_at"] = turn.get("created_at")
        payload["current_turn"] = turn
    return payload


async def _start(agent: dict[str, Any], tokens: list[str]) -> dict[str, Any]:
    _require_command_access(agent)
    target_token, objective, rounds, timeout, delay, supervisor = _parse_start(tokens)
    target = await _resolve_target(agent, target_token)
    privileged = _internal_admin(agent)
    body = ConversationCreateRequest(
        name=(f"{agent.get('name') or agent['id']} ↔ {target['name']}")[:128],
        objective=objective,
        scope=COMMAND_SCOPE,
        participant_agent_ids=[agent["id"], target["id"]],
        turn_policy="supervisor" if supervisor else "alternating",
        supervisor_agent_id=agent["id"] if supervisor else None,
        first_agent_id=agent["id"],
        max_rounds=rounds,
        turn_timeout_seconds=timeout,
        persist_summary=True,
    )
    created = await create_conversation(privileged, body)
    try:
        await _set_turn_delay(created["id"], delay)
        started = await start_conversation(privileged, created["id"])
        await _annotate_kickoff(created["id"], delay)
    except Exception:
        try:
            await cancel_conversation(privileged, created["id"])
        except Exception:
            pass
        raise
    started = await _enrich_conversation(started)
    return {
        "kind": "started",
        "text": (
            f"Xerrameca iniciada amb {target['name']} ({target['id']}). "
            f"rounds={rounds}, timeout={timeout}s, delay={delay}s, "
            f"policy={'supervisor' if supervisor else 'alternating'}."
        ),
        "conversation": started,
    }


async def _agents(agent: dict[str, Any], tokens: list[str]) -> dict[str, Any]:
    if tokens and tokens != ["available"]:
        raise HTTPException(status_code=422, detail="Ús: /xerrameca agents [available]")
    agents = await _available_agents(agent)
    lines = [f"{item['name']} — {item['id']} — active" for item in agents]
    return {
        "kind": "agents",
        "text": "AGENTS DISPONIBLES\n" + ("\n".join(lines) if lines else "Cap agent disponible"),
        "agents": agents,
    }


async def _status(agent: dict[str, Any]) -> dict[str, Any]:
    conversations = await list_conversations(agent)
    enriched = [await _enrich_conversation(item) for item in conversations]
    lines = [
        f"{item['id']} — {item['status']} — ronda {item['current_round']}/{item['max_rounds']} — {item['name']}"
        for item in enriched[:50]
    ]
    return {
        "kind": "status",
        "text": "XERRAMEQUES\n" + ("\n".join(lines) if lines else "Cap Xerrameca visible"),
        "conversations": enriched,
    }


def _looks_like_conversation_id(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError):
        return False


async def _get(agent: dict[str, Any], conversation_id: str) -> dict[str, Any]:
    conversation = await _enrich_conversation(
        await get_conversation(agent, conversation_id)
    )
    messages = await list_messages(agent, conversation_id)
    recent = messages[-8:]
    return {
        "kind": "conversation",
        "text": (
            f"{conversation['name']} — {conversation['status']} — "
            f"ronda {conversation['current_round']}/{conversation['max_rounds']} — "
            f"delay={conversation['turn_delay_seconds']}s"
        ),
        "conversation": conversation,
        "recent_messages": recent,
    }


async def _stop(agent: dict[str, Any], conversation_id: str) -> dict[str, Any]:
    conversation = await get_conversation(agent, conversation_id)
    if not _is_admin(agent) and conversation.get("created_by_agent_id") != agent["id"]:
        raise HTTPException(
            status_code=403,
            detail="Només l'agent que ha iniciat aquesta Xerrameca la pot cancel·lar",
        )
    stopped = await cancel_conversation(_internal_admin(agent), conversation_id)
    stopped = await _enrich_conversation(stopped)
    return {
        "kind": "stopped",
        "text": f"Xerrameca {conversation_id} cancel·lada.",
        "conversation": stopped,
    }


async def run_command(agent: dict[str, Any], command: str) -> dict[str, Any]:
    """Parse and execute one uniform Xerrameca slash command."""
    try:
        tokens = shlex.split(command.strip())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Comanda invàlida: {exc}") from exc
    if not tokens or tokens[0].lower() not in {"/xerrameca", "xerrameca"}:
        raise HTTPException(status_code=422, detail="La comanda ha de començar per /xerrameca")

    args = tokens[1:]
    if not args or args[0].lower() in {"help", "-h", "--help"}:
        return {"kind": "help", "text": HELP_TEXT}

    action = args[0].lower()
    if action == "agents":
        return await _agents(agent, [item.lower() for item in args[1:]])
    if action == "status":
        if len(args) != 1:
            raise HTTPException(status_code=422, detail="Ús: /xerrameca status")
        return await _status(agent)
    if action == "stop":
        if len(args) != 2:
            raise HTTPException(status_code=422, detail="Ús: /xerrameca stop <conversation_id>")
        return await _stop(agent, args[1])
    if len(args) == 1 and _looks_like_conversation_id(args[0]):
        return await _get(agent, args[0])
    return await _start(agent, args)
