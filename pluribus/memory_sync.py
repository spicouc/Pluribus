"""Incremental, scope-safe memory synchronization for Pluribus agents."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator

from pluribus.audit import log_audit
from pluribus.db import get_db

router = APIRouter(prefix="/v1/memory", tags=["memory-sync"])

DEFAULT_ACTIVE_POLL_SECONDS = 5
DEFAULT_IDLE_POLL_SECONDS = 30
DEFAULT_WRITE_DEBOUNCE_SECONDS = 2
DEFAULT_MAX_WRITE_DELAY_SECONDS = 5
_MAX_SCAN_EVENTS = 2000
_SCAN_BATCH_SIZE = 200


class MemorySyncPolicy(BaseModel):
    active_poll_seconds: int = Field(default=DEFAULT_ACTIVE_POLL_SECONDS, ge=2, le=300)
    idle_poll_seconds: int = Field(default=DEFAULT_IDLE_POLL_SECONDS, ge=5, le=3600)
    write_debounce_seconds: int = Field(default=DEFAULT_WRITE_DEBOUNCE_SECONDS, ge=0, le=60)
    max_write_delay_seconds: int = Field(default=DEFAULT_MAX_WRITE_DELAY_SECONDS, ge=1, le=300)

    @model_validator(mode="after")
    def validate_cadence(self) -> "MemorySyncPolicy":
        if self.idle_poll_seconds < self.active_poll_seconds:
            raise ValueError("idle_poll_seconds no pot ser menor que active_poll_seconds")
        if self.max_write_delay_seconds < self.write_debounce_seconds:
            raise ValueError("max_write_delay_seconds no pot ser menor que write_debounce_seconds")
        return self


class MemorySyncPolicyResponse(MemorySyncPolicy):
    agent_id: str
    updated_at: str | None = None


class MemorySyncChange(BaseModel):
    seq: int
    fact_id: str
    scope: str
    change_type: str
    changed_at: str
    category: str | None = None
    agent_id: str | None = None
    key: str | None = None
    content: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    version: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


class MemorySyncResponse(BaseModel):
    cursor: int
    next_cursor: int
    has_more: bool
    changes: list[MemorySyncChange]
    recommended_poll_seconds: int
    policy: MemorySyncPolicyResponse


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return [item for item in parsed if isinstance(item, str)] if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _caller(request: Request) -> dict[str, Any]:
    agent = getattr(request.state, "agent", None)
    if not agent or not agent.get("id"):
        raise HTTPException(status_code=401, detail="Autenticació requerida")
    return agent


def _permissions(agent: dict[str, Any]) -> dict[str, Any]:
    return _json_dict(agent.get("permissions", {}))


def _is_admin(agent: dict[str, Any]) -> bool:
    return bool(_permissions(agent).get("admin", False))


def _allowed_scopes(agent: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(_json_list(agent.get("allowed_scopes", []))))


def _require_read(agent: dict[str, Any]) -> None:
    if _is_admin(agent):
        return
    if not _permissions(agent).get("read", False):
        raise HTTPException(status_code=403, detail="L'agent no té permís 'read'")
    if not _allowed_scopes(agent):
        raise HTTPException(status_code=403, detail="L'agent no té scopes de lectura")


async def init_memory_sync_db() -> None:
    """Create the durable change feed, policies and fact-change triggers."""
    async with get_db() as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_change_log (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                fact_id TEXT NOT NULL,
                scope TEXT NOT NULL,
                change_type TEXT NOT NULL CHECK (change_type IN ('upsert','delete')),
                changed_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_memory_change_log_scope_seq
                ON memory_change_log(scope, seq);

            CREATE TABLE IF NOT EXISTS agent_memory_policies (
                agent_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
                active_poll_seconds INTEGER NOT NULL DEFAULT 5,
                idle_poll_seconds INTEGER NOT NULL DEFAULT 30,
                write_debounce_seconds INTEGER NOT NULL DEFAULT 2,
                max_write_delay_seconds INTEGER NOT NULL DEFAULT 5,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TRIGGER IF NOT EXISTS memory_sync_facts_ai
            AFTER INSERT ON facts
            BEGIN
                INSERT INTO memory_change_log(fact_id, scope, change_type)
                VALUES (
                    new.id,
                    new.scope,
                    CASE WHEN new.deleted_at IS NULL THEN 'upsert' ELSE 'delete' END
                );
            END;

            CREATE TRIGGER IF NOT EXISTS memory_sync_facts_au_same_scope
            AFTER UPDATE ON facts
            WHEN old.scope = new.scope
            BEGIN
                INSERT INTO memory_change_log(fact_id, scope, change_type)
                VALUES (
                    new.id,
                    new.scope,
                    CASE WHEN new.deleted_at IS NULL THEN 'upsert' ELSE 'delete' END
                );
            END;

            CREATE TRIGGER IF NOT EXISTS memory_sync_facts_au_scope_change
            AFTER UPDATE OF scope ON facts
            WHEN old.scope != new.scope
            BEGIN
                INSERT INTO memory_change_log(fact_id, scope, change_type)
                VALUES (old.id, old.scope, 'delete');

                INSERT INTO memory_change_log(fact_id, scope, change_type)
                VALUES (
                    new.id,
                    new.scope,
                    CASE WHEN new.deleted_at IS NULL THEN 'upsert' ELSE 'delete' END
                );
            END;

            CREATE TRIGGER IF NOT EXISTS memory_sync_facts_ad
            AFTER DELETE ON facts
            BEGIN
                INSERT INTO memory_change_log(fact_id, scope, change_type)
                VALUES (old.id, old.scope, 'delete');
            END;
            """
        )

        cursor = await db.execute("SELECT COUNT(*) AS total FROM memory_change_log")
        if (await cursor.fetchone())["total"] == 0:
            await db.execute(
                """
                INSERT INTO memory_change_log(fact_id, scope, change_type)
                SELECT id, scope, 'upsert'
                FROM facts
                WHERE deleted_at IS NULL
                ORDER BY created_at ASC, id ASC
                """
            )
        await db.commit()


