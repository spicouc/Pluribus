"""
Router MCP (Model Context Protocol) lleuger per Pluribus v2.

Permet que Cursor, Claude Desktop i altres clients MCP descobreixin
i cridin eines de memòria de Pluribus.

Endpoints:
  POST /mcp  → JSON-RPC entry point (tools/list, tools/call)
  GET  /mcp  → Llista d'eines en format llegible
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from pluribus.audit import log_audit
from pluribus.config import settings
from pluribus.db import get_db
from pluribus.embedding import embedding_service

# ── Categories persistents de la flota ─────────────────────────────────
# Els fets d'infraestructura no es poden esborrar via MCP (el camí que usa
# el procés de purga de les 06:00, que actua sense agent autenticat).
_PROTECTED_CATEGORIES = {"system", "config", "entities"}

router = APIRouter(prefix="/mcp", tags=["mcp"])

# ── Definició d'eines ────────────────────────────────────────────────

TOOLS = [
    {
        "name": "memory_write",
        "description": "Escriu un fet a la memòria compartida de Pluribus. Pluribus desa fets, els fragmenta i genera embeddings per cerca semàntica.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Contingut del fet a emmagatzemar"},
                "scope": {"type": "string", "enum": ["shared", "local"], "default": "shared", "description": "Àmbit del fet"},
                "category": {"type": "string", "enum": ["", "profile", "preferences", "entities", "events", "cases", "patterns"], "default": "", "description": "Categoria del fet (estil OpenViking)"},
                "key": {"type": "string", "description": "Clau opcional per identificar el fet", "default": None},
                "metadata": {"type": "object", "description": "Metadades附加", "default": {}},
            },
            "required": ["content"],
        },
    },
    {
        "name": "memory_query",
        "description": "Cerca fets per text complet (FTS5). Retorna fets que coincideixin amb la consulta.",
        "input_schema": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Text de cerca"},
                "scope": {"type": "string", "enum": ["shared", "local"], "default": "shared"},
                "category": {"type": "string", "description": "Filtrar per categoria (buit = totes)", "default": ""},
                "limit": {"type": "integer", "default": 10, "maximum": 50},
            },
            "required": ["q"],
        },
    },
    {
        "name": "memory_search_semantic",
        "description": "Cerca semàntica per similitud cosinus. Millor per trobar informació relacionada encara que no comparteixi paraules exactes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text de cerca semàntica"},
                "scope": {"type": "string", "enum": ["shared", "local"], "default": "shared"},
                "category": {"type": "string", "description": "Filtrar per categoria (buit = totes)", "default": ""},
                "top_k": {"type": "integer", "default": 5, "maximum": 50},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_delete",
        "description": "Elimina un fet per ID (soft delete). Requereix permís d'escriptura.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fact_id": {"type": "string", "description": "ID del fet a eliminar"},
            },
            "required": ["fact_id"],
        },
    },
    {
        "name": "memory_get_fact",
        "description": "Obté un fet concret per ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fact_id": {"type": "string", "description": "ID del fet"},
            },
            "required": ["fact_id"],
        },
    },
    {
        "name": "memory_stats",
        "description": "Obté estadístiques de la memòria: nombre de fets, fragments, agents, etc.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "memory_ls",
        "description": "Llista fets com un ls de directori. Filtra per scope, categoria i límit.",
        "input_schema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": ["shared", "local"], "default": "shared"},
                "category": {"type": "string", "description": "Filtrar per categoria (buit = totes)", "default": ""},
                "limit": {"type": "integer", "default": 20, "maximum": 100},
                "offset": {"type": "integer", "default": 0},
            },
            "required": [],
        },
    },
    {
        "name": "knowledge_traverse",
        "description": "Navega el knowledge graph des d'una entitat: troba nodes i arestes relacionats amb BFS pur (sense embeddings, nomes SQL). Ideal per consultes relacionals rapides com 'que coneix X?' o 'qui treballa amb Y?'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "ID o nom de l'entitat inicial (ex: 'Alice', 'uuid-xxx')"},
                "hops": {"type": "integer", "default": 2, "description": "Profunditat BFS (1-3)"},
                "direction": {"type": "string", "enum": ["out", "in", "both"], "default": "both", "description": "Direccio: out (subject->object), in (object->subject), both"}
            },
            "required": ["entity"]
        },
    },
]


# ── Helpers ───────────────────────────────────────────────────────────

def _get_agent_from_request(request: Request) -> dict[str, Any] | None:
    """Obté l'agent del request state, si existeix."""
    return getattr(request.state, "agent", None)


