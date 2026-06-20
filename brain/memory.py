"""Router principal amb tots els endpoints de /v1/memory/.

Conté la lògica de negoci per crear, consultar, actualitzar i eliminar fets,
així com la cerca per text (FTS5) i la cerca semàntica.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import numpy as np
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from starlette.responses import JSONResponse

from brain.audit import log_audit
from brain.config import settings
from brain.db import get_db
from brain.webhooks import trigger_fact_created_webhooks
from brain.embedding import embedding_service
from brain.models import (
    AuditEntry,
    FactResponse,
    LsResponse,
    QueryParams,
    SearchRequest,
    SearchResponse,
    SearchResult,
    SemanticSearchRequest,
    SemanticSearchResponse,
    SemanticSearchResult,
    UpdateRequest,
    WriteRequest,
    WriteResponse,
)

router = APIRouter(prefix="/v1/memory", tags=["memory"])


def _check_permission(
    agent: dict[str, Any],
    permission: str,
    scope: Optional[str] = None,
) -> None:
    """Comprova si l'agent té un permís específic i, opcionalment, un àmbit.

    Args:
        agent: Dict de l'agent (request.state.agent).
        permission: Nom del permís a comprovar (read, write, delete, admin).
        scope: Àmbit a verificar (opcional).

    Raises:
        HTTPException 403 si no té permís.
    """
    perms = agent.get("permissions", {})
    if not perms.get(permission, False):
        raise HTTPException(
            status_code=403,
            detail=f"L'agent no té permís '{permission}'",
        )
    if scope is not None:
        allowed = agent.get("allowed_scopes", ["shared"])
        if scope not in allowed:
            raise HTTPException(
                status_code=403,
                detail=f"Àmbit '{scope}' no permès per a aquest agent",
            )


async def _generate_embeddings_background(
    fact_id: str,
    chunks: list[str],
) -> None:
    """Tasca de fons per generar embeddings per als fragments d'un fet.

    S'executa en un threadpool per no bloquejar el loop d'events.
    """
    import asyncio
    import aiosqlite

    try:
        for chunk_text in chunks:
            # Genera l'embedding amb prefix "passage: "
            vec = await asyncio.to_thread(
                embedding_service.get_embedding,
                chunk_text,
                "passage: ",
            )
            blob = vec.astype(np.float32).tobytes()

            db_path = settings.DB_PATH
            async with aiosqlite.connect(str(db_path)) as db:
                await db.execute(
                    "INSERT INTO chunks (fact_id, chunk_text, embedding_blob) VALUES (?, ?, ?)",
                    (fact_id, chunk_text, blob),
                )
                await db.commit()
    except Exception:
        # Si falla l'embedding, simplement no guardem els vectors
        # El fet s'ha creat correctament, només perdem la cerca semàntica
        pass


from datetime import datetime
@router.post("/write", status_code=201, response_model=WriteResponse)
async def write_memory(
    request: Request,
    body: WriteRequest,
    background_tasks: BackgroundTasks,
) -> WriteResponse:
    """Crea un nou fet a la memòria compartida.

    Si el contingut supera els 500 caràcters, es divideix en fragments
    amb solapament de 50 caràcters. Els embeddings es generen en segon pla.
    """
    agent: dict[str, Any] = request.state.agent
    _check_permission(agent, "write", body.scope)

    async with get_db() as db:
        cursor = await db.execute(
            """
            INSERT INTO facts (scope, category, agent_id, key, content, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (body.scope, body.category or "events", agent["id"], body.key, body.content, json.dumps(body.metadata)),
        )
        # Obtenim l'UUID generat pel DEFAULT de la columna id
        rowid = cursor.lastrowid
        fact_id = ""
        if rowid:
            cursor2 = await db.execute(
                "SELECT id FROM facts WHERE rowid = ?", (rowid,)
            )
            row = await cursor2.fetchone()
            fact_id = row["id"] if row else ""

        # Divideix en fragments
        chunks = embedding_service.split_into_chunks(body.content)
        num_chunks = len(chunks)

        # Insereix fragments sense embedding (la tasca de fons els omplirà)
        for chunk_text in chunks:
            # Guardem un BLOB buit temporalment
            empty_blob = b"\x00" * (settings.EMBED_DIM * 4)
            await db.execute(
                "INSERT INTO chunks (fact_id, chunk_text, embedding_blob) VALUES (?, ?, ?)",
                (fact_id, chunk_text, empty_blob),
            )

        await db.commit()

        # Programa la generació d'embeddings en segon pla
        background_tasks.add_task(
            _generate_embeddings_background,
            fact_id,
            chunks,
        )

        # Auditoria
        await log_audit(
            db, agent["id"], "CREATE", "fact",
            resource_id=fact_id,
            payload=json.dumps({"scope": body.scope, "content_length": len(body.content)}),
        )
        await db.commit()

        # Dispara webhooks de fets nous
        await trigger_fact_created_webhooks(
            background_tasks=background_tasks,
            fact_id=fact_id,
            content=body.content,
            scope=body.scope,
            category=body.category or "events",
            agent_id=agent["id"],
            timestamp=datetime.now().isoformat(),
        )
    return WriteResponse(
        fact_id=fact_id,
        message="Fet creat correctament",
        chunks_generated=num_chunks,
    )


