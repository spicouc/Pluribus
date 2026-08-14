"""Periodic Pluribus maintenance worker.

Consolidates unconsolidated facts, discovers semantic relations, performs
maintenance and optionally refreshes the Notion cache. Configuration is read
from the same PLURIBUS_* settings as the API service.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import numpy as np
import requests

from pluribus.config import settings
from pluribus.db import get_db, init_db

logger = logging.getLogger("pluribus_worker")

BATCH_SIZE = int(os.getenv("PLURIBUS_WORKER_BATCH_SIZE", "10"))
SEMANTIC_THRESHOLD = float(os.getenv("PLURIBUS_SEMANTIC_THRESHOLD", "0.55"))
CONSOLIDATION_FALLBACK_MODEL = os.getenv(
    "PLURIBUS_CONSOLIDATION_FALLBACK_MODEL",
    "hf.co/bartowski/gemma-2-2b-it-abliterated-GGUF:Q4_K_M",
)


async def ensure_worker_tables(db) -> None:
    """Create worker-owned bookkeeping tables and backfill legacy mappings."""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS consolidated_facts (
            fact_id TEXT PRIMARY KEY REFERENCES facts(id) ON DELETE CASCADE,
            consolidated_id TEXT NOT NULL REFERENCES consolidated(id) ON DELETE CASCADE,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_consolidated_facts_consolidated "
        "ON consolidated_facts(consolidated_id)"
    )

    cursor = await db.execute(
        "SELECT id, source_facts FROM consolidated WHERE source_facts IS NOT NULL"
    )
    for row in await cursor.fetchall():
        try:
            fact_ids = json.loads(row["source_facts"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(fact_ids, list):
            continue
        for fact_id in fact_ids:
            if not isinstance(fact_id, str):
                continue
            await db.execute(
                """INSERT OR IGNORE INTO consolidated_facts(fact_id, consolidated_id)
                   SELECT ?, ? WHERE EXISTS (SELECT 1 FROM facts WHERE id = ?)""",
                (fact_id, row["id"], fact_id),
            )
    await db.commit()


def _ollama_chat(model: str, prompt: str, timeout: int) -> str:
    response = requests.post(
        f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/chat",
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "think": False,
            "options": {"temperature": 0.3, "num_predict": 512},
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return (response.json().get("message", {}).get("content", "") or "").strip()


def _summarize_sync(content: str) -> tuple[str, str]:
    prompt = (
        "Resumeix el següent text en 1-2 frases curtes en català. "
        "Sigues concís i mantingues la informació clau.\n\n"
        f"Text: {content}"
    )
    summary = _ollama_chat(settings.CONSOLIDATION_MODEL, prompt, 60)
    model = settings.CONSOLIDATION_MODEL
    if not summary:
        logger.warning("Consolidació buida amb %s; provant fallback", model)
        summary = _ollama_chat(CONSOLIDATION_FALLBACK_MODEL, prompt, 90)
        model = CONSOLIDATION_FALLBACK_MODEL
    if not summary:
        raise RuntimeError("Cap model de consolidació ha retornat resum")
    return summary, model


async def consolidate_facts(db) -> dict[str, Any]:
    """Process facts not present in consolidated_facts, without time checkpoints."""
    stats = {"processed": 0, "errors": 0, "summaries_created": 0}
    cursor = await db.execute(
        """SELECT f.id, f.content, f.agent_id, f.created_at
           FROM facts f
           LEFT JOIN consolidated_facts cf ON cf.fact_id = f.id
           WHERE f.deleted_at IS NULL AND cf.fact_id IS NULL
           ORDER BY f.created_at ASC, f.id ASC
           LIMIT ?""",
        (BATCH_SIZE,),
    )
    facts = await cursor.fetchall()
    if not facts:
        logger.info("No hi ha facts pendents de consolidar.")
        return stats

    logger.info("Consolidant %d facts pendents...", len(facts))
    for fact in facts:
        stats["processed"] += 1
        try:
            summary, model = await asyncio.to_thread(_summarize_sync, fact["content"])
            source_facts = json.dumps([fact["id"]])
            cursor2 = await db.execute(
                """INSERT INTO consolidated (agent_id, summary, source_facts, model)
                   VALUES (?, ?, ?, ?)""",
                (fact["agent_id"], summary, source_facts, model),
            )
            rowid = cursor2.lastrowid
            c3 = await db.execute("SELECT id FROM consolidated WHERE rowid = ?", (rowid,))
            consolidated_row = await c3.fetchone()
            if not consolidated_row:
                raise RuntimeError("No s'ha pogut recuperar el consolidated creat")
            await db.execute(
                "INSERT INTO consolidated_facts(fact_id, consolidated_id) VALUES (?, ?)",
                (fact["id"], consolidated_row["id"]),
            )
            await db.commit()
            stats["summaries_created"] += 1
        except Exception as exc:
            await db.rollback()
            logger.error("Error consolidant fact %s: %s", fact["id"], exc)
            stats["errors"] += 1
    return stats


async def compute_semantic_relations(db) -> dict[str, Any]:
    """Create semantic relations only between facts that share the same scope."""
    stats = {"relations_created": 0, "facts_checked": 0, "errors": 0}
    cursor = await db.execute(
        """SELECT c.fact_id, f.scope, c.embedding_blob
           FROM chunks c
           JOIN facts f ON f.id = c.fact_id
           WHERE f.deleted_at IS NULL
             AND c.embedding_blob IS NOT NULL
             AND length(c.embedding_blob) = ?""",
        (settings.EMBED_DIM * 4,),
    )

    vectors_by_scope: dict[str, dict[str, list[np.ndarray]]] = {}
    for row in await cursor.fetchall():
        vec = np.frombuffer(row["embedding_blob"], dtype=np.float32)
        if vec.size != settings.EMBED_DIM or not np.all(np.isfinite(vec)):
            continue
        norm = float(np.linalg.norm(vec))
        if norm <= 0:
            continue
        vectors_by_scope.setdefault(row["scope"], {}).setdefault(
            row["fact_id"], []
        ).append(vec / norm)

    stats["facts_checked"] = sum(
        len(vectors_by_fact) for vectors_by_fact in vectors_by_scope.values()
    )
    if stats["facts_checked"] < 2:
        return stats

    cursor = await db.execute(
        "SELECT source_fact_id, target_fact_id FROM fact_relations"
    )
    existing = {
        tuple(sorted((row["source_fact_id"], row["target_fact_id"])))
        for row in await cursor.fetchall()
    }

    for scope, vectors_by_fact in sorted(vectors_by_scope.items()):
        fact_ids = sorted(vectors_by_fact)
        if len(fact_ids) < 2:
            continue
        for i, source_id in enumerate(fact_ids):
            source = np.stack(vectors_by_fact[source_id])
            for target_id in fact_ids[i + 1:]:
                pair = tuple(sorted((source_id, target_id)))
                if pair in existing:
                    continue
                target = np.stack(vectors_by_fact[target_id])
                similarity = float(np.max(source @ target.T))
                if similarity < SEMANTIC_THRESHOLD:
                    continue
                try:
                    await db.execute(
                        """INSERT INTO fact_relations
                           (source_fact_id, target_fact_id, relation_type,
                            relation_strength, discovered_by)
                           VALUES (?, ?, 'related_to', ?, 'semantic')""",
                        (source_id, target_id, round(similarity, 4)),
                    )
                    existing.add(pair)
                    stats["relations_created"] += 1
                except Exception as exc:
                    logger.warning(
                        "Error creant relació %s/%s a scope %s: %s",
                        source_id,
                        target_id,
                        scope,
                        exc,
                    )
                    stats["errors"] += 1
    await db.commit()
    return stats


async def maintenance(db) -> dict[str, Any]:
    actions: dict[str, Any] = {}
    cursor = await db.execute(
        """SELECT COUNT(*) AS cnt FROM chunks c
           LEFT JOIN facts f ON f.id = c.fact_id
           WHERE f.id IS NULL OR f.deleted_at IS NOT NULL"""
    )
    orphan_count = (await cursor.fetchone())["cnt"]
    if orphan_count:
        await db.execute(
            """DELETE FROM chunks
               WHERE fact_id NOT IN (SELECT id FROM facts WHERE deleted_at IS NULL)"""
        )
        actions["orphan_chunks_deleted"] = orphan_count

    cursor = await db.execute(
        "SELECT COUNT(*) AS cnt FROM embedding_cache WHERE created_at < datetime('now', '-7 days')"
    )
    old_cache = (await cursor.fetchone())["cnt"]
    if old_cache:
        await db.execute(
            "DELETE FROM embedding_cache WHERE created_at < datetime('now', '-7 days')"
        )
        actions["old_embedding_cache_deleted"] = old_cache

    await db.execute("PRAGMA optimize")
    await db.commit()
    return actions


async def run_notion_sync() -> dict[str, Any]:
    if not settings.NOTION_API_KEY:
        return {"synced": 0, "skipped": "NOTION_API_KEY not configured"}
    try:
        from pluribus.notion import sync_notion_cache
        return await sync_notion_cache()
    except Exception as exc:
        logger.warning("Error en sync Notion: %s", exc)
        return {"synced": 0, "error": str(exc)}


async def run() -> dict[str, Any]:
    start = time.time()
    results: dict[str, Any] = {"started_at": datetime.now(timezone.utc).isoformat()}
    try:
        await init_db()
        async with get_db() as db:
            await ensure_worker_tables(db)
            results["consolidation"] = await consolidate_facts(db)
            results["semantic_relations"] = await compute_semantic_relations(db)
            results["maintenance"] = await maintenance(db)
        results["notion_sync"] = await run_notion_sync()
    except Exception as exc:
        logger.exception("Error fatal al worker")
        results["error"] = str(exc)
    results["elapsed_seconds"] = round(time.time() - start, 2)
    return results


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    result = asyncio.run(run())
    logger.info("Resultat worker: %s", json.dumps(result, ensure_ascii=False))
    if result.get("error"):
        raise SystemExit(1)
    if result.get("consolidation", {}).get("errors", 0):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
