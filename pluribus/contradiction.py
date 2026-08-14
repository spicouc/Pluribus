"""Detecció de contradiccions entre fets de Pluribus.

El detector és una tasca auxiliar: només compara fets actius del mateix scope
que el fact nou. Qualsevol notificació externa s'ha de fer mitjançant el
subsistema central de webhooks signats; aquest mòdul no envia URLs arbitràries.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

import numpy as np
import requests

from pluribus.config import settings
from pluribus.db import get_db

logger = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.50
MAX_CANDIDATES = 5

NEGATION_WORDS = [
    "no", "not", "n't", "never", "without", "cannot", "can't",
    "doesn't", "don't", "isn't", "aren't", "wasn't", "weren't",
    "won't", "wouldn't", "shouldn't", "couldn't", "hasn't", "haven't",
    "ain't", "mustn't", "needn't", "daren't", "nor",
]

_NEGATION_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in NEGATION_WORDS) + r")\b",
    re.IGNORECASE,
)


def _has_negation(text: str) -> bool:
    return bool(_NEGATION_RE.search(text))


def _check_contradiction_heuristic(text_a: str, text_b: str) -> Optional[bool]:
    has_a = _has_negation(text_a)
    has_b = _has_negation(text_b)
    if has_a != has_b:
        return True
    return None


def _check_contradiction_llm(text_a: str, text_b: str) -> bool:
    prompt = (
        "Do these two statements contradict each other?\n\n"
        f"Statement A: {text_a[:500]}\n\n"
        f"Statement B: {text_b[:500]}\n\n"
        "Reply with exactly one word: CONTRADICTION or CONSISTENT."
    )
    try:
        resp = requests.post(
            f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/generate",
            json={
                "model": settings.CONSOLIDATION_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.0, "num_predict": 10},
            },
            timeout=120,
        )
        resp.raise_for_status()
        result = resp.json().get("response", "").strip().upper()
        return "CONTRADICTION" in result or result == "YES"
    except Exception as exc:
        logger.warning("Ollama contradiction check failed: %s", exc)
        return False


async def check_contradictions(
    fact_id: str,
    content: str,
    agent_id: str,
) -> None:
    """Detecta contradiccions sense travessar límits de scope."""
    try:
        await _check_contradictions_impl(fact_id, content, agent_id)
    except Exception as exc:
        logger.error("Contradiction check failed for fact %s: %s", fact_id, exc)


async def _fact_scope(fact_id: str) -> str | None:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT scope FROM facts WHERE id = ? AND deleted_at IS NULL",
            (fact_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else row["scope"]


async def _check_contradictions_impl(
    fact_id: str,
    content: str,
    agent_id: str,
) -> None:
    del agent_id  # API compatibility; authorization is encoded by the fact scope.

    from pluribus.embedding import embedding_service
    from pluribus.vector_index import vector_index

    scope = await _fact_scope(fact_id)
    if scope is None:
        return

    vec = await embedding_service.get_embedding_async(content, "passage: ")
    if vec.size != settings.EMBED_DIM or not np.all(np.isfinite(vec)):
        return
    if float(np.linalg.norm(vec)) <= 0:
        logger.debug("Contradiction check skipped: embedding unavailable")
        return

    scored = await vector_index.search(
        vec,
        scope_filter=scope,
        agent_id_filter=None,
        top_k=MAX_CANDIDATES,
    )
    if not scored:
        return

    chunk_ids = [chunk_id for chunk_id, _score in scored]
    score_map = {chunk_id: score for chunk_id, score in scored}
    placeholders = ",".join("?" for _ in chunk_ids)

    async with get_db() as db:
        cursor = await db.execute(
            f"""SELECT c.id, c.fact_id, c.chunk_text
                FROM chunks c
                JOIN facts f ON c.fact_id = f.id
                WHERE c.id IN ({placeholders})
                  AND f.deleted_at IS NULL
                  AND f.scope = ?
                  AND f.id != ?""",
            [*chunk_ids, scope, fact_id],
        )
        rows = await cursor.fetchall()

        candidates: dict[str, dict] = {}
        for row in rows:
            fid = row["fact_id"]
            score = score_map.get(row["id"], 0.0)
            if score < SIMILARITY_THRESHOLD:
                continue
            if fid not in candidates or score > candidates[fid]["score"]:
                candidates[fid] = {
                    "fact_id": fid,
                    "chunk_text": row["chunk_text"],
                    "score": score,
                }

        new_relations = 0
        for fid, candidate in candidates.items():
            is_contradiction = _check_contradiction_heuristic(
                content,
                candidate["chunk_text"],
            )
            if is_contradiction is None:
                is_contradiction = await asyncio.to_thread(
                    _check_contradiction_llm,
                    content,
                    candidate["chunk_text"],
                )
            if not is_contradiction:
                continue

            existing = await db.execute(
                """SELECT id FROM fact_relations
                   WHERE ((source_fact_id = ? AND target_fact_id = ?)
                      OR (source_fact_id = ? AND target_fact_id = ?))
                     AND relation_type = 'contradiction'""",
                (fact_id, fid, fid, fact_id),
            )
            if await existing.fetchone():
                continue

            await db.execute(
                """INSERT INTO fact_relations
                   (source_fact_id, target_fact_id, relation_type,
                    relation_strength, discovered_by)
                   VALUES (?, ?, 'contradiction', ?, 'auto:contradiction_check')""",
                (fact_id, fid, round(candidate["score"], 4)),
            )
            new_relations += 1
            logger.info(
                "Contradiction detected inside scope %s: %s <-> %s (score %.4f)",
                scope,
                fact_id,
                fid,
                candidate["score"],
            )

        if new_relations:
            await db.commit()
            logger.info(
                "Created %d contradiction relation(s) for fact %s",
                new_relations,
                fact_id,
            )
