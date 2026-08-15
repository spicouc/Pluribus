"""Administrative REST API for Xerrameca Runner v1."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response

from .runner import (
    RunnerConfigUpsert,
    RunnerSystemUpdate,
    delete_runner_config,
    get_runner_system,
    list_runner_configs,
    rotate_runner_secret,
    update_runner_system,
    upsert_runner_config,
)
from .runner_dialogue import runner_tick


router = APIRouter(prefix="/v1/xerrameca", tags=["xerrameca-runner"])


def _agent(request: Request) -> dict[str, Any]:
    return request.state.agent


@router.get("/runner/system")
async def runner_system_get(request: Request) -> dict[str, Any]:
    return await get_runner_system(_agent(request))


@router.patch("/runner/system")
async def runner_system_patch(
    request: Request, body: RunnerSystemUpdate
) -> dict[str, Any]:
    return await update_runner_system(_agent(request), body)


@router.post("/runner/tick")
async def runner_manual_tick(request: Request) -> dict[str, Any]:
    return await runner_tick(_agent(request))


@router.get("/runners")
async def runner_configs(request: Request) -> list[dict[str, Any]]:
    return await list_runner_configs(_agent(request))


@router.put("/runners/{agent_id}")
async def runner_config_put(
    request: Request, agent_id: str, body: RunnerConfigUpsert
) -> dict[str, Any]:
    return await upsert_runner_config(_agent(request), agent_id, body)


@router.post("/runners/{agent_id}/rotate-secret")
async def runner_secret_rotate(request: Request, agent_id: str) -> dict[str, Any]:
    return await rotate_runner_secret(_agent(request), agent_id)


@router.delete("/runners/{agent_id}", status_code=204)
async def runner_config_delete(request: Request, agent_id: str) -> Response:
    await delete_runner_config(_agent(request), agent_id)
    return Response(status_code=204)
