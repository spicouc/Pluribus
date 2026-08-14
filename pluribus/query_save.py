"""
Endpoint per guardar insights de queries com a fets nous,
amb relacions automàtiques als facts que els van originar.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

import numpy as np
from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import BaseModel, Field

from pluribus.audit import log_audit
from pluribus.config import settings
from pluribus.db import get_db
from pluribus.embedding import embedding_service
from pluribus.memory import _check_permission, _generate_embeddings_background

router = APIRouter(prefix="/v1/memory", tags=["query"])


class QuerySaveRequest(BaseModel):
    """Guardar un insight derivat d'una query com a fact nou."""

    query: str = Field(..., description="La query original que va generar aquest insight")
    content: str = Field(..., description="El contingut de l'insight a guardar")
    scope: str = "shared"
    metadata: dict[str, Any] = Field(default_factory=lambda: {"type": "query_insight"})
    source_fact_ids: Optional[list[str]] = Field(
        None,
        description="IDs dels facts que van originar aquest insight.",
    )


async def _visible_source_ids(db, source_ids: list[str], scope: str) -> list[str]:
    """Return only active source facts from exactly the requested scope."""
    if not source_ids:
        return []
    unique_ids = list(dict.fromkeys(source_ids))
    placeholders = ",".join("?" for _ in unique_ids)
    cursor = await db.execute(
        f"""SELECT id FROM facts
            WHERE id IN ({placeholders})
              AND deleted_at IS NULL
              AND scope = ?""",
        [*unique_ids, scope],
    )
    visible = {row["id"] for row in await cursor.fetchall()}
    return [fact_id for fact_id in unique_ids if fact_id in visible]


@router.post("/query-save", status_code=201)
async def query_and_save(
    request: Request,
    body: QuerySaveRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """Guarda un insight i crea relacions només amb facts visibles del mateix scope."""
    agent: dict[str, Any] = request.state.agent
    _check_permission(agent, "write", body.scope)

    async with get_db() as db:
        cursor = await db.execute(
            """INSERT INTO facts (scope, agent_id, key, content, metadata)
               VALUES (?, ?, ?, ?, ?)""",
            (body.scope, agent["id"], None, body.content, json.dumps(body.metadata)),
        )
        rowid = cursor.lastrowid
        cursor2 = await db.execute("SELECT id FROM facts WHERE rowid = ?", (rowid,))
        row = await cursor2.fetchone()
        fact_id = row["id"] if row else ""

        chunks = embedding_service.split_into_chunks(body.content)
        empty_blob = b"\x00" * (settings.EMBED_DIM * 4)
        for chunk_text in chunks:
            await db.execute(
                "INSERT INTO chunks (fact_id, chunk_text, embedding_blob) VALUES (?, ?, ?)",
                (fact_id, chunk_text, empty_blob),
            )
        await db.commit()

        background_tasks.add_task(_generate_embeddings_background, fact_id, chunks)

        source_ids: list[str] = []
        if body.source_fact_ids:
            source_ids = await _visible_source_ids(db, body.source_fact_ids, body.scope)
        else:
            try:
                query_vec = await asyncio.to_thread(
                    embedding_service.get_embedding,
                    body.query,
                    "query: ",
                )
                if float(np.linalg.norm(query_vec)) > 0:
                    from pluribus.vector_index import vector_index

                    scored = await vector_index.search(
                        query_vec,
                        scope_filter=body.scope,
                        top_k=5,
                    )
                    candidate_ids: list[str] = []
                    for chunk_id, _score in scored:
                        c = await db.execute(
                            "SELECT fact_id FROM chunks WHERE id = ?",
                            (chunk_id,),
                        )
                        crow = await c.fetchone()
                        if crow:
                            candidate_ids.append(crow["fact_id"])
                    source_ids = await _visible_source_ids(db, candidate_ids, body.scope)
            except Exception:
                source_ids = []

        relations_created = 0
        for src_id in source_ids:
            if src_id == fact_id:
                continue
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
            relations_created += 1

        await log_audit(
            db,
            agent["id"],
            "CREATE",
            "fact",
            resource_id=fact_id,
            payload=json.dumps(
                {
                    "action": "query_save",
                    "query": body.query,
                    "source_count": relations_created,
                }
            ),
        )
        await db.commit()

    return {
        "fact_id": fact_id,
        "message": "Insight guardat correctament",
        "relations_created": relations_created,
    }
