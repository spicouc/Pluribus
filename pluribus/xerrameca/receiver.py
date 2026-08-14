"""Reference receiver for Xerrameca Runner callbacks.

The receiver is intentionally generic: it verifies the Runner HMAC, deduplicates
turn deliveries durably, acknowledges quickly, invokes a configured Python
handler in the background and completes the turn through Pluribus REST using the
agent's own API key.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import importlib
import inspect
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Callable

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse


_ALLOWED_RESULTS = {"continue", "complete", "blocked", "needs_human", "error"}
_MAX_BODY_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ReceiverSettings:
    runner_secret: str
    pluribus_url: str
    pluribus_api_key: str
    handler_spec: str | None
    state_db: str
    reply_timeout_seconds: float

    @classmethod
    def from_env(cls) -> "ReceiverSettings":
        secret = os.getenv("XERRAMECA_RUNNER_SECRET", "").strip()
        api_key = os.getenv("PLURIBUS_API_KEY", "").strip()
        if not secret:
            raise RuntimeError("XERRAMECA_RUNNER_SECRET és obligatori")
        if not api_key:
            raise RuntimeError("PLURIBUS_API_KEY és obligatori")
        return cls(
            runner_secret=secret,
            pluribus_url=os.getenv("PLURIBUS_URL", "http://127.0.0.1:8000").rstrip("/"),
            pluribus_api_key=api_key,
            handler_spec=os.getenv("XERRAMECA_HANDLER", "").strip() or None,
            state_db=os.getenv("XERRAMECA_RECEIVER_DB", "./xerrameca_receiver.db"),
            reply_timeout_seconds=float(os.getenv("XERRAMECA_REPLY_TIMEOUT", "30")),
        )


def _signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(secret: str, body: bytes, provided: str | None) -> bool:
    if not provided:
        return False
    return hmac.compare_digest(_signature(secret, body), provided.strip())


def _turn_fields(payload: dict[str, Any]) -> tuple[str, str]:
    """Extract the canonical Runner v1 nested turn envelope.

    A small top-level compatibility path is retained for early development
    payloads, but the production Runner sends payload['turn']['id'] and
    payload['turn']['lease_token'].
    """
    turn = payload.get("turn")
    if isinstance(turn, dict):
        turn_id = turn.get("id")
        lease_token = turn.get("lease_token")
    else:
        turn_id = payload.get("turn_id")
        lease_token = payload.get("lease_token")
    if not isinstance(turn_id, str) or not turn_id:
        raise ValueError("turn.id obligatori")
    if not isinstance(lease_token, str) or len(lease_token) < 16:
        raise ValueError("turn.lease_token obligatori")
    return turn_id, lease_token


def _init_state_db(path: str) -> None:
    db_path = Path(path)
    if db_path.parent and str(db_path.parent) not in {"", "."}:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS deliveries (
                   idempotency_key TEXT PRIMARY KEY,
                   turn_id TEXT NOT NULL,
                   status TEXT NOT NULL,
                   last_error TEXT,
                   received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                   updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        db.commit()


def _claim_delivery(path: str, key: str, turn_id: str) -> bool:
    """Return True only for the first durable observation of a delivery key."""
    with sqlite3.connect(path, timeout=5) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT status FROM deliveries WHERE idempotency_key = ?", (key,)
        ).fetchone()
        if row:
            db.rollback()
            return False
        db.execute(
            "INSERT INTO deliveries (idempotency_key, turn_id, status) VALUES (?, ?, 'accepted')",
            (key, turn_id),
        )
        db.commit()
        return True


def _set_delivery(path: str, key: str, status: str, error: str | None = None) -> None:
    with sqlite3.connect(path, timeout=5) as db:
        db.execute(
            """UPDATE deliveries
               SET status = ?, last_error = ?, updated_at = CURRENT_TIMESTAMP
               WHERE idempotency_key = ?""",
            (status, error[:1000] if error else None, key),
        )
        db.commit()


async def default_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Safe default: acknowledge the turn but require an operator/real handler."""
    try:
        turn_id, _ = _turn_fields(payload)
    except ValueError:
        turn_id = "unknown"
    return {
        "content": f"Torn {turn_id} rebut pel receptor Xerrameca de referència; falta configurar XERRAMECA_HANDLER.",
        "result": "needs_human",
        "metadata": {"receiver": "reference", "handler": "default"},
    }


def load_handler(spec: str | None) -> Callable[[dict[str, Any]], Any]:
    if not spec:
        return default_handler
    if ":" not in spec:
        raise RuntimeError("XERRAMECA_HANDLER ha de tenir format modul:funcio")
    module_name, function_name = spec.split(":", 1)
    module = importlib.import_module(module_name)
    handler = getattr(module, function_name, None)
    if not callable(handler):
        raise RuntimeError(f"Handler no invocable: {spec}")
    return handler


