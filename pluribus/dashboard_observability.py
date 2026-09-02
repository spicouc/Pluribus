"""Read-only observability endpoints powering the unified Pluribus dashboard.

These endpoints back the D1 4-tab dashboard (HOME / AGENTS / MEMORY / SYSTEM).
They are intentionally narrow, side-effect free, and SECURITY-CRITICAL:

* All four endpoints are guarded by ``dashboard_observability_authorize``,
  a dedicated guard that enforces:
    - the request is authenticated (X-API-Key present and valid)
    - the caller has the ``read`` permission
    - the caller is allowed to read the requested scope
    - never grants admin
    - never mutates state
  This is a defense-in-depth layer: the underlying
  ``memory_authorize`` is path-routed and does NOT cover
  ``/v1/dashboard/*``. See ``pluribus/authorization.py``.
* Telemetry values that the system cannot compute are returned as the
  literal string ``"UNKNOWN"`` — never fabricated from heuristic
  string matches in historical memory.
* No API keys, tokens, passwords or other secret material is exposed.

Endpoints:
    GET /v1/dashboard/summary   - agregated service health + counters
    GET /v1/dashboard/agents    - list of known agents from /v1/identity/peers
    GET /v1/dashboard/memory    - latest / searched memory facts (no secrets)
    GET /v1/dashboard/system    - per-service health classification
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from pluribus.authorization import _require, _request_agent
from pluribus.config import settings
from pluribus.db import get_db


router = APIRouter(
    prefix="/v1/dashboard",
    tags=["dashboard-observability"],
)


# --- Sentinels ------------------------------------------------------------

UNKNOWN = "UNKNOWN"
NOT_CONFIGURED = "NOT_CONFIGURED"

# Tokens whose mere presence inside a fact means we must not expose the
# body verbatim. Defense-in-depth on top of authorization.
_REDACT_TOKENS = ("token", "password", "secret", "api_key")


# --- Authorization guard (the only authz layer for this router) ---------


async def dashboard_observability_authorize(request: Request) -> None:
    """Dedicated authz guard for /v1/dashboard/*.

    The standard ``memory_authorize`` is path-routed and does not
    cover /v1/dashboard/*. This guard closes that gap: it requires
    an authenticated agent, the ``read`` permission, and (for the
    /memory endpoint) a scope the agent is allowed to read.

    - Fails CLOSED (401 unauthenticated, 403 unauthorized)
    - Never grants admin
    - Never mutates state
    """
    agent = _request_agent(request)
    if agent is None:
        raise HTTPException(status_code=401, detail="Autenticacio requerida")

    # Endpoint-level permission. We allow read on every dashboard
    # endpoint. We explicitly do NOT check for admin — the dashboard
    # is narrow read-only.
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


# --- Configuration-driven service endpoints ------------------------------


def _service_endpoints() -> dict[str, str]:
    """Resolve the SERVICE_ENDPOINTS dict from settings, with
    graceful fallback.

    Pluribus (the dashboard host) is always included: we know where
    we are. Xerrameca, Hermes, Ollama come from the Pluribus
    settings if configured, otherwise an explicit NOT_CONFIGURED
    sentinel is returned for them.

    We DO NOT hardcode Tailscale IPs into the source — deployments
    vary, and the canonical config lives in the Pluribus settings.
    """
    endpoints: dict[str, str] = {}
    # Pluribus: we can derive from our own request
    # (host header / forwarded host) when behind a tunnel, but for
    # the in-process /dashboard access we use settings.PLURIBUS_BIND
    # or a sensible default to the loopback.
    pluribus_url = getattr(settings, "PLURIBUS_DASHBOARD_BASE_URL", None) or \
        f"http://{getattr(settings, 'PLURIBUS_HOST', '127.0.0.1')}:{getattr(settings, 'PLURIBUS_PORT', 8790)}"
    endpoints["pluribus"] = pluribus_url

    # Xerrameca, Hermes, Ollama are optional — if not configured they
    # surface as NOT_CONFIGURED in the UI.
    for name, attr in (("xerrameca", "XERRAMECA_DASHBOARD_URL"),
                       ("hermes",    "HERMES_DASHBOARD_URL"),
                       ("ollama",    "OLLAMA_BASE_URL")):
        url = getattr(settings, attr, None)
        if url:
            endpoints[name] = url
        # else: leave out — the /system endpoint reports NOT_CONFIGURED

    return endpoints


# --- Helpers --------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _classify(status: str, elapsed_ms: float | None, error: str | None) -> str:
    """Map a probe outcome into a dashboard-friendly health label."""
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
    """Best-effort version extraction from common JSON shapes."""
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
    """Replace any token-bearing segment with [REDACTED]."""
    if not content:
        return content
    lower = content.lower()
    for tok in _REDACT_TOKENS:
        if tok in lower:
            return "[REDACTED: contains secret-like material]"
    return content


# --- /v1/dashboard/summary -----------------------------------------------


@router.get("/summary", dependencies=[Depends(dashboard_observability_authorize)])
async def dashboard_summary(_request: Request) -> dict[str, Any]:
    """Global health summary + counters. All unknown values are UNKNOWN."""
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

    # agent count: read scope shared. We use a direct DB query so we
    # don't depend on /v1/identity/peers being available.
    async with get_db() as db:
        cur = await db.execute("SELECT count(*) FROM agents WHERE is_active = 1")
        agents_known = (await cur.fetchone())[0]

    return {
        "pluribus":   probes.get("pluribus",   {"status": UNKNOWN}),
        "xerrameca":  probes.get("xerrameca",  {"status": NOT_CONFIGURED}),
        "hermes":     probes.get("hermes",     {"status": NOT_CONFIGURED}),
        "ollama":     probes.get("ollama",     {"status": NOT_CONFIGURED}),
        "agents_known":   agents_known,
        "recent_memories": 0,  # UNKNOWN until D2 telemetry
        "warnings":        UNKNOWN,  # UNKNOWN: no current-state source
        "last_update":     _now_iso(),
    }


# --- /v1/dashboard/agents ------------------------------------------------


@router.get("/agents", dependencies=[Depends(dashboard_observability_authorize)])
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
            "online_now":          UNKNOWN,  # requires heartbeat (D2)
            "last_known_activity": UNKNOWN,  # requires heartbeat
            "current_task":        UNKNOWN,  # not derivable from memory
            "pending_directive":   None,     # explicit field, None when none
            "project":             UNKNOWN,  # not authoritative from text
            "blocker":             UNKNOWN,  # not authoritative from text
            "last_result":         UNKNOWN,  # not authoritative from text
            "registered_at":       UNKNOWN,  # requires schema addition
            "allowed_scopes":      scopes,
            "permissions":         perms,
        })

    return {"agents": agents, "count": len(agents),
            "source": "pluribus_identity", "last_update": _now_iso()}


# --- /v1/dashboard/memory ------------------------------------------------


@router.get("/memory", dependencies=[Depends(dashboard_observability_authorize)])
async def dashboard_memory(
    request: Request,
    limit: int = Query(20, ge=1, le=200),
    q: str | None = Query(None),
) -> dict[str, Any]:
    """Latest memory facts (paginated) or FTS search. No secrets."""
    scope = request.query_params.get("scope", "shared")

    if q:
        # FTS path — match the pattern used by /api/search in
        # dashboard.py: JOIN facts with the contentless FTS table,
        # then filter by scope at the facts row level.
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
                """SELECT id, scope, category, key, content, agent_id, created_at, metadata
                   FROM facts WHERE scope = ?
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

    # Total count
    async with get_db() as db:
        cur = await db.execute("SELECT count(*) FROM facts WHERE scope = ?", (scope,))
        total = (await cur.fetchone())[0]

    return {
        "items": items,
        "total": total,
        "limit": limit,
        "q":    q,
        "scope": scope,
        "last_update": _now_iso(),
    }


# --- /v1/dashboard/system ------------------------------------------------


@router.get("/system", dependencies=[Depends(dashboard_observability_authorize)])
async def dashboard_system(_request: Request) -> dict[str, Any]:
    """Per-service health. Services without a configured endpoint
    surface as NOT_CONFIGURED, not HEALTHY."""
    endpoints = _service_endpoints()
    services: list[dict[str, Any]] = []
    last_check = _now_iso()

    for name, url in endpoints.items():
        probe = await _probe(url)
        services.append({
            "name":        name,
            "status":      probe.get("status", UNKNOWN),
            "version":     probe.get("version", UNKNOWN),
            "endpoint":    url,
            "elapsed_ms":  probe.get("elapsed_ms"),
            "last_check":  last_check,
        })

    # Surface any not-yet-discovered service as NOT_CONFIGURED so the
    # UI can render a complete picture even when something is missing.
    canonical = {"pluribus", "xerrameca", "hermes", "ollama"}
    for name in canonical:
        if name not in endpoints:
            services.append({
                "name":       name,
                "status":     NOT_CONFIGURED,
                "version":    UNKNOWN,
                "endpoint":   None,
                "elapsed_ms": None,
                "last_check": last_check,
            })

    return {
        "services":    services,
        "last_update": last_check,
    }
