"""Generic authenticated identity-provider endpoints for external services.

These routes expose only the authenticated caller and scope-compatible peers.
They never return API-key material, hashes, fingerprints, IPs, or private metadata.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from pluribus.db import get_db
from pluribus.validation import validate_scope


router = APIRouter(prefix="/v1/identity", tags=["identity"])


def _decode_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _decode_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    if not isinstance(value, str):
        return []
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []
    return [item for item in decoded if isinstance(item, str)] if isinstance(decoded, list) else []


def _public_identity(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "permissions": _decode_object(row["permissions"]),
        "allowed_scopes": _decode_list(row["allowed_scopes"]),
        "capabilities": _decode_object(row["capabilities"]),
        "is_active": bool(row["is_active"]),
    }


def _require_scope_access(agent: dict[str, Any], scope: str) -> None:
    permissions = agent.get("permissions") or {}
    if permissions.get("admin", False):
        return
    if not permissions.get("read", False) or not permissions.get("write", False):
        raise HTTPException(
            status_code=403,
            detail="La descoberta de peers requereix permisos read + write",
        )
    if scope not in (agent.get("allowed_scopes") or []):
        raise HTTPException(
            status_code=403,
            detail=f"Àmbit '{scope}' no permès per a aquest agent",
        )


@router.get("/me")
async def identity_me(request: Request) -> dict[str, Any]:
    """Return the full public identity of the authenticated caller."""
    caller: dict[str, Any] = request.state.agent
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT id, name, permissions, allowed_scopes, capabilities, is_active
               FROM agents WHERE id = ? AND is_active = 1""",
            (caller["id"],),
        )
        row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Agent autenticat no trobat o inactiu")
    return _public_identity(row)


@router.get("/peers")
async def identity_peers(
    request: Request,
    scope: str = Query(default="shared", min_length=1, max_length=128),
) -> list[dict[str, Any]]:
    """Return active peers eligible to collaborate with the caller in a scope."""
    try:
        scope = validate_scope(scope)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    caller: dict[str, Any] = request.state.agent
    _require_scope_access(caller, scope)

    async with get_db() as db:
        cursor = await db.execute(
            """SELECT id, name, permissions, allowed_scopes, capabilities, is_active
               FROM agents
               WHERE is_active = 1 AND id != ?
               ORDER BY name COLLATE NOCASE, id""",
            (caller["id"],),
        )
        rows = await cursor.fetchall()

    peers: list[dict[str, Any]] = []
    for row in rows:
        identity = _public_identity(row)
        permissions = identity["permissions"]
        eligible = permissions.get("admin", False) or (
            permissions.get("read", False)
            and permissions.get("write", False)
            and scope in identity["allowed_scopes"]
        )
        if eligible:
            peers.append(identity)
    return peers
