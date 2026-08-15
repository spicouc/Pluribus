"""Passive-by-default monitor for Xerrameca dialogue health."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from pluribus.db import get_db

from .service import _now


router = APIRouter(prefix="/v1/xerrameca/monitor", tags=["xerrameca-monitor"])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MonitorUpdate(StrictModel):
    enabled: bool | None = None
    poll_interval_seconds: float | None = Field(default=None, ge=2, le=3600)
    stalled_after_seconds: int | None = Field(default=None, ge=30, le=86400)
    near_rounds_threshold: int | None = Field(default=None, ge=1, le=20)
    loop_window: int | None = Field(default=None, ge=3, le=12)
    auto_pause_stalled: bool | None = None
    auto_pause_loop: bool | None = None


AlertStatus = Literal["open", "acknowledged", "resolved"]


def _agent(request: Request) -> dict[str, Any]:
    agent = getattr(request.state, "agent", None) or {}
    if not (agent.get("permissions") or {}).get("admin", False):
        raise HTTPException(status_code=403, detail="Xerrameca Monitor: permís admin requerit")
    return agent


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _age_seconds(value: str | None, now: datetime) -> float | None:
    parsed = _parse_time(value)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds())


def _normalize_text(value: str) -> str:
    return " ".join(value.lower().split())[:8000]


async def _runtime(db: Any) -> Any:
    cursor = await db.execute(
        "SELECT * FROM xerrameca_monitor_runtime WHERE singleton = 1"
    )
    return await cursor.fetchone()


def _runtime_payload(row: Any) -> dict[str, Any]:
    return {
        "enabled": bool(row["enabled"]),
        "poll_interval_seconds": float(row["poll_interval_seconds"]),
        "stalled_after_seconds": int(row["stalled_after_seconds"]),
        "near_rounds_threshold": int(row["near_rounds_threshold"]),
        "loop_window": int(row["loop_window"]),
        "auto_pause_stalled": bool(row["auto_pause_stalled"]),
        "auto_pause_loop": bool(row["auto_pause_loop"]),
        "updated_at": row["updated_at"],
    }


async def get_monitor_state() -> dict[str, Any]:
    async with get_db() as db:
        return _runtime_payload(await _runtime(db))


async def update_monitor_state(body: MonitorUpdate) -> dict[str, Any]:
    values = body.model_dump(exclude_none=True)
    if not values:
        raise HTTPException(status_code=400, detail="No hi ha canvis")
    bool_fields = {"enabled", "auto_pause_stalled", "auto_pause_loop"}
    assignments = []
    params: list[Any] = []
    for key, value in values.items():
        assignments.append(f"{key} = ?")
        params.append(1 if key in bool_fields and value else 0 if key in bool_fields else value)
    assignments.append("updated_at = ?")
    params.append(_now())
    async with get_db() as db:
        await db.execute(
            f"UPDATE xerrameca_monitor_runtime SET {', '.join(assignments)} WHERE singleton = 1",
            params,
        )
        await db.commit()
        return _runtime_payload(await _runtime(db))


async def _recent_results(db: Any, conversation_id: str, limit: int) -> list[dict[str, Any]]:
    cursor = await db.execute(
        """SELECT from_agent_id, content, created_at
           FROM xerrameca_messages
           WHERE conversation_id = ? AND message_type = 'result' AND from_agent_id IS NOT NULL
           ORDER BY created_at DESC, rowid DESC LIMIT ?""",
        (conversation_id, limit),
    )
    rows = [dict(row) for row in await cursor.fetchall()]
    rows.reverse()
    return rows


def _looks_like_loop(rows: list[dict[str, Any]]) -> bool:
    if len(rows) < 4:
        return False
    tail = rows[-4:]
    return (
        tail[0]["from_agent_id"] == tail[2]["from_agent_id"]
        and tail[1]["from_agent_id"] == tail[3]["from_agent_id"]
        and _normalize_text(tail[0]["content"]) == _normalize_text(tail[2]["content"])
        and _normalize_text(tail[1]["content"]) == _normalize_text(tail[3]["content"])
    )


async def _conversation_snapshot(db: Any, conv: Any, runtime: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    now_dt = datetime.now(timezone.utc)
    current_turn = None
    if conv["current_turn_id"]:
        cursor = await db.execute(
            """SELECT id, assigned_agent_id, status, claimed_by, claimed_at,
                      lease_until, created_at, dialogue_round, turn_in_round, phase
               FROM xerrameca_turns WHERE id = ?""",
            (conv["current_turn_id"],),
        )
        row = await cursor.fetchone()
        current_turn = dict(row) if row else None

    cursor = await db.execute(
        """SELECT p.agent_id, p.role, p.position, p.enabled, a.name, a.is_active
           FROM xerrameca_participants p JOIN agents a ON a.id = p.agent_id
           WHERE p.conversation_id = ? ORDER BY p.position""",
        (conv["id"],),
    )
    participants = [dict(row) for row in await cursor.fetchall()]

    progress_age = _age_seconds(conv["updated_at"], now_dt)
    turn_age = _age_seconds(current_turn["created_at"], now_dt) if current_turn else None
    claim_age = _age_seconds(current_turn["claimed_at"], now_dt) if current_turn else None
    alerts: list[dict[str, Any]] = []

    def add(kind: str, severity: str, message: str, details: dict[str, Any] | None = None) -> None:
        alerts.append(
            {
                "alert_type": kind,
                "severity": severity,
                "message": message,
                "details": details or {},
            }
        )

    if conv["status"] == "active":
        if current_turn is None:
            add("no_current_turn", "critical", "Conversa activa sense torn actual")
        elif current_turn["status"] == "ready" and (turn_age or 0) >= runtime["stalled_after_seconds"]:
            add(
                "stalled_ready",
                "critical",
                "Torn ready sense ser reclamat durant massa temps",
                {"age_seconds": turn_age, "turn_id": current_turn["id"]},
            )
        elif current_turn["status"] == "claimed":
            lease_until = _parse_time(current_turn["lease_until"])
            if lease_until and lease_until <= now_dt:
                add(
                    "lease_expired",
                    "warning",
                    "Lease caducada pendent de recuperació",
                    {"turn_id": current_turn["id"], "lease_until": current_turn["lease_until"]},
                )
            if (claim_age or 0) >= runtime["stalled_after_seconds"]:
                add(
                    "stalled_claimed",
                    "critical",
                    "Torn reclamat sense progrés durant massa temps",
                    {"age_seconds": claim_age, "turn_id": current_turn["id"]},
                )

        remaining = int(conv["max_rounds"]) - int(conv["current_round"])
        if remaining <= int(runtime["near_rounds_threshold"]):
            add(
                "near_max_rounds",
                "info" if remaining > 0 else "warning",
                "La conversa és a prop del límit de rondes" if remaining > 0 else "Límit de rondes assolit",
                {"remaining_rounds": max(0, remaining)},
            )
        if conv["completion_proposed_by_agent_id"]:
            add(
                "completion_pending",
                "info",
                "Hi ha una proposta de finalització pendent de confirmació",
                {"proposed_by": conv["completion_proposed_by_agent_id"]},
            )

        recent = await _recent_results(db, conv["id"], int(runtime["loop_window"]))
        if _looks_like_loop(recent):
            add("possible_loop", "warning", "Patró de respostes repetides detectat")

    if conv["status"] == "blocked":
        reason = conv["block_reason"] or "blocked"
        add(
            "needs_human" if reason == "needs_human" else "blocked",
            "warning",
            "La conversa requereix intervenció humana" if reason == "needs_human" else f"Conversa bloquejada: {reason}",
            {"reason": reason},
        )
    elif conv["status"] == "error":
        add("conversation_error", "critical", f"Conversa en error: {conv['block_reason'] or 'agent_error'}")

    snapshot = {
        "conversation_id": conv["id"],
        "name": conv["name"],
        "status": conv["status"],
        "scope": conv["scope"],
        "protocol_version": conv["protocol_version"],
        "turn_policy": conv["turn_policy"],
        "current_round": conv["current_round"],
        "max_rounds": conv["max_rounds"],
        "completion_pending": bool(conv["completion_proposed_by_agent_id"]),
        "updated_at": conv["updated_at"],
        "idle_seconds": progress_age,
        "participants": participants,
        "current_turn": current_turn,
        "health": "critical" if any(a["severity"] == "critical" for a in alerts) else "warning" if any(a["severity"] == "warning" for a in alerts) else "ok",
        "live_alerts": alerts,
    }
    return snapshot, alerts


async def _upsert_alert(db: Any, conversation_id: str, alert: dict[str, Any], now: str) -> str:
    cursor = await db.execute(
        """SELECT id FROM xerrameca_monitor_alerts
           WHERE conversation_id = ? AND alert_type = ?
             AND status IN ('open','acknowledged')
           ORDER BY first_seen_at DESC LIMIT 1""",
        (conversation_id, alert["alert_type"]),
    )
    row = await cursor.fetchone()
    details = json.dumps(alert.get("details") or {}, ensure_ascii=False, sort_keys=True)
    if row:
        await db.execute(
            """UPDATE xerrameca_monitor_alerts
               SET severity = ?, message = ?, details = ?, last_seen_at = ?,
                   occurrences = occurrences + 1
               WHERE id = ?""",
            (alert["severity"], alert["message"], details, now, row["id"]),
        )
        return row["id"]
    alert_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO xerrameca_monitor_alerts
           (id, conversation_id, alert_type, severity, status, message, details,
            first_seen_at, last_seen_at, occurrences)
           VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, 1)""",
        (
            alert_id,
            conversation_id,
            alert["alert_type"],
            alert["severity"],
            alert["message"],
            details,
            now,
            now,
        ),
    )
    return alert_id


