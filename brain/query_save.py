"""
Endpoint per guardar insights de queries com a fets nous,
amb relacions automàtiques als facts que els van originar.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from brain.audit import log_audit
from brain.db import get_db
from brain.embedding import embedding_service
from brain.memory import (
    _check_permission,
    _generate_embeddings_background,
    WriteResponse,
)

router = APIRouter(prefix="/v1/memory", tags=["query"])


class QuerySaveRequest(BaseModel):
    """Guardar un insight derivat d'una query com a fact nou."""
    query: str = Field(..., description="La query original que va generar aquest insight")
    content: str = Field(..., description="El contingut de l'insight a guardar")
    scope: str = "shared"
    metadata: dict[str, Any] = Field(default_factory=lambda: {"type": "query_insight"})
    source_fact_ids: Optional[list[str]] = Field(
        None,
        description="IDs dels facts que van originar aquest insight. Si no es proporcionen, es busquen automàticament.",
    )


@router.post("/query-save", status_code=201)
async def query_and_save(
    request: Request,
    body: QuerySaveRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """
    Guarda un insight derivat d'una query com a fact nou,
    i el relaciona amb els facts origen.

    1. Guarda el content com a fact
    2. Si no es proporcionen source_fact_ids, fa una cerca semàntica
       per trobar els facts relacionats
    3. Crea relacions 'informed_by' entre el nou fact i els facts origen
    """
    agent: dict[str, Any] = request.state.agent
    _check_permission(agent, "write", body.scope)

    async with get_db() as db:
        # 1. Guardar el fact
        cursor = await db.execute(
            """INSERT INTO facts (scope, agent_id, key, content, metadata)
               VALUES (?, ?, ?, ?, ?)""",
            (body.scope, agent["id"], None, body.content, json.dumps(body.metadata)),
        )
        rowid = cursor.lastrowid
        cursor2 = await db.execute("SELECT id FROM facts WHERE rowid = ?", (rowid,))
        row = await cursor2.fetchone()
        fact_id = row["id"] if row else ""

        # 2. Dividir en chunks i programar embeddings
        chunks = embedding_service.split_into_chunks(body.content)
        for chunk_text in chunks:
            empty_blob = b"\x00" * (768 * 4)  # EMBED_DIM * 4
            await db.execute(
                "INSERT INTO chunks (fact_id, chunk_text, embedding_blob) VALUES (?, ?, ?)",
                (fact_id, chunk_text, empty_blob),
            )

        await db.commit()

        # Programar embeddings en background
        background_tasks.add_task(
            _generate_embeddings_background,
            fact_id,
            chunks,
        )

        # 3. Trobar facts origen
        source_ids = body.source_fact_ids
        if not source_ids:
            # Cerca semàntica per trobar facts relacionats amb la query
            try:
                query_vec = embedding_service.get_embedding(body.query, "query: ")
                from brain.vector_index import vector_index
                scored = await vector_index.search(
                    query_vec,
                    scope_filter=body.scope,
                    top_k=5,
                )
                if scored:
                    seen_fact_ids: set[str] = set()
                    source_ids = []
                    for chunk_id, score in scored:
                        c = await db.execute(
                            "SELECT fact_id FROM chunks WHERE id = ?",
                            (chunk_id,),
                        )
                        crow = await c.fetchone()
                        if crow and crow["fact_id"] not in seen_fact_ids:
                            seen_fact_ids.add(crow["fact_id"])
                            source_ids.append(crow["fact_id"])
            except Exception:
                pass

        # 4. Crear relacions informed_by
        if source_ids:
            for src_id in source_ids:
                # No crear relació amb un mateix
                if src_id == fact_id:
                    continue
                # Comprovar que el fact origen existeix
                check = await db.execute(
                    "SELECT id FROM facts WHERE id = ? AND deleted_at IS NULL",
                    (src_id,),
                )
                if not await check.fetchone():
                    continue
                # No duplicar
                dup = await db.execute(
                    """SELECT id FROM fact_relations
                       WHERE source_fact_id = ? AND target_fact_id = ?
                         AND relation_type = 'informed_by'""",
                    (fact_id, src_id),
                )
                if await dup.fetchone():
                    continue
                await db.execute(
                    """INSERT INTO fact_relations
                       (source_fact_id, target_fact_id, relation_type, relation_strength, discovered_by)
                       VALUES (?, ?, 'informed_by', 1.0, 'auto:query_save')""",
                    (fact_id, src_id),
                )

        await db.commit()

        # Auditoria
        await log_audit(
            db, agent["id"], "CREATE", "fact",
            resource_id=fact_id,
            payload=json.dumps({
                "action": "query_save",
                "query": body.query,
                "source_count": len(source_ids or []),
            }),
        )
        await db.commit()

    return {
        "fact_id": fact_id,
        "message": "Insight guardat correctament",
        "relations_created": len(source_ids or []),
    }