async def _call_handler(handler: Callable[[dict[str, Any]], Any], payload: dict[str, Any]) -> dict[str, Any]:
    result = handler(payload)
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, dict):
        raise ValueError("El handler ha de retornar un objecte/dict")
    content = result.get("content")
    turn_result = result.get("result", "continue")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("El handler ha de retornar content no buit")
    if turn_result not in _ALLOWED_RESULTS:
        raise ValueError(f"result invàlid: {turn_result}")
    metadata = result.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("metadata ha de ser un objecte")
    return {
        "content": content,
        "result": turn_result,
        "next_agent_id": result.get("next_agent_id"),
        "metadata": metadata,
    }


async def _reply_to_pluribus(settings: ReceiverSettings, payload: dict[str, Any], result: dict[str, Any]) -> None:
    turn_id, lease_token = _turn_fields(payload)
    body = {
        "content": result["content"],
        "result": result["result"],
        "lease_token": lease_token,
        "metadata": result.get("metadata") or {},
    }
    if result.get("next_agent_id"):
        body["next_agent_id"] = result["next_agent_id"]

    url = f"{settings.pluribus_url}/v1/xerrameca/turns/{turn_id}/reply"
    async with httpx.AsyncClient(timeout=settings.reply_timeout_seconds, trust_env=False) as client:
        response = await client.post(
            url,
            headers={"X-API-Key": settings.pluribus_api_key},
            json=body,
        )
    if response.status_code < 200 or response.status_code >= 300:
        detail = response.text[:500]
        raise RuntimeError(f"Pluribus reply HTTP {response.status_code}: {detail}")


async def _process_delivery(
    settings: ReceiverSettings,
    handler: Callable[[dict[str, Any]], Any],
    key: str,
    payload: dict[str, Any],
) -> None:
    try:
        _set_delivery(settings.state_db, key, "processing")
        result = await _call_handler(handler, payload)
        await _reply_to_pluribus(settings, payload, result)
        _set_delivery(settings.state_db, key, "completed")
    except Exception as exc:
        # Best effort: tell Pluribus that the turn errored while the lease is
        # still valid. If that also fails, normal lease expiry recovers it.
        _set_delivery(settings.state_db, key, "error", str(exc))
        try:
            await _reply_to_pluribus(
                settings,
                payload,
                {
                    "content": f"Error del receptor Xerrameca: {type(exc).__name__}",
                    "result": "error",
                    "metadata": {"receiver_error": type(exc).__name__},
                },
            )
        except Exception:
            pass


def create_receiver_app(
    settings: ReceiverSettings | None = None,
    handler: Callable[[dict[str, Any]], Any] | None = None,
) -> FastAPI:
    settings = settings or ReceiverSettings.from_env()
    _init_state_db(settings.state_db)
    handler = handler or load_handler(settings.handler_spec)

    app = FastAPI(title="Xerrameca Runner Reference Receiver", version="1.0.0")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "receiver": "xerrameca-reference", "version": "1.0.0"}

    @app.post("/xerrameca/turn")
    async def receive_turn(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > _MAX_BODY_BYTES:
                    raise HTTPException(status_code=413, detail="Payload massa gran")
            except ValueError:
                raise HTTPException(status_code=400, detail="Content-Length invàlid")

        body = await request.body()
        if len(body) > _MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Payload massa gran")
        if not verify_signature(
            settings.runner_secret,
            body,
            request.headers.get("X-Pluribus-Signature"),
        ):
            raise HTTPException(status_code=401, detail="Signatura HMAC invàlida")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="JSON invàlid") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Payload ha de ser un objecte")
        if payload.get("event") not in {None, "xerrameca.turn.claimed"}:
            raise HTTPException(status_code=422, detail="Event Xerrameca no suportat")

        try:
            turn_id, _lease_token = _turn_fields(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        key = (
            request.headers.get("X-Pluribus-Idempotency-Key")
            or payload.get("idempotency_key")
            or turn_id
        )
        key = str(key)
        if not key or len(key) > 256:
            raise HTTPException(status_code=422, detail="idempotency_key invàlida")
        # Runner v1 uses turn_id as the stable idempotency key. Reject a
        # conflicting caller-supplied key rather than deduplicating the wrong turn.
        if key != turn_id:
            raise HTTPException(status_code=422, detail="idempotency_key ha de coincidir amb turn.id")

        first = _claim_delivery(settings.state_db, key, turn_id)
        if not first:
            return JSONResponse(
                status_code=200,
                content={"accepted": True, "duplicate": True, "turn_id": turn_id},
            )

        background_tasks.add_task(_process_delivery, settings, handler, key, payload)
        return JSONResponse(
            status_code=202,
            content={"accepted": True, "duplicate": False, "turn_id": turn_id},
        )

    return app


# Convenient ASGI entrypoint. Environment is read only when this module is used
# directly by uvicorn, not when imported by Pluribus/tests.
def _build_default_app() -> FastAPI:
    try:
        return create_receiver_app()
    except RuntimeError as exc:
        app = FastAPI(title="Xerrameca Runner Reference Receiver")

        @app.get("/health")
        async def unhealthy() -> JSONResponse:
            return JSONResponse(status_code=503, content={"status": "error", "detail": str(exc)})

        return app


app = _build_default_app()