@router.get("/query", response_model=list[FactResponse])
async def query_memory(
    request: Request,
    params: QueryParams = Depends(),
) -> list[FactResponse]:
    """Cerca fets per text usant FTS5.

    Aquest endpoint està sempre disponible i no requereix embeddings.
    """
    agent: dict[str, Any] = request.state.agent
    _check_permission(agent, "read")

    async with get_db() as db:
        # Escapem la query FTS5: dividim en paraules i afegim *
        terms = params.q.strip().split()
        fts_query = " OR ".join(f'"{t}"*' for t in terms if t)
        if not fts_query:
            return []

        sql = """
            SELECT f.id, f.scope, f.category, f.agent_id, f.key, f.content,
                   f.metadata, f.version, f.created_at, f.updated_at
            FROM facts f
            JOIN facts_fts fts ON f.id = fts.fact_id
            WHERE facts_fts MATCH ?
              AND f.deleted_at IS NULL
              AND f.scope = ?
        """
        bind_params: list[Any] = [fts_query, params.scope]

        if params.category:
            sql += " AND f.category = ?"
            bind_params.append(params.category)

        if params.agent_id:
            sql += " AND f.agent_id = ?"
            bind_params.append(params.agent_id)

        sql += " ORDER BY f.updated_at DESC LIMIT ?"
        bind_params.append(params.limit)

        cursor = await db.execute(sql, bind_params)
        rows = await cursor.fetchall()

        results = []
        for row in rows:
            row_dict = dict(row)
            try:
                metadata = json.loads(row_dict["metadata"]) if isinstance(row_dict["metadata"], str) else row_dict["metadata"]
            except (json.JSONDecodeError, TypeError):
                metadata = {}
            results.append(FactResponse(
                id=row_dict["id"],
                scope=row_dict["scope"],
                category=row_dict.get("category", ""),
                agent_id=row_dict["agent_id"],
                key=row_dict["key"],
                content=row_dict["content"],
                metadata=metadata,
                version=row_dict["version"],
                created_at=row_dict["created_at"],
                updated_at=row_dict["updated_at"],
            ))

        # Auditoria
        await log_audit(
            db, agent["id"], "READ", "fact",
            payload=json.dumps({"query": params.q, "results": len(results)}),
        )
        await db.commit()

    return results


