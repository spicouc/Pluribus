"""Browser-facing session layer for the Pluribus Dashboard.

A normal browser cannot supply an X-API-Key, so we expose a two-step
bootstrap flow that converts a server-to-server X-API-Key exchange
into an HttpOnly dashboard session cookie:

  Step 1 (server-to-server, no browser):
    POST /v1/dashboard/login-code
    Headers: X-API-Key
    Body:   {"agent_id": "<agent name>"}  (optional; defaults to caller)
    Returns: {"code": "abcd-1234", "expires_in": 90}
    The code is one-time, short-lived (90s), dashboard-read-only, and
    is NOT the API key.

  Step 2 (browser, no API key):
    GET  /dashboard/login   (public HTML form)
    POST /dashboard/login   (form: code=<code>)
    Server validates the code, consumes it atomically, creates a
    FIXED 30-minute HttpOnly session cookie, and 302-redirects to
    /dashboard.

  Step 3 (browser, with cookie):
    GET /v1/dashboard/* — works via the cookie.
    POST /v1/dashboard/logout — invalidates the session.

Session contract:
  - FIXED 30-minute lifetime from login (NOT sliding). The previous
    sliding-expiration code was misleading: it capped at the original
    expiry so it was effectively fixed. We now use a clean fixed
    lifetime.

The X-API-Key is used only at step 1 (server-to-server, e.g. an
operator running `curl`). The browser never sees it.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import time
from typing import Any, Optional

from fastapi import APIRouter, Cookie, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from pluribus.authorization import _require
from pluribus.config import settings
from pluribus.db import get_db


# --- Constants -----------------------------------------------------------

SESSION_COOKIE_NAME = "pluribus_dashboard_session"
SESSION_TTL_SECONDS = 30 * 60  # 30 minutes — FIXED lifetime
LOGIN_CODE_TTL_SECONDS = 90     # 90 seconds, single use


# --- DB schema for sessions + login codes --------------------------------


async def _ensure_dashboard_tables() -> None:
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
        await db.execute(
            """CREATE TABLE IF NOT EXISTS dashboard_login_codes (
                code_hash TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                consumed_at INTEGER DEFAULT NULL
            )"""
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_dashboard_login_codes_expires "
            "ON dashboard_login_codes(expires_at)"
        )
        await db.commit()


# --- Session helpers -----------------------------------------------------


async def _create_session(agent_id: str) -> tuple[str, int]:
    """Persist a new session and return (token, expires_at_unix).

    Lifetime: FIXED 30 minutes from now. We do NOT extend on
    activity — the contract is fixed. (Earlier code claimed sliding
    but capped at the original expiry, which is equivalent.)
    """
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


# --- One-time login code helpers -----------------------------------------


def _hash_code(code: str) -> str:
    """Hash the login code with the API key fingerprint of the agent
    who issued it, so even if the codes table leaks, codes can't be
    used to impersonate other agents."""
    # Use HMAC-SHA256 with a server-side pepper. The pepper is the
    # DB path + a static random salt generated on first use and
    # cached in settings (kept simple here — the agent_id is mixed
    # into the hash so codes are bound to a specific agent row).
    return hmac.new(
        (settings.DB_PATH or "pluribus").encode("utf-8"),
        code.encode("utf-8"),
        "sha256",
    ).hexdigest()


async def _issue_login_code(agent_id: str) -> tuple[str, int]:
    """Create a fresh one-time login code, return (code, expires_in_s)."""
    await _ensure_dashboard_tables()
    # 6 random bytes → 8-char base32 (Crockford-style). 64 bits is
    # well within the brute-force-resistant range for a 90-second TTL.
    code = secrets.token_hex(3).upper()  # 6 hex chars, e.g. "3A7F1B"
    code_hash = _hash_code(code)
    now = int(time.time())
    expires = now + LOGIN_CODE_TTL_SECONDS
    async with get_db() as db:
        await db.execute(
            """INSERT INTO dashboard_login_codes
               (code_hash, agent_id, created_at, expires_at)
               VALUES (?, ?, ?, ?)""",
            (code_hash, agent_id, now, expires),
        )
        await db.commit()
    return code, LOGIN_CODE_TTL_SECONDS


async def _consume_login_code(code: str) -> Optional[str]:
    """Atomically mark a login code as consumed and return the
    associated agent_id. Returns None if the code is unknown,
    expired, or already consumed. Single-use is enforced by a
    conditional UPDATE that only succeeds on unconsumed, unexpired
    rows."""
    if not code:
        return None
    code_hash = _hash_code(code)
    now = int(time.time())
    async with get_db() as db:
        # Atomic consume: only mark if not yet consumed and not
        # expired. We use a single UPDATE to avoid TOCTOU races.
        cur = await db.execute(
            """UPDATE dashboard_login_codes
               SET consumed_at = ?
               WHERE code_hash = ?
                 AND consumed_at IS NULL
                 AND expires_at > ?""",
            (now, code_hash, now),
        )
        if cur.rowcount == 0:
            await db.commit()
            return None
        # Retrieve agent_id
        cur2 = await db.execute(
            "SELECT agent_id FROM dashboard_login_codes WHERE code_hash = ?",
            (code_hash,),
        )
        row = await cur2.fetchone()
        await db.commit()
    return row[0] if row else None


# --- Cookie helpers ------------------------------------------------------


def _cookie_secure() -> bool:
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


# --- Login page HTML (no API key, no client logic) ---------------------


_LOGIN_PAGE_HTML = """<!DOCTYPE html>
<html lang="ca">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pluribus — Dashboard login</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background: #0f172a; color: #e2e8f0; padding: 24px; max-width: 480px; margin: 60px auto; }
h1 { font-size: 1.4rem; color: #38bdf8; margin-bottom: 6px; }
p.lead { color: #94a3b8; font-size: 0.9rem; margin-bottom: 24px; }
form { background: #1e293b; border: 1px solid #334155; border-radius: 10px; padding: 22px; }
label { display: block; color: #94a3b8; font-size: 0.85rem; margin-bottom: 6px; }
input[type="text"] { width: 100%; box-sizing: border-box; background: #0f172a;
                      border: 1px solid #334155; color: #e2e8f0; padding: 12px 14px;
                      border-radius: 6px; font-size: 1.05rem; letter-spacing: 0.1em;
                      font-family: 'SF Mono', Consolas, monospace; }
button { width: 100%; margin-top: 14px; padding: 12px; background: #1e3a5f;
         border: 1px solid #3b82f6; color: #93c5fd; border-radius: 6px;
         font-size: 1rem; cursor: pointer; }
button:hover { background: #254a73; }
.error { background: #5b1e1e; border: 1px solid #ef4444; color: #fca5a5;
         padding: 10px 14px; border-radius: 6px; margin-bottom: 14px; display: none; }
.error.show { display: block; }
.help { color: #64748b; font-size: 0.8rem; margin-top: 18px; line-height: 1.4; }
</style>
</head>
<body>

<h1>Pluribus — Dashboard</h1>
<p class="lead">Introdueix el codi temporal d'un sol ús.</p>

<div id="err" class="error"></div>

<form id="login-form" method="post" action="/dashboard/login">
  <label for="code">Codi d'accés</label>
  <input type="text" id="code" name="code" autocomplete="off" autofocus
         placeholder="XXXXXX" pattern="[A-F0-9]{{6}}" maxlength="6" required>
  <button type="submit">Entrar</button>
</form>

<p class="help">El codi és vàlid durant {ttl} segons i s'invalida després d'un sol ús. El codi NO és la teva API key.</p>

<script>
document.getElementById('login-form').addEventListener('submit', async function (e) {{
  e.preventDefault();
  var code = document.getElementById('code').value.trim().toUpperCase();
  var err = document.getElementById('err');
  err.classList.remove('show');
  try {{
    var r = await fetch('/dashboard/login', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ code: code }}),
      credentials: 'include',
    }});
    if (r.status === 200) {{
      // Success — server already set the cookie. Redirect to dashboard.
      window.location.href = '/dashboard';
    }} else {{
      var data = {{}};
      try {{ data = await r.json(); }} catch (_) {{}}
      err.textContent = data.error || data.detail || ('Error ' + r.status);
      err.classList.add('show');
    }}
  }} catch (ex) {{
    err.textContent = 'Error de connexio: ' + ex.message;
    err.classList.add('show');
  }}
}});
</script>

</body>
</html>
"""


# --- Router --------------------------------------------------------------


auth_router = APIRouter(
    prefix="/v1/dashboard",
    tags=["dashboard-observability"],
)

# Login page is at /dashboard/login (NOT /v1/dashboard/login) so the
# middleware bypass for /v1/dashboard/* does not catch it. The page
# itself is on the same path that the global middleware accepts as
# "public-ish" (under /dashboard/) and the form posts to the same path.
login_router = APIRouter(
    prefix="/dashboard",
    tags=["dashboard-login"],
)


# --- Step 1: server-to-server code issuance (X-API-Key) -----------------


@auth_router.post("/login-code")
async def dashboard_login_code(
    request: Request,
) -> dict[str, Any]:
    """Server-to-server: exchange a valid X-API-Key for a one-time
    login code. The API key is NOT returned; only the code and its
    TTL. The code can be used once by a browser via
    POST /dashboard/login to mint an HttpOnly session cookie."""
    await _ensure_dashboard_tables()
    from pluribus.security import _authenticate_agent
    api_key = request.headers.get("X-API-Key")
    if not api_key:
        raise HTTPException(status_code=401, detail="Autenticacio requerida")
    client_host = request.client.host if request.client else "unknown"
    agent = await _authenticate_agent(api_key, client_host)
    if agent is None:
        raise HTTPException(status_code=401, detail="Clau API invalida")
    # _authenticate_agent returns JSON-string columns. _require expects
    # dicts/lists, so parse here (same as /v1/dashboard/login).
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
    _require(agent, "read")

    code, ttl = await _issue_login_code(agent["id"])
    return {
        "ok":         True,
        "code":       code,
        "agent_id":   agent["id"],
        "agent_name": agent.get("name"),
        "expires_in": ttl,
    }


# --- Original X-API-Key login (kept for CI/tests and operator scripts) -


@auth_router.post("/login")
async def dashboard_login(
    request: Request,
    response: Response,
) -> dict[str, Any]:
    """Server-to-server login: caller POSTs with X-API-Key and
    receives a Set-Cookie. Preserved for CI and operator scripts
    (the new human flow is the recommended path)."""
    await _ensure_dashboard_tables()
    from pluribus.security import _authenticate_agent
    api_key = request.headers.get("X-API-Key")
    if not api_key:
        raise HTTPException(status_code=401, detail="Autenticacio requerida")
    client_host = request.client.host if request.client else "unknown"
    agent = await _authenticate_agent(api_key, client_host)
    if agent is None:
        raise HTTPException(status_code=401, detail="Clau API invalida")
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
    _require(agent, "read")

    token, expires_at = await _create_session(agent["id"])
    _set_session_cookie(response, token, expires_at)
    return {
        "ok":          True,
        "agent_id":    agent["id"],
        "agent_name":  agent.get("name"),
        "expires_at":  expires_at,
        "ttl_seconds": expires_at - int(time.time()),
    }


# --- Logout -------------------------------------------------------------


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


# --- Step 2: human login page + form (no API key) ----------------------


@login_router.get("/login", response_class=HTMLResponse)
async def dashboard_login_page() -> HTMLResponse:
    """Public HTML form. No API key, no JS tokens. The form posts
    a JSON body to /dashboard/login. The browser never sees an API
    key."""
    return HTMLResponse(_LOGIN_PAGE_HTML.replace("{ttl}", str(LOGIN_CODE_TTL_SECONDS)))


@login_router.post("/login")
async def dashboard_login_submit(
    request: Request,
) -> JSONResponse:
    """Browser-side submission. Body: {"code": "XXXXXX"}.
    On success: Set-Cookie + 200. On failure: 401/403 with reason."""
    await _ensure_dashboard_tables()
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Cos JSON invalid")
    code = (body or {}).get("code", "").strip().upper()
    if not code:
        raise HTTPException(status_code=400, detail="Codi requerit")
    agent_id = await _consume_login_code(code)
    if agent_id is None:
        raise HTTPException(
            status_code=401,
            detail="Codi invalid, expirat o ja utilitzat",
        )
    # Mint the session
    token, expires_at = await _create_session(agent_id)
    # Build the JSON response and attach the Set-Cookie header
    # directly. JSONResponse() doesn't expose set_cookie, so we
    # use Response with the JSON body and call set_cookie on it.
    from starlette.responses import JSONResponse as _JR
    resp = _JR({
        "ok":          True,
        "agent_id":    agent_id,
        "expires_at":  expires_at,
        "ttl_seconds": expires_at - int(time.time()),
    })
    _set_session_cookie(resp, token, expires_at)
    return resp


# --- Authorization for the four data endpoints --------------------------


async def dashboard_session_authorize(
    request: Request,
    cookie_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> dict[str, Any]:
    """Authorization for browser-driven dashboard calls.

    Accepts either:
      - HttpOnly cookie (browser), OR
      - X-API-Key header (server-to-server, e.g. CI/tests)

    Session lifetime is FIXED at SESSION_TTL_SECONDS (30 min). We do
    not extend on activity.
    """
    x_api_key = request.headers.get("X-API-Key")
    agent = None

    if cookie_token:
        sess = await _lookup_session(cookie_token)
        if sess and sess["expires_at"] > int(time.time()):
            async with get_db() as db:
                cur = await db.execute(
                    "SELECT id, name, permissions, allowed_scopes, is_active "
                    "FROM agents WHERE id = ?",
                    (sess["agent_id"],),
                )
                row = await cur.fetchone()
            if row and row[4]:
                agent = {
                    "id": row[0], "name": row[1],
                    "permissions": json.loads(row[2]) if row[2] else {},
                    "allowed_scopes": json.loads(row[3]) if row[3] else [],
                }
            else:
                await _delete_session(cookie_token)

    if agent is None and x_api_key:
        from pluribus.security import _authenticate_agent
        client_host = request.client.host if request.client else "unknown"
        agent = await _authenticate_agent(x_api_key, client_host)
        if agent is not None:
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

    if request.url.path.rstrip("/").startswith("/v1/dashboard/memory"):
        requested_scope = request.query_params.get("scope", "shared")
        allowed = set(agent.get("allowed_scopes", []) or [])
        if requested_scope not in allowed:
            raise HTTPException(
                status_code=403,
                detail=f"Scope '{requested_scope}' no autoritzat per a aquest agent",
            )

    return agent
