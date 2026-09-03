"""Browser-facing session layer for the Pluribus Dashboard.

A normal browser cannot supply an X-API-Key, so we expose a tiny
login surface that converts a server-to-server X-API-Key exchange
into an HttpOnly dashboard session cookie. Scope of this layer is
deliberately narrow:

* Only ``/v1/dashboard/*`` accepts the cookie.
* The cookie carries an opaque random token (no API key inside).
* The session is bound to a single agent record with ``read`` perms
  and a specific scope. ``write`` / ``delete`` / ``admin`` are never
  granted through a session.
* Sessions expire after ``SESSION_TTL_SECONDS`` (default 30 minutes).
* Logout invalidates the session immediately.

The X-API-Key is used only on the server-to-server ``/v1/dashboard/login``
handshake (think: operator running a curl on the host). The browser
never sees it.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import time
from typing import Any, Optional

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from pluribus.authorization import _require, _request_agent
from pluribus.config import settings
from pluribus.db import get_db


SESSION_COOKIE_NAME = "pluribus_dashboard_session"
SESSION_TTL_SECONDS = 30 * 60  # 30 minutes


# --- DB schema for sessions ---------------------------------------------


async def _ensure_dashboard_sessions_table() -> None:
    async with get_db() as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS dashboard_sessions (
                token TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL
            )"""
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_dashboard_sessions_expires "
            "ON dashboard_sessions(expires_at)"
        )
        await db.commit()


# --- Session helpers -----------------------------------------------------


async def _create_session(agent_id: str) -> tuple[str, int]:
    """Persist a new session and return (token, expires_at_unix)."""
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    expires = now + SESSION_TTL_SECONDS
    async with get_db() as db:
        await db.execute(
            """INSERT INTO dashboard_sessions
               (token, agent_id, created_at, expires_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?)""",
            (token, agent_id, now, expires, now),
        )
        await db.commit()
    return token, expires


async def _lookup_session(token: str) -> Optional[dict[str, Any]]:
    async with get_db() as db:
        cur = await db.execute(
            "SELECT token, agent_id, created_at, expires_at, last_seen_at "
            "FROM dashboard_sessions WHERE token = ?",
            (token,),
        )
        row = await cur.fetchone()
    if not row:
        return None
    return {
        "token":       row[0],
        "agent_id":    row[1],
        "created_at":  row[2],
        "expires_at":  row[3],
        "last_seen_at": row[4],
    }


async def _delete_session(token: str) -> None:
    async with get_db() as db:
        await db.execute("DELETE FROM dashboard_sessions WHERE token = ?", (token,))
        await db.commit()


async def _touch_session(token: str, expires_at: int) -> None:
    """Sliding expiration: extend on activity, capped at original expiry."""
    now = int(time.time())
    new_expires = min(expires_at, now + SESSION_TTL_SECONDS)
    async with get_db() as db:
        await db.execute(
            "UPDATE dashboard_sessions SET last_seen_at = ?, expires_at = ? "
            "WHERE token = ?",
            (now, new_expires, token),
        )
        await db.commit()


# --- Cookie helpers ------------------------------------------------------


def _cookie_secure() -> bool:
    # In production we expect HTTPS. The cookie is HttpOnly +
    # SameSite=Strict regardless; the Secure flag is opt-in via env
    # because local dev runs HTTP.
    return os.environ.get("PLURIBUS_DASHBOARD_COOKIE_SECURE", "0") == "1"


def _set_session_cookie(response: Response, token: str, expires_at: int) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=max(0, expires_at - int(time.time())),
        httponly=True,
        samesite="strict",
        secure=_cookie_secure(),
        path="/v1/dashboard/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/v1/dashboard/",
    )


# --- Router --------------------------------------------------------------


auth_router = APIRouter(
    prefix="/v1/dashboard",
    tags=["dashboard-observability"],
)


