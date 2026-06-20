"""Router del graf de coneixement: relacions entre fets."""

from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.responses import JSONResponse

from brain.db import get_db
from brain.models import (
    CreateRelationRequest,
    GraphEdge,
    GraphNode,
    GraphResponse,
    RelationResponse,
)

router = APIRouter(prefix="/v1/knowledge", tags=["knowledge"])


def _check_read_permission(agent: dict[str, Any]) -> None:
    if not agent.get("permissions", {}).get("read", False):
        raise HTTPException(status_code=403, detail="Sense permís de lectura")


@router.get("/graph", response_model=GraphResponse)
async def get_knowledge_graph(
    request: Request,
    fact_id: Optional[str] = Query(None, description="Fact central per explorar el subgraf"),
    depth: int = Query(1, ge=1, le=3, description="Profunditat del graf"),
    limit: int = Query(50, ge=1, le=200, description="Màxim de nodes"),
) -> GraphResponse:
    """Retorna el graf de coneixement al voltant d'un fact o complet."""
    agent: dict[str, Any] = request.state.agent
    _check_read_permission(agent)

    async with get_db() as db:
        if fact_id:
            # Subgraf centrat en un fact
            visited_facts: set[str] = set()
            edges_list: list[dict] = []
            current_level = {fact_id}
            visited_facts.add(fact_id)

            for _ in range(depth):
                if not current_level:
                    break
                placeholders = ",".join("?" for _ in current_level)
                cursor = await db.execute(
                    f"""
                    SELECT r.*, f1.content as source_content, f2.content as target_content,
                           f1.created_at as source_created, f2.created_at as target_created
                    FROM fact_relations r
                    JOIN facts f1 ON r.source_fact_id = f1.id
                    JOIN facts f2 ON r.target_fact_id = f2.id
                    WHERE (r.source_fact_id IN ({placeholders}) OR r.target_fact_id IN ({placeholders}))
                    ORDER BY r.relation_strength DESC
                    LIMIT ?
                    """,
                    list(current_level) + list(current_level) + [limit * 2],
                )
                rows = await cursor.fetchall()
                next_level: set[str] = set()
                for row in rows:
                    rdict = dict(row)
                    edges_list.append(rdict)
                    if rdict["source_fact_id"] not in visited_facts:
                        next_level.add(rdict["source_fact_id"])
                    if rdict["target_fact_id"] not in visited_facts:
                        next_level.add(rdict["target_fact_id"])
                visited_facts.update(next_level)
                current_level = next_level

            # Construir nodes
            fact_ids_to_fetch = visited_facts
            nodes: list[GraphNode] = []
            added_facts = set()

            for e in edges_list:
                for fid, content, created in [
                    (e["source_fact_id"], e["source_content"], e["source_created"]),
                    (e["target_fact_id"], e["target_content"], e["target_created"]),
                ]:
                    if fid not in added_facts:
                        added_facts.add(fid)
                        preview = content[:120] + "..." if len(content) > 120 else content
                        nodes.append(GraphNode(
                            id=fid,
                            type="fact",
                            label=preview[:60],
                            content_preview=preview,
                            created_at=created or "",
                        ))

            # Construir arestes
            edges = []
            for e in edges_list:
                edges.append(GraphEdge(
                    source=e["source_fact_id"],
                    target=e["target_fact_id"],
                    relation=e["relation_type"],
                    strength=e["relation_strength"],
                ))

        else:
            # Graf complet (totes les relacions)
            cursor = await db.execute("""
                SELECT r.*, f1.content as source_content, f2.content as target_content,
                       f1.created_at as source_created, f2.created_at as target_created
                FROM fact_relations r
                JOIN facts f1 ON r.source_fact_id = f1.id
                JOIN facts f2 ON r.target_fact_id = f2.id
                ORDER BY r.relation_strength DESC
                LIMIT ?
            """, (limit,))
            rows = await cursor.fetchall()

            fact_ids: set[str] = set()
            edges = []
            for row in rows:
                rdict = dict(row)
                fact_ids.add(rdict["source_fact_id"])
                fact_ids.add(rdict["target_fact_id"])
                edges.append(GraphEdge(
                    source=rdict["source_fact_id"],
                    target=rdict["target_fact_id"],
                    relation=rdict["relation_type"],
                    strength=rdict["relation_strength"],
                ))

            nodes = []
            if fact_ids:
                # Get fact info
                cursor2 = await db.execute(
                    f"""
                    SELECT id, content, created_at FROM facts
                    WHERE id IN ({','.join('?' for _ in fact_ids)})
                    """,
                    list(fact_ids),
                )
                for frow in await cursor2.fetchall():
                    fdict = dict(frow)
                    preview = fdict["content"][:120] + "..." if len(fdict["content"]) > 120 else fdict["content"]
                    nodes.append(GraphNode(
                        id=fdict["id"],
                        type="fact",
                        label=preview[:60],
                        content_preview=preview,
                        created_at=fdict["created_at"] or "",
                    ))

        # Total counts
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM facts WHERE deleted_at IS NULL")
        total_facts = (await cursor.fetchone())["cnt"]
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM fact_relations")
        total_relations = (await cursor.fetchone())["cnt"]

        return GraphResponse(
            nodes=nodes,
            edges=edges,
            total_facts=total_facts,
            total_relations=total_relations,
        )


