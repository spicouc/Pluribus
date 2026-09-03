"""Middleware d'autenticació per API Key amb bcrypt i rate limiting."""
from __future__ import annotations

import json
import time
from collections import OrderedDict, defaultdict
from threading import Lock
from typing import Callable

import bcrypt
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from pluribus.api_keys import fingerprint_api_key, is_current_api_key
from pluribus.config import settings
from pluribus.db import get_db


_MAX_CACHE_SIZE = 256
_LEGACY_SCAN_WINDOW = 60.0
_LEGACY_SCAN_PER_CLIENT = 5
_LEGACY_SCAN_GLOBAL = 30


class LRUCache:
    """Petit cache LRU protegit per lock per resultats de bcrypt."""

    def __init__(self, maxsize: int = _MAX_CACHE_SIZE):
        self._maxsize = maxsize
        self._cache: OrderedDict[tuple[str, str], bool] = OrderedDict()
        self._lock = Lock()

    def get(self, key: tuple[str, str]) -> bool | None:
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            return None

    def put(self, key: tuple[str, str], value: bool) -> None:
        with self._lock:
            self._cache[key] = value
            self._cache.move_to_end(key)
            if len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


_bcrypt_cache = LRUCache()
_rate_limiter: dict[str, list[float]] = defaultdict(list)
_last_rate_cleanup: float = time.time()

# Transitional limiter for expensive legacy-key scans. New prefixed keys never
# enter this path. Successful legacy authentication stores a fingerprint so all
# subsequent requests use the O(1) lookup path.
_legacy_scan_lock = Lock()
_legacy_scan_by_client: dict[str, list[float]] = defaultdict(list)
_legacy_scan_global: list[float] = []


def _cleanup_rate_limiter() -> None:
    global _last_rate_cleanup
    now = time.time()
    if now - _last_rate_cleanup < settings.RATE_LIMIT_WINDOW:
        return
    window_start = now - settings.RATE_LIMIT_WINDOW
    for agent_id in list(_rate_limiter.keys()):
        _rate_limiter[agent_id] = [ts for ts in _rate_limiter[agent_id] if ts >= window_start]
        if not _rate_limiter[agent_id]:
            del _rate_limiter[agent_id]
    _last_rate_cleanup = now


def _check_rate_limit(agent_id: str) -> bool:
    global _last_rate_cleanup
    now = time.time()
    if now - _last_rate_cleanup >= 60:
        _cleanup_rate_limiter()
    window_start = now - settings.RATE_LIMIT_WINDOW
    valid = [ts for ts in _rate_limiter[agent_id] if ts >= window_start]
    _rate_limiter[agent_id] = valid
    return len(valid) < settings.RATE_LIMIT


def _record_request(agent_id: str) -> None:
    _rate_limiter[agent_id].append(time.time())


def _allow_legacy_scan(client_id: str) -> bool:
    """Bound expensive fallback scans before any legacy bcrypt loop runs."""
    global _legacy_scan_global
    now = time.time()
    cutoff = now - _LEGACY_SCAN_WINDOW
    client_id = client_id or "unknown"

    with _legacy_scan_lock:
        client_hits = [ts for ts in _legacy_scan_by_client[client_id] if ts >= cutoff]
        global_hits = [ts for ts in _legacy_scan_global if ts >= cutoff]
        _legacy_scan_by_client[client_id] = client_hits
        _legacy_scan_global = global_hits

        if len(client_hits) >= _LEGACY_SCAN_PER_CLIENT:
            return False
        if len(global_hits) >= _LEGACY_SCAN_GLOBAL:
            return False

        _legacy_scan_by_client[client_id].append(now)
        _legacy_scan_global.append(now)
        return True


def _verify_candidate(row_dict: dict, api_key: str, fingerprint: str) -> bool:
    """Run at most one bcrypt verification for a selected candidate."""
    cache_key = (row_dict["api_key_hash"], fingerprint)
    cached = _bcrypt_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        valid = bcrypt.checkpw(
            api_key.encode("utf-8"),
            row_dict["api_key_hash"].encode("utf-8"),
        )
    except Exception:
        valid = False
    _bcrypt_cache.put(cache_key, valid)
    return valid


