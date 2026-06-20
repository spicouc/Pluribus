"""
Webhook system for Pluribus (Brain v2).

Permet als agents configurar webhooks per rebre notificacions
quan es creen fets nous, amb filtre per scope i category.
"""

from __future__ import annotations

import json
import secrets
from typing import Any, Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, Field

from brain.db import get_db

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


# ─── Models ────────────────────────────────────────────

class WebhookCreateRequest(BaseModel):
    """Sol·licitud per crear un webhook."""
    url: str
    scope: Optional[str] = None
    category: Optional[str] = None
    events: list[str] = Field(default_factory=lambda: ["fact.created"])


class WebhookCreateResponse(BaseModel):
    """Resposta després de crear un webhook."""
    id: str
    message: str = "Webhook creat correctament"


class WebhookResponse(BaseModel):
    """Un webhook tal com es retorna al client."""
    id: str
    url: str
    scope: Optional[str] = None
    category: Optional[str] = None
    events: list[str]
    created_at: str
    last_triggered_at: Optional[str] = None


# ─── Helpers ───────────────────────────────────────────

def _check_admin(agent: dict[str, Any]) -> None:
    """Comprova que l'agent tingui permisos admin."""
    if not agent.get("permissions", {}).get("admin", False):
        raise HTTPException(
            status_code=403,
            detail="Es requereixen permisos admin per gestionar webhooks",
        )


async def _dispatch_webhook(url: str, payload: dict[str, Any]) -> None:
    """Envia una crida HTTP POST a un webhook. Silenciosa si falla."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(url, json=payload)
    except Exception:
        pass  # Silently ignore — never crash the main service


async def trigger_fact_created_webhooks(
    background_tasks: BackgroundTasks,
    fact_id: str,
    content: str,
    scope: str,
    category: str,
    agent_id: str,
    timestamp: str,
) -> None:
    """Dispara tots els webhooks que matxin per scope/category.

    S'ha de cridar des de write_memory() amb background_tasks.
    """
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT id, url, scope, category FROM webhooks
               WHERE (scope IS NULL OR scope = ?)
                 AND (category IS NULL OR category = ?)
                 AND events LIKE '%"fact.created"%'""",
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
        background_tasks.add_task(_dispatch_webhook, row["url"], payload)
        async with get_db() as db:
            await db.execute(
                "UPDATE webhooks SET last_triggered_at = datetime('now') WHERE id = ?",
                (row["id"],),
            )
            await db.commit()


# ─── Endpoints ─────────────────────────────────────────

@router.post("", status_code=201, response_model=WebhookCreateResponse)
async def create_webhook(request: Request, body: WebhookCreateRequest) -> WebhookCreateResponse:
    """Crea un webhook per rebre notificacions de fets nous."""
    _check_admin(request.state.agent)

    webhook_id = secrets.token_hex(16)

    async with get_db() as db:
        await db.execute(
            """INSERT INTO webhooks (id, url, scope, category, events)
               VALUES (?, ?, ?, ?, ?)""",
            (webhook_id, body.url, body.scope, body.category,
             json.dumps(body.events)),
        )
        await db.commit()

    return WebhookCreateResponse(id=webhook_id)


@router.get("", response_model=list[WebhookResponse])
async def list_webhooks(request: Request) -> list[WebhookResponse]:
    """Llista tots els webhooks configurats."""
    _check_admin(request.state.agent)

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM webhooks ORDER BY created_at DESC",
        )
        rows = await cursor.fetchall()

    result = []
    for row in rows:
        try:
            events = json.loads(row["events"]) if isinstance(row["events"], str) else row["events"]
        except (json.JSONDecodeError, TypeError):
            events = ["fact.created"]

        result.append(WebhookResponse(
            id=row["id"],
            url=row["url"],
            scope=row["scope"],
            category=row["category"],
            events=events,
            created_at=row["created_at"],
            last_triggered_at=row["last_triggered_at"],
        ))

    return result


@router.delete("/{webhook_id}", status_code=204)
async def delete_webhook(request: Request, webhook_id: str) -> None:
    """Elimina un webhook."""
    _check_admin(request.state.agent)

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id FROM webhooks WHERE id = ?", (webhook_id,),
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Webhook no trobat")

        await db.execute("DELETE FROM webhooks WHERE id = ?", (webhook_id,))
        await db.commit()