@router.post("/search/semantic", response_model=SemanticSearchResponse)
async def semantic_search(
    request: Request,
    body: SemanticSearchRequest,
) -> SemanticSearchResponse:
    """Cerca semàntica per similitud cosinus usant embeddings.

    Si el model d'embeddings falla, fa fallback a FTS5.
    """
    agent: dict[str, Any] = request.state.agent
    _check_permission(agent, "read")

    semantic_fallback = False

    try:
        # Genera embedding de la consulta
        query_vec = embedding_service.get_embedding(body.query, "query: ")

        async with get_db() as db:
            # Obté tots els fragments actius
            sql = """
                SELECT c.id, c.fact_id, c.chunk_text, c.embedding_blob
                FROM chunks c
                JOIN facts f ON c.fact_id = f.id
                WHERE f.deleted_at IS NULL
                  AND f.scope = ?
            """
            bind_params: list[Any] = [body.scope]
            if body.category:
                sql += " AND f.category = ?"
                bind_params.append(body.category)
            if body.agent_id:
                sql += " AND f.agent_id = ?"
                bind_params.append(body.agent_id)

            cursor = await db.execute(sql, bind_params)
            rows = await cursor.fetchall()

            # Carrega vectors
            chunks_with_vecs: list[tuple[str, np.ndarray, str, str]] = []
            for row in rows:
                blob = row["embedding_blob"]
                if blob is None or len(blob) < settings.EMBED_DIM * 4:
                    continue
                vec = np.frombuffer(blob, dtype=np.float32)
                if len(vec) != settings.EMBED_DIM:
                    continue
                # Normalitza per si de cas
                vec = embedding_service._normalize(vec)
                chunks_with_vecs.append((row["id"], vec, row["fact_id"], row["chunk_text"]))

            if not chunks_with_vecs:
                return SemanticSearchResponse(
                    results=[],
                    query=body.query,
                    top_k=body.top_k,
                )

            # Cerca semàntica
            chunk_ids_to_score = [(c[0], c[1]) for c in chunks_with_vecs]
            scored = embedding_service.semantic_search(query_vec, chunk_ids_to_score, body.top_k)

            # Mapa chunk_id -> (fact_id, chunk_text)
            chunk_map = {c[0]: (c[2], c[3]) for c in chunks_with_vecs}

            # Obté informació completa dels facts
            fact_ids = list(set(c[2] for c in chunks_with_vecs))
            fact_info: dict[str, dict] = {}
            for fid in fact_ids:
                cursor2 = await db.execute(
                    "SELECT id, scope, agent_id, key, content, metadata FROM facts WHERE id = ?",
                    (fid,),
                )
                frow = await cursor2.fetchone()
                if frow:
                    fdict = dict(frow)
                    try:
                        fdict["metadata"] = json.loads(fdict["metadata"]) if isinstance(fdict["metadata"], str) else fdict["metadata"]
                    except (json.JSONDecodeError, TypeError):
                        fdict["metadata"] = {}
                    fact_info[fid] = fdict

            results: list[SemanticSearchResult] = []
            for chunk_id, score in scored:
                if chunk_id not in chunk_map:
                    continue
                fact_id, chunk_text = chunk_map[chunk_id]
                finfo = fact_info.get(fact_id, {})
                results.append(SemanticSearchResult(
                    fact_id=fact_id,
                    content=chunk_text,
                    scope=finfo.get("scope", body.scope),
                    category=finfo.get("category", ""),
                    agent_id=finfo.get("agent_id"),
                    key=finfo.get("key"),
                    metadata=finfo.get("metadata", {}),
                    score=round(score, 4),
                ))

        # Auditoria
        async with get_db() as db:
            await log_audit(
                db, agent["id"], "SEARCH", "fact",
                payload=json.dumps({"query": body.query, "results": len(results), "semantic": True}),
            )
            await db.commit()

        return SemanticSearchResponse(
            results=results[:body.top_k],
            query=body.query,
            top_k=body.top_k,
            semantic_fallback=False,
        )

    except Exception as exc:
        # Fallback a FTS5 si falla l'embedding
        semantic_fallback = True

        async with get_db() as db:
            terms = body.query.strip().split()
            fts_query = " OR ".join(f'"{t}"*' for t in terms if t)
            if not fts_query:
                return SemanticSearchResponse(
                    results=[],
                    query=body.query,
                    top_k=body.top_k,
                    semantic_fallback=True,
                )

            sql = """
                SELECT f.id, f.scope, f.category, f.agent_id, f.key, f.content,
                       f.metadata, f.version, f.created_at, f.updated_at
                FROM facts f
                JOIN facts_fts fts ON f.id = fts.fact_id
                WHERE facts_fts MATCH ?
                  AND f.deleted_at IS NULL
                  AND f.scope = ?
            """
            bind_params = [fts_query, body.scope]
            if body.category:
                sql += " AND f.category = ?"
                bind_params.append(body.category)
            if body.agent_id:
                sql += " AND f.agent_id = ?"
                bind_params.append(body.agent_id)
            sql += " ORDER BY f.updated_at DESC LIMIT ?"
            bind_params.append(body.top_k)

            cursor = await db.execute(sql, bind_params)
            rows = await cursor.fetchall()

            results = []
            for row in rows:
                row_dict = dict(row)
                try:
                    metadata = json.loads(row_dict["metadata"]) if isinstance(row_dict["metadata"], str) else row_dict["metadata"]
                except (json.JSONDecodeError, TypeError):
                    metadata = {}
                results.append(SemanticSearchResult(
                    fact_id=row_dict["id"],
                    content=row_dict["content"],
                    scope=row_dict["scope"],
                    category=row_dict.get("category", ""),
                    agent_id=row_dict["agent_id"],
                    key=row_dict["key"],
                    metadata=metadata,
                    score=0.0,
                ))

            await log_audit(
                db, agent["id"], "SEARCH", "fact",
                payload=json.dumps({"query": body.query, "results": len(results), "semantic": False, "fallback_reason": str(exc)}),
            )
            await db.commit()

        return SemanticSearchResponse(
            results=results,
            query=body.query,
            top_k=body.top_k,
            semantic_fallback=True,
        )