async def _authenticate_agent(api_key: str, client_id: str = "unknown") -> dict | None:
    """Authenticate through indexed fingerprint lookup, with bounded legacy fallback."""
    if not isinstance(api_key, str) or not (20 <= len(api_key) <= 200):
        return None

    fingerprint = fingerprint_api_key(api_key)
    select_fields = (
        "id, name, api_key_hash, api_key_fingerprint, permissions, allowed_scopes"
    )

    try:
        async with get_db() as db:
            # Fast path: deterministic indexed selector, then bcrypt verifier.
            cursor = await db.execute(
                f"SELECT {select_fields} FROM agents "
                "WHERE is_active = 1 AND api_key_fingerprint = ? LIMIT 1",
                (fingerprint,),
            )
            row = await cursor.fetchone()
            if row:
                row_dict = dict(row)
                return row_dict if _verify_candidate(row_dict, api_key, fingerprint) else None

            # Every key generated by current Pluribus has a prefix and a stored
            # fingerprint. If no row matched, it is invalid: never scan bcrypt.
            if is_current_api_key(api_key):
                return None

            # Compatibility path for pre-fingerprint keys. It is deliberately
            # bounded before the SELECT/bcrypt loop to prevent unauthenticated
            # CPU amplification. A successful login self-migrates the row.
            if not _allow_legacy_scan(client_id):
                return None

            cursor = await db.execute(
                f"SELECT {select_fields} FROM agents "
                "WHERE is_active = 1 AND api_key_fingerprint IS NULL"
            )
            rows = await cursor.fetchall()
            for legacy_row in rows:
                row_dict = dict(legacy_row)
                if not _verify_candidate(row_dict, api_key, fingerprint):
                    continue

                await db.execute(
                    """UPDATE agents
                       SET api_key_fingerprint = ?
                       WHERE id = ? AND api_key_fingerprint IS NULL""",
                    (fingerprint, row_dict["id"]),
                )
                await db.commit()
                row_dict["api_key_fingerprint"] = fingerprint
                return row_dict
    except Exception:
        # Authentication always fails closed if DB/migration state is invalid.
        return None
    return None


def register_security_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def api_key_auth_middleware(request: Request, call_next: Callable) -> Response:
        path = request.url.path
        # Public surfaces: the dashboard HTML, the login page, the
        # login form submission, and the dashboard data endpoints
        # (which carry their own guard).
        if path in {"/health", "/dashboard"} or \
                path.startswith("/dashboard/") or \
                path == "/v1/dashboard/login" or \
                path.startswith("/v1/dashboard/"):
            return await call_next(request)

        if path in ("/mcp", "/mcp/") and request.method == "GET":
            return await call_next(request)

        api_key = request.headers.get("X-API-Key")
        if not api_key:
            return JSONResponse(status_code=401, content={"detail": "Falta la capçalera X-API-Key"})

        client_host = request.client.host if request.client else "unknown"
        agent_found = await _authenticate_agent(api_key, client_host)
        if agent_found is None:
            return JSONResponse(status_code=401, content={"detail": "Clau API invàlida"})

        if not _check_rate_limit(agent_found["id"]):
            return JSONResponse(
                status_code=429,
                content={"detail": "Límit de peticions superat. Torna-ho a provar més tard."},
            )
        _record_request(agent_found["id"])

        try:
            permissions = json.loads(agent_found["permissions"]) if isinstance(agent_found["permissions"], str) else agent_found["permissions"]
            if not isinstance(permissions, dict):
                raise TypeError("permissions must be an object")
        except (json.JSONDecodeError, TypeError):
            permissions = {"read": False, "write": False, "delete": False, "admin": False}

        try:
            allowed_scopes = json.loads(agent_found["allowed_scopes"]) if isinstance(agent_found["allowed_scopes"], str) else agent_found["allowed_scopes"]
            if not isinstance(allowed_scopes, list):
                raise TypeError("allowed_scopes must be a list")
        except (json.JSONDecodeError, TypeError):
            allowed_scopes = []

        request.state.agent = {
            "id": agent_found["id"],
            "name": agent_found["name"],
            "permissions": permissions,
            "allowed_scopes": allowed_scopes,
        }
        return await call_next(request)
