"""Read-only dashboard configuration view backed by the active state env file."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from pluribus.config import settings
from pluribus.embedding import embedding_service

router = APIRouter(tags=["admin-config"])

_SENSITIVE_MARKERS = ("_KEY", "_SECRET", "_TOKEN", "_PASSWORD")


def _read_safe_env(path_text: str) -> dict[str, str]:
    path = Path(path_text)
    if not path.exists():
        return {"_error": "EnvironmentFile no trobat"}
    if path.is_symlink():
        return {"_error": "EnvironmentFile symlink no permès"}

    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key.startswith("PLURIBUS_"):
            continue
        if any(marker in key.upper() for marker in _SENSITIVE_MARKERS):
            continue
        result[key] = value.strip()
    return result


@router.get("/api/config")
async def secure_get_config(request: Request) -> JSONResponse:
    """Expose only non-secret config from the same file systemd actually loads."""
    config = await asyncio.to_thread(_read_safe_env, settings.ENV_PATH)
    config.update(
        {
            "_ENV_PATH": settings.ENV_PATH,
            "_OLLAMA_BASE_URL": settings.OLLAMA_BASE_URL,
            "_OLLAMA_MODEL": settings.OLLAMA_MODEL,
            "_CONSOLIDATION_MODEL": settings.CONSOLIDATION_MODEL,
            "_EMBED_DIM": str(settings.EMBED_DIM),
            "_MAX_CHUNK_SIZE": str(settings.MAX_CHUNK_SIZE),
            "_CHUNK_OVERLAP": str(settings.CHUNK_OVERLAP),
            "_RATE_LIMIT": str(settings.RATE_LIMIT),
            "_RATE_LIMIT_WINDOW": str(settings.RATE_LIMIT_WINDOW),
            "_API_PORT": str(settings.API_PORT),
            "_embedding_ready": str(embedding_service.is_ready),
            "_version": "2.0.0",
        }
    )
    return JSONResponse(config)
