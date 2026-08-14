"""Signed webhook delivery with SSRF protections and delivery observability."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, Field

from pluribus.config import settings
from pluribus.db import get_db

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])

_SUPPORTED_EVENTS = {"fact.created"}
_BLOCKED_HOSTNAMES = {
    "localhost",
    "metadata.google.internal",
    "metadata.aws.internal",
}


class WebhookCreateRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    scope: Optional[str] = None
    category: Optional[str] = None
    events: list[str] = Field(
        default_factory=lambda: ["fact.created"], min_length=1, max_length=10
    )


class WebhookCreateResponse(BaseModel):
    id: str
    secret: str
    message: str = "Webhook creat correctament; guarda el secret, només es mostra ara"


class WebhookResponse(BaseModel):
    id: str
    url: str
    scope: Optional[str] = None
    category: Optional[str] = None
    events: list[str]
    created_at: str
    last_triggered_at: Optional[str] = None
    last_attempted_at: Optional[str] = None
    last_status: Optional[int] = None
    last_error: Optional[str] = None
    needs_rotation: bool = False


def _check_admin(agent: dict[str, Any]) -> None:
    if not agent.get("permissions", {}).get("admin", False):
        raise HTTPException(
            status_code=403,
            detail="Es requereixen permisos admin per gestionar webhooks",
        )


async def _ensure_webhook_schema(db) -> None:
    """Migrate legacy webhook rows lazily without blocking service startup."""
    cursor = await db.execute("PRAGMA table_info(webhooks)")
    columns = {row["name"] for row in await cursor.fetchall()}
    migrations = {
        "secret": "ALTER TABLE webhooks ADD COLUMN secret TEXT",
        "last_attempted_at": "ALTER TABLE webhooks ADD COLUMN last_attempted_at TEXT",
        "last_status": "ALTER TABLE webhooks ADD COLUMN last_status INTEGER",
        "last_error": "ALTER TABLE webhooks ADD COLUMN last_error TEXT",
    }
    changed = False
    for name, sql in migrations.items():
        if name not in columns:
            await db.execute(sql)
            changed = True
    if changed:
        await db.commit()


def _address_is_allowed(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    ):
        return False
    if ip.is_global:
        return True
    return bool(settings.WEBHOOK_ALLOW_PRIVATE)


async def _validate_webhook_url(url: str) -> str:
    """Validate scheme, credentials and every currently resolved destination IP."""
    candidate = url.strip()
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="URL de webhook invàlida") from exc

    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="El webhook ha de ser http o https")
    if not parsed.hostname:
        raise HTTPException(status_code=400, detail="El webhook necessita hostname")
    if parsed.username is not None or parsed.password is not None:
        raise HTTPException(status_code=400, detail="No s'admeten credencials dins la URL")
    if parsed.fragment:
        raise HTTPException(status_code=400, detail="No s'admeten fragments a la URL")

    hostname = parsed.hostname.lower().rstrip(".")
    if hostname in _BLOCKED_HOSTNAMES or hostname.endswith(".localhost"):
        raise HTTPException(status_code=400, detail="Destinació de webhook bloquejada")

    effective_port = port or (443 if parsed.scheme == "https" else 80)
    try:
        # Literal IPs do not need DNS. Hostnames are resolved in a worker thread.
        try:
            addresses = [str(ipaddress.ip_address(hostname))]
        except ValueError:
            infos = await asyncio.to_thread(
                socket.getaddrinfo,
                hostname,
                effective_port,
                0,
                socket.SOCK_STREAM,
            )
            addresses = list({info[4][0] for info in infos})
    except (OSError, socket.gaierror) as exc:
        raise HTTPException(status_code=400, detail="No es pot resoldre el webhook") from exc

    if not addresses:
        raise HTTPException(status_code=400, detail="El webhook no resol cap adreça")
    if any(not _address_is_allowed(address) for address in addresses):
        raise HTTPException(status_code=400, detail="La destinació del webhook no està permesa")
    return candidate


def _serialize_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


async def _record_delivery(
    webhook_id: str,
    status: int | None,
    error: str | None,
    success: bool,
) -> None:
    async with get_db() as db:
        await _ensure_webhook_schema(db)
        await db.execute(
            """UPDATE webhooks
               SET last_attempted_at = datetime('now'),
                   last_status = ?,
                   last_error = ?,
                   last_triggered_at = CASE
                       WHEN ? THEN datetime('now')
                       ELSE last_triggered_at
                   END
               WHERE id = ?""",
            (status, error[:500] if error else None, 1 if success else 0, webhook_id),
        )
        await db.commit()


async def _dispatch_webhook(
    webhook_id: str,
    url: str,
    secret: str | None,
    payload: dict[str, Any],
) -> None:
    """Deliver one signed webhook. Failure is recorded but never crashes writes."""
    if not secret:
        await _record_delivery(
            webhook_id,
            None,
            "Webhook legacy sense secret; elimina'l i recrea'l",
            False,
        )
        return

    try:
        validated_url = await _validate_webhook_url(url)
        body = _serialize_payload(payload)
        delivery_id = secrets.token_hex(16)
        headers = {
            "Content-Type": "application/json",
            "X-Pluribus-Event": str(payload.get("event", "")),
            "X-Pluribus-Delivery": delivery_id,
            "X-Pluribus-Signature": _signature(secret, body),
        }
        async with httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(validated_url, content=body, headers=headers)

        success = 200 <= response.status_code < 300
        await _record_delivery(
            webhook_id,
            response.status_code,
            None if success else f"HTTP {response.status_code}",
            success,
        )
    except Exception as exc:
        await _record_delivery(
            webhook_id,
            None,
            f"{type(exc).__name__}: {exc}",
            False,
        )


async def trigger_fact_created_webhooks(
    background_tasks: BackgroundTasks,
    fact_id: str,
    content: str,
    scope: str,
    category: str,
    agent_id: str,
    timestamp: str,
) -> None:
    """Schedule matching webhook deliveries without marking success early."""
    async with get_db() as db:
        await _ensure_webhook_schema(db)
        cursor = await db.execute(
            """SELECT id, url, scope, category, events, secret
               FROM webhooks
               WHERE (scope IS NULL OR scope = ?)
                 AND (category IS NULL OR category = ?)""",
            (scope, category),
        )
        rows = await cursor.fetchall()

    payload = {
        "event": "fact.created",
        "fact_id": fact_id,
        "content": content,
        "scope": scope,
        "category": category,
        "agent_id": agent_id,
        "timestamp": timestamp,
    }

    for row in rows:
        try:
            events = json.loads(row["events"]) if isinstance(row["events"], str) else row["events"]
        except (json.JSONDecodeError, TypeError):
            events = []
        if "fact.created" not in events:
            continue
        background_tasks.add_task(
            _dispatch_webhook,
            row["id"],
            row["url"],
            row["secret"],
            payload,
        )


@router.post("", status_code=201, response_model=WebhookCreateResponse)
async def create_webhook(request: Request, body: WebhookCreateRequest) -> WebhookCreateResponse:
    _check_admin(request.state.agent)
    unsupported = set(body.events) - _SUPPORTED_EVENTS
    if unsupported:
        raise HTTPException(
            status_code=400,
            detail=f"Events no suportats: {', '.join(sorted(unsupported))}",
        )

    validated_url = await _validate_webhook_url(body.url)
    webhook_id = secrets.token_hex(16)
    signing_secret = secrets.token_urlsafe(32)

    async with get_db() as db:
        await _ensure_webhook_schema(db)
        await db.execute(
            """INSERT INTO webhooks (id, url, scope, category, events, secret)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                webhook_id,
                validated_url,
                body.scope,
                body.category,
                json.dumps(body.events),
                signing_secret,
            ),
        )
        await db.commit()

    return WebhookCreateResponse(id=webhook_id, secret=signing_secret)


