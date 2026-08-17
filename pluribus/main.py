"""Punt d'entrada principal de l'aplicació Pluribus."""

from __future__ import annotations

from contextlib import asynccontextmanager

import asyncio
import json

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from pluribus.admin_config import router as admin_config_router
from pluribus.admin_config_view import router as admin_config_view_router
from pluribus.agents import router as agents_router
from pluribus.authorization import (
    agents_authorize,
    dashboard_authorize,
    knowledge_authorize,
    mcp_authorize,
    memory_authorize,
)
from pluribus.compact import compact_database
from pluribus.config import settings
from pluribus.dashboard import router as dashboard_router
from pluribus.db import get_db, init_db
from pluribus.directives import router as directives_router
from pluribus.directives_schema import init_directives_db
from pluribus.embedding import embedding_service
from pluribus.expiry_worker import expiry_worker_loop
from pluribus.identity_provider import router as identity_provider_router
from pluribus.knowledge import router as knowledge_router
from pluribus.lint import router as lint_router
from pluribus.mcp import router as mcp_router
from pluribus.mcp_async import router as mcp_async_router
from pluribus.memory import router as memory_router
from pluribus.memory_sync import init_memory_sync_db, router as memory_sync_router
from pluribus.query_save import router as query_save_router
from pluribus.recall import router as recall_router
from pluribus.security import register_security_middleware
from pluribus.semantic_async import router as semantic_router
from pluribus.webhooks import router as webhooks_router
from pluribus.xerrameca import router as xerrameca_router
from pluribus.xerrameca.console_entry import router as xerrameca_console_entry_router
from pluribus.xerrameca.dialogue_schema import init_xerrameca_dialogue_db
from pluribus.xerrameca.monitor import monitor_loop, router as xerrameca_monitor_router
from pluribus.xerrameca.monitor_schema import init_xerrameca_monitor_db
from pluribus.xerrameca.runner_dialogue import runner_loop
from pluribus.xerrameca.runner_router import router as xerrameca_runner_router
from pluribus.xerrameca.runner_schema import init_xerrameca_runner_db
from pluribus.xerrameca.schema import init_xerrameca_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicialitza DB abans de servir trànsit i gestiona workers interns."""
    await init_db()
    await init_directives_db()
    await init_memory_sync_db()
    await init_xerrameca_db()
    await init_xerrameca_dialogue_db()
    await init_xerrameca_runner_db()
    await init_xerrameca_monitor_db()
    print(
        "✓ Base de dades, Directives, Memory Sync, Xerrameca Dialogue, Runner i Monitor inicialitzats correctament"
    )

    task_handles: list[asyncio.Task] = []

    async def _run_expiry() -> None:
        try:
            await expiry_worker_loop()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            print(f"⚠ Worker d'expiració aturat: {exc}")

    expiry_task = asyncio.create_task(_run_expiry())
    task_handles.append(expiry_task)
    print("✓ Worker d'expiració (TTL) iniciat cada 5 minuts")

    async def _run_compact() -> None:
        while True:
            try:
                await asyncio.sleep(86400)
                print("🗜 Iniciant compactació programada...")
                result = await asyncio.to_thread(compact_database)
                print(f"🗜 Compactació completada: {json.dumps(result)}")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                print(f"⚠ Error en compactació programada: {exc}")

    compact_task = asyncio.create_task(_run_compact())
    task_handles.append(compact_task)
    print("✓ Worker de compactació (VACUUM) iniciat cada 24h")

    async def _run_xerrameca_runner() -> None:
        try:
            await runner_loop()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            print(f"⚠ Xerrameca Runner aturat: {exc}")

    runner_task = asyncio.create_task(_run_xerrameca_runner())
    task_handles.append(runner_task)
    print("✓ Xerrameca Runner iniciat (desactivat per defecte fins habilitació admin)")

    async def _run_xerrameca_monitor() -> None:
        try:
            await monitor_loop()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            print(f"⚠ Xerrameca Monitor aturat: {exc}")

    monitor_task = asyncio.create_task(_run_xerrameca_monitor())
    task_handles.append(monitor_task)
    print("✓ Xerrameca Monitor iniciat (observació passiva per defecte)")

    yield

    for task in task_handles:
        task.cancel()
    await asyncio.gather(*task_handles, return_exceptions=True)
    print("✓ Workers aturats correctament")
    print("✓ Servei Pluribus aturat")


app = FastAPI(
    title="Pluribus - Multi-agent shared memory service",
    description="Servei de memòria compartida multi-agent — Pluribus",
    version="2.4.0",
    lifespan=lifespan,
)

register_security_middleware(app)

memory_dependencies = [Depends(memory_authorize)]
# Recall performs defense-in-depth authorization inside its own service so it is
# safe for REST and non-HTTP callers such as MCP.
app.include_router(recall_router)
# Memory Sync has its own scope-safe service authorization and must precede the
# legacy dynamic memory routes.
app.include_router(memory_sync_router)
# Directives are a separate control plane: facts remain passive memory.
app.include_router(directives_router)
# Async semantic routes must precede the legacy duplicates in memory.py.
app.include_router(semantic_router, dependencies=memory_dependencies)
app.include_router(memory_router, dependencies=memory_dependencies)
app.include_router(query_save_router, dependencies=memory_dependencies)
app.include_router(lint_router, dependencies=memory_dependencies)
# The public dashboard entry switches to Xerrameca/Monitor views and delegates
# the default view to the legacy dashboard. Data APIs remain authenticated.
app.include_router(xerrameca_console_entry_router)
# Hardened config read/mutation routes must precede dashboard.py's legacy duplicates.
app.include_router(admin_config_view_router, dependencies=[Depends(dashboard_authorize)])
app.include_router(admin_config_router, dependencies=[Depends(dashboard_authorize)])
app.include_router(dashboard_router, dependencies=[Depends(dashboard_authorize)])
# Intercept MCP semantic/recall/sync/directive calls while delegating other tools.
app.include_router(mcp_async_router, dependencies=[Depends(mcp_authorize)])
app.include_router(mcp_router, dependencies=[Depends(mcp_authorize)])
app.include_router(identity_provider_router, dependencies=[Depends(agents_authorize)])
app.include_router(agents_router, dependencies=[Depends(agents_authorize)])
app.include_router(xerrameca_router)
app.include_router(xerrameca_runner_router)
app.include_router(xerrameca_monitor_router)
app.include_router(webhooks_router)
# Current graph model is global, so fail closed to admin until it becomes scope-aware.
app.include_router(knowledge_router, dependencies=[Depends(knowledge_authorize)])


async def _sqlite_is_healthy() -> bool:
    """Execute a real bounded DB query instead of reporting a constant."""
    try:
        async with asyncio.timeout(2.0):
            async with get_db() as db:
                cursor = await db.execute("SELECT 1 AS ok")
                row = await cursor.fetchone()
                return bool(row and row["ok"] == 1)
    except Exception:
        return False


@app.get("/health")
async def health() -> JSONResponse:
    sqlite_ok = await _sqlite_is_healthy()
    try:
        embedding_ready = await embedding_service.check_ready()
    except Exception:
        embedding_ready = False

    if not sqlite_ok:
        status = "error"
        status_code = 503
    elif not embedding_ready:
        status = "degraded"
        status_code = 200
    else:
        status = "ok"
        status_code = 200

    return JSONResponse(
        status_code=status_code,
        content={
            "status": status,
            "sqlite": sqlite_ok,
            "embedding_ready": embedding_ready,
            "version": "2.4.0",
        },
    )


@app.post("/v1/admin/compact", status_code=200)
async def admin_compact(request: Request) -> dict:
    agent: dict = request.state.agent
    if not agent.get("permissions", {}).get("admin", False):
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
        "pluribus.main:app",
        host="0.0.0.0",
        port=settings.API_PORT,
        workers=1,
    )