@router.get("/search", response_model=SearchResponse)
async def search_memory(
    request: Request,
    params: SearchRequest = Depends(),
) -> SearchResponse:
    """Cerca combinada: FTS5 (semantic=false) o semàntica (semantic=true).

    - semantic=false → cerca per text complet (FTS5), com /v1/memory/query
    - semantic=true → embed query + cerca per cosinus a chunks
    """
    agent: dict[str, Any] = request.state.agent
    _check_permission(agent, "read")

    if not params.semantic:
        # --- FTS5 search ---
        async with get_db() as db:
            terms = params.q.strip().split()
            fts_query = " OR ".join(f'"{t}"*' for t in terms if t)
            if not fts_query:
                return SearchResponse(results=[], query=params.q, total=0, semantic_used=False)

            sql = """
                SELECT f.id, f.scope, f.category, f.agent_id, f.key, f.content,
                       f.metadata, f.version, f.created_at, f.updated_at
                FROM facts f
                JOIN facts_fts fts ON f.id = fts.fact_id
                WHERE facts_fts MATCH ?
                  AND f.deleted_at IS NULL
                  AND f.scope = ?
            """
            bind_params: list[Any] = [fts_query, params.scope]

            if params.category:
                sql += " AND f.category = ?"
                bind_params.append(params.category)

            if params.agent_id:
                sql += " AND f.agent_id = ?"
                bind_params.append(params.agent_id)

            sql += " ORDER BY f.updated_at DESC LIMIT ?"
            bind_params.append(params.limit)

            cursor = await db.execute(sql, bind_params)
            rows = await cursor.fetchall()

            results = []
            for row in rows:
                row_dict = dict(row)
                try:
                    metadata = json.loads(row_dict["metadata"]) if isinstance(row_dict["metadata"], str) else row_dict["metadata"]
                except (json.JSONDecodeError, TypeError):
                    metadata = {}
                results.append(SearchResult(
                    fact_id=row_dict["id"],
                    content=row_dict["content"],
                    scope=row_dict["scope"],
                    category=row_dict.get("category", ""),
                    agent_id=row_dict["agent_id"],
                    key=row_dict["key"],
                    metadata=metadata,
                    score=1.0,
                    match_type="fts5",
                ))

            await log_audit(
                db, agent["id"], "SEARCH", "fact",
                payload=json.dumps({"query": params.q, "results": len(results), "semantic": False}),
            )
            await db.commit()

        return SearchResponse(results=results, query=params.q, total=len(results), semantic_used=False)

    # --- Semantic search ---
    try:
        query_vec = embedding_service.get_embedding(params.q)
    except Exception as exc:
        # Fallback a FTS5
        async with get_db() as db:
            terms = params.q.strip().split()
            fts_query = " OR ".join(f'"{t}"*' for t in terms if t)
            if not fts_query:
                return SearchResponse(results=[], query=params.q, total=0, semantic_used=False)

            sql = """
                SELECT f.id, f.scope, f.agent_id, f.key, f.content,
                       f.metadata, f.version, f.created_at, f.updated_at
                FROM facts f
                JOIN facts_fts fts ON f.id = fts.fact_id
                WHERE facts_fts MATCH ?
                  AND f.deleted_at IS NULL
                  AND f.scope = ?
            """
            bind_params = [fts_query, params.scope]
            if params.agent_id:
                sql += " AND f.agent_id = ?"
                bind_params.append(params.agent_id)
            sql += " ORDER BY f.updated_at DESC LIMIT ?"
            bind_params.append(params.limit)

            cursor = await db.execute(sql, bind_params)
            rows = await cursor.fetchall()

            results = []
            for row in rows:
                row_dict = dict(row)
                try:
                    metadata = json.loads(row_dict["metadata"]) if isinstance(row_dict["metadata"], str) else row_dict["metadata"]
                except (json.JSONDecodeError, TypeError):
                    metadata = {}
                results.append(SearchResult(
                    fact_id=row_dict["id"],
                    content=row_dict["content"],
                    scope=row_dict["scope"],
                    agent_id=row_dict["agent_id"],
                    key=row_dict["key"],
                    metadata=metadata,
                    score=0.0,
                    match_type="fts5",
                    snippet=row_dict["content"][:200] if row_dict["content"] else "",
                ))

            await log_audit(
                db, agent["id"], "SEARCH", "fact",
                payload=json.dumps({"query": params.q, "results": len(results), "semantic": True, "fallback": True, "error": str(exc)}),
            )
            await db.commit()

        return SearchResponse(results=results, query=params.q, total=len(results), semantic_used=False)

    # Cerca semàntica real
    async with get_db() as db:
        sql = """
            SELECT c.id, c.fact_id, c.chunk_text, c.embedding_blob
            FROM chunks c
            JOIN facts f ON c.fact_id = f.id
            WHERE f.deleted_at IS NULL
              AND f.scope = ?
        """
        bind_params = [params.scope]
        if params.agent_id:
            sql += " AND f.agent_id = ?"
            bind_params.append(params.agent_id)

        cursor = await db.execute(sql, bind_params)
        rows = await cursor.fetchall()

        chunks_with_vecs: list[tuple[str, np.ndarray, str, str]] = []
        for row in rows:
            blob = row["embedding_blob"]
            if blob is None or len(blob) < settings.EMBED_DIM * 4:
                continue
            vec = np.frombuffer(blob, dtype=np.float32)
            if len(vec) != settings.EMBED_DIM:
                continue
            vec = embedding_service._normalize(vec)
            chunks_with_vecs.append((row["id"], vec, row["fact_id"], row["chunk_text"]))

        if not chunks_with_vecs:
            return SearchResponse(results=[], query=params.q, total=0, semantic_used=True)

        scored = embedding_service.semantic_search(
            query_vec,
            [(c[0], c[1]) for c in chunks_with_vecs],
            params.limit,
        )

        chunk_map = {c[0]: (c[2], c[3]) for c in chunks_with_vecs}
        fact_ids = list(set(c[2] for c in chunks_with_vecs))

        fact_info: dict[str, dict] = {}
        for fid in fact_ids:
            cursor2 = await db.execute(
                "SELECT id, scope, agent_id, key, content, metadata FROM facts WHERE id = ?",
                (fid,),
            )
            frow = await cursor2.fetchone()
            if frow:
                fdict = dict(frow)
                try:
                    fdict["metadata"] = json.loads(fdict["metadata"]) if isinstance(fdict["metadata"], str) else fdict["metadata"]
                except (json.JSONDecodeError, TypeError):
                    fdict["metadata"] = {}
                fact_info[fid] = fdict

        results = []
        for chunk_id, score in scored:
            if chunk_id not in chunk_map:
                continue
            fact_id, chunk_text = chunk_map[chunk_id]
            finfo = fact_info.get(fact_id, {})
            results.append(SearchResult(
                fact_id=fact_id,
                content=chunk_text,
                scope=finfo.get("scope", params.scope),
                agent_id=finfo.get("agent_id"),
                key=finfo.get("key"),
                metadata=finfo.get("metadata", {}),
                score=round(score, 4),
                match_type="semantic",
                snippet=chunk_text[:200] if chunk_text else "",
            ))

        await log_audit(
            db, agent["id"], "SEARCH", "fact",
            payload=json.dumps({"query": params.q, "results": len(results), "semantic": True}),
        )
        await db.commit()

    return SearchResponse(results=results, query=params.q, total=len(results), semantic_used=True)


