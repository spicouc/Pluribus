"""Automatic signed dispatch for Xerrameca turns.

The Runner never executes arbitrary local commands. It atomically claims a ready
turn for the target agent and POSTs a signed JSON envelope to that agent's
configured HTTP endpoint. The remote agent processes the message and completes
the turn through the normal Xerrameca REST/MCP reply API using its own API key.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import secrets
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from pluribus.audit import log_audit
from pluribus.db import get_db
from pluribus.webhooks import (
    _post_pinned,
    _resolve_webhook_target,
    _serialize_payload,
    _signature,
    _validate_webhook_url,
)

from .claim import claim_turn
from .service import _now


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunnerSystemUpdate(_StrictModel):
    enabled: bool | None = None
    poll_interval_seconds: float | None = Field(default=None, ge=0.25, le=60.0)
    max_dispatches_per_tick: int | None = Field(default=None, ge=1, le=100)


class RunnerConfigUpsert(_StrictModel):
    endpoint_url: str = Field(min_length=1, max_length=2048)
    enabled: bool = True
    request_timeout_seconds: int = Field(default=30, ge=2, le=120)
    max_failures: int = Field(default=3, ge=1, le=20)
    cooldown_seconds: int = Field(default=60, ge=10, le=3600)


async def _audit(
    db: Any,
    agent_id: str,
    action: str,
    resource_id: str,
    payload: dict[str, Any] | None = None,
) -> None:
    await log_audit(
        db,
        agent_id,
        action,
        "xerrameca_runner",
        resource_id=resource_id,
        payload=json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
    )


def _require_admin(agent: dict[str, Any]) -> None:
    if not bool((agent.get("permissions") or {}).get("admin", False)):
        raise HTTPException(status_code=403, detail="Xerrameca Runner: permís admin requerit")


def _utc_after(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def _parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    try:
        parsed = json.loads(value or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


async def get_runner_system(agent: dict[str, Any]) -> dict[str, Any]:
    _require_admin(agent)
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT enabled, poll_interval_seconds, max_dispatches_per_tick,
                      updated_at
               FROM xerrameca_runner_runtime WHERE singleton = 1"""
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=500, detail="Runtime Runner no inicialitzat")
        return {
            "enabled": bool(row["enabled"]),
            "poll_interval_seconds": row["poll_interval_seconds"],
            "max_dispatches_per_tick": row["max_dispatches_per_tick"],
            "updated_at": row["updated_at"],
        }


async def update_runner_system(
    agent: dict[str, Any], body: RunnerSystemUpdate
) -> dict[str, Any]:
    _require_admin(agent)
    values = body.model_dump(exclude_none=True)
    if not values:
        raise HTTPException(status_code=400, detail="No hi ha canvis")

    updates: list[str] = []
    params: list[Any] = []
    for key in ("poll_interval_seconds", "max_dispatches_per_tick"):
        if key in values:
            updates.append(f"{key} = ?")
            params.append(values[key])
    if "enabled" in values:
        updates.append("enabled = ?")
        params.append(1 if values["enabled"] else 0)
    updates.append("updated_at = ?")
    params.append(_now())

    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            f"UPDATE xerrameca_runner_runtime SET {', '.join(updates)} WHERE singleton = 1",
            params,
        )
        await _audit(db, agent["id"], "XERRAMECA_RUNNER_SYSTEM", "system", values)
        await db.commit()
    return await get_runner_system(agent)