async def get_memory_sync_policy(agent_id: str) -> MemorySyncPolicyResponse:
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT agent_id, active_poll_seconds, idle_poll_seconds,
                   write_debounce_seconds, max_write_delay_seconds, updated_at
            FROM agent_memory_policies
            WHERE agent_id = ?
            """,
            (agent_id,),
        )
        row = await cursor.fetchone()

    if not row:
        return MemorySyncPolicyResponse(agent_id=agent_id)

    return MemorySyncPolicyResponse(
        agent_id=row["agent_id"],
        active_poll_seconds=row["active_poll_seconds"],
        idle_poll_seconds=row["idle_poll_seconds"],
        write_debounce_seconds=row["write_debounce_seconds"],
        max_write_delay_seconds=row["max_write_delay_seconds"],
        updated_at=row["updated_at"],
    )


async def set_memory_sync_policy(
    caller: dict[str, Any],
    agent_id: str,
    policy: MemorySyncPolicy,
) -> MemorySyncPolicyResponse:
    if not _is_admin(caller):
        raise HTTPException(status_code=403, detail="Només un admin pot modificar la política de sync")
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id FROM agents WHERE id = ? AND is_active = 1",
            (agent_id,),
        )
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Agent no trobat o inactiu")
        await db.execute(
            """
            INSERT INTO agent_memory_policies(
                agent_id, active_poll_seconds, idle_poll_seconds,
                write_debounce_seconds, max_write_delay_seconds
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET
                active_poll_seconds = excluded.active_poll_seconds,
                idle_poll_seconds = excluded.idle_poll_seconds,
                write_debounce_seconds = excluded.write_debounce_seconds,
                max_write_delay_seconds = excluded.max_write_delay_seconds,
                updated_at = datetime('now')
            """,
            (
                agent_id,
                policy.active_poll_seconds,
                policy.idle_poll_seconds,
                policy.write_debounce_seconds,
                policy.max_write_delay_seconds,
            ),
        )
        await log_audit(
            db,
            caller["id"],
            "UPDATE",
            "memory_sync_policy",
            resource_id=agent_id,
            payload=json.dumps(policy.model_dump()),
        )
        await db.commit()
    return await get_memory_sync_policy(agent_id)


async def _admin_scopes() -> list[str]:
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT DISTINCT scope FROM facts WHERE deleted_at IS NULL
            UNION
            SELECT DISTINCT scope FROM memory_change_log
            ORDER BY scope
            """
        )
        rows = await cursor.fetchall()
    return [row["scope"] for row in rows if row["scope"]]