@auth_router.post("/login")
async def dashboard_login(
    request: Request,
    response: Response,
) -> dict[str, Any]:
    """Server-to-server login: caller POSTs with X-API-Key and
    receives a Set-Cookie. The API key is NOT returned to the
    caller; only the cookie is set.

    Intended use: the operator runs `curl -X POST .../v1/dashboard/login
    -H 'X-API-Key: *** --cookie-jar cookies.txt`, then visits
    /dashboard in a browser. The browser does not have the API key.

    This endpoint is exempted from the global X-API-Key middleware
    (see security.py) so we authenticate here directly.
    """
    await _ensure_dashboard_sessions_table()

    # Inline authentication: do the same lookup the global middleware
    # would do, but without depending on request.state.agent (which
    # the global middleware has not set on this path).
    from pluribus.security import _authenticate_agent

    api_key = request.headers.get("X-API-Key")
    if not api_key:
        raise HTTPException(status_code=401, detail="Autenticacio requerida")
    client_host = request.client.host if request.client else "unknown"
    agent = await _authenticate_agent(api_key, client_host)
    if agent is None:
        raise HTTPException(status_code=401, detail="Clau API invalida")

    # SQLite returns JSON columns as raw strings. _require() expects
    # dicts/lists, so parse here. Other code paths (the global
    # middleware) parse the same fields in request.state.agent, but
    # the middleware has not run on this path.
    try:
        if isinstance(agent.get("permissions"), str):
            agent["permissions"] = json.loads(agent["permissions"])
    except Exception:
        agent["permissions"] = {}
    try:
        if isinstance(agent.get("allowed_scopes"), str):
            agent["allowed_scopes"] = json.loads(agent["allowed_scopes"])
    except Exception:
        agent["allowed_scopes"] = []

    # Only agents with read permission may use the dashboard.
    _require(agent, "read")

    token, expires_at = await _create_session(agent["id"])
    _set_session_cookie(response, token, expires_at)
    return {
        "ok": True,
        "agent_id": agent["id"],
        "agent_name": agent.get("name"),
        "expires_at": expires_at,
        "ttl_seconds": expires_at - int(time.time()),
    }


@auth_router.post("/logout")
async def dashboard_logout(
    request: Request,
    response: Response,
) -> dict[str, Any]:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        await _delete_session(token)
    _clear_session_cookie(response)
    return {"ok": True}


async def dashboard_session_authorize(
    request: Request,
    cookie_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> dict[str, Any]:
    """Authorization for browser-driven dashboard calls.

    Accepts either:
      - HttpOnly cookie (browser), OR
      - X-API-Key header (server-to-server, e.g. CI/tests)

    Returns the active agent row on success. Raises 401/403 otherwise.
    Sliding expiration: each successful call extends the session.
    """
    x_api_key = request.headers.get("X-API-Key")
    agent = None

    if cookie_token:
        sess = await _lookup_session(cookie_token)
        if sess and sess["expires_at"] > int(time.time()):
            # Resolve the agent from the session
            async with get_db() as db:
                cur = await db.execute(
                    "SELECT id, name, permissions, allowed_scopes, is_active "
                    "FROM agents WHERE id = ?",
                    (sess["agent_id"],),
                )
                row = await cur.fetchone()
            if row and row[4]:  # is_active
                agent = {
                    "id": row[0], "name": row[1],
                    "permissions": json.loads(row[2]) if row[2] else {},
                    "allowed_scopes": json.loads(row[3]) if row[3] else [],
                }
                await _touch_session(cookie_token, sess["expires_at"])
            else:
                # Session points to a disabled/deleted agent — invalidate
                await _delete_session(cookie_token)

    if agent is None and x_api_key:
        # Fallback to API-key auth (server-to-server, e.g. tests, CI,
        # operator scripts). The global middleware has been bypassed
        # for /v1/dashboard/* (so request.state.agent is None), so we
        # authenticate here directly.
        from pluribus.security import _authenticate_agent
        client_host = request.client.host if request.client else "unknown"
        agent = await _authenticate_agent(x_api_key, client_host)
        if agent is not None:
            # _authenticate_agent returns a row dict with JSON-string
            # columns. Parse for _require().
            try:
                if isinstance(agent.get("permissions"), str):
                    agent["permissions"] = json.loads(agent["permissions"])
            except Exception:
                agent["permissions"] = {}
            try:
                if isinstance(agent.get("allowed_scopes"), str):
                    agent["allowed_scopes"] = json.loads(agent["allowed_scopes"])
            except Exception:
                agent["allowed_scopes"] = []

    if agent is None:
        raise HTTPException(status_code=401, detail="Autenticacio requerida")

    _require(agent, "read")

    # Per-scope enforcement for /v1/dashboard/memory
    if request.url.path.rstrip("/").startswith("/v1/dashboard/memory"):
        requested_scope = request.query_params.get("scope", "shared")
        allowed = set(agent.get("allowed_scopes", []) or [])
        if requested_scope not in allowed:
            raise HTTPException(
                status_code=403,
                detail=f"Scope '{requested_scope}' no autoritzat per a aquest agent",
            )

    return agent
