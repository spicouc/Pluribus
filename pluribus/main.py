"""Punt d'entrada principal de l'aplicació Brain.

Crea l'instància FastAPI, registra el middleware de seguretat,
els routers i el lifespan per inicialitzar la base de dades.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from pluribus.config import settings
from pluribus.dashboard import router as dashboard_router
from pluribus.db import init_db
from pluribus.embedding import embedding_service
from pluribus.mcp import router as mcp_router
from pluribus.agents import router as agents_router
from pluribus.webhooks import router as webhooks_router
from pluribus.memory import router as memory_router
from pluribus.security import register_security_middleware
from pluribus.knowledge import router as knowledge_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gestor del cicle de vida de l'aplicació.

    En iniciar: inicialitza l'esquema de la base de dades.
    En tancar: neteja recursos (el model d'embeddings no necessita neteja).
    """
    try:
        await init_db()
        print("✓ Base de dades inicialitzada correctament")
    except Exception as exc:
        print(f"⚠ Error inicialitzant la base de dades: {exc}")
    yield
    # Neteja en shutdown
    print("✓ Servicio Brain aturat")


# Creació de l'aplicació FastAPI
app = FastAPI(
    title="Pluribus - Multi-agent shared memory service",
    description="Servei de memòria compartida multi-agent (ex Brain)",
    version="1.0.0",
    lifespan=lifespan,
)

# Registra el middleware de seguretat (API Key auth)
register_security_middleware(app)

# Inclou els routers
app.include_router(memory_router)
app.include_router(dashboard_router)
app.include_router(mcp_router)
app.include_router(agents_router)
app.include_router(webhooks_router)
app.include_router(knowledge_router)


@app.get("/health")
async def health() -> JSONResponse:
    """Endpoint de salut del servei."""
    embedding_ready = embedding_service.is_ready
    return JSONResponse({
        "status": "ok",
        "sqlite": True,
        "embedding_ready": embedding_ready,
        "version": "1.0.0",
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "brain.main:app",
        host="0.0.0.0",
        port=settings.API_PORT,
        workers=1,
    )
