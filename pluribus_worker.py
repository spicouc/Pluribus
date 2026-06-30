#!/usr/bin/env python3
"""Pluribus Worker — Consolidació, relacions semàntiques, manteniment i sincronització Notion.

Script autònom que corre periòdicament (via systemd timer) per:
1. Consolidació: detecta fets sense consolidar, els resumeix amb LLM (Ollama)
2. Relacions semàntiques: compara embeddings de fets nous vs existents, crea relacions
3. Manteniment: neteja embeddings orfes, optimitza DB
4. Sincronització Notion: (opcional) si NOTION_API_KEY està configurada
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import numpy as np
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("pluribus_worker")

# Constants
DB_PATH = "/opt/pluribus/data/pluribus.db"
OLLAMA_BASE_URL = "http://100.85.57.11:11434"
OLLAMA_MODEL = "nomic-embed-text-v2-moe:latest"
CONSOLIDATION_MODEL = "qwen2.5:3b"
EMBED_DIM = 768
BATCH_SIZE = 10
SEMANTIC_THRESHOLD = 0.55  # Cosine similarity threshold for relation discovery


async def get_db() -> aiosqlite.Connection:
    db_path = Path(DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA busy_timeout=5000")
    return conn


async def ensure_tables(db: aiosqlite.Connection) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS consolidated (
            id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
            session_id TEXT, agent_id TEXT, summary TEXT NOT NULL,
            source_facts TEXT, model TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_consolidated_session_id ON consolidated(session_id)")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS notion_cache (
            id TEXT PRIMARY KEY, title TEXT, markdown TEXT, url TEXT,
            embedding_blob BLOB, last_synced TEXT, parent_db TEXT
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS notion_links (
            id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
            fact_id TEXT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
            notion_page_id TEXT NOT NULL REFERENCES notion_cache(id),
            relevance REAL DEFAULT 0.0, match_type TEXT DEFAULT 'auto',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS notion_sync_log (
            id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
            page_id TEXT, action TEXT, error TEXT,
            synced_at TEXT DEFAULT (datetime('now'))
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_notion_links_fact ON notion_links(fact_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_notion_links_page ON notion_links(notion_page_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_consolidated_agent_id ON consolidated(agent_id)")
    # Phase 1: fact_relations
    await db.execute("""
        CREATE TABLE IF NOT EXISTS fact_relations (
            id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
            source_fact_id TEXT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
            target_fact_id TEXT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
            relation_type TEXT NOT NULL DEFAULT 'related_to',
            relation_strength REAL DEFAULT 0.5,
            discovered_by TEXT DEFAULT 'worker',
            metadata TEXT DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_fact_relations_source ON fact_relations(source_fact_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_fact_relations_target ON fact_relations(target_fact_id)")
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_fact_relations_type ON fact_relations(relation_type)
    """)
    await db.commit()


def get_embedding(text: str) -> np.ndarray | None:
    """Obté l'embedding d'un text via Ollama."""
    try:
        resp = requests.post(
            f"{OLLAMA_BASE_URL}/api/embed",
            json={"model": OLLAMA_MODEL, "input": text},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        vec = np.array(data["embeddings"][0], dtype=np.float32)
        if len(vec.shape) > 1:
            vec = vec.flatten()
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec
    except Exception as exc:
        logger.warning(f"Error getting embedding: {exc}")
        return None


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity entre dos vectors normalitzats L2."""
    return float(np.dot(a, b))


async def compute_semantic_relations(db: aiosqlite.Connection) -> dict[str, Any]:
    """Descobreix relacions semàntiques entre fets comparant embeddings.

    Per cada fact sense relacionar, compara els seus chunks embeddings
    contra tots els altres facts. Si la similitud cosinus > threshold,
    crea una relació a fact_relations.
    """
    stats = {"relations_created": 0, "facts_checked": 0, "errors": 0}

    # Obtenir facts que tenen embeddings (no buits)
    cursor = await db.execute("""
        SELECT c.id as chunk_id, c.fact_id, c.chunk_text, c.embedding_blob
        FROM chunks c
        JOIN facts f ON c.fact_id = f.id
        WHERE f.deleted_at IS NULL
          AND c.embedding_blob IS NOT NULL
          AND length(c.embedding_blob) = ?
    """, (EMBED_DIM * 4,))
    rows = await cursor.fetchall()
    if not rows:
        logger.info("No chunks amb embeddings disponibles.")
        return stats

    logger.info(f"Analitzant {len(rows)} chunks per trobar relacions semàntiques...")

    # Carregar tots els vectors en memòria
    chunks_with_vecs: list[tuple[str, str, np.ndarray]] = []
    for row in rows:
        rd = dict(row)
        blob = rd["embedding_blob"]
        if blob is None or len(blob) < EMBED_DIM * 4:
            continue
        vec = np.frombuffer(blob, dtype=np.float32)
        if len(vec) != EMBED_DIM:
            continue
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        chunks_with_vecs.append((rd["chunk_id"], rd["fact_id"], vec))

    if len(chunks_with_vecs) < 2:
        logger.info("Cal almenys 2 chunks per descobrir relacions.")
        return stats

    # Per cada fact, compara els seus vectors contra tots els altres
    fact_chunks: dict[str, list[tuple[str, np.ndarray]]] = {}
    for chunk_id, fact_id, vec in chunks_with_vecs:
        if fact_id not in fact_chunks:
            fact_chunks[fact_id] = []
        fact_chunks[fact_id].append((chunk_id, vec))

    fact_ids = list(fact_chunks.keys())
    stats["facts_checked"] = len(fact_ids)

    # Per a cada parell de facts, calcular la millor similitud entre els seus chunks
    created_count = 0
    for i in range(len(fact_ids)):
        for j in range(i + 1, len(fact_ids)):
            fid_a = fact_ids[i]
            fid_b = fact_ids[j]

            # Comprovar si ja existeix relació entre aquests dos facts
            cursor = await db.execute(
                "SELECT id FROM fact_relations WHERE (source_fact_id = ? AND target_fact_id = ?) OR (source_fact_id = ? AND target_fact_id = ?)",
                (fid_a, fid_b, fid_b, fid_a),
            )
            if await cursor.fetchone():
                continue  # Ja relacionats

            # Calcular millor similitud entre chunks
            best_similarity = 0.0
            for _, vec_a in fact_chunks[fid_a]:
                for _, vec_b in fact_chunks[fid_b]:
                    sim = cosine_similarity(vec_a, vec_b)
                    if sim > best_similarity:
                        best_similarity = sim
                        if best_similarity >= 1.0:
                            break
                if best_similarity >= 1.0:
                    break

            if best_similarity >= SEMANTIC_THRESHOLD:
                try:
                    await db.execute(
                        """INSERT INTO fact_relations
                           (source_fact_id, target_fact_id, relation_type, relation_strength, discovered_by)
                           VALUES (?, ?, 'related_to', ?, 'semantic')""",
                        (fid_a, fid_b, round(best_similarity, 4)),
                    )
                    created_count += 1
                    if created_count % 10 == 0:
                        await db.commit()
                except Exception as exc:
                    logger.warning(f"Error creant relació: {exc}")
                    stats["errors"] += 1

    await db.commit()
    stats["relations_created"] = created_count
    if created_count > 0:
        logger.info(f"✓ Creades {created_count} relacions semàntiques noves.")
    return stats


async def consolidate_facts(db: aiosqlite.Connection) -> dict[str, Any]:
    stats = {"processed": 0, "errors": 0, "summaries_created": 0}

    cursor = await db.execute("SELECT MAX(created_at) as last_consolidated FROM consolidated")
    row = await cursor.fetchone()
    last_consolidated = row["last_consolidated"] if row and row["last_consolidated"] else "1970-01-01"

    cursor = await db.execute(
        """SELECT id, content, agent_id, key, scope, created_at
           FROM facts WHERE deleted_at IS NULL AND created_at > ?
           ORDER BY created_at ASC LIMIT ?""",
        (last_consolidated, BATCH_SIZE),
    )
    facts = await cursor.fetchall()

    if not facts:
        logger.info("No hi ha facts nous per consolidar.")
        return stats

    logger.info(f"Consolidant {len(facts)} facts...")

    for fact in facts:
        fact_dict = dict(fact)
        try:
            prompt = (
                "Resumeix el següent text en 1-2 frases curtes en català. "
                "Sigues concís i mantingues la informació clau.\n\n"
                f"Text: {fact_dict['content']}"
            )
            resp = requests.post(
                f"{OLLAMA_BASE_URL}/api/chat",
                json={
                    "model": CONSOLIDATION_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "options": {"temperature": 0.3, "num_predict": 512},
                },
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()
            summary = data.get("message", {}).get("content", "")
            if not summary:
                raise RuntimeError("Ollama no ha retornat cap resum")

            cursor2 = await db.execute(
                "SELECT id FROM consolidated WHERE source_facts LIKE ?",
                (f'%{fact_dict["id"]}%',),
            )
            if await cursor2.fetchone():
                logger.debug(f"Fact {fact_dict['id'][:8]} ja consolidat, ometent.")
                continue

            source_facts = json.dumps([fact_dict["id"]])
            await db.execute(
                """INSERT INTO consolidated (agent_id, summary, source_facts, model)
                   VALUES (?, ?, ?, ?)""",
                (fact_dict.get("agent_id"), summary, source_facts, CONSOLIDATION_MODEL),
            )
            stats["summaries_created"] += 1
            logger.info(f"✓ Consolidat fact {fact_dict['id'][:8]}... → {summary[:80]}...")

        except requests.RequestException as exc:
            logger.error(f"✗ Error cridant Ollama per fact {fact_dict['id'][:8]}: {exc}")
            stats["errors"] += 1
        except Exception as exc:
            logger.error(f"✗ Error inesperat consolidant fact {fact_dict['id'][:8]}: {exc}")
            stats["errors"] += 1

        stats["processed"] += 1

    await db.commit()
    return stats


async def maintenance(db: aiosqlite.Connection) -> dict[str, Any]:
    actions = {}

    cursor = await db.execute("""
        SELECT COUNT(*) as cnt FROM chunks c
        LEFT JOIN facts f ON c.fact_id = f.id
        WHERE f.id IS NULL OR f.deleted_at IS NOT NULL
    """)
    row = await cursor.fetchone()
    orphan_chunks = row["cnt"] if row else 0

    if orphan_chunks > 0:
        await db.execute("""
            DELETE FROM chunks WHERE fact_id IN (
                SELECT fact_id FROM chunks c
                LEFT JOIN facts f ON c.fact_id = f.id
                WHERE f.id IS NULL OR f.deleted_at IS NOT NULL
            )
        """)
        await db.commit()
        actions["orphan_chunks_deleted"] = orphan_chunks
        logger.info(f"Netejats {orphan_chunks} chunks orfes.")

    cursor = await db.execute("""
        SELECT COUNT(*) as cnt FROM embedding_cache
        WHERE created_at < datetime('now', '-7 days')
    """)
    row = await cursor.fetchone()
    old_cache = row["cnt"] if row else 0

    if old_cache > 0:
        await db.execute("""
            DELETE FROM embedding_cache WHERE created_at < datetime('now', '-7 days')
        """)
        await db.commit()
        actions["old_cache_deleted"] = old_cache
        logger.info(f"Netejades {old_cache} entrades antigues de cache.")

    try:
        db_path = Path(DB_PATH)
        if db_path.exists():
            cursor = await db.execute("PRAGMA page_count")
            page_count = (await cursor.fetchone())[0]
            cursor = await db.execute("PRAGMA freelist_count")
            free_pages = (await cursor.fetchone())[0]
            if free_pages > 0 and (free_pages / page_count) > 0.1:
                logger.info(f"Optimitzant DB: {page_count} pàgines, {free_pages} lliures ({free_pages/page_count*100:.1f}%)")
                await db.execute("VACUUM")
                await db.commit()
                actions["vacuum_done"] = True
                logger.info("✓ VACUUM completat.")
    except Exception as exc:
        logger.warning(f"No s'ha pogut fer VACUUM: {exc}")

    return actions


async def run_notion_sync(db: aiosqlite.Connection) -> dict[str, Any]:
    try:
        import importlib
        notion = importlib.import_module("pluribus.notion")
        result = await notion.sync_notion_cache()
        logger.info(f"Notion sync: {result}")
        return result
    except ImportError:
        logger.info("Mòdul pluribus.notion no disponible, ometent sync Notion.")
        return {"synced": 0, "error": "notion module not found"}
    except Exception as exc:
        logger.warning(f"Error en sync Notion: {exc}")
        return {"synced": 0, "error": str(exc)}


async def run() -> dict[str, Any]:
    start_time = time.time()
    logger.info("=" * 60)
    logger.info("🧠 Pluribus Worker iniciant ronda...")
    logger.info("=" * 60)

    results: dict[str, Any] = {"started_at": datetime.now(timezone.utc).isoformat()}

    db = await get_db()
    try:
        await ensure_tables(db)
        logger.info("✓ Taules verificades.")

        # 1. Consolidació
        logger.info("--- Consolidació ---")
        cons_result = await consolidate_facts(db)
        results["consolidation"] = cons_result
        logger.info(f"Consolidació: {cons_result['summaries_created']} resums creats de {cons_result['processed']} processats.")

        # 2. Relacions semàntiques (Phase 1)
        logger.info("--- Relacions Semàntiques ---")
        rel_result = await compute_semantic_relations(db)
        results["semantic_relations"] = rel_result
        logger.info(f"Relacions: {rel_result['relations_created']} creades, {rel_result['facts_checked']} facts analitzats.")

        # 3. Manteniment
        logger.info("--- Manteniment ---")
        maint_result = await maintenance(db)
        results["maintenance"] = maint_result
        logger.info(f"Manteniment: {maint_result}")

        # 4. Notion sync (opcional)
        logger.info("--- Notion Sync ---")
        notion_result = await run_notion_sync(db)
        results["notion_sync"] = notion_result
        logger.info(f"Notion: {notion_result.get('synced', 0)} pàgines sincronitzades.")

    except Exception as exc:
        logger.error(f"Error fatal al worker: {exc}")
        results["error"] = str(exc)
    finally:
        await db.close()

    elapsed = time.time() - start_time
    results["elapsed_seconds"] = round(elapsed, 2)
    logger.info(f"✅ Ronda completada en {elapsed:.2f}s")
    logger.info("=" * 60)

    return results


def main() -> None:
    result = asyncio.run(run())
    if result.get("consolidation", {}).get("errors", 0) > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