async def _pause_for_monitor(db: Any, conv: Any, reason: str) -> bool:
    if conv["status"] != "active":
        return False
    if conv["current_turn_id"]:
        await db.execute(
            """UPDATE xerrameca_turns
               SET status = 'ready', claimed_by = NULL, lease_token = NULL,
                   claimed_at = NULL, lease_until = NULL
               WHERE id = ? AND status = 'claimed'""",
            (conv["current_turn_id"],),
        )
    cursor = await db.execute(
        """UPDATE xerrameca_conversations
           SET status = 'paused', block_reason = ?, updated_at = ?
           WHERE id = ? AND status = 'active'""",
        (reason, _now(), conv["id"]),
    )
    return cursor.rowcount == 1


async def monitor_once(*, persist: bool = True) -> dict[str, Any]:
    """Evaluate all non-terminal conversations once."""
    async with get_db() as db:
        runtime = await _runtime(db)
        if not bool(runtime["enabled"]):
            return {"enabled": False, "conversations": [], "alerts_seen": 0, "auto_paused": 0}

        cursor = await db.execute(
            """SELECT * FROM xerrameca_conversations
               WHERE status IN ('active','paused','blocked','error')
               ORDER BY updated_at DESC"""
        )
        conversations = await cursor.fetchall()
        snapshots: list[dict[str, Any]] = []
        seen: dict[str, set[str]] = {}
        auto_paused = 0
        now = _now()

        for conv in conversations:
            snapshot, alerts = await _conversation_snapshot(db, conv, runtime)
            snapshots.append(snapshot)
            seen[conv["id"]] = {alert["alert_type"] for alert in alerts}
            if persist:
                for alert in alerts:
                    await _upsert_alert(db, conv["id"], alert, now)

            alert_types = seen[conv["id"]]
            should_pause_stalled = bool(runtime["auto_pause_stalled"]) and bool(
                alert_types & {"stalled_ready", "stalled_claimed", "no_current_turn"}
            )
            should_pause_loop = bool(runtime["auto_pause_loop"]) and "possible_loop" in alert_types
            if should_pause_stalled or should_pause_loop:
                reason = "monitor_stalled" if should_pause_stalled else "monitor_possible_loop"
                if await _pause_for_monitor(db, conv, reason):
                    auto_paused += 1

        if persist:
            cursor = await db.execute(
                """SELECT id, conversation_id, alert_type
                   FROM xerrameca_monitor_alerts
                   WHERE status IN ('open','acknowledged')"""
            )
            for row in await cursor.fetchall():
                if row["conversation_id"] in seen and row["alert_type"] not in seen[row["conversation_id"]]:
                    await db.execute(
                        """UPDATE xerrameca_monitor_alerts
                           SET status = 'resolved', resolved_at = ?, last_seen_at = ?
                           WHERE id = ?""",
                        (now, now, row["id"]),
                    )
            await db.commit()

        return {
            "enabled": True,
            "conversations": snapshots,
            "alerts_seen": sum(len(v) for v in seen.values()),
            "auto_paused": auto_paused,
            "checked_at": now,
        }