@router.put("/{fact_id}", response_model=WriteResponse)
async def update_memory(
    request: Request,
    fact_id: str,
    body: UpdateRequest,
    background_tasks: BackgroundTasks,
) -> WriteResponse:
    """Actualitza un fet existent.

    Incrementa la versió, esborra els fragments antics i en genera de nous.
    Els nous embeddings es generen en segon pla.
    """
    agent: dict[str, Any] = request.state.agent
    _check_permission(agent, "write")

    async with get_db() as db:
        # Comprova que el fet existeix i no està eliminat
        cursor = await db.execute(
            "SELECT id, content, metadata FROM facts WHERE id = ? AND deleted_at IS NULL",
            (fact_id,),
        )
        existing = await cursor.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Fet no trobat")

        existing_dict = dict(existing)
        new_metadata = body.metadata
        if new_metadata is None:
            try:
                new_metadata = json.loads(existing_dict["metadata"]) if isinstance(existing_dict["metadata"], str) else existing_dict["metadata"]
            except (json.JSONDecodeError, TypeError):
                new_metadata = {}

        # Actualitza el fet
        if body.category is not None:
            await db.execute(
                """
                UPDATE facts
                SET content = ?, metadata = ?, category = ?, version = version + 1, updated_at = datetime('now')
                WHERE id = ? AND deleted_at IS NULL
                """,
                (body.content, json.dumps(new_metadata), body.category, fact_id),
            )
        else:
            await db.execute(
                """
                UPDATE facts
                SET content = ?, metadata = ?, version = version + 1, updated_at = datetime('now')
                WHERE id = ? AND deleted_at IS NULL
                """,
                (body.content, json.dumps(new_metadata), fact_id),
            )

        # Esborra fragments antics (hard delete)
        await db.execute("DELETE FROM chunks WHERE fact_id = ?", (fact_id,))

        # Divideix en fragments nous
        chunks = embedding_service.split_into_chunks(body.content)

        # Insereix fragments nous (sense embedding encara)
        for chunk_text in chunks:
            empty_blob = b"\x00" * (settings.EMBED_DIM * 4)
            await db.execute(
                "INSERT INTO chunks (fact_id, chunk_text, embedding_blob) VALUES (?, ?, ?)",
                (fact_id, chunk_text, empty_blob),
            )

        await db.commit()

        # Programa la generació d'embeddings en segon pla
        background_tasks.add_task(
            _generate_embeddings_background,
            fact_id,
            chunks,
        )

        # Auditoria
        await log_audit(
            db, agent["id"], "UPDATE", "fact",
            resource_id=fact_id,
            payload=json.dumps({"content_length": len(body.content)}),
        )
        await db.commit()

    return WriteResponse(
        fact_id=fact_id,
        message="Fet actualitzat correctament",
        chunks_generated=len(chunks),
    )