def _error(code: int, message: str, id_: Any = None) -> JSONResponse:
    # JSON-RPC error codes can be negative (e.g. -32603); use HTTP 500 for transport
    http_code = 500 if code < 0 else code
    return JSONResponse(
        status_code=http_code,
        content={"jsonrpc": "2.0", "error": {"code": code, "message": message}, "id": id_},
    )


def _success(result: Any, id_: Any = None) -> JSONResponse:
    return JSONResponse(
        content={"jsonrpc": "2.0", "result": result, "id": id_},
    )


# ── Endpoints ─────────────────────────────────────────────────────────

@router.get("/")
async def mcp_list_tools() -> JSONResponse:
    """Llista les eines disponibles (format JSON-RPC)."""
    return _success({
        "tools": TOOLS,
        "protocol": "model-context-protocol",
        "version": "1.0.0",
    })


@router.post("/")
async def mcp_handle(request: Request) -> JSONResponse:
    """Punt d'entrada JSON-RPC per al protocol MCP.

    Accepta:
      {"method": "tools/list", "params": {}, "id": 1}
      {"method": "tools/call", "params": {"name": "memory_write", "arguments": {...}}, "id": 2}
    """
    try:
        body = await request.json()
    except Exception:
        return _error(-32700, "Parse error: invalid JSON")

    method = body.get("method", "")
    params = body.get("params", {})
    id_ = body.get("id", None)

    if method == "tools/list":
        return _success({"tools": TOOLS}, id_)

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        return await _handle_tool_call(request, tool_name, arguments, id_)

    return _error(-32601, "Method not found: {method}", id_)


async def _handle_tool_call(
    request: Request, tool_name: str, arguments: dict[str, Any], id_: Any
) -> JSONResponse:
    """Executa una eina i retorna el resultat."""

    if tool_name == "memory_write":
        return await _tool_write(arguments, id_)
    elif tool_name == "memory_query":
        return await _tool_query(arguments, id_)
    elif tool_name == "memory_search_semantic":
        return await _tool_search_semantic(arguments, id_)
    elif tool_name == "memory_delete":
        return await _tool_delete(arguments, id_)
    elif tool_name == "memory_get_fact":
        return await _tool_get_fact(arguments, id_)
    elif tool_name == "memory_stats":
        return await _tool_stats(id_)
    elif tool_name == "memory_ls":
        return await _tool_ls(arguments, id_)
    elif tool_name == "knowledge_traverse":
        return await _tool_knowledge_traverse(arguments, id_)
    else:
        return _error(-32602, "Unknown tool: {tool_name}", id_)


# ── Implementacions de les eines ──────────────────────────────────────

async def _tool_write(args: dict[str, Any], id_: Any) -> JSONResponse:
    content = args.get("content", "")
    if not content:
        return _error(-32602, "content is required", id_)

    scope = args.get("scope", "shared")
    category = args.get("category", "")
    key = args.get("key")
    metadata = args.get("metadata", {})

    try:
        async with get_db() as db:
            cursor = await db.execute(
                "INSERT INTO facts (scope, category, agent_id, key, content, metadata) VALUES (?, ?, ?, ?, ?, ?)",
                (scope, category, None, key, content, json.dumps(metadata)),
            )
            rowid = cursor.lastrowid
            fact_id = ""
            if rowid:
                c2 = await db.execute("SELECT id FROM facts WHERE rowid = ?", (rowid,))
                row = await c2.fetchone()
                fact_id = row["id"] if row else ""

            # Chunks
            chunks = embedding_service.split_into_chunks(content)
            empty_blob = b"\x00" * (settings.EMBED_DIM * 4)
            for chunk_text in chunks:
                await db.execute(
                    "INSERT INTO chunks (fact_id, chunk_text, embedding_blob) VALUES (?, ?, ?)",
                    (fact_id, chunk_text, empty_blob),
                )

            await log_audit(db, None, "CREATE", "fact", resource_id=fact_id,
                            payload=json.dumps({"scope": scope, "source": "mcp"}))
            await db.commit()

        return _success({
            "fact_id": fact_id,
            "message": "Fet creat correctament",
            "chunks": len(chunks),
        }, id_)
    except Exception as e:
        return _error(-32603, f"Error writing memory: {str(e)}", id_)


