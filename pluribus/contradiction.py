"""
Detecció de contradiccions entre fets del Brain v2.

Quan s'escriu un fact nou:
1. Es genera l'embedding del contingut
2. Es cercan facts semànticament similars (> threshold 0.50)
3. Per cada match, es comprova:
   a. Heurística: si un text té paraules de negació i l'altre no → contradicció
   b. LLM (Ollama): si l'heurística no és concloent, es consulta qwen2.5:3b
4. Si es detecta contradicció, es crea una relació type="contradiction" al knowledge graph
"""

from __future__ import annotations

import os
import asyncio
import json
import logging
import re
from typing import Optional

import aiosqlite
import numpy as np
import requests

from pluribus.config import settings
import urllib.request
import json as _json

# Webhook URL for contradiction notifications (from .env or empty)
WEBHOOK_URL = ""  # Set via BRAIN_CONTRADICTION_WEBHOOK in .env or edit here


logger = logging.getLogger(__name__)

# Llindar de similitud cosinus per considerar dos texts com a potencialment contradictoris
SIMILARITY_THRESHOLD = 0.50

# Màxim de facts similars a avaluar per write
MAX_CANDIDATES = 5

# Paraules de negació per detecció heurística
NEGATION_WORDS = [
    "no", "not", "n't", "never", "without", "cannot", "can't",
    "doesn't", "don't", "isn't", "aren't", "wasn't", "weren't",
    "won't", "wouldn't", "shouldn't", "couldn't", "hasn't", "haven't",
    "ain't", "mustn't", "needn't", "daren't", "nor",
]

# Compil·lem una regex per detecció ràpida
_NEGATION_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in NEGATION_WORDS) + r")\b",
    re.IGNORECASE,
)


def _has_negation(text: str) -> bool:
    """Detecta si un text conté paraules de negació."""
    return bool(_NEGATION_RE.search(text))


def _check_contradiction_heuristic(text_a: str, text_b: str) -> Optional[bool]:
    """
    Heurística de detecció de contradiccions basada en negació.

    Si un text afirma alguna cosa i l'altre la nega (presència de negació
    en un però no a l'altre), és probablement una contradicció.

    Retorna True (contradicció), False (consistent) o None (incert).
    """
    has_a = _has_negation(text_a)
    has_b = _has_negation(text_b)

    # Si un té negació i l'altre no → probable contradicció
    if has_a != has_b:
        return True

    # Ambdós tenen o no tenen negació → incert
    return None


def _check_contradiction_llm(text_a: str, text_b: str) -> bool:
    """
    Consulta Ollama per determinar si dos texts es contradiuen.

    S'usa quan l'heurística no és concloent.
    Retorna True si el model confirma contradicció.
    """
    prompt = (
        "Do these two statements contradict each other?\n\n"
        f"Statement A: {text_a[:500]}\n\n"
        f"Statement B: {text_b[:500]}\n\n"
        "Reply with exactly one word: CONTRADICTION or CONSISTENT."
    )

    try:
        resp = requests.post(
            f"{settings.OLLAMA_BASE_URL}/api/generate",
            json={
                "model": settings.CONSOLIDATION_MODEL,  # qwen2.5:3b
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.0, "num_predict": 10},
            },
            timeout=120,
        )
        resp.raise_for_status()
        result = resp.json().get("response", "").strip().upper()
        # Accept both "CONTRADICTION" and "YES" (for backward compat)
        return "CONTRADICTION" in result or result == "YES"
    except Exception as exc:
        logger.warning("Ollama contradiction check failed: %s", exc)
        return False


async def check_contradictions(
    fact_id: str,
    content: str,
    agent_id: str,
) -> None:
    """
    Tasca de fons per detectar contradiccions entre el nou fact i facts existents.

    S'executa en background després de cada write_memory.
    No llança excepcions — si falla, només es loggeja.
    """
    try:
        await _check_contradictions_impl(fact_id, content, agent_id)
    except Exception as exc:
        logger.error("Contradiction check failed for fact %s: %s", fact_id, exc)