@router.delete("/{fact_id}", status_code=204)
async def delete_memory(
    request: Request,
    fact_id: str,
) -> JSONResponse:
    """Elimina un fet (soft delete).

    Marca deleted_at a la taula facts i esborra físicament els fragments.
    """
    agent: dict[str, Any] = request.state.agent
    _check_permission(agent, "delete")

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id FROM facts WHERE id = ? AND deleted_at IS NULL",
            (fact_id,),
        )
        existing = await cursor.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Fet no trobat")

        # Soft delete
        await db.execute(
            "UPDATE facts SET deleted_at = datetime('now') WHERE id = ?",
            (fact_id,),
        )

        # Hard delete dels chunks
        await db.execute("DELETE FROM chunks WHERE fact_id = ?", (fact_id,))

        # Auditoria
        await log_audit(
            db, agent["id"], "DELETE", "fact",
            resource_id=fact_id,
        )
        await db.commit()

    return JSONResponse(status_code=204, content=None)


@router.get("/ls", response_model=LsResponse)
async def ls_memory(
    request: Request,
    scope: str = "shared",
    category: str = "",
    agent_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> LsResponse:
    """Llista fets com un ls de directori. Filtra per scope, categoria i agent."""
    agent: dict[str, Any] = request.state.agent
    _check_permission(agent, "read")

    async with get_db() as db:
        where = ["f.deleted_at IS NULL"]
        bind: list[Any] = []

        if scope:
            where.append("f.scope = ?")
            bind.append(scope)
        if category:
            where.append("f.category = ?")
            bind.append(category)
        if agent_id:
            where.append("f.agent_id = ?")
            bind.append(agent_id)

        where_clause = " AND ".join(where) if where else "1=1"

        # Count total
        cursor = await db.execute(
            f"SELECT COUNT(*) as cnt FROM facts f WHERE {where_clause}", bind
        )
        total = (await cursor.fetchone())["cnt"]

        # Fetch items
        cursor = await db.execute(
            f"""SELECT f.id, f.scope, f.category, f.agent_id, f.key,
                       substr(f.content, 1, 120) as content_preview,
                       f.version, f.created_at, f.updated_at
                FROM facts f
                WHERE {where_clause}
                ORDER BY f.updated_at DESC
                LIMIT ? OFFSET ?""",
            [*bind, limit, offset],
        )
        rows = await cursor.fetchall()

    items = [dict(r) for r in rows]
    return LsResponse(items=items, total=total, scope=scope, filters={
        "category": category, "agent_id": agent_id,
    })


@router.get("/audit", response_model=list[AuditEntry])
async def get_audit_log(
    request: Request,
    agent_id: Optional[str] = None,
    action: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Retorna el registre d'auditoria. Només per agents amb admin=true."""
    agent: dict[str, Any] = request.state.agent
    _check_permission(agent, "admin")

    async with get_db() as db:
        sql = "SELECT id, agent_id, action, resource_type, resource_id, payload, timestamp FROM audit_log WHERE 1=1"
        bind_params: list[Any] = []

        if agent_id:
            sql += " AND agent_id = ?"
            bind_params.append(agent_id)
        if action:
            sql += " AND action = ?"
            bind_params.append(action)

        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        bind_params.append(limit)
        bind_params.append(offset)

        cursor = await db.execute(sql, bind_params)
        rows = await cursor.fetchall()

    return [dict(row) for row in rows]