async def _tool_query(args: dict[str, Any], id_: Any) -> JSONResponse:
    q = args.get("q", "")
    scope = args.get("scope", "shared")
    limit = min(args.get("limit", 10), 50)

    if not q:
        return _error(-32602, "q is required", id_)

    try:
        terms = q.strip().split()
        fts_query = " OR ".join(f'"{t}"*' for t in terms if t)
        if not fts_query:
            return _success({"results": [], "total": 0}, id_)

        async with get_db() as db:
            sql = """SELECT f.id, f.content, f.scope, f.category, f.agent_id, f.key, f.metadata, f.created_at
                   FROM facts f
                   JOIN facts_fts fts ON f.id = fts.fact_id
                   WHERE facts_fts MATCH ?
                     AND f.deleted_at IS NULL
                     AND f.scope = ?"""
            bind = [fts_query, scope]
            category = args.get("category", "")
            if category:
                sql += " AND f.category = ?"
                bind.append(category)
            sql += " ORDER BY f.updated_at DESC LIMIT ?"
            bind.append(limit)
            cursor = await db.execute(sql, bind)
            rows = await cursor.fetchall()

        results = []
        for row in rows:
            rd = dict(row)
            try:
                rd["metadata"] = json.loads(rd["metadata"]) if isinstance(rd["metadata"], str) else rd["metadata"]
            except Exception:
                rd["metadata"] = {}
            results.append(rd)

        return _success({"results": results, "total": len(results), "query": q}, id_)
    except Exception as e:
        return _error(-32603, f"Error querying memory: {str(e)}", id_)


async def _tool_search_semantic(args: dict[str, Any], id_: Any) -> JSONResponse:
    query = args.get("query", "")
    scope = args.get("scope", "shared")
    top_k = min(args.get("top_k", 5), 50)

    if not query:
        return _error(-32602, "query is required", id_)

    try:
        import numpy as np
        query_vec = embedding_service.get_embedding(query, "query: ")

        async with get_db() as db:
            cursor = await db.execute(
                """SELECT c.id, c.fact_id, c.chunk_text, c.embedding_blob
                   FROM chunks c
                   JOIN facts f ON c.fact_id = f.id
                   WHERE f.deleted_at IS NULL AND f.scope = ?""",
                (scope,),
            )
            rows = await cursor.fetchall()

        chunks_with_vecs = []
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
            return _success({"results": [], "query": query}, id_)

        scored = embedding_service.semantic_search(
            query_vec,
            [(c[0], c[1]) for c in chunks_with_vecs],
            top_k,
        )

        chunk_map = {c[0]: (c[2], c[3]) for c in chunks_with_vecs}
        results = []
        for chunk_id, score in scored:
            if chunk_id not in chunk_map:
                continue
            fact_id, chunk_text = chunk_map[chunk_id]
            results.append({
                "fact_id": fact_id,
                "content": chunk_text,
                "score": round(score, 4),
            })

        return _success({"results": results, "total": len(results), "query": query}, id_)
    except Exception as e:
        # Fallback a FTS5
        terms = query.strip().split()
        fts_query = " OR ".join(f'"{t}"*' for t in terms if t)
        if not fts_query:
            return _success({"results": [], "query": query, "fallback": True}, id_)

        try:
            async with get_db() as db:
                cursor = await db.execute(
                    """SELECT f.id, f.content, f.scope, f.agent_id, f.key, f.metadata, f.created_at
                       FROM facts f
                       JOIN facts_fts fts ON f.id = fts.fact_id
                       WHERE facts_fts MATCH ?
                         AND f.deleted_at IS NULL
                         AND f.scope = ?
                       ORDER BY f.updated_at DESC LIMIT ?""",
                    (fts_query, scope, top_k),
                )
                rows = await cursor.fetchall()
            results = [dict(r) for r in rows]
            return _success({"results": results, "total": len(results), "query": query, "fallback": True}, id_)
        except Exception as e2:
            return _error(-32603, f"Semantic search failed: {str(e)}. Fallback also failed: {str(e2)}", id_)


