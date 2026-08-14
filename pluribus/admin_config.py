"""Hardened administrative configuration mutation endpoints."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from pluribus.audit import log_audit
from pluribus.config import settings
from pluribus.db import get_db

router = APIRouter(tags=["admin-config"])

_EDITABLE_KEYS = {
    "PLURIBUS_OLLAMA_BASE_URL",
    "PLURIBUS_OLLAMA_MODEL",
    "PLURIBUS_CONSOLIDATION_MODEL",
    "PLURIBUS_EMBED_DIM",
    "PLURIBUS_MAX_CHUNK_SIZE",
    "PLURIBUS_CHUNK_OVERLAP",
    "PLURIBUS_RATE_LIMIT",
    "PLURIBUS_RATE_LIMIT_WINDOW",
    "PLURIBUS_API_PORT",
}
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/@+\-]+$")
_INTEGER_RANGES = {
    "PLURIBUS_EMBED_DIM": (1, 16384),
    "PLURIBUS_MAX_CHUNK_SIZE": (64, 100000),
    "PLURIBUS_CHUNK_OVERLAP": (0, 99999),
    "PLURIBUS_RATE_LIMIT": (1, 100000),
    "PLURIBUS_RATE_LIMIT_WINDOW": (1, 86400),
    "PLURIBUS_API_PORT": (1024, 65535),
}
_MODEL_KEYS = {
    "PLURIBUS_OLLAMA_MODEL",
    "PLURIBUS_CONSOLIDATION_MODEL",
}


def _require_admin(request: Request) -> dict[str, Any]:
    agent = getattr(request.state, "agent", None) or {}
    if not agent.get("permissions", {}).get("admin", False):
        raise HTTPException(status_code=403, detail="Permís admin requerit")
    return agent


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no", ""}:
            return False
    raise HTTPException(status_code=400, detail="_restart ha de ser booleà")


def _scalar_text(key: str, value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise HTTPException(status_code=400, detail=f"Valor invàlid per {key}")
    text = str(value).strip()
    if not text or any(ch in text for ch in ("\r", "\n", "\x00")):
        raise HTTPException(status_code=400, detail=f"Valor invàlid per {key}")
    if len(text) > 2048:
        raise HTTPException(status_code=400, detail=f"Valor massa llarg per {key}")
    return text


def _validate_updates(body: dict[str, Any]) -> tuple[dict[str, str], bool]:
    """Return canonical env strings plus optional restart request."""
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Cos JSON invàlid")

    restart = _bool_value(body.get("_restart", False))
    unknown = set(body) - _EDITABLE_KEYS - {"_restart"}
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Claus de configuració no editables: {', '.join(sorted(unknown))}",
        )

    updates: dict[str, str] = {}
    for key in _EDITABLE_KEYS:
        if key not in body:
            continue
        text = _scalar_text(key, body[key])
        if key in _INTEGER_RANGES:
            try:
                number = int(text, 10)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"{key} ha de ser enter") from exc
            minimum, maximum = _INTEGER_RANGES[key]
            if not minimum <= number <= maximum:
                raise HTTPException(
                    status_code=400,
                    detail=f"{key} fora de rang [{minimum}, {maximum}]",
                )
            text = str(number)
        elif key in _MODEL_KEYS:
            if len(text) > 300 or not _MODEL_RE.fullmatch(text):
                raise HTTPException(status_code=400, detail=f"Nom de model invàlid per {key}")
        elif key == "PLURIBUS_OLLAMA_BASE_URL":
            try:
                parsed = urlsplit(text)
                port = parsed.port
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="OLLAMA_BASE_URL invàlida") from exc
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise HTTPException(status_code=400, detail="OLLAMA_BASE_URL ha de ser http(s)")
            if parsed.username is not None or parsed.password is not None or parsed.fragment:
                raise HTTPException(status_code=400, detail="OLLAMA_BASE_URL conté components no permesos")
            if port is not None and not (1 <= port <= 65535):
                raise HTTPException(status_code=400, detail="Port Ollama invàlid")
        updates[key] = text

    max_chunk = int(updates.get("PLURIBUS_MAX_CHUNK_SIZE", settings.MAX_CHUNK_SIZE))
    overlap = int(updates.get("PLURIBUS_CHUNK_OVERLAP", settings.CHUNK_OVERLAP))
    if overlap >= max_chunk:
        raise HTTPException(
            status_code=400,
            detail="PLURIBUS_CHUNK_OVERLAP ha de ser menor que PLURIBUS_MAX_CHUNK_SIZE",
        )
    return updates, restart


def _render_updated_env(existing: str, updates: dict[str, str]) -> str:
    lines = existing.splitlines(keepends=True)
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        replaced = False
        stripped = line.lstrip()
        if not stripped.startswith("#") and "=" in stripped:
            current_key = stripped.split("=", 1)[0].strip()
            if current_key in updates:
                output.append(f"{current_key}={updates[current_key]}\n")
                seen.add(current_key)
                replaced = True
        if not replaced:
            output.append(line if line.endswith("\n") else line + "\n")
    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={value}\n")
    return "".join(output)


def _atomic_update_env(path_text: str, updates: dict[str, str]) -> None:
    path = Path(path_text)
    if path.exists() and path.is_symlink():
        raise RuntimeError("Refusing to replace a symlinked env file")
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    rendered = _render_updated_env(existing, updates)

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".pluribus-env-",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _restart_service() -> None:
    subprocess.Popen(
        ["systemctl", "restart", "pluribus"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


async def _audit(agent_id: str, action: str, payload: dict[str, Any]) -> None:
    try:
        async with get_db() as db:
            await log_audit(
                db,
                agent_id,
                action,
                "config",
                resource_id="pluribus.env",
                payload=json.dumps(payload, sort_keys=True),
            )
            await db.commit()
    except Exception:
        # Configuration mutation has already succeeded; audit failure must not
        # convert it into a misleading client-visible failure.
        pass


@router.post("/api/config/save")
async def secure_save_config(request: Request) -> JSONResponse:
    agent = _require_admin(request)
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="JSON invàlid") from exc
    updates, restart = _validate_updates(body)
    if not updates:
        raise HTTPException(status_code=400, detail="No hi ha canvis editables")

    try:
        await asyncio.to_thread(_atomic_update_env, settings.ENV_PATH, updates)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="No s'ha pogut escriure la configuració") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    await _audit(agent["id"], "CONFIG_UPDATE", {"updated_keys": sorted(updates)})
    result: dict[str, Any] = {
        "message": "Configuració guardada",
        "updated_keys": sorted(updates),
        "restart": restart,
    }
    if restart:
        _restart_service()
        result["message"] = "Configuració guardada. Reiniciant Pluribus..."
        result["restarting"] = True
    return JSONResponse(result)


@router.post("/api/config/restart")
async def secure_restart(request: Request) -> JSONResponse:
    agent = _require_admin(request)
    _restart_service()
    await _audit(agent["id"], "SERVICE_RESTART", {})
    return JSONResponse({"message": "Reiniciant Pluribus..."})


@router.get("/api/config/restart")
async def reject_get_restart(request: Request) -> JSONResponse:
    _require_admin(request)
    raise HTTPException(status_code=405, detail="El restart requereix POST")
