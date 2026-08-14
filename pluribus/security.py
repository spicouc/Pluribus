"""Middleware d'autenticació per API Key amb bcrypt i rate limiting."""
from __future__ import annotations

import json
import time
from collections import defaultdict, OrderedDict
from threading import Lock
from typing import Callable

import bcrypt
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from pluribus.config import settings
from pluribus.db import get_db


_MAX_CACHE_SIZE = 256


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


async def _authenticate_agent(api_key: str) -> dict | None:
    """Valida una API key. Agents desactivats mai no poden autenticar-se."""
    try:
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT id, name, api_key_hash, permissions, allowed_scopes "
                "FROM agents WHERE is_active = 1"
            )
            rows = await cursor.fetchall()
            for row in rows:
                row_dict = dict(row)
                cache_key = (row_dict["api_key_hash"], api_key)
                cached = _bcrypt_cache.get(cache_key)
                if cached is True:
                    return row_dict
                if cached is False:
                    continue
                try:
                    valid = bcrypt.checkpw(
                        api_key.encode("utf-8"),
                        row_dict["api_key_hash"].encode("utf-8"),
                    )
                except Exception:
                    valid = False
                _bcrypt_cache.put(cache_key, valid)
                if valid:
                    return row_dict
    except Exception:
        # Authentication fails closed if the DB cannot be read.
        return None
    return None


def register_security_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def api_key_auth_middleware(request: Request, call_next: Callable) -> Response:
        # Only the health endpoint and dashboard HTML shell are public.
        public_paths = {"/health", "/dashboard"}
        if request.url.path in public_paths:
            return await call_next(request)

        # MCP GET only exposes the tool catalogue; calls remain authenticated.
        if request.url.path in ("/mcp", "/mcp/") and request.method == "GET":
            return await call_next(request)

        api_key = request.headers.get("X-API-Key")
        if not api_key:
            return JSONResponse(status_code=401, content={"detail": "Falta la capçalera X-API-Key"})

        agent_found = await _authenticate_agent(api_key)
        if agent_found is None:
            return JSONResponse(status_code=401, content={"detail": "Clau API invàlida"})

        if not _check_rate_limit(agent_found["id"]):
            return JSONResponse(
                status_code=429,
                content={"detail": "Límit de peticions superat. Torna-ho a provar més tard."},
            )
        _record_request(agent_found["id"])

        # Invalid permission data fails closed instead of granting read/write.
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