async def _tool_delete(args: dict[str, Any], id_: Any) -> JSONResponse:
    fact_id = args.get("fact_id", "")
    if not fact_id:
        return _error(-32602, "fact_id is required", id_)

    try:
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT id, category FROM facts WHERE id = ? AND deleted_at IS NULL", (fact_id,)
            )
            existing = await cursor.fetchone()
            if not existing:
                return _error(-32602, "Fact not found: {fact_id}", id_)

            # ── Protecció de categories persistents ──
            # Els fets d'infraestructura (system/config/entities) només es poden
            # esborrar per un agent autenticat amb permís 'admin'. Així, el procés
            # de purga de les 06:00 (que corre sense agent autenticat) no pot
            # destruir la memòria persistent de la flota.
            cat = existing["category"] or ""
            if cat in _PROTECTED_CATEGORIES:
                return _error(-32602, "Insufficient perms: fact protegit (system/config/entities)", id_)

            await db.execute("UPDATE facts SET deleted_at = datetime('now') WHERE id = ?", (fact_id,))
            await db.execute("DELETE FROM chunks WHERE fact_id = ?", (fact_id,))
            await log_audit(db, None, "DELETE", "fact", resource_id=fact_id)
            await db.commit()

        return _success({"fact_id": fact_id, "message": "Fet eliminat correctament"}, id_)
    except Exception as e:
        return _error(-32603, f"Error deleting fact: {str(e)}", id_)


