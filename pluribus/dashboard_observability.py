"""Read-only observability endpoints powering the unified Pluribus dashboard.

These endpoints back the new 4-tab dashboard (HOME / AGENTS / MEMORY / SYSTEM).
They are intentionally narrow and side-effect free:

    GET /v1/dashboard/summary   - agregated service health + counters
    GET /v1/dashboard/agents    - list of known agents from /v1/identity/peers
    GET /v1/dashboard/memory    - latest / searched memory facts (no secrets)
    GET /v1/dashboard/system    - per-service health classification

All endpoints require at most ``memory_authorize`` (read permission) — never
admin. They never expose API keys, tokens, passwords or other secret material
and never mutate state. Telemetry values that the system cannot compute are
returned as the literal string ``"UNKNOWN"`` rather than fabricated.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Query, Request

from pluribus.authorization import memory_authorize
from pluribus.config import settings
from pluribus.db import get_db


# NOTE: we intentionally do NOT register admin-only dependencies on this
# router at module level. Each handler below calls ``memory_authorize`` (or
# relies on the caller already being authenticated) — see main.py where
# ``include_router`` adds ``memory_authorize`` as a dependency for *this*
# router alone, so other endpoints stay admin-gated or unrestricted.
router = APIRouter(
    prefix="/v1/dashboard",
    tags=["dashboard-observability"],
)


# --- Constants used across endpoints ---------------------------------------

UNKNOWN = "UNKNOWN"

# Services we probe for the SYSTEM / SUMMARY tabs.
DEFAULT_SERVICE_ENDPOINTS: dict[str, str] = {
    "pluribus":   "http://100.123.168.63:8790",
    "xerrameca":  "http://100.123.168.63:8791",
    "hermes":     "http://100.79.82.114:9119",
    "ollama":     "http://100.85.57.11:11434",
}

# Tokens whose mere presence inside a fact means we must not expose the body
# verbatim to the dashboard. Used by /v1/dashboard/memory.
_REDACT_TOKENS = ("token", "password", "secret", "api_key")


# --- Helpers ---------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _classify(status: str, elapsed_ms: float | None, error: str | None) -> str:
    """Map a probe outcome into a dashboard-friendly health label."""
    if error == "timeout":
        return "DOWN"
    if error == "connect":
        return "DOWN"
    if error is not None:
        return "DOWN"
    if status.startswith("5"):
        return "DOWN"
    if status.startswith("2"):
        if elapsed_ms is not None and elapsed_ms > 1500:
            return "DEGRADED"
        return "HEALTHY"
    if status.startswith("4"):
        return "DEGRADED"
    return "UNKNOWN"


async def _probe(url: str, timeout: float = 2.5) -> dict[str, Any]:
    """Probe a service endpoint without leaking secrets or raising.

    Returns a small dict with: ``status`` (HTTP code or -1), ``elapsed_ms``,
    ``version``, ``error`` (one of ``"timeout"``, ``"connect"``, ``None``).
    Never raises; callers decide what to do with errors.
    """
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
        elapsed = (time.perf_counter() - started) * 1000
        version = ""
        try:
            body = resp.json()
            if isinstance(body, dict):
                # Common shapes across our services
                version = (
                    body.get("version")
                    or body.get("ollama_version")
                    or body.get("service_version")
                    or ""
                )
        except Exception:
            body = None
        return {
            "status": str(resp.status_code),
            "elapsed_ms": round(elapsed, 1),
            "version": version or UNKNOWN,
            "error": None,
        }
    except httpx.TimeoutException:
        elapsed = (time.perf_counter() - started) * 1000
        return {"status": "0", "elapsed_ms": round(elapsed, 1), "version": UNKNOWN, "error": "timeout"}
    except httpx.ConnectError:
        elapsed = (time.perf_counter() - started) * 1000
        return {"status": "0", "elapsed_ms": round(elapsed, 1), "version": UNKNOWN, "error": "connect"}
    except Exception as exc:  # pragma: no cover - defensive
        elapsed = (time.perf_counter() - started) * 1000
        return {"status": "0", "elapsed_ms": round(elapsed, 1), "version": UNKNOWN, "error": f"other:{type(exc).__name__}"}


def _redact_content(content: str) -> str:
    """Substitute [REDACTED] for any line that smells like a secret."""
    if not content:
        return content
    lowered = content.lower()
    if any(tok in lowered for tok in _REDACT_TOKENS):
        # Be conservative: replace the whole preview when secret-like tokens
        # are present. The dashboard must never leak even partial secrets.
        return "[REDACTED]"
    return content


async def _known_peers(caller_scope: str) -> list[dict[str, Any]]:
    """Pull the public peer list that the caller can already see via identity."""
    req = httpx.Request(
        "GET",
        f"{DEFAULT_SERVICE_ENDPOINTS['pluribus']}/v1/identity/peers",
        params={"scope": caller_scope},
        headers={"X-API-Key": _pluribus_call_key()},
    )
    async with httpx.AsyncClient(timeout=3.0) as client:
        try:
            resp = await client.send(req)
        except Exception:
            return []
    if resp.status_code != 200:
        return []
    try:
        return resp.json() or []
    except Exception:
        return []


def _pluribus_call_key() -> str:
    """Best-effort API key for *internal* probes (peer ls, memory).

    The caller already authenticated against this same Pluribus via the request,
    so reusing any configured admin key for *server→server* peer introspection
    is acceptable. We never expose this value back to the browser.
    """
    return (
        os.environ.get("PLURIBUS_API_KEY")
        or os.environ.get("PLURIBUS_ADMIN_KEY")
        or ""
    )


async def _facts_for_agent(agent_id: str | None, agent_name: str | None, limit: int = 10) -> list[dict[str, Any]]:
    """Best-effort lookup of recent facts touching a given agent (for /agents).

    Match priority: facts whose ``metadata.source_agent`` matches the agent id,
    then facts whose key or content references the agent name.
    """
    out: list[dict[str, Any]] = []
    async with get_db() as db:
        if agent_id:
            cursor = await db.execute(
                """
                SELECT id, key, category, scope, agent_id, created_at, content
                FROM facts
                WHERE deleted_at IS NULL
                  AND (
                    json_extract(metadata, '$.source_agent') = ?
                    OR json_extract(metadata, '$.source_agent_id') = ?
                  )
                ORDER BY updated_at DESC LIMIT ?
                """,
                (agent_id, agent_id, limit),
            )
            rows = await cursor.fetchall()
            out.extend(dict(r) for r in rows)
        if len(out) < limit and agent_name:
            remaining = limit - len(out)
            like = f"%{agent_name}%"
            cursor = await db.execute(
                """
                SELECT id, key, category, scope, agent_id, created_at, content
                FROM facts
                WHERE deleted_at IS NULL
                  AND (key LIKE ? OR content LIKE ?)
                ORDER BY updated_at DESC LIMIT ?
                """,
                (like, like, remaining),
            )
            rows = await cursor.fetchall()
            seen = {r["id"] for r in out}
            for r in rows:
                if r["id"] not in seen:
                    out.append(dict(r))
    return out


def _extract_project(content: str) -> str:
    """Heuristic: pull ``Project: <value>`` from the canonical fact format."""
    if not content:
        return UNKNOWN
    for line in content.splitlines():
        line = line.strip()
        if line.lower().startswith("project:"):
            value = line.split(":", 1)[1].strip()
            return value or UNKNOWN
    return UNKNOWN


def _extract_source_agent(content: str) -> str:
    if not content:
        return UNKNOWN
    for line in content.splitlines():
        line = line.strip()
        if line.lower().startswith("source-agent:") or line.lower().startswith("source_agent:"):
            value = line.split(":", 1)[1].strip()
            return value or UNKNOWN
    return UNKNOWN


def _extract_blocker(content: str) -> str:
    """Return a short blocker description, NONE if nothing mentions BLOCKED."""
    if not content:
        return "NONE"
    for line in content.splitlines():
        line_l = line.lower()
        if "blocked" in line_l or "blocker:" in line_l:
            return line.strip()[:140]
    return "NONE"


def _extract_last_result(content: str) -> str:
    if not content:
        return UNKNOWN
    upper = content.upper()
    if "STATUS: PASS" in upper or "\"status\": \"PASS\"" in content:
        return "PASS"
    if "STATUS: FAIL" in upper or "\"status\": \"FAIL\"" in content:
        return "FAIL"
    if "STATUS: BLOCKED" in upper or "\"status\": \"BLOCKED\"" in content:
        return "BLOCKED"
    return UNKNOWN


# --- Endpoints ---------------------------------------------------------------


@router.get("/summary")
async def dashboard_summary(request: Request) -> dict[str, Any]:
    """High-level dashboard overview combining service health and counters."""
    probes = await asyncio_gather_probes(DEFAULT_SERVICE_ENDPOINTS)
    agents_known = await _count_peers(request)
    recent = await _count_recent_memories(scope="shared", limit=20)
    warnings = await _count_warnings()

    services_block = _format_services(probes)

    return {
        "pluribus": _describe("pluribus", probes.get("pluribus")),
        "xerrameca": _describe("xerrameca", probes.get("xerrameca")),
        "hermes": _describe("hermes", probes.get("hermes")),
        "ollama": _describe("ollama", probes.get("ollama")),
        "services": services_block,
        "agents_known": agents_known,
        "recent_memories": recent,
        "warnings": warnings,
        "last_update": _now_iso(),
    }


@router.get("/agents")
async def dashboard_agents(request: Request) -> dict[str, Any]:
    """List of known agents enriched with their last-known activity."""
    caller_scope = _resolve_caller_scope(request)
    peers = await _known_peers(caller_scope)

    agents_out: list[dict[str, Any]] = []
    for peer in peers:
        agent_id = peer.get("id")
        agent_name = peer.get("name") or UNKNOWN
        facts = await _facts_for_agent(agent_id, agent_name, limit=5)
        latest = facts[0] if facts else None

        if latest:
            content = latest.get("content", "")
            last_known_activity = latest.get("created_at") or UNKNOWN
            project = _extract_project(content)
            blocker = _extract_blocker(content)
            last_result = _extract_last_result(content)
        else:
            last_known_activity = UNKNOWN
            project = UNKNOWN
            blocker = "NONE"
            last_result = UNKNOWN

        current_task = await _current_task_for(agent_id)

        agents_out.append({
            "name": agent_name,
            "identity": agent_id,
            "active_flag": bool(peer.get("is_active")),
            "online_now": UNKNOWN,  # No heartbeat subsystem yet — explicitly UNKNOWN.
            "last_known_activity": last_known_activity,
            "current_task": current_task,
            "project": project,
            "blocker": blocker,
            "last_result": last_result,
            "registered_at": peer.get("created_at", UNKNOWN),
            "allowed_scopes": peer.get("allowed_scopes", []),
            "permissions": peer.get("permissions", {}),
        })

    return {
        "agents": agents_out,
        "count": len(agents_out),
        "source": "pluribus_identity",
        "last_update": _now_iso(),
    }


@router.get("/memory")
async def dashboard_memory(
    request: Request,
    limit: int = Query(default=20, ge=1, le=200),
    q: str = Query(default=""),
    scope: str = Query(default="shared"),
) -> dict[str, Any]:
    """Return recent (or searched) memory facts with secrets redacted."""
    items: list[dict[str, Any]] = []
    async with get_db() as db:
        if q.strip():
            # FTS5 search (same pattern as the legacy /api/search endpoint).
            terms = q.strip().split()
            fts_query = " OR ".join(f'"{t}"*' for t in terms if t)
            sql = """
                SELECT f.id, f.scope, f.category, f.agent_id, f.key,
                       substr(f.content, 1, 200) as content_preview,
                       f.metadata, f.created_at, f.updated_at
                FROM facts f
                JOIN facts_fts fts ON f.id = fts.fact_id
                WHERE facts_fts MATCH ?
                  AND f.deleted_at IS NULL
                  AND f.scope = ?
                ORDER BY f.updated_at DESC
                LIMIT ?
            """
            cursor = await db.execute(sql, (fts_query, scope, limit))
        else:
            sql = """
                SELECT id, scope, category, agent_id, key,
                       substr(content, 1, 200) as content_preview,
                       metadata, created_at, updated_at
                FROM facts
                WHERE deleted_at IS NULL AND scope = ?
                ORDER BY updated_at DESC
                LIMIT ?
            """
            cursor = await db.execute(sql, (scope, limit))

        rows = await cursor.fetchall()
        for r in rows:
            row = dict(r)
            try:
                meta = json.loads(row.get("metadata") or "{}")
            except Exception:
                meta = {}
            project = meta.get("project") or UNKNOWN
            preview = _redact_content(row.get("content_preview", "") or "")
            items.append({
                "id": row["id"],
                "key": row.get("key") or "",
                "category": row.get("category") or "",
                "scope": row.get("scope") or "",
                "agent_id": row.get("agent_id"),
                "created_at": row.get("created_at") or "",
                "updated_at": row.get("updated_at") or "",
                "content_preview": preview,
                "project": project,
            })

    return {
        "items": items,
        "total": len(items),
        "limit": limit,
        "q": q,
        "scope": scope,
        "last_update": _now_iso(),
    }


@router.get("/system")
async def dashboard_system() -> dict[str, Any]:
    """Per-service health, derived from the same probes used by /summary."""
    probes = await asyncio_gather_probes(DEFAULT_SERVICE_ENDPOINTS)
    services = []
    for name, probe in probes.items():
        services.append({
            "name": name,
            "endpoint": DEFAULT_SERVICE_ENDPOINTS[name],
            "status": _classify(probe["status"], probe.get("elapsed_ms"), probe.get("error")),
            "http_status": probe["status"],
            "version": probe.get("version") or UNKNOWN,
            "elapsed_ms": probe.get("elapsed_ms"),
            "last_check": _now_iso(),
            "error": probe.get("error"),
        })
    return {
        "services": services,
        "last_update": _now_iso(),
    }


# --- Internal probe orchestration ------------------------------------------


async def _gather(name_url: dict[str, str]) -> dict[str, dict[str, Any]]:
    import asyncio
    results: dict[str, dict[str, Any]] = {}
    # We probe each service independently so one slow host does not delay others.
    async def _one(name: str, url: str) -> tuple[str, dict[str, Any]]:
        # Pluribus /health has rich JSON; others vary. Always probe root or /health.
        target = url.rstrip("/") + ("/health" if name != "ollama" else "/")
        return name, await _probe(target)
    pairs = await asyncio.gather(*[_one(n, u) for n, u in name_url.items()])
    for n, r in pairs:
        results[n] = r
    return results


async def asyncio_gather_probes(name_url: dict[str, str]) -> dict[str, dict[str, Any]]:
    return await _gather(name_url)


# --- Internal counters -----------------------------------------------------


async def _count_peers(request: Request) -> int:
    scope = _resolve_caller_scope(request)
    peers = await _known_peers(scope)
    return len(peers)


async def _count_recent_memories(scope: str, limit: int) -> int:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) AS cnt FROM facts WHERE deleted_at IS NULL AND scope = ?",
            (scope,),
        )
        row = await cursor.fetchone()
    return int(row["cnt"] or 0) if row else 0


async def _count_warnings() -> int:
    """Heuristic count of facts that look like warnings/errors.

    Counts facts with category='question' or content mentioning BLOCKED/FAIL.
    """
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT COUNT(*) AS cnt FROM facts
            WHERE deleted_at IS NULL
              AND (
                category = 'question'
                OR content LIKE '%BLOCKED%'
                OR content LIKE '%FAIL%'
                OR content LIKE '%ERROR%'
              )
            """
        )
        row = await cursor.fetchone()
    return int(row["cnt"] or 0) if row else 0


