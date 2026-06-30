"""
Endpoint de linting (health check) per a la memòria de Pluribus v2.

Genera un report estructurat amb:
- Facts orfes (0 relacions)
- Facts amb metadata incompleta
- Contradiccions actives
- Estadístiques generals
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import JSONResponse

from pluribus.db import get_db

router = APIRouter(prefix="/v1/memory", tags=["lint"])


@router.post("/lint")
async def lint_memory(request: Request) -> JSONResponse:
    """Genera un report de salut de la memòria compartida."""
    agent: dict[str, Any] = request.state.agent
    if not agent.get("permissions", {}).get("read", False):
        raise HTTPException(status_code=403, detail="Sense permís de lectura")

    report: dict[str, Any] = {}

    async with get_db() as db:
        # === Estadístiques generals ===
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM facts WHERE deleted_at IS NULL"
        )
        row = await cursor.fetchone()
        total_active = row["cnt"]

        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM facts WHERE deleted_at IS NOT NULL"
        )
        row = await cursor.fetchone()
        total_deleted = row["cnt"]

        cursor = await db.execute("SELECT COUNT(*) as cnt FROM fact_relations")
        row = await cursor.fetchone()
        total_relations = row["cnt"]

        cursor = await db.execute("SELECT COUNT(*) as cnt FROM chunks")
        row = await cursor.fetchone()
        total_chunks = row["cnt"]

        report["stats"] = {
            "active_facts": total_active,
            "deleted_facts": total_deleted,
            "total_relations": total_relations,
            "total_chunks": total_chunks,
        }

        # === Facts orfes (0 relacions, actius) ===
        # Facts que no apareixen com a source ni target a cap relació
        cursor = await db.execute(
            """SELECT f.id, f.content, f.metadata, f.created_at
               FROM facts f
               WHERE f.deleted_at IS NULL
                 AND f.id NOT IN (
                   SELECT source_fact_id FROM fact_relations
                   UNION
                   SELECT target_fact_id FROM fact_relations
                 )
               ORDER BY f.created_at DESC
               LIMIT 50"""
        )
        orphan_rows = await cursor.fetchall()
        orphan_facts = []
        for r in orphan_rows:
            metadata_str = r["metadata"] if isinstance(r["metadata"], str) else "{}"
            try:
                meta = json.loads(metadata_str)
            except (json.JSONDecodeError, TypeError):
                meta = {}
            orphan_facts.append({
                "id": r["id"],
                "preview": r["content"][:100],
                "type": meta.get("type", "unknown"),
                "topic": meta.get("topic", "unknown"),
                "created_at": r["created_at"],
            })
        report["orphan_facts"] = {
            "count": len(orphan_facts),
            "total_in_db": len(orphan_rows),
            "facts": orphan_facts[:20],  # Top 20
        }

        # === Facts amb metadata incompleta ===
        cursor = await db.execute(
            """SELECT id, content, metadata, created_at
               FROM facts
               WHERE deleted_at IS NULL
               ORDER BY created_at DESC
               LIMIT 200"""
        )
        all_active = await cursor.fetchall()
        incomplete_metadata = []
        for r in all_active:
            metadata_str = r["metadata"] if isinstance(r["metadata"], str) else "{}"
            try:
                meta = json.loads(metadata_str)
            except (json.JSONDecodeError, TypeError):
                meta = {}
            missing = []
            for field in ["type", "topic", "agent"]:
                if field not in meta or not meta[field]:
                    missing.append(field)
            if missing:
                incomplete_metadata.append({
                    "id": r["id"],
                    "preview": r["content"][:80],
                    "missing_fields": missing,
                    "created_at": r["created_at"],
                })

        report["incomplete_metadata"] = {
            "count": len(incomplete_metadata),
            "facts": incomplete_metadata[:20],  # Top 20
        }

        # === Contradiccions actives ===
        cursor = await db.execute(
            """SELECT r.id as relation_id,
                      r.source_fact_id, f1.content as source_content,
                      r.target_fact_id, f2.content as target_content,
                      r.relation_strength, r.discovered_by, r.created_at
               FROM fact_relations r
               JOIN facts f1 ON r.source_fact_id = f1.id AND f1.deleted_at IS NULL
               JOIN facts f2 ON r.target_fact_id = f2.id AND f2.deleted_at IS NULL
               WHERE r.relation_type = 'contradiction'
               ORDER BY r.relation_strength DESC
               LIMIT 50"""
        )
        contradiction_rows = await cursor.fetchall()
        contradictions = []
        for r in contradiction_rows:
            contradictions.append({
                "source_id": r["source_fact_id"],
                "source_preview": r["source_content"][:80],
                "target_id": r["target_fact_id"],
                "target_preview": r["target_content"][:80],
                "strength": r["relation_strength"],
                "discovered_by": r["discovered_by"],
                "created_at": r["created_at"],
            })
        report["contradictions"] = {
            "count": len(contradictions),
            "pairs": contradictions[:20],  # Top 20
        }

        # === Distribució per agent ===
        cursor = await db.execute(
            """SELECT agent_id, COUNT(*) as cnt
               FROM facts
               WHERE deleted_at IS NULL AND agent_id IS NOT NULL
               GROUP BY agent_id
               ORDER BY cnt DESC"""
        )
        agent_rows = await cursor.fetchall()
        by_agent = {r["agent_id"]: r["cnt"] for r in agent_rows}
        report["by_agent"] = by_agent

        # === Distribució per tipus ===
        type_counts: dict[str, int] = {}
        for r in all_active:
            metadata_str = r["metadata"] if isinstance(r["metadata"], str) else "{}"
            try:
                meta = json.loads(metadata_str)
            except (json.JSONDecodeError, TypeError):
                meta = {}
            t = meta.get("type", "unknown")
            type_counts[t] = type_counts.get(t, 0) + 1
        report["by_type"] = dict(
            sorted(type_counts.items(), key=lambda x: -x[1])
        )

    return JSONResponse(report)