async def _tool_get_fact(args: dict[str, Any], id_: Any) -> JSONResponse:
    fact_id = args.get("fact_id", "")
    if not fact_id:
        return _error(-32602, "fact_id is required", id_)

    try:
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT id, scope, agent_id, key, content, metadata, version, created_at, updated_at FROM facts WHERE id = ? AND deleted_at IS NULL",
                (fact_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return _error(-32602, "Fact not found: {fact_id}", id_)

            rd = dict(row)
            try:
                rd["metadata"] = json.loads(rd["metadata"]) if isinstance(rd["metadata"], str) else rd["metadata"]
            except Exception:
                rd["metadata"] = {}

        return _success({"fact": rd}, id_)
    except Exception as e:
        return _error(-32603, f"Error reading fact: {str(e)}", id_)


async def _tool_stats(id_: Any) -> JSONResponse:
    try:
        async with get_db() as db:
            cursor = await db.execute("SELECT COUNT(*) as cnt FROM facts WHERE deleted_at IS NULL")
            total_active = (await cursor.fetchone())["cnt"]

            cursor = await db.execute("SELECT COUNT(*) as cnt FROM chunks")
            total_chunks = (await cursor.fetchone())["cnt"]

            cursor = await db.execute("SELECT COUNT(*) as cnt FROM agents")
            total_agents = (await cursor.fetchone())["cnt"]

            cursor = await db.execute("SELECT COUNT(*) as cnt FROM consolidated")
            total_consolidated = (await cursor.fetchone())["cnt"]

        return _success({
            "total_active_facts": total_active,
            "total_chunks": total_chunks,
            "total_agents": total_agents,
            "total_consolidated": total_consolidated,
            "embedding_ready": embedding_service.is_ready,
            "version": "2.0.0",
        }, id_)
    except Exception as e:
        return _error(-32603, f"Error getting stats: {str(e)}", id_)


async def _tool_ls(args: dict[str, Any], id_: Any) -> JSONResponse:
    """Llista fets amb filtre per scope, categoria, key i límit."""
    scope = args.get("scope", "shared")
    category = args.get("category", "")
    key = args.get("key", "")
    limit = min(args.get("limit", 20), 100)
    offset = args.get("offset", 0)

    try:
        async with get_db() as db:
            where = ["f.deleted_at IS NULL", "f.scope = ?"]
            bind = [scope]
            if category:
                where.append("f.category = ?")
                bind.append(category)
            if key:
                where.append("f.key = ?")
                bind.append(key)

            where_clause = " AND ".join(where)
            cursor = await db.execute(
                f"SELECT COUNT(*) as cnt FROM facts f WHERE {where_clause}", bind
            )
            total = (await cursor.fetchone())["cnt"]

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
        return _success({
            "items": items,
            "total": total,
            "scope": scope,
            "filters": {"category": category},
        }, id_)
    except Exception as e:
        return _error(-32603, f"Error listing facts: {str(e)}", id_)
async def _tool_knowledge_traverse(args, id_):
    """Graph traversal BFS pur (sense embeddings) via /v1/knowledge/traverse."""
    entity = args.get("entity", "")
    if not entity:
        return _error(-32602, "entity is required", id_)

    hops = args.get("hops", 2)
    if not isinstance(hops, int) or hops < 1 or hops > 3:
        hops = 2

    direction = args.get("direction", "both")
    if direction not in ("out", "in", "both"):
        direction = "both"

    try:
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT id, name, type FROM entities WHERE (id = ? OR name = ?) AND deleted_at IS NULL",
                (entity, entity),
            )
            start = await cursor.fetchone()
            if not start:
                return _error(-32602, "Entity not found: " + str(entity), id_)

            start_id = start["id"]
            nodes = {start_id: {"id": start_id, "name": start["name"], "type": start["type"] or "", "hop": 0}}
            edges = []
            current_ids = {start_id}
            visited_ids = {start_id}
            edge_ids = set()

            for hop in range(1, hops + 1):
                if not current_ids:
                    break

                ids_list = list(current_ids)
                placeholders = ",".join(["?"] * len(ids_list))

                if direction == "out":
                    sql = f"""SELECT t.id, t.subject_id, t.predicate, t.object_id, t.confidence,
                                     es.name as subject_name, es.type as subject_type,
                                     eo.name as object_name, eo.type as object_type
                              FROM triples t
                              JOIN entities es ON t.subject_id = es.id
                              JOIN entities eo ON t.object_id = eo.id
                              WHERE t.subject_id IN ({placeholders})
                                AND t.deleted_at IS NULL
                                AND es.deleted_at IS NULL
                                AND eo.deleted_at IS NULL"""
                elif direction == "in":
                    sql = f"""SELECT t.id, t.subject_id, t.predicate, t.object_id, t.confidence,
                                     es.name as subject_name, es.type as subject_type,
                                     eo.name as object_name, eo.type as object_type
                              FROM triples t
                              JOIN entities es ON t.subject_id = es.id
                              JOIN entities eo ON t.object_id = eo.id
                              WHERE t.object_id IN ({placeholders})
                                AND t.deleted_at IS NULL
                                AND es.deleted_at IS NULL
                                AND eo.deleted_at IS NULL"""
                else:
                    sql = f"""SELECT t.id, t.subject_id, t.predicate, t.object_id, t.confidence,
                                     es.name as subject_name, es.type as subject_type,
                                     eo.name as object_name, eo.type as object_type
                              FROM triples t
                              JOIN entities es ON t.subject_id = es.id
                              JOIN entities eo ON t.object_id = eo.id
                              WHERE (t.subject_id IN ({placeholders}) OR t.object_id IN ({placeholders}))
                                AND t.deleted_at IS NULL
                                AND es.deleted_at IS NULL
                                AND eo.deleted_at IS NULL"""
                    ids_list = ids_list * 2

                cursor = await db.execute(sql, ids_list)
                rows = await cursor.fetchall()

                next_ids = set()
                for row in rows:
                    if row["id"] in edge_ids:
                        continue
                    edge_ids.add(row["id"])

                    for nid, nname, ntype in [
                        (row["subject_id"], row["subject_name"], row["subject_type"]),
                        (row["object_id"], row["object_name"], row["object_type"]),
                    ]:
                        if nid not in nodes:
                            nodes[nid] = {"id": nid, "name": nname, "type": ntype or "", "hop": hop}
                        if nid not in visited_ids:
                            next_ids.add(nid)

                    edges.append({
                        "subject_id": row["subject_id"],
                        "subject_name": row["subject_name"],
                        "predicate": row["predicate"],
                        "object_id": row["object_id"],
                        "object_name": row["object_name"],
                        "confidence": float(row["confidence"]) if row["confidence"] else 1.0,
                        "hop": hop,
                    })

                current_ids = next_ids - visited_ids
                visited_ids.update(next_ids)

        return _success({
            "entity": entity,
            "nodes": [nodes[k] for k in nodes],
            "edges": edges,
            "hops": hops,
            "total_nodes": len(nodes),
            "total_edges": len(edges),
        }, id_)
    except Exception as e:
        return _error(-32603, "Error traversing graph: " + str(e), id_)
