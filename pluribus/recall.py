"""Unified, scope-safe memory recall for Pluribus.

Recall v2 combines lexical FTS5 and semantic retrieval, ranks complete facts rather
than individual chunks, and applies authorization inside the service itself so
internal/MCP callers cannot bypass scope restrictions accidentally.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

import numpy as np
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from pluribus.audit import log_audit
from pluribus.config import settings
from pluribus.db import get_db
from pluribus.embedding import embedding_service
from pluribus.validation import validate_category, validate_query, validate_scope

router = APIRouter(prefix="/v1/memory", tags=["recall"])


class RecallRequest(BaseModel):
    query: str
    scope: str | None = None
    category: str | None = None
    limit: int = Field(default=10, ge=1, le=50)

    _query = field_validator("query")(validate_query)

    @field_validator("scope")
    @classmethod
    def validate_optional_scope(cls, value: str | None) -> str | None:
        return None if value is None else validate_scope(value)

    @field_validator("category")
    @classmethod
    def validate_optional_category(cls, value: str | None) -> str | None:
        if value in {None, ""}:
            return None
        return validate_category(value)


class RecallResult(BaseModel):
    fact_id: str
    scope: str
    category: str = ""
    agent_id: str | None = None
    key: str | None = None
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    version: int = 1
    created_at: str
    updated_at: str
    score: float
    match_type: str
    snippet: str = ""
    signals: dict[str, Any] = Field(default_factory=dict)


class RecallResponse(BaseModel):
    query: str
    scopes: list[str]
    category: str | None = None
    results: list[RecallResult]
    total: int
    semantic_available: bool


def _permissions(agent: dict[str, Any]) -> dict[str, Any]:
    value = agent.get("permissions", {}) or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = {}
    return value if isinstance(value, dict) else {}


def _allowed_scopes(agent: dict[str, Any]) -> list[str]:
    value = agent.get("allowed_scopes", []) or []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = []
    if not isinstance(value, list):
        return []
    scopes: list[str] = []
    for item in value:
        try:
            normalized = validate_scope(item)
        except (TypeError, ValueError):
            continue
        if normalized not in scopes:
            scopes.append(normalized)
    return scopes


async def _resolve_scopes(agent: dict[str, Any], requested_scope: str | None) -> list[str]:
    """Resolve scopes and enforce read authorization inside the recall service."""
    if not agent or not agent.get("id"):
        raise HTTPException(status_code=401, detail="Autenticació requerida")

    perms = _permissions(agent)
    is_admin = bool(perms.get("admin", False))
    if not is_admin and not perms.get("read", False):
        raise HTTPException(status_code=403, detail="L'agent no té permís 'read'")

    if requested_scope is not None:
        scope = validate_scope(requested_scope)
        if not is_admin and scope not in _allowed_scopes(agent):
            raise HTTPException(
                status_code=403,
                detail=f"Àmbit '{scope}' no permès per a aquest agent",
            )
        return [scope]

    if not is_admin:
        scopes = _allowed_scopes(agent)
        if not scopes:
            raise HTTPException(status_code=403, detail="L'agent no té scopes de lectura")
        return scopes

    # Admin recall without an explicit scope may inspect every active scope.
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT DISTINCT scope FROM facts WHERE deleted_at IS NULL ORDER BY scope"
        )
        rows = await cursor.fetchall()
    return [row["scope"] for row in rows if row["scope"]]


def _fts_prefix_query(text: str) -> str:
    terms: list[str] = []
    for token in text.strip().split():
        escaped = token.replace('"', '""')
        if escaped:
            terms.append(f'"{escaped}"*')
    return " OR ".join(terms)


def _metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _bounded_number(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return max(0.0, min(1.0, number))


def _recency_score(updated_at: str) -> float:
    try:
        parsed = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age_days = max(
            0.0,
            (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()
            / 86400.0,
        )
        return 1.0 / (1.0 + age_days / 30.0)
    except (TypeError, ValueError):
        return 0.0


def _rank_score(rank: int | None) -> float:
    if rank is None:
        return 0.0
    # Reciprocal-rank style score normalized so rank 1 == 1.0.
    return 61.0 / (60.0 + float(rank))


def _scope_clause(scopes: list[str]) -> tuple[str, list[str]]:
    if not scopes:
        return "1=0", []
    placeholders = ",".join("?" for _ in scopes)
    return f"f.scope IN ({placeholders})", list(scopes)


async def _fts_candidates(
    query: str,
    scopes: list[str],
    category: str | None,
    candidate_limit: int,
) -> dict[str, int]:
    fts_query = _fts_prefix_query(query)
    if not fts_query:
        return {}

    scope_sql, params = _scope_clause(scopes)
    sql = f"""
        SELECT f.id
        FROM facts f
        JOIN facts_fts fts ON f.id = fts.fact_id
        WHERE facts_fts MATCH ?
          AND f.deleted_at IS NULL
          AND {scope_sql}
    """
    bind: list[Any] = [fts_query, *params]
    if category:
        sql += " AND f.category = ?"
        bind.append(category)
    sql += " ORDER BY bm25(facts_fts), f.updated_at DESC LIMIT ?"
    bind.append(candidate_limit)

    async with get_db() as db:
        cursor = await db.execute(sql, bind)
        rows = await cursor.fetchall()
    return {row["id"]: rank for rank, row in enumerate(rows, start=1)}


async def _semantic_candidates(
    query: str,
    scopes: list[str],
    category: str | None,
    candidate_limit: int,
) -> tuple[dict[str, tuple[int, float, str]], bool]:
    try:
        query_vec = await embedding_service.get_embedding_async(query, "query: ")
    except Exception:
        return {}, False

    if (
        query_vec.ndim != 1
        or len(query_vec) != settings.EMBED_DIM
        or not np.all(np.isfinite(query_vec))
        or float(np.linalg.norm(query_vec)) <= 0
    ):
        return {}, False

    scope_sql, params = _scope_clause(scopes)
    sql = f"""
        SELECT c.id AS chunk_id, c.fact_id, c.chunk_text, c.embedding_blob
        FROM chunks c
        JOIN facts f ON c.fact_id = f.id
        WHERE f.deleted_at IS NULL
          AND {scope_sql}
          AND c.embedding_blob IS NOT NULL
          AND length(c.embedding_blob) = ?
    """
    bind: list[Any] = [*params, settings.EMBED_DIM * 4]
    if category:
        sql += " AND f.category = ?"
        bind.append(category)

    async with get_db() as db:
        cursor = await db.execute(sql, bind)
        rows = await cursor.fetchall()

    vectors: list[tuple[str, np.ndarray]] = []
    chunk_info: dict[str, tuple[str, str]] = {}
    for row in rows:
        vec = np.frombuffer(row["embedding_blob"], dtype=np.float32)
        if (
            len(vec) != settings.EMBED_DIM
            or not np.all(np.isfinite(vec))
            or float(np.linalg.norm(vec)) <= 0
        ):
            continue
        chunk_id = row["chunk_id"]
        vectors.append((chunk_id, embedding_service._normalize(vec)))
        chunk_info[chunk_id] = (row["fact_id"], row["chunk_text"])

    if not vectors:
        return {}, False

    scored = embedding_service.semantic_search(
        query_vec,
        vectors,
        min(max(candidate_limit * 3, candidate_limit), 500),
    )
    facts: dict[str, tuple[int, float, str]] = {}
    fact_rank = 0
    for chunk_id, similarity in scored:
        info = chunk_info.get(chunk_id)
        if info is None:
            continue
        fact_id, chunk_text = info
        if fact_id in facts:
            continue
        fact_rank += 1
        facts[fact_id] = (fact_rank, float(similarity), chunk_text)
        if fact_rank >= candidate_limit:
            break
    return facts, True


async def _load_facts(fact_ids: set[str]) -> dict[str, dict[str, Any]]:
    if not fact_ids:
        return {}
    placeholders = ",".join("?" for _ in fact_ids)
    async with get_db() as db:
        cursor = await db.execute(
            f"""SELECT id, scope, category, agent_id, key, content, metadata,
                       version, created_at, updated_at
                FROM facts
                WHERE deleted_at IS NULL AND id IN ({placeholders})""",
            list(fact_ids),
        )
        rows = await cursor.fetchall()
    return {row["id"]: dict(row) for row in rows}


async def recall_service(agent: dict[str, Any], body: RecallRequest) -> RecallResponse:
    scopes = await _resolve_scopes(agent, body.scope)
    if not scopes:
        return RecallResponse(
            query=body.query,
            scopes=[],
            category=body.category,
            results=[],
            total=0,
            semantic_available=False,
        )

    candidate_limit = min(200, max(25, body.limit * 5))
    fts_ranks = await _fts_candidates(
        body.query, scopes, body.category, candidate_limit
    )
    semantic, semantic_available = await _semantic_candidates(
        body.query, scopes, body.category, candidate_limit
    )

    fact_ids = set(fts_ranks) | set(semantic)
    facts = await _load_facts(fact_ids)
    ranked: list[RecallResult] = []

    for fact_id, fact in facts.items():
        fts_rank = fts_ranks.get(fact_id)
        semantic_info = semantic.get(fact_id)
        semantic_rank = semantic_info[0] if semantic_info else None
        semantic_similarity = semantic_info[1] if semantic_info else 0.0
        semantic_snippet = semantic_info[2] if semantic_info else ""

        metadata = _metadata(fact.get("metadata"))
        recency = _recency_score(fact.get("updated_at", ""))
        importance = _bounded_number(metadata.get("importance"))
        confidence = _bounded_number(metadata.get("confidence"))
        fts_score = _rank_score(fts_rank)
        semantic_rank_score = _rank_score(semantic_rank)
        semantic_similarity_score = max(0.0, min(1.0, semantic_similarity))
        semantic_score = (
            0.5 * semantic_rank_score + 0.5 * semantic_similarity_score
            if semantic_info
            else 0.0
        )

        score = (
            0.42 * fts_score
            + 0.42 * semantic_score
            + 0.08 * recency
            + 0.04 * importance
            + 0.04 * confidence
        )
        if fts_rank is not None and semantic_info is not None:
            match_type = "hybrid"
        elif semantic_info is not None:
            match_type = "semantic"
        else:
            match_type = "fts5"

        content = fact.get("content") or ""
        snippet = semantic_snippet or content[:240]
        ranked.append(
            RecallResult(
                fact_id=fact_id,
                scope=fact.get("scope", ""),
                category=fact.get("category") or "",
                agent_id=fact.get("agent_id"),
                key=fact.get("key"),
                content=content,
                metadata=metadata,
                version=fact.get("version", 1),
                created_at=fact.get("created_at", ""),
                updated_at=fact.get("updated_at", ""),
                score=round(score, 6),
                match_type=match_type,
                snippet=snippet[:240],
                signals={
                    "fts_rank": fts_rank,
                    "semantic_rank": semantic_rank,
                    "semantic_similarity": round(semantic_similarity, 6)
                    if semantic_info
                    else None,
                    "recency": round(recency, 6),
                    "importance": importance,
                    "confidence": confidence,
                },
            )
        )

    ranked.sort(key=lambda item: (item.score, item.updated_at), reverse=True)
    results = ranked[: body.limit]

    async with get_db() as db:
        await log_audit(
            db,
            agent["id"],
            "RECALL",
            "fact",
            payload=json.dumps(
                {
                    "query": body.query,
                    "scopes": scopes,
                    "category": body.category,
                    "results": len(results),
                    "semantic_available": semantic_available,
                }
            ),
        )
        await db.commit()

    return RecallResponse(
        query=body.query,
        scopes=scopes,
        category=body.category,
        results=results,
        total=len(results),
        semantic_available=semantic_available,
    )


@router.post("/recall", response_model=RecallResponse)
async def recall_memory(request: Request, body: RecallRequest) -> RecallResponse:
    """Recall complete facts across the caller's authorized scopes."""
    agent = getattr(request.state, "agent", None) or {}
    return await recall_service(agent, body)
