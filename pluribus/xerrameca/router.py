"""API REST de Xerrameca v1."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from .claim import claim_turn
from .control import update_participant_safe, update_system_state_safe
from .dialogue import (
    create_conversation,
    get_conversation,
    list_conversations,
    reply_turn,
    start_conversation,
)
from .models import (
    AssignTurnRequest,
    ConversationCreateRequest,
    ConversationSettingsUpdate,
    FinishRequest,
    ParticipantUpdate,
    ReasonRequest,
    ReplyRequest,
    ResumeRequest,
    SkipTurnRequest,
    XerramecaSystemUpdate,
)
from .service import (
    assign_turn,
    cancel_conversation,
    finish_conversation,
    get_system_state,
    inbox,
    list_messages,
    pause_conversation,
    resume_conversation,
    skip_turn,
    update_conversation_settings,
)


router = APIRouter(prefix="/v1/xerrameca", tags=["xerrameca"])


def _agent(request: Request) -> dict[str, Any]:
    return request.state.agent


@router.get("/system")
async def system_state(request: Request) -> dict[str, Any]:
    return await get_system_state(_agent(request))


@router.patch("/system")
async def system_update(
    request: Request, body: XerramecaSystemUpdate
) -> dict[str, Any]:
    return await update_system_state_safe(_agent(request), body)


@router.get("/inbox")
async def agent_inbox(request: Request) -> dict[str, Any]:
    return await inbox(_agent(request))


@router.post("/turns/{turn_id}/claim")
async def turn_claim(request: Request, turn_id: str) -> dict[str, Any]:
    return await claim_turn(_agent(request), turn_id)


@router.post("/turns/{turn_id}/reply")
async def turn_reply(
    request: Request, turn_id: str, body: ReplyRequest
) -> dict[str, Any]:
    return await reply_turn(_agent(request), turn_id, body)


@router.post("/conversations", status_code=201)
async def conversation_create(
    request: Request, body: ConversationCreateRequest
) -> dict[str, Any]:
    return await create_conversation(_agent(request), body)


@router.get("/conversations")
async def conversations(request: Request) -> list[dict[str, Any]]:
    return await list_conversations(_agent(request))


@router.get("/conversations/{conversation_id}")
async def conversation_get(
    request: Request, conversation_id: str
) -> dict[str, Any]:
    return await get_conversation(_agent(request), conversation_id)


@router.get("/conversations/{conversation_id}/messages")
async def conversation_messages(
    request: Request, conversation_id: str
) -> list[dict[str, Any]]:
    return await list_messages(_agent(request), conversation_id)


@router.post("/conversations/{conversation_id}/start")
async def conversation_start(
    request: Request, conversation_id: str
) -> dict[str, Any]:
    return await start_conversation(_agent(request), conversation_id)


@router.post("/conversations/{conversation_id}/pause")
async def conversation_pause(
    request: Request, conversation_id: str, body: ReasonRequest
) -> dict[str, Any]:
    return await pause_conversation(_agent(request), conversation_id, body.reason)


@router.post("/conversations/{conversation_id}/resume")
async def conversation_resume(
    request: Request, conversation_id: str, body: ResumeRequest
) -> dict[str, Any]:
    return await resume_conversation(_agent(request), conversation_id, body)


@router.patch("/conversations/{conversation_id}/settings")
async def conversation_settings(
    request: Request, conversation_id: str, body: ConversationSettingsUpdate
) -> dict[str, Any]:
    return await update_conversation_settings(
        _agent(request), conversation_id, body
    )


@router.patch("/conversations/{conversation_id}/participants/{agent_id}")
async def participant_settings(
    request: Request,
    conversation_id: str,
    agent_id: str,
    body: ParticipantUpdate,
) -> dict[str, Any]:
    return await update_participant_safe(
        _agent(request), conversation_id, agent_id, body
    )


@router.post("/conversations/{conversation_id}/turn/assign")
async def conversation_assign_turn(
    request: Request, conversation_id: str, body: AssignTurnRequest
) -> dict[str, Any]:
    return await assign_turn(_agent(request), conversation_id, body)


@router.post("/conversations/{conversation_id}/turn/skip")
async def conversation_skip_turn(
    request: Request, conversation_id: str, body: SkipTurnRequest
) -> dict[str, Any]:
    return await skip_turn(_agent(request), conversation_id, body)


@router.post("/conversations/{conversation_id}/finish")
async def conversation_finish(
    request: Request, conversation_id: str, body: FinishRequest
) -> dict[str, Any]:
    return await finish_conversation(_agent(request), conversation_id, body)


@router.post("/conversations/{conversation_id}/cancel")
async def conversation_cancel(
    request: Request, conversation_id: str
) -> dict[str, Any]:
    return await cancel_conversation(_agent(request), conversation_id)