async def _load_fact_for_sync(fact_id: str, scope: str) -> dict[str, Any] | None:
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT id, scope, category, agent_id, key, content, metadata,
                   version, created_at, updated_at
            FROM facts
            WHERE id = ? AND scope = ? AND deleted_at IS NULL
            """,
            (fact_id, scope),
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


async def memory_sync_service(
    agent: dict[str, Any],
    cursor: int = 0,
    limit: int = 100,
) -> MemorySyncResponse:
    """Return only changed facts since cursor, while advancing over hidden events."""
    _require_read(agent)
    if cursor < 0:
        raise HTTPException(status_code=422, detail="cursor invàlid")
    if not 1 <= limit <= 200:
        raise HTTPException(status_code=422, detail="limit ha d'estar entre 1 i 200")

    scopes = await _admin_scopes() if _is_admin(agent) else _allowed_scopes(agent)
    allowed = set(scopes)
    policy = await get_memory_sync_policy(agent["id"])

    latest_by_fact_scope: dict[tuple[str, str], dict[str, Any]] = {}
    scanned = 0
    scan_cursor = cursor
    exhausted = False

    while len(latest_by_fact_scope) < limit and scanned < _MAX_SCAN_EVENTS:
        batch_limit = min(_SCAN_BATCH_SIZE, _MAX_SCAN_EVENTS - scanned)
        async with get_db() as db:
            db_cursor = await db.execute(
                """
                SELECT seq, fact_id, scope, change_type, changed_at
                FROM memory_change_log
                WHERE seq > ?
                ORDER BY seq ASC
                LIMIT ?
                """,
                (scan_cursor, batch_limit),
            )
            rows = await db_cursor.fetchall()

        if not rows:
            exhausted = True
            break

        scanned += len(rows)
        reached_limit = False
        for row in rows:
            scan_cursor = row["seq"]
            if row["scope"] in allowed:
                latest_by_fact_scope[(row["fact_id"], row["scope"])] = dict(row)
                if len(latest_by_fact_scope) >= limit:
                    reached_limit = True
                    break

        if reached_limit:
            break
        if len(rows) < batch_limit:
            exhausted = True
            break

    changes: list[MemorySyncChange] = []
    for event in sorted(latest_by_fact_scope.values(), key=lambda item: item["seq"]):
        if event["change_type"] == "delete":
            changes.append(
                MemorySyncChange(
                    seq=event["seq"],
                    fact_id=event["fact_id"],
                    scope=event["scope"],
                    change_type="delete",
                    changed_at=event["changed_at"],
                )
            )
            continue

        fact = await _load_fact_for_sync(event["fact_id"], event["scope"])
        if not fact:
            changes.append(
                MemorySyncChange(
                    seq=event["seq"],
                    fact_id=event["fact_id"],
                    scope=event["scope"],
                    change_type="delete",
                    changed_at=event["changed_at"],
                )
            )
            continue

        changes.append(
            MemorySyncChange(
                seq=event["seq"],
                fact_id=fact["id"],
                scope=fact["scope"],
                change_type="upsert",
                changed_at=event["changed_at"],
                category=fact["category"] or "",
                agent_id=fact["agent_id"],
                key=fact["key"],
                content=fact["content"],
                metadata=_json_dict(fact["metadata"]),
                version=fact["version"],
                created_at=fact["created_at"],
                updated_at=fact["updated_at"],
            )
        )

    if exhausted:
        has_more = False
    else:
        async with get_db() as db:
            db_cursor = await db.execute(
                "SELECT 1 FROM memory_change_log WHERE seq > ? LIMIT 1",
                (scan_cursor,),
            )
            has_more = bool(await db_cursor.fetchone())

    recommended = policy.active_poll_seconds if changes or has_more else policy.idle_poll_seconds
    return MemorySyncResponse(
        cursor=cursor,
        next_cursor=scan_cursor,
        has_more=has_more,
        changes=changes,
        recommended_poll_seconds=recommended,
        policy=policy,
    )


@router.get("/sync", response_model=MemorySyncResponse)
async def memory_sync(
    request: Request,
    cursor: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
) -> MemorySyncResponse:
    return await memory_sync_service(_caller(request), cursor=cursor, limit=limit)


@router.get("/sync/policy", response_model=MemorySyncPolicyResponse)
async def read_own_memory_sync_policy(request: Request) -> MemorySyncPolicyResponse:
    caller = _caller(request)
    return await get_memory_sync_policy(caller["id"])


@router.put("/sync/policy/{agent_id}", response_model=MemorySyncPolicyResponse)
async def update_memory_sync_policy(
    request: Request,
    agent_id: str,
    body: MemorySyncPolicy,
) -> MemorySyncPolicyResponse:
    return await set_memory_sync_policy(_caller(request), agent_id, body)
