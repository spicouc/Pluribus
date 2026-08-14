"""Async semantic-search routes registered ahead of legacy synchronous routes."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
from fastapi import APIRouter, Depends, Request

from pluribus.audit import log_audit
from pluribus.config import settings
from pluribus.db import get_db
from pluribus.embedding import embedding_service
from pluribus.models import (
    SearchRequest,
    SearchResponse,
    SearchResult,
    SemanticSearchRequest,
    SemanticSearchResponse,
    SemanticSearchResult,
)

router = APIRouter(prefix="/v1/memory", tags=["semantic-search"])


def _fts_prefix_query(text: str) -> str:
    """Build a quoted FTS5 prefix query without exposing FTS syntax."""
    terms = []
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


async def _audit_search(agent_id: str, query: str, result_count: int, semantic: bool, fallback: bool = False) -> None:
    async with get_db() as db:
        await log_audit(
            db,
            agent_id,
            "SEARCH",
            "fact",
            payload=json.dumps(
                {
                    "query": query,
                    "results": result_count,
                    "semantic": semantic,
                    "fallback": fallback,
                }
            ),
        )
        await db.commit()


async def _fts_lookup(
    query: str,
    scope: str,
    category: str = "",
    agent_id: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    fts_query = _fts_prefix_query(query)
    if not fts_query:
        return []

    sql = """
        SELECT f.id, f.scope, f.category, f.agent_id, f.key, f.content, f.metadata
        FROM facts f
        JOIN facts_fts fts ON f.id = fts.fact_id
        WHERE facts_fts MATCH ?
          AND f.deleted_at IS NULL
          AND f.scope = ?
    """
    params: list[Any] = [fts_query, scope]
    if category:
        sql += " AND f.category = ?"
        params.append(category)
    if agent_id:
        sql += " AND f.agent_id = ?"
        params.append(agent_id)
    sql += " ORDER BY f.updated_at DESC LIMIT ?"
    params.append(limit)

    async with get_db() as db:
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()

    return [
        {
            "fact_id": row["id"],
            "content": row["content"],
            "scope": row["scope"],
            "category": row["category"] or "",
            "agent_id": row["agent_id"],
            "key": row["key"],
            "metadata": _metadata(row["metadata"]),
            "score": 0.0,
        }
        for row in rows
    ]


async def semantic_lookup(
    query: str,
    scope: str,
    category: str = "",
    agent_id: str | None = None,
    top_k: int = 5,
) -> tuple[list[dict[str, Any]], bool]:
    """Return semantic results and whether FTS5 fallback was used."""
    try:
        query_vec = await embedding_service.get_embedding_async(query, "query: ")
    except Exception:
        return await _fts_lookup(query, scope, category, agent_id, top_k), True

    if (
        query_vec.ndim != 1
        or len(query_vec) != settings.EMBED_DIM
        or not np.all(np.isfinite(query_vec))
        or float(np.linalg.norm(query_vec)) <= 0
    ):
        return await _fts_lookup(query, scope, category, agent_id, top_k), True

    sql = """
        SELECT c.id AS chunk_id, c.fact_id, c.chunk_text, c.embedding_blob,
               f.scope, f.category, f.agent_id, f.key, f.metadata
        FROM chunks c
        JOIN facts f ON c.fact_id = f.id
        WHERE f.deleted_at IS NULL
          AND f.scope = ?
          AND c.embedding_blob IS NOT NULL
          AND length(c.embedding_blob) = ?
    """
    params: list[Any] = [scope, settings.EMBED_DIM * 4]
    if category:
        sql += " AND f.category = ?"
        params.append(category)
    if agent_id:
        sql += " AND f.agent_id = ?"
        params.append(agent_id)

    async with get_db() as db:
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()

    vectors: list[tuple[str, np.ndarray]] = []
    info: dict[str, dict[str, Any]] = {}
    for row in rows:
        vec = np.frombuffer(row["embedding_blob"], dtype=np.float32)
        if (
            len(vec) != settings.EMBED_DIM
            or not np.all(np.isfinite(vec))
            or float(np.linalg.norm(vec)) <= 0
        ):
            continue
        vec = embedding_service._normalize(vec)
        chunk_id = row["chunk_id"]
        vectors.append((chunk_id, vec))
        info[chunk_id] = {
            "fact_id": row["fact_id"],
            "content": row["chunk_text"],
            "scope": row["scope"],
            "category": row["category"] or "",
            "agent_id": row["agent_id"],
            "key": row["key"],
            "metadata": _metadata(row["metadata"]),
        }

    if not vectors:
        return await _fts_lookup(query, scope, category, agent_id, top_k), True

    scored = embedding_service.semantic_search(query_vec, vectors, top_k)
    if not scored:
        return await _fts_lookup(query, scope, category, agent_id, top_k), True

    results: list[dict[str, Any]] = []
    for chunk_id, score in scored:
        row = info.get(chunk_id)
        if row is None:
            continue
        results.append({**row, "score": round(float(score), 4)})
    return results[:top_k], False


@router.post("/search/semantic", response_model=SemanticSearchResponse)
async def semantic_search_async(
    request: Request,
    body: SemanticSearchRequest,
) -> SemanticSearchResponse:
    agent = request.state.agent
    rows, fallback = await semantic_lookup(
        body.query,
        body.scope,
        body.category,
        body.agent_id,
        body.top_k,
    )
    await _audit_search(agent["id"], body.query, len(rows), semantic=True, fallback=fallback)
    return SemanticSearchResponse(
        results=[SemanticSearchResult(**row) for row in rows],
        query=body.query,
        top_k=body.top_k,
        semantic_fallback=fallback,
    )


@router.get("/search", response_model=SearchResponse)
async def search_memory_async(
    request: Request,
    params: SearchRequest = Depends(),
) -> SearchResponse:
    agent = request.state.agent
    if not params.semantic:
        rows = await _fts_lookup(
            params.q,
            params.scope,
            params.category,
            params.agent_id,
            params.limit,
        )
        results = [
            SearchResult(
                **{**row, "score": 1.0},
                match_type="fts5",
                snippet=row["content"][:200] if row["content"] else "",
            )
            for row in rows
        ]
        await _audit_search(agent["id"], params.q, len(results), semantic=False)
        return SearchResponse(
            results=results,
            query=params.q,
            total=len(results),
            semantic_used=False,
        )

    rows, fallback = await semantic_lookup(
        params.q,
        params.scope,
        params.category,
        params.agent_id,
        params.limit,
    )
    results = [
        SearchResult(
            **row,
            match_type="fts5" if fallback else "semantic",
            snippet=row["content"][:200] if row["content"] else "",
        )
        for row in rows
    ]
    await _audit_search(agent["id"], params.q, len(results), semantic=True, fallback=fallback)
    return SearchResponse(
        results=results,
        query=params.q,
        total=len(results),
        semantic_used=not fallback,
    )