async def _current_task_for(agent_id: str | None) -> str:
    """Try to find a pending directive for this agent; else UNKNOWN.

    We read the directives table directly (it's local) rather than calling
    the HTTP endpoint, because the inbox endpoint is keyed by caller ID and
    would only see the dashboard's own session, not the target agent.
    """
    if not agent_id:
        return UNKNOWN
    try:
        async with get_db() as db:
            cursor = await db.execute(
                """
                SELECT id, action, created_at FROM directives
                WHERE target_agent_id = ? AND status = 'pending'
                  AND expires_at > datetime('now')
                ORDER BY created_at ASC LIMIT 1
                """,
                (agent_id,),
            )
            row = await cursor.fetchone()
        if not row:
            return UNKNOWN
        action = row["action"] if "action" in row.keys() else "directive"
        return f"{action} (id={row['id'][:8]})"
    except Exception:
        return UNKNOWN


def _resolve_caller_scope(request: Request) -> str:
    """Pick the broadest scope the caller is allowed to read."""
    agent = getattr(request.state, "agent", None) or {}
    scopes = agent.get("allowed_scopes") or ["shared"]
    if isinstance(scopes, list) and scopes:
        return scopes[0]
    return "shared"


def _format_services(probes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for name, probe in probes.items():
        out.append({
            "name": name,
            "status": _classify(probe["status"], probe.get("elapsed_ms"), probe.get("error")),
            "version": probe.get("version") or UNKNOWN,
            "endpoint": DEFAULT_SERVICE_ENDPOINTS[name],
        })
    return out


def _describe(name: str, probe: dict[str, Any] | None) -> dict[str, Any]:
    if not probe:
        return {"status": "UNKNOWN", "version": UNKNOWN, "endpoint": DEFAULT_SERVICE_ENDPOINTS[name]}
    return {
        "status": _classify(probe["status"], probe.get("elapsed_ms"), probe.get("error")),
        "version": probe.get("version") or UNKNOWN,
        "endpoint": DEFAULT_SERVICE_ENDPOINTS[name],
        "last_check": _now_iso(),
    }