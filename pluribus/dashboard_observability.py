"""Read-only observability endpoints powering the unified Pluribus dashboard.

Browser flow (D1):
  1. Operator runs curl POST /v1/dashboard/login with X-API-Key,
     receives a Set-Cookie: HttpOnly session token.
  2. Operator opens the dashboard in a browser. The browser auto-
     attaches the cookie to /v1/dashboard/* requests.
  3. Endpoints never see the API key (cookie-only auth path).

Server-to-server / CI / tests: X-API-Key still works as a fallback.

This file is intentionally narrow, side-effect free, and security-
critical. All endpoints are guarded by ``dashboard_session_authorize``
(``pluribus/dashboard_session.py``) which:
  - requires an authenticated agent (cookie OR X-API-Key)
  - requires the ``read`` permission
  - enforces allowed scope on /memory
  - never grants admin
  - never mutates state

Telemetry values that the system cannot compute are returned as the
literal string ``"UNKNOWN"`` — never fabricated from heuristic
string matches in historical memory.

Endpoints:
    GET /v1/dashboard/summary   - agregated service health + counters
    GET /v1/dashboard/agents    - list of known agents from /v1/identity/peers
    GET /v1/dashboard/memory    - latest / searched memory facts (no secrets)
    GET /v1/dashboard/system    - per-service health classification
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from pluribus.config import settings
from pluribus.db import get_db
from pluribus.dashboard_session import dashboard_session_authorize


router = APIRouter(
    prefix="/v1/dashboard",
    tags=["dashboard-observability"],
)


# --- Sentinels ------------------------------------------------------------

UNKNOWN = "UNKNOWN"
NOT_CONFIGURED = "NOT_CONFIGURED"

# Secret-like substrings: any of these in a memory fact triggers
# REDACTED. Defense-in-depth on top of authorization.
_SECRET_TOKENS = (
    "token", "password", "secret", "api_key", "api-key",
    "authorization", "bearer", "sk-", "ghp_", "x-api-key",
    "x_api_key", "access_token", "refresh_token", "client_secret",
    "private_key", "secret_key", "auth_token", "session_token",
    "pass@",  # URL userinfo password (e.g. https://user:pass@host)
    "x-auth", "x-token", "cookie:",
)


# --- Configuration-driven service endpoints ------------------------------


def _service_endpoints() -> dict[str, str]:
    """Resolve the SERVICE_ENDPOINTS dict from settings.

    Pluribus (the dashboard host) is always included. The other
    services are optional — if not configured they surface as
    NOT_CONFIGURED in the UI. We DO NOT hardcode deployment-
    specific Tailscale IPs into the source.
    """
    endpoints: dict[str, str] = {}
    pluribus_url = getattr(settings, "PLURIBUS_DASHBOARD_BASE_URL", None) or \
        f"http://{getattr(settings, 'PLURIBUS_HOST', '127.0.0.1')}:{getattr(settings, 'PLURIBUS_PORT', 8790)}"
    endpoints["pluribus"] = pluribus_url
    for name, attr in (("xerrameca", "XERRAMECA_DASHBOARD_URL"),
                       ("hermes",    "HERMES_DASHBOARD_URL"),
                       ("ollama",    "OLLAMA_BASE_URL")):
        url = getattr(settings, attr, None)
        if url:
            endpoints[name] = url
    return endpoints


def _sanitize_endpoint(url: str | None) -> str:
    """Strip userinfo, query and fragment from a URL.

    Browser displays scheme://host[:port] only. Never expose
    credentials embedded in the URL.
    """
    if not url:
        return UNKNOWN
    try:
        p = urlparse(url)
    except Exception:
        return UNKNOWN
    if not p.scheme or not p.hostname:
        return UNKNOWN
    host = p.hostname
    if p.port:
        return f"{p.scheme}://{host}:{p.port}"
    return f"{p.scheme}://{host}"


# --- Helpers --------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _classify(status: str, elapsed_ms: float | None, error: str | None) -> str:
    if error is not None:
        return "DOWN"
    if status.startswith("5"):
        return "DOWN"
    if status.startswith("2"):
        if elapsed_ms is not None and elapsed_ms > 1500:
            return "DEGRADED"
        return "HEALTHY"
    return "DOWN"


async def _probe(url: str, timeout: float = 2.5) -> dict[str, Any]:
    """Probe a single HTTP service. Never raises."""
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url)
        dt = (time.time() - t0) * 1000
        return {"status": _classify(str(r.status_code), dt, None),
                "elapsed_ms": round(dt, 1),
                "version": _try_extract_version(r.text)}
    except httpx.TimeoutException:
        return {"status": "DOWN", "elapsed_ms": None, "error": "timeout"}
    except Exception as e:
        return {"status": "DOWN", "elapsed_ms": None, "error": str(e)[:80]}


def _try_extract_version(body: str) -> str | None:
    try:
        j = json.loads(body)
    except Exception:
        return None
    for key in ("version", "Version", "VERSION"):
        v = j.get(key)
        if v:
            return str(v)
    return None


def _redact_content(content: str) -> str:
    """If any secret-like token appears, replace the whole content
    with [REDACTED: ...]. Defense-in-depth; primary boundary is
    authorization + scope."""
    if not content:
        return content
    lower = content.lower()
    for tok in _SECRET_TOKENS:
        if tok in lower:
            return "[REDACTED: contains secret-like material]"
    return content


# --- /v1/dashboard/summary -----------------------------------------------


@router.get("/summary", dependencies=[Depends(dashboard_session_authorize)])
async def dashboard_summary(_request: Request) -> dict[str, Any]:
    """Global health summary + counters. All unknown values are UNKNOWN
    or computed cheaply from a real DB read (no fabricated numbers)."""
    endpoints = _service_endpoints()
    probes: dict[str, dict[str, Any]] = {}
    if "pluribus" in endpoints:
        probes["pluribus"] = await _probe(endpoints["pluribus"] + "/health")
    else:
        probes["pluribus"] = {"status": NOT_CONFIGURED}
    for name in ("xerrameca", "hermes", "ollama"):
        if name in endpoints:
            probes[name] = await _probe(endpoints[name])
        else:
            probes[name] = {"status": NOT_CONFIGURED}

    async with get_db() as db:
        cur = await db.execute("SELECT count(*) FROM agents WHERE is_active = 1")
        agents_known = (await cur.fetchone())[0]
        # recent_memories is a real, cheap count from the shared scope
        cur = await db.execute(
            "SELECT count(*) FROM facts WHERE scope = 'shared' "
            "AND deleted_at IS NULL"
        )
        recent_memories_count = (await cur.fetchone())[0]

    return {
        "pluribus":         probes.get("pluribus",   {"status": UNKNOWN}),
        "xerrameca":        probes.get("xerrameca",  {"status": NOT_CONFIGURED}),
        "hermes":           probes.get("hermes",     {"status": NOT_CONFIGURED}),
        "ollama":           probes.get("ollama",     {"status": NOT_CONFIGURED}),
        "agents_known":     agents_known,
        "recent_memories":  recent_memories_count,
        "warnings":         UNKNOWN,  # UNKNOWN: no current-state source
        "last_update":      _now_iso(),
    }


# --- /v1/dashboard/agents ------------------------------------------------


@router.get("/agents", dependencies=[Depends(dashboard_session_authorize)])
async def dashboard_agents(_request: Request) -> dict[str, Any]:
    """List known agents. ALL telemetry fields are UNKNOWN by default.

    No heuristic inference. The fields exist so the UI can render
    a uniform table, but no value is fabricated from text matches
    in historical memory.
    """
    async with get_db() as db:
        cur = await db.execute(
            "SELECT id, name, permissions, allowed_scopes, is_active "
            "FROM agents ORDER BY name"
        )
        rows = await cur.fetchall()

    agents = []
    for row in rows:
        agent_id, name, perms_json, scopes_json, is_active = row
        try:
            perms = json.loads(perms_json) if perms_json else {}
        except Exception:
            perms = {}
        try:
            scopes = json.loads(scopes_json) if scopes_json else []
        except Exception:
            scopes = []
        agents.append({
            "name":                name,
            "identity":            agent_id,
            "active_flag":         bool(is_active),
            "online_now":          UNKNOWN,
            "last_known_activity": UNKNOWN,
            "current_task":        UNKNOWN,
            "pending_directive":   None,
            "project":             UNKNOWN,
            "blocker":             UNKNOWN,
            "last_result":         UNKNOWN,
            "registered_at":       UNKNOWN,
            "allowed_scopes":      scopes,
            "permissions":         perms,
        })

    return {"agents": agents, "count": len(agents),
            "source": "pluribus_identity", "last_update": _now_iso()}


# --- /v1/dashboard/memory ------------------------------------------------


@router.get("/memory", dependencies=[Depends(dashboard_session_authorize)])
async def dashboard_memory(
    request: Request,
    limit: int = Query(20, ge=1, le=200),
    q: str | None = Query(None),
) -> dict[str, Any]:
    """Latest memory facts (paginated) or FTS search. No secrets."""
    scope = request.query_params.get("scope", "shared")

    if q:
        async with get_db() as db:
            cur = await db.execute(
                """SELECT f.id, f.scope, f.category, f.key, f.content,
                          f.agent_id, f.created_at, f.metadata
                   FROM facts f
                   JOIN facts_fts fts ON f.id = fts.fact_id
                   WHERE facts_fts MATCH ? AND f.scope = ?
                     AND f.deleted_at IS NULL
                   ORDER BY f.created_at DESC LIMIT ?""",
                (q, scope, limit),
            )
            rows = await cur.fetchall()
    else:
        async with get_db() as db:
            cur = await db.execute(
                """SELECT id, scope, category, key, content, agent_id,
                          created_at, metadata
                   FROM facts WHERE scope = ? AND deleted_at IS NULL
                   ORDER BY created_at DESC LIMIT ?""",
                (scope, limit),
            )
            rows = await cur.fetchall()

    items = []
    for row in rows:
        fid, fscope, fcat, fkey, fcontent, fagent, fcreated, fmetadata = row
        try:
            meta = json.loads(fmetadata) if fmetadata else {}
        except Exception:
            meta = {}
        # project is ONLY read from explicit structured metadata, never
        # from text. If the metadata lacks `project`, the field is UNKNOWN.
        project = meta.get("project", UNKNOWN)
        items.append({
            "id":               fid,
            "key":              fkey,
            "category":         fcat,
            "scope":            fscope,
            "agent_id":         fagent,
            "created_at":       fcreated,
            "content_preview":  _redact_content((fcontent or "")[:500]),
            "project":          project,
        })

    async with get_db() as db:
        cur = await db.execute(
            "SELECT count(*) FROM facts WHERE scope = ? AND deleted_at IS NULL",
            (scope,),
        )
        total = (await cur.fetchone())[0]

    return {
        "items":       items,
        "total":       total,
        "limit":       limit,
        "q":           q,
        "scope":       scope,
        "last_update": _now_iso(),
    }


# --- /v1/dashboard/system ------------------------------------------------


@router.get("/system", dependencies=[Depends(dashboard_session_authorize)])
async def dashboard_system(_request: Request) -> dict[str, Any]:
    """Per-service health. Endpoints are sanitized: the UI never
    sees userinfo / query / fragment of a configured URL."""
    endpoints = _service_endpoints()
    services: list[dict[str, Any]] = []
    last_check = _now_iso()

    for name, url in endpoints.items():
        probe = await _probe(url)
        services.append({
            "name":        name,
            "status":      probe.get("status", UNKNOWN),
            "version":     probe.get("version", UNKNOWN),
            "endpoint":    _sanitize_endpoint(url),
            "elapsed_ms":  probe.get("elapsed_ms"),
            "last_check":  last_check,
        })

    canonical = {"pluribus", "xerrameca", "hermes", "ollama"}
    for name in canonical:
        if name not in endpoints:
            services.append({
                "name":       name,
                "status":     NOT_CONFIGURED,
                "version":    UNKNOWN,
                "endpoint":   UNKNOWN,
                "elapsed_ms": None,
                "last_check": last_check,
            })

    return {
        "services":    services,
        "last_update": last_check,
    }
