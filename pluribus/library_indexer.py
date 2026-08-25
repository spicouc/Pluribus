"""L3 external semantic indexer for the document library (library_indexer).

This module is the ONLY place that turns document chunks from ``pending`` into
``ready`` embeddings. It is invoked from the **external** worker
(``pluribus_worker.py`` / ``pluribus-worker.service``), never from a background
daemon inside the FastAPI process.

Responsibilities (phase L3):

* Read ``document_chunks`` rows whose ``embedding_state`` is ``pending`` or
  ``retryable``.
* Generate a 768-dim float32 embedding via the shared Ollama client.
* Write ``embedding_blob`` and set ``embedding_state = 'ready'`` along with
  ``embedding_model`` / ``embedding_dim``.
* Reuse previously computed vectors by a safe identity key =
  ``(chunk_sha, embedding_model, embedding_dim)``. A model change changes the
  key, so incompatible vectors are never silently reused.
* Bounded, idempotent retries: on Ollama failure we increment
  ``embedding_attempts`` and only flip to ``error`` after exhausting
  ``MAX_ATTEMPTS`` so the worker keeps retrying ``retryable`` chunks.
* A zero / NaN / wrong-dim vector is NEVER committed as a valid embedding.

Hard-rule compliance matches the rest of the library: this indexer never touches
``facts``, ``facts_fts``, ``chunks``, the Fact VectorIndex, Recall v2 or
``notion_cache``. The document chunk -> DocumentVectorIndex path stays fully
decoupled from the facts TurboVec index.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

import numpy as np

from pluribus.config import settings
from pluribus.embedding import embedding_service

logger = logging.getLogger("pluribus_library_indexer")

# Bounded retries per chunk across indexer runs before an Ollama failure moves a
# chunk to the terminal 'error' state (clean, retryable later if desired).
MAX_ATTEMPTS = int(os.getenv("PLURIBUS_LIBRARY_MAX_ATTEMPTS", "3"))
# How many pending chunks to process in one worker pass (bounded ramp).
BATCH_SIZE = int(os.getenv("PLURIBUS_LIBRARY_BATCH_SIZE", "25"))

_EMBED_COLS = (
    "c.id, c.document_id, c.version_id, c.chunk_sha, c.chunk_text, "
    "c.embedding_model, c.embedding_dim, c.embedding_attempts"
)


def _is_valid_embedding(vec: Any) -> bool:
    """A valid embedding is 1-D, the right dim, finite and with norm > 0.

    A zero vector is never a valid embedding: it carries no information and
    would silently distort cosine similarity. NaN / Inf are rejected too.
    """
    if vec is None or not isinstance(vec, np.ndarray):
        return False
    if vec.ndim != 1 or vec.shape[0] != settings.EMBED_DIM:
        return False
    if not np.all(np.isfinite(vec)):
        return False
    if float(np.linalg.norm(vec)) <= 0:
        return False
    return True


def _blob_to_vec(blob: Any) -> Optional[np.ndarray]:
    """Decode a stored float32 blob into a validated vector, or None."""
    try:
        if blob is None or len(blob) != settings.EMBED_DIM * 4:
            return None
        vec = np.frombuffer(blob, dtype=np.float32)
        if not _is_valid_embedding(vec):
            return None
        return vec
    except (TypeError, ValueError):
        return None


async def _reuse_blob(db, sha: str, model: str, dim: int) -> Optional[bytes]:
    """Return a previously stored valid vector for the given reuse key.

    The reuse cache is keyed on ``(sha, model, dim)`` so a change to the
    embedding model (or dim) presents a different key and forces regeneration.
    We also re-validate the stored blob before returning it — an invalid cache
    row (zero / NaN / wrong dim) is treated as absent.
    """
    cursor = await db.execute(
        "SELECT embedding_blob FROM document_embedding_cache "
        "WHERE sha = ? AND model = ? AND dim = ?",
        (sha, model, dim),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    blob = row["embedding_blob"]
    if _blob_to_vec(blob) is None:
        return None
    return blob


async def _store_reuse(db, sha: str, model: str, dim: int, blob: bytes) -> None:
    """Persist a validated vector blob into the (sha, model, dim) cache."""
    await db.execute(
        "INSERT OR REPLACE INTO document_embedding_cache "
        "(sha, model, dim, embedding_blob) VALUES (?, ?, ?, ?)",
        (sha, model, dim, blob),
    )


async def _pending_chunks(db, limit: int = BATCH_SIZE):
    """Return pending/retryable chunks whose owning document is alive.

    Soft-deleted documents are skipped so their pending chunks don't get
    embeddings generated after deletion. ``retryable`` chunks (Ollama was down
    or a transient failure) are picked up again on the next pass.
    """
    cursor = await db.execute(
        f"""SELECT {_EMBED_COLS}
            FROM document_chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE c.embedding_state IN ('pending', 'retryable')
              AND d.deleted_at IS NULL
            ORDER BY c.created_at ASC, c.id ASC
            LIMIT ?""",
        (limit,),
    )
    return await cursor.fetchall()


async def run_library_indexer(db) -> dict[str, Any]:
    """Process document chunks: pending/retryable -> ready (or error/retryable).

    ``db`` is an open aiosqlite connection (the worker already holds one).
    Never raises on Ollama failure — degraded operation is the intended design.
    Ollama being down leaves chunks retryable/error, never corrupts them, and
    never commits a zero vector.
    """
    stats: dict[str, Any] = {
        "processed": 0,
        "ready": 0,
        "reused": 0,
        "failed": 0,
        "skipped": 0,
        "ollama_down": False,
    }

    pending = await _pending_chunks(db)
    if not pending:
        logger.info("Library indexer: no pending document chunks.")
        return stats

    # Fail-fast, cleanly: if Ollama reports not ready, mark pending chunks
    # retryable and exit degraded without calling the API.
    ollama_ready = await embedding_service.check_ready(force=False)
    if not ollama_ready:
        stats["ollama_down"] = True
        stats["skipped"] = len(pending)
        for row in pending:
            await db.execute(
                "UPDATE document_chunks SET embedding_state = 'retryable', "
                "embedding_attempts = embedding_attempts + 1 WHERE id = ?",
                (row["id"],),
            )
        await db.commit()
        logger.warning(
            "Library indexer: Ollama not ready; %d chunks left retryable",
            len(pending),
        )
        return stats

    model = settings.OLLAMA_MODEL
    dim = settings.EMBED_DIM

    for row in pending:
        stats["processed"] += 1
        chunk_id = row["id"]
        sha = row["chunk_sha"]
        text = row["chunk_text"]
        try:
            # 1) Safe reuse lookup keyed on sha+model+dim (model safety L3-15).
            reused = await _reuse_blob(db, sha, model, dim)
            if reused is not None:
                await db.execute(
                    """UPDATE document_chunks
                       SET embedding_blob = ?, embedding_state = 'ready',
                           embedding_model = ?, embedding_dim = ?,
                           embedding_attempts = embedding_attempts + 1
                       WHERE id = ?""",
                    (reused, model, dim, chunk_id),
                )
                stats["reused"] += 1
                stats["ready"] += 1
                continue

            # 2) Generate a fresh embedding in a thread (Ollama is blocking).
            vec = await asyncio.to_thread(embedding_service.get_embedding, text)

            # 3) Never accept a zero / NaN / wrong-dim vector.
            if not _is_valid_embedding(vec):
                raise ValueError(
                    "Ollama returned an invalid embedding (zero/NaN/wrong dim)"
                )

            blob = vec.astype(np.float32).tobytes()
            await db.execute(
                """UPDATE document_chunks
                   SET embedding_blob = ?, embedding_state = 'ready',
                       embedding_model = ?, embedding_dim = ?,
                       embedding_attempts = embedding_attempts + 1
                   WHERE id = ?""",
                (blob, model, dim, chunk_id),
            )
            # Persist reusable vector keyed on sha+model+dim.
            await _store_reuse(db, sha, model, dim, blob)
            stats["ready"] += 1
        except Exception as exc:
            logger.warning("Library indexer failed chunk %s: %s", chunk_id, exc)
            await _bump_attempt(db, chunk_id)
            stats["failed"] += 1
    await db.commit()
    return stats


async def _bump_attempt(db, chunk_id: str) -> None:
    """Increment attempts; flip to 'error' after MAX_ATTEMPTS, else retryable."""
    cursor = await db.execute(
        "SELECT embedding_attempts FROM document_chunks WHERE id = ?",
        (chunk_id,),
    )
    row = await cursor.fetchone()
    attempts = int(row["embedding_attempts"]) if row else 0
    attempts += 1
    if attempts >= MAX_ATTEMPTS:
        await db.execute(
            "UPDATE document_chunks SET embedding_state = 'error', "
            "embedding_attempts = ? WHERE id = ?",
            (attempts, chunk_id),
        )
    else:
        await db.execute(
            "UPDATE document_chunks SET embedding_state = 'retryable', "
            "embedding_attempts = ? WHERE id = ?",
            (attempts, chunk_id),
        )


async def reset_to_pending(db) -> int:
    """Test/ops helper: move 'retryable'/'error' chunks back to 'pending'.

    Lets bounded retries resume after an external fix (e.g. Ollama restored).
    """
    cursor = await db.execute(
        "UPDATE document_chunks SET embedding_state = 'pending' "
        "WHERE embedding_state IN ('retryable', 'error')"
    )
    await db.commit()
    return cursor.rowcount