def _runner_public(row: Any) -> dict[str, Any]:
    return {
        "agent_id": row["agent_id"],
        "agent_name": row["agent_name"] if "agent_name" in row.keys() else None,
        "endpoint_url": row["endpoint_url"],
        "enabled": bool(row["enabled"]),
        "request_timeout_seconds": row["request_timeout_seconds"],
        "max_failures": row["max_failures"],
        "cooldown_seconds": row["cooldown_seconds"],
        "consecutive_failures": row["consecutive_failures"],
        "circuit_open_until": row["circuit_open_until"],
        "last_attempted_at": row["last_attempted_at"],
        "last_success_at": row["last_success_at"],
        "last_status": row["last_status"],
        "last_error": row["last_error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


async def list_runner_configs(agent: dict[str, Any]) -> list[dict[str, Any]]:
    _require_admin(agent)
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT r.*, a.name AS agent_name
               FROM xerrameca_runners r
               JOIN agents a ON a.id = r.agent_id
               ORDER BY a.name, r.agent_id"""
        )
        return [_runner_public(row) for row in await cursor.fetchall()]


async def upsert_runner_config(
    admin: dict[str, Any], agent_id: str, body: RunnerConfigUpsert
) -> dict[str, Any]:
    _require_admin(admin)
    endpoint_url = await _validate_webhook_url(body.endpoint_url)
    now = _now()
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT id, is_active FROM agents WHERE id = ?", (agent_id,)
        )
        target = await cursor.fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="Agent no trobat")

        cursor = await db.execute(
            "SELECT secret, created_at FROM xerrameca_runners WHERE agent_id = ?",
            (agent_id,),
        )
        existing = await cursor.fetchone()
        created = existing is None
        secret = existing["secret"] if existing else secrets.token_urlsafe(32)
        created_at = existing["created_at"] if existing else now
        await db.execute(
            """INSERT INTO xerrameca_runners
               (agent_id, endpoint_url, secret, enabled, request_timeout_seconds,
                max_failures, cooldown_seconds, consecutive_failures,
                circuit_open_until, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?)
               ON CONFLICT(agent_id) DO UPDATE SET
                 endpoint_url = excluded.endpoint_url,
                 enabled = excluded.enabled,
                 request_timeout_seconds = excluded.request_timeout_seconds,
                 max_failures = excluded.max_failures,
                 cooldown_seconds = excluded.cooldown_seconds,
                 consecutive_failures = 0,
                 circuit_open_until = NULL,
                 last_error = NULL,
                 updated_at = excluded.updated_at""",
            (
                agent_id,
                endpoint_url,
                secret,
                1 if body.enabled else 0,
                body.request_timeout_seconds,
                body.max_failures,
                body.cooldown_seconds,
                created_at,
                now,
            ),
        )
        await _audit(
            db,
            admin["id"],
            "XERRAMECA_RUNNER_CONFIG",
            agent_id,
            {
                "endpoint_url": endpoint_url,
                "enabled": body.enabled,
                "request_timeout_seconds": body.request_timeout_seconds,
                "max_failures": body.max_failures,
                "cooldown_seconds": body.cooldown_seconds,
                "created": created,
            },
        )
        await db.commit()
        cursor = await db.execute(
            """SELECT r.*, a.name AS agent_name
               FROM xerrameca_runners r JOIN agents a ON a.id = r.agent_id
               WHERE r.agent_id = ?""",
            (agent_id,),
        )
        row = await cursor.fetchone()
        result = _runner_public(row)
        if created:
            result["secret"] = secret
            result["secret_notice"] = "Guarda el secret; no es torna a mostrar excepte en rotar-lo"
        return result


async def rotate_runner_secret(admin: dict[str, Any], agent_id: str) -> dict[str, Any]:
    _require_admin(admin)
    secret = secrets.token_urlsafe(32)
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """UPDATE xerrameca_runners
               SET secret = ?, consecutive_failures = 0, circuit_open_until = NULL,
                   last_error = NULL, updated_at = ?
               WHERE agent_id = ?""",
            (secret, _now(), agent_id),
        )
        if cursor.rowcount != 1:
            raise HTTPException(status_code=404, detail="Runner d'agent no configurat")
        await _audit(db, admin["id"], "XERRAMECA_RUNNER_ROTATE_SECRET", agent_id)
        await db.commit()
    return {
        "agent_id": agent_id,
        "secret": secret,
        "secret_notice": "Guarda el secret; substitueix immediatament l'anterior",
    }


async def delete_runner_config(admin: dict[str, Any], agent_id: str) -> None:
    _require_admin(admin)
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "DELETE FROM xerrameca_runners WHERE agent_id = ?", (agent_id,)
        )
        if cursor.rowcount != 1:
            raise HTTPException(status_code=404, detail="Runner d'agent no configurat")
        await _audit(db, admin["id"], "XERRAMECA_RUNNER_DELETE", agent_id)
        await db.commit()


async def _runner_runtime() -> Any:
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT enabled, poll_interval_seconds, max_dispatches_per_tick
               FROM xerrameca_runner_runtime WHERE singleton = 1"""
        )
        return await cursor.fetchone()


async def _candidate_rows(limit: int) -> list[dict[str, Any]]:
    now = _now()
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT t.id AS turn_id, t.conversation_id, t.round_no,
                      t.assigned_agent_id, t.created_at AS turn_created_at,
                      c.name AS conversation_name, c.objective, c.scope,
                      c.turn_policy, c.max_rounds, c.turn_timeout_seconds,
                      r.endpoint_url, r.secret, r.request_timeout_seconds,
                      r.max_failures, r.cooldown_seconds,
                      a.name AS agent_name, a.permissions, a.allowed_scopes,
                      m.id AS input_message_id, m.from_agent_id, m.to_agent_id,
                      m.message_type, m.content, m.metadata,
                      m.created_at AS message_created_at
               FROM xerrameca_turns t
               JOIN xerrameca_conversations c ON c.id = t.conversation_id
               JOIN xerrameca_participants p
                 ON p.conversation_id = c.id AND p.agent_id = t.assigned_agent_id
               JOIN xerrameca_runners r ON r.agent_id = t.assigned_agent_id
               JOIN agents a ON a.id = t.assigned_agent_id
               JOIN xerrameca_messages m ON m.id = t.input_message_id
               JOIN xerrameca_runtime xr ON xr.singleton = 1
               WHERE xr.enabled = 1
                 AND c.status = 'active' AND c.enabled = 1
                 AND p.enabled = 1 AND a.is_active = 1 AND r.enabled = 1
                 AND (r.circuit_open_until IS NULL OR r.circuit_open_until <= ?)
                 AND (t.status = 'ready'
                      OR (t.status = 'claimed' AND t.lease_until <= ?))
               ORDER BY t.created_at ASC
               LIMIT ?""",
            (now, now, max(limit * 4, limit)),
        )
        rows = []
        seen_agents: set[str] = set()
        for row in await cursor.fetchall():
            agent_id = row["assigned_agent_id"]
            if agent_id in seen_agents:
                continue
            seen_agents.add(agent_id)
            item = dict(row)
            item["permissions"] = _parse_json_object(item["permissions"])
            item["allowed_scopes"] = _parse_json_list(item["allowed_scopes"])
            item["metadata"] = _parse_json_object(item["metadata"])
            rows.append(item)
            if len(rows) >= limit:
                break
        return rows


async def _release_claim(turn_id: str, agent_id: str, lease_token: str) -> bool:
    """Release only the exact lease acquired by this Runner attempt."""
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """UPDATE xerrameca_turns
               SET status = 'ready', claimed_by = NULL, lease_token = NULL,
                   claimed_at = NULL, lease_until = NULL
               WHERE id = ? AND status = 'claimed'
                 AND claimed_by = ? AND lease_token = ?""",
            (turn_id, agent_id, lease_token),
        )
        await db.commit()
        return cursor.rowcount == 1


async def _record_success(agent_id: str, status: int, turn_id: str) -> None:
    async with get_db() as db:
        await db.execute(
            """UPDATE xerrameca_runners
               SET consecutive_failures = 0, circuit_open_until = NULL,
                   last_attempted_at = ?, last_success_at = ?, last_status = ?,
                   last_error = NULL, updated_at = ?
               WHERE agent_id = ?""",
            (_now(), _now(), status, _now(), agent_id),
        )
        await _audit(
            db,
            agent_id,
            "XERRAMECA_RUNNER_DISPATCH",
            turn_id,
            {"status": status},
        )
        await db.commit()


async def _record_failure(
    agent_id: str,
    status: int | None,
    error: str,
    max_failures: int,
    cooldown_seconds: int,
    turn_id: str,
) -> None:
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT consecutive_failures FROM xerrameca_runners WHERE agent_id = ?",
            (agent_id,),
        )
        row = await cursor.fetchone()
        failures = (row["consecutive_failures"] if row else 0) + 1
        circuit_until = _utc_after(cooldown_seconds) if failures >= max_failures else None
        await db.execute(
            """UPDATE xerrameca_runners
               SET consecutive_failures = ?, circuit_open_until = ?,
                   last_attempted_at = ?, last_status = ?, last_error = ?,
                   updated_at = ?
               WHERE agent_id = ?""",
            (
                failures,
                circuit_until,
                _now(),
                status,
                error[:500],
                _now(),
                agent_id,
            ),
        )
        await _audit(
            db,
            agent_id,
            "XERRAMECA_RUNNER_FAILED",
            turn_id,
            {"status": status, "failures": failures, "circuit_open_until": circuit_until},
        )
        await db.commit()


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
            "lease_token": claim["lease_token"],
            "lease_until": claim["lease_until"],
        },
        "input_message": claim["input_message"],
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
    }


async def runner_tick(admin: dict[str, Any] | None = None) -> dict[str, Any]:
    """Process a bounded set of ready turns. Admin is required for manual ticks."""
    if admin is not None:
        _require_admin(admin)
    runtime = await _runner_runtime()
    if not runtime or not bool(runtime["enabled"]):
        return {"enabled": False, "attempted": 0, "results": []}

    limit = int(runtime["max_dispatches_per_tick"])
    candidates = await _candidate_rows(limit)
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
    """Background dispatcher. Runtime enable/disable is read on every cycle."""
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
            # A broken endpoint/config must not terminate Pluribus. Individual
            # delivery failures are tracked per runner; unexpected loop errors
            # wait briefly before trying again.
            await asyncio.sleep(2.0)
