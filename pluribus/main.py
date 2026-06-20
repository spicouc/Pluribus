"""Punt d'entrada principal de l'aplicació Brain.

Crea l'instància FastAPI, registra el middleware de seguretat,
els routers i el lifespan per inicialitzar la base de dades.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import asyncio
import json
from fastapi import FastAPI, Request
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
from pluribus.expiry_worker import expiry_worker_loop
from pluribus.db import get_db
from pluribus.compact import compact_database


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gestor del cicle de vida de l'aplicació.

    En iniciar: inicialitza l'esquema de la base de dades.
    En tancar: neteja recursos.
    """
    try:
        await init_db()
        print("✓ Base de dades inicialitzada correctament")
    except Exception as exc:
        print(f"⚠ Error inicialitzant la base de dades: {exc}")

    # Inicia workers en segon pla
    import asyncio
    task_handles = []

    # Worker d'expiració cada 5 minuts
    async def _run_expiry():
        try:
            await expiry_worker_loop()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            print(f"⚠ Worker d'expiració aturat: {exc}")

    expiry_task = asyncio.create_task(_run_expiry())
    task_handles.append(expiry_task)
    print(f"✓ Worker d'expiració (TTL) iniciat cada 5 minuts")

    # Worker de compactació cada 24h
    async def _run_compact():
        while True:
            try:
                await asyncio.sleep(86400)  # 24 hores
                print("🗜 Iniciant compactació programada...")
                result = await asyncio.to_thread(compact_database)
                print(f"🗜 Compactació completada: {json.dumps(result)}")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                print(f"⚠ Error en compactació programada: {exc}")

    compact_task = asyncio.create_task(_run_compact())
    task_handles.append(compact_task)
    print(f"✓ Worker de compactació (VACUUM) iniciat cada 24h")

    yield

    # Neteja en shutdown
    for task in task_handles:
        task.cancel()
    await asyncio.gather(*task_handles, return_exceptions=True)
    print("✓ Workers aturats correctament")
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


@app.post("/v1/admin/compact", status_code=200)
async def admin_compact(request: Request) -> dict:
    """Executa compactació de la base de dades. Admin només."""
    agent: dict = request.state.agent
    perms = agent.get("permissions", {})
    if not perms.get("admin", False):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Permís admin requerit")

    result = await asyncio.to_thread(compact_database)
    return {
        "message": "Compactació completada",
        "archived_facts": result["archived_facts"],
        "space_before": result["space_before"],
        "space_after": result["space_after"],
        "space_saved": result["space_saved"],
        "vacuum_done": result["vacuum_done"],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "brain.main:app",
        host="0.0.0.0",
        port=settings.API_PORT,
        workers=1,
    )
