"""Dialogue-aware Runner dispatch while preserving Runner v1 configuration/state."""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

from fastapi import HTTPException

from pluribus.webhooks import (
    _post_pinned,
    _resolve_webhook_target,
    _serialize_payload,
    _signature,
)

from .claim import claim_turn
from .runner import (
    _candidate_rows as _legacy_candidate_rows,
    _record_failure,
    _record_success,
    _release_claim,
    _require_admin,
    _runner_runtime,
)
from .service import _now


async def _candidate_rows(limit: int) -> list[dict[str, Any]]:
    """Filter out successor turns whose command-level delay has not elapsed."""
    candidates = await _legacy_candidate_rows(max(limit * 4, limit))
    now = _now()
    ready = [
        candidate
        for candidate in candidates
        if not candidate.get("turn_created_at")
        or candidate["turn_created_at"] <= now
    ]
    return ready[:limit]


async def _dispatch_one(candidate: dict[str, Any]) -> dict[str, Any]:
    agent = {
        "id": candidate["assigned_agent_id"],
        "name": candidate["agent_name"],
        "permissions": candidate["permissions"],
        "allowed_scopes": candidate["allowed_scopes"],
    }
    try:
        claim = await claim_turn(agent, candidate["turn_id"])
    except HTTPException as exc:
        if exc.status_code == 409:
            return {"turn_id": candidate["turn_id"], "status": "raced"}
        raise

    delivery_id = secrets.token_hex(16)
    payload = {
        "event": "xerrameca.turn.claimed",
        "delivery_id": delivery_id,
        "idempotency_key": candidate["turn_id"],
        "agent": {
            "id": candidate["assigned_agent_id"],
            "name": candidate["agent_name"],
        },
        "conversation": {
            "id": candidate["conversation_id"],
            "name": candidate["conversation_name"],
            "objective": candidate["objective"],
            "scope": candidate["scope"],
            "turn_policy": candidate["turn_policy"],
            "max_rounds": candidate["max_rounds"],
        },
        "turn": {
            "id": claim["turn_id"],
            "round": claim["round"],
            "turn_sequence": claim.get("turn_sequence"),
            "turn_in_round": claim.get("turn_in_round"),
            "phase": claim.get("phase"),
            "ready_at": claim.get("ready_at"),
            "lease_token": claim["lease_token"],
            "lease_until": claim["lease_until"],
        },
        "input_message": claim["input_message"],
        "dialogue_context": claim.get("dialogue_context") or {},
        "reply": {
            "rest_path": f"/v1/xerrameca/turns/{claim['turn_id']}/reply",
            "mcp_tool": "xerrameca_reply",
        },
    }

    status: int | None = None
    try:
        target = await _resolve_webhook_target(candidate["endpoint_url"])
        body = _serialize_payload(payload)
        headers = {
            "Content-Type": "application/json",
            "X-Pluribus-Event": "xerrameca.turn.claimed",
            "X-Pluribus-Delivery": delivery_id,
            "X-Pluribus-Idempotency-Key": candidate["turn_id"],
            "X-Pluribus-Signature": _signature(candidate["secret"], body),
        }
        async with asyncio.timeout(candidate["request_timeout_seconds"]):
            status = await _post_pinned(target, body, headers)
        if not 200 <= status < 300:
            raise RuntimeError(f"HTTP {status}")
    except Exception as exc:
        await _release_claim(
            candidate["turn_id"],
            candidate["assigned_agent_id"],
            claim["lease_token"],
        )
        await _record_failure(
            candidate["assigned_agent_id"],
            status,
            type(exc).__name__ if status is None else f"HTTP {status}",
            candidate["max_failures"],
            candidate["cooldown_seconds"],
            candidate["turn_id"],
        )
        return {
            "turn_id": candidate["turn_id"],
            "agent_id": candidate["assigned_agent_id"],
            "status": "failed",
            "http_status": status,
        }

    await _record_success(candidate["assigned_agent_id"], status, candidate["turn_id"])
    return {
        "turn_id": candidate["turn_id"],
        "agent_id": candidate["assigned_agent_id"],
        "status": "dispatched",
        "http_status": status,
        "lease_until": claim["lease_until"],
        "dialogue_round": claim["round"],
        "phase": claim.get("phase"),
    }


async def runner_tick(admin: dict[str, Any] | None = None) -> dict[str, Any]:
    if admin is not None:
        _require_admin(admin)
    runtime = await _runner_runtime()
    if not runtime or not bool(runtime["enabled"]):
        return {"enabled": False, "attempted": 0, "results": []}

    candidates = await _candidate_rows(int(runtime["max_dispatches_per_tick"]))
    results: list[dict[str, Any]] = []
    for candidate in candidates:
        try:
            results.append(await _dispatch_one(candidate))
        except HTTPException as exc:
            results.append(
                {
                    "turn_id": candidate["turn_id"],
                    "agent_id": candidate["assigned_agent_id"],
                    "status": "skipped",
                    "reason": f"HTTP {exc.status_code}",
                }
            )
        except Exception:
            results.append(
                {
                    "turn_id": candidate["turn_id"],
                    "agent_id": candidate["assigned_agent_id"],
                    "status": "error",
                }
            )
    return {"enabled": True, "attempted": len(results), "results": results}


async def runner_loop() -> None:
    while True:
        try:
            runtime = await _runner_runtime()
            if runtime and bool(runtime["enabled"]):
                await runner_tick()
                delay = float(runtime["poll_interval_seconds"])
            else:
                delay = 2.0
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(2.0)