@router.get("", response_model=list[WebhookResponse])
async def list_webhooks(request: Request) -> list[WebhookResponse]:
    _check_admin(request.state.agent)
    async with get_db() as db:
        await _ensure_webhook_schema(db)
        cursor = await db.execute(
            """SELECT id, url, scope, category, events, created_at,
                      last_triggered_at, last_attempted_at, last_status,
                      last_error, secret
               FROM webhooks
               ORDER BY created_at DESC"""
        )
        rows = await cursor.fetchall()

    result: list[WebhookResponse] = []
    for row in rows:
        try:
            events = json.loads(row["events"]) if isinstance(row["events"], str) else row["events"]
        except (json.JSONDecodeError, TypeError):
            events = []
        result.append(
            WebhookResponse(
                id=row["id"],
                url=row["url"],
                scope=row["scope"],
                category=row["category"],
                events=events,
                created_at=row["created_at"],
                last_triggered_at=row["last_triggered_at"],
                last_attempted_at=row["last_attempted_at"],
                last_status=row["last_status"],
                last_error=row["last_error"],
                needs_rotation=not bool(row["secret"]),
            )
        )
    return result


@router.delete("/{webhook_id}", status_code=204)
async def delete_webhook(request: Request, webhook_id: str) -> None:
    _check_admin(request.state.agent)
    async with get_db() as db:
        await _ensure_webhook_schema(db)
        cursor = await db.execute("SELECT id FROM webhooks WHERE id = ?", (webhook_id,))
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Webhook no trobat")
        await db.execute("DELETE FROM webhooks WHERE id = ?", (webhook_id,))
        await db.commit()