@router.get("/relations", response_model=list[RelationResponse])
async def list_relations(
    request: Request,
    fact_id: Optional[str] = Query(None),
    relation_type: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> list[RelationResponse]:
    """Llista les relacions entre fets, opcionalment filtrades."""
    agent: dict[str, Any] = request.state.agent
    _check_read_permission(agent)

    async with get_db() as db:
        sql = "SELECT id, source_fact_id, target_fact_id, relation_type, relation_strength, discovered_by, created_at FROM fact_relations WHERE 1=1"
        params: list[Any] = []

        if fact_id:
            sql += " AND (source_fact_id = ? OR target_fact_id = ?)"
            params.extend([fact_id, fact_id])
        if relation_type:
            sql += " AND relation_type = ?"
            params.append(relation_type)

        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        return [RelationResponse(**dict(r)) for r in rows]


@router.post("/relate", status_code=201, response_model=RelationResponse)
async def create_relation(request: Request, body: CreateRelationRequest) -> RelationResponse:
    """Crea una relació manual entre dos fets."""
    agent: dict[str, Any] = request.state.agent
    if not agent.get("permissions", {}).get("write", False):
        raise HTTPException(status_code=403, detail="Sense permís d'escriptura")

    async with get_db() as db:
        # Verify both facts exist
        cursor = await db.execute(
            "SELECT id FROM facts WHERE id IN (?, ?) AND deleted_at IS NULL",
            (body.source_fact_id, body.target_fact_id),
        )
        existing = await cursor.fetchall()
        if len(existing) != 2:
            raise HTTPException(status_code=404, detail="Un o ambdós fets no existeixen")

        # Create relation
        cursor = await db.execute(
            """INSERT INTO fact_relations (source_fact_id, target_fact_id, relation_type, relation_strength, discovered_by)
               VALUES (?, ?, ?, ?, 'manual')""",
            (body.source_fact_id, body.target_fact_id, body.relation_type, body.relation_strength),
        )
        await db.commit()

        # Get created
        cursor = await db.execute(
            "SELECT id, source_fact_id, target_fact_id, relation_type, relation_strength, discovered_by, created_at FROM fact_relations WHERE rowid = ?",
            (cursor.lastrowid,),
        )
        row = await cursor.fetchone()
        return RelationResponse(**dict(row))


@router.delete("/relations/{relation_id}", status_code=204)
async def delete_relation(request: Request, relation_id: str) -> None:
    """Elimina una relació."""
    agent: dict[str, Any] = request.state.agent
    if not agent.get("permissions", {}).get("delete", False):
        raise HTTPException(status_code=403, detail="Sense permís")

    async with get_db() as db:
        await db.execute("DELETE FROM fact_relations WHERE id = ?", (relation_id,))
        await db.commit()