async def _check_contradictions_impl(
    fact_id: str,
    content: str,
    agent_id: str,
) -> None:
    """Implementació interna de la detecció de contradiccions."""
    from pluribus.embedding import embedding_service
    from pluribus.vector_index import vector_index

    # 1. Generar embedding del nou contingut
    vec = await asyncio.to_thread(
        embedding_service.get_embedding, content, "passage: "
    )

    if np.all(vec == 0):
        logger.debug("Contradiction check skipped: Ollama not available")
        return

    # 2. Cercar facts semànticament similars via TurboVec
    scored = await vector_index.search(
        vec,
        scope_filter=None,
        agent_id_filter=None,
        top_k=MAX_CANDIDATES,
    )

    if not scored or len(scored) == 0:
        return

    # 3. Per cada chunk similar, comprovar si el fact origen és diferent del nou
    chunk_ids = [c[0] for c in scored]
    score_map = {c[0]: c[1] for c in scored}

    placeholders = ",".join("?" for _ in chunk_ids)

    async with aiosqlite.connect(str(settings.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            f"""SELECT c.id, c.fact_id, c.chunk_text
                FROM chunks c
                JOIN facts f ON c.fact_id = f.id
                WHERE c.id IN ({placeholders})
                  AND f.deleted_at IS NULL
                  AND f.id != ?""",
            chunk_ids + [fact_id],
        )
        rows = await cursor.fetchall()

        # Agrupar per fact_id (un fact pot tenir múltiples chunks)
        candidates: dict[str, dict] = {}
        for row in rows:
            fid = row["fact_id"]
            score = score_map.get(row["id"], 0.0)

            if score < SIMILARITY_THRESHOLD:
                continue

            # Quedar-nos amb el text del chunk més similar
            if fid not in candidates or score > candidates[fid]["score"]:
                candidates[fid] = {
                    "fact_id": fid,
                    "chunk_text": row["chunk_text"],
                    "score": score,
                }

        if not candidates:
            return

        new_relations = 0
        for fid, cand in candidates.items():
            # 4. Detecció per heurística primer
            is_contradiction = _check_contradiction_heuristic(
                content, cand["chunk_text"]
            )

            # 5. Si l'heurística no decideix, consultar Ollama
            if is_contradiction is None:
                is_contradiction = await asyncio.to_thread(
                    _check_contradiction_llm,
                    content,
                    cand["chunk_text"],
                )

            if not is_contradiction:
                continue

            # 6. Crear relació de contradicció al knowledge graph
            existing = await db.execute(
                """SELECT id FROM fact_relations
                   WHERE ((source_fact_id = ? AND target_fact_id = ?)
                      OR (source_fact_id = ? AND target_fact_id = ?))
                     AND relation_type = 'contradiction'""",
                (fact_id, fid, fid, fact_id),
            )
            if await existing.fetchone():
                continue  # Ja existeix, no duplicar

            await db.execute(
                """INSERT INTO fact_relations
                   (source_fact_id, target_fact_id, relation_type, relation_strength, discovered_by)
                   VALUES (?, ?, 'contradiction', ?, 'auto:contradiction_check')""",
                (fact_id, fid, round(cand["score"], 4)),
            )
            new_relations += 1
            logger.info(
                "Contradiction detected: %s <-> %s (score: %.4f, heuristic=%s)",
                fact_id,
                fid,
                cand["score"],
                is_contradiction,
            )

        await db.commit()

        if new_relations > 0:
            # Notify webhook if configured
            wh_url = WEBHOOK_URL or os.environ.get("BRAIN_CONTRADICTION_WEBHOOK", "")
            if wh_url:
                try:
                    payload = _json.dumps({
                        "event": "contradiction_detected",
                        "source_fact_id": fact_id,
                        "target_fact_id": fid,
                        "score": round(cand["score"], 4),
                        "content_a": content[:200],
                        "content_b": cand["chunk_text"][:200],
                        "timestamp": datetime.utcnow().isoformat(),
                    }).encode()
                    req = urllib.request.Request(wh_url, data=payload,
                        headers={"Content-Type": "application/json"},
                        method="POST")
                    urllib.request.urlopen(req, timeout=5)
                except Exception as wh_err:
                    logger.warning("Webhook notification failed: %s", wh_err)

            logger.info(
                "Created %d contradiction relation(s) for fact %s",
                new_relations,
                fact_id,
                )