async def list_alerts(status: AlertStatus | None = None, limit: int = 200) -> list[dict[str, Any]]:
    sql = "SELECT * FROM xerrameca_monitor_alerts"
    params: list[Any] = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, last_seen_at DESC LIMIT ?"
    params.append(limit)
    async with get_db() as db:
        cursor = await db.execute(sql, params)
        rows = []
        for row in await cursor.fetchall():
            item = dict(row)
            try:
                item["details"] = json.loads(item["details"] or "{}")
            except (json.JSONDecodeError, TypeError):
                item["details"] = {}
            rows.append(item)
        return rows


async def set_alert_status(alert_id: str, status: AlertStatus, agent_id: str) -> dict[str, Any]:
    if status == "open":
        raise HTTPException(status_code=422, detail="No es pot reobrir manualment una alerta")
    now = _now()
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM xerrameca_monitor_alerts WHERE id = ?", (alert_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Alerta no trobada")
        if status == "acknowledged":
            await db.execute(
                """UPDATE xerrameca_monitor_alerts
                   SET status = 'acknowledged', acknowledged_at = ?,
                       acknowledged_by_agent_id = ? WHERE id = ?""",
                (now, agent_id, alert_id),
            )
        else:
            await db.execute(
                """UPDATE xerrameca_monitor_alerts
                   SET status = 'resolved', resolved_at = ? WHERE id = ?""",
                (now, alert_id),
            )
        await db.commit()
        cursor = await db.execute(
            "SELECT * FROM xerrameca_monitor_alerts WHERE id = ?", (alert_id,)
        )
        return dict(await cursor.fetchone())


async def monitor_loop() -> None:
    """Background monitor. Runtime config is re-read every cycle."""
    while True:
        try:
            state = await get_monitor_state()
            interval = max(2.0, float(state["poll_interval_seconds"]))
            if state["enabled"]:
                await monitor_once(persist=True)
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"⚠ Xerrameca Monitor: {type(exc).__name__}: {exc}")
            await asyncio.sleep(30)


@router.get("/system")
async def monitor_system(request: Request) -> dict[str, Any]:
    _agent(request)
    return await get_monitor_state()


@router.patch("/system")
async def monitor_system_update(request: Request, body: MonitorUpdate) -> dict[str, Any]:
    _agent(request)
    return await update_monitor_state(body)


@router.post("/tick")
async def monitor_tick(request: Request) -> dict[str, Any]:
    _agent(request)
    return await monitor_once(persist=True)


@router.get("/snapshot")
async def monitor_snapshot(request: Request) -> dict[str, Any]:
    _agent(request)
    return await monitor_once(persist=False)


@router.get("/alerts")
async def monitor_alerts(
    request: Request,
    status: AlertStatus | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[dict[str, Any]]:
    _agent(request)
    return await list_alerts(status, limit)


@router.post("/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(request: Request, alert_id: str) -> dict[str, Any]:
    agent = _agent(request)
    return await set_alert_status(alert_id, "acknowledged", agent["id"])


@router.post("/alerts/{alert_id}/resolve")
async def resolve_alert(request: Request, alert_id: str) -> dict[str, Any]:
    agent = _agent(request)
    return await set_alert_status(alert_id, "resolved", agent["id"])
