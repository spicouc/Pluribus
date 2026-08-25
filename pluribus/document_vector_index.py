"""DocumentVectorIndex — independent semantic index for the document library.

The document library derives its OWN semantic vector index, separate from the
facts TurboVec index. Data flow (phase L3):

    documents -> document_versions -> document_chunks -> embeddings
        -> DocumentVectorIndex

Hard-rule compliance: document chunks are NEVER routed into the facts
``FactVectorIndex`` (``pluribus/vector_index.py``). The two generation counters
are fully decoupled:

* ``document_vector_index_state.generation`` is bumped only by the ``docvec_*``
  triggers (document_chunks / documents) — never by facts tables.
* ``vector_index_state.generation`` tracks the *facts* TurboVec index.

Rebuilding the document index therefore never bumps the fact generation and
vice versa (tests L3-12 / L3-13 assert exactly this).

Implementation notes:
* The index is a derived, rebuildable artefact whose source of truth is SQLite.
  ``_SCAN_SQL`` selects only chunks that are: on a non-deleted document, on the
  document's CURRENT version, ``embedding_state='ready'``, with the correct
  embedding dim, and a finite, non-zero vector.
* A lazy ``ensure_loaded()`` mirrors the FactVectorIndex generation-check
  pattern but reads the DOCUMENT generation counter, never the fact one.
* Uses a numpy brute-force cosine scan (independent of ``turbovec``) so the
  document index stays decoupled from the facts semantic stack.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any, Optional

import numpy as np

from pluribus.config import settings

logger = logging.getLogger(__name__)

# Only index live, current-version, ready, correctly-dimensioned, finite,
# non-zero vectors. ``v.version = d.current_version`` selects only the latest
# version of each document. Per-vector metadata matches the L3 spec.
_SCAN_SQL = """
    SELECT c.id             AS chunk_id,
           c.document_id    AS document_id,
           c.version_id     AS version_id,
           c.embedding_blob AS embedding_blob,
           c.embedding_dim  AS embedding_dim,
           c.heading_path   AS heading_path,
           c.section        AS section,
           c.line_start     AS line_start,
           c.line_end       AS line_end,
           d.scope          AS scope,
           d.title          AS title,
           d.category       AS category
    FROM document_chunks c
    JOIN documents d         ON d.id = c.document_id
    JOIN document_versions v ON v.id = c.version_id
    WHERE c.embedding_state = 'ready'
      AND c.embedding_dim = ?
      AND d.deleted_at IS NULL
      AND v.version = d.current_version
"""


class DocumentVectorIndex:
    """Derived semantic index over the document library, independent of facts.

    Lifecycle mirrors the FactVectorIndex but is grounded in the dedicated
    ``document_vector_index_state`` generation counter (split generations).
    """

    def __init__(self) -> None:
        self._vectors: Optional[np.ndarray] = None
        self._meta: list[dict[str, Any]] = []
        self._loaded = False
        self._building = False
        self._generation: Optional[int] = None
        self._db_path: Optional[str] = None

    def invalidate(self) -> None:
        """Force the next search/stat to rebuild from SQLite."""
        self._generation = None
        self._loaded = False

    def _read_generation_sync(self) -> int:
        conn = sqlite3.connect(str(settings.DB_PATH))
        try:
            row = conn.execute(
                "SELECT generation FROM document_vector_index_state "
                "WHERE singleton = 1"
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def _scan_sync(self) -> tuple[int, list[dict[str, Any]], Optional[np.ndarray]]:
        """Pull valid vectors+metadata from SQLite in one consistent snapshot."""
        dim = settings.EMBED_DIM
        conn = sqlite3.connect(str(settings.DB_PATH))
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN")
            gen_row = conn.execute(
                "SELECT generation FROM document_vector_index_state "
                "WHERE singleton = 1"
            ).fetchone()
            generation = int(gen_row["generation"]) if gen_row else 0

            rows = conn.execute(_SCAN_SQL, (dim,)).fetchall()
            meta: list[dict[str, Any]] = []
            vectors: list[np.ndarray] = []
            for row in rows:
                blob = row["embedding_blob"]
                if blob is None or len(blob) != dim * 4:
                    continue
                vec = np.frombuffer(blob, dtype=np.float32)
                if vec.ndim != 1 or vec.shape[0] != dim:
                    continue
                if not np.all(np.isfinite(vec)):
                    continue
                norm = float(np.linalg.norm(vec))
                if norm <= 0:
                    continue
                vec = vec / norm
                vectors.append(vec)
                meta.append(
                    {
                        "chunk_id": row["chunk_id"],
                        "document_id": row["document_id"],
                        "version_id": row["version_id"],
                        "scope": row["scope"],
                        "title": row["title"],
                        "category": row["category"],
                        # "filename": documents have no filesystem path; the L3
                        # spec wants a filename-ish label, so derive a stable
                        # slug from the title (or fall back to the chunk id).
                        "filename": row["title"] or row["chunk_id"],
                        "heading_path": row["heading_path"] or "",
                        "section": row["section"] or "",
                        "line_start": int(row["line_start"] or 0),
                        "line_end": int(row["line_end"] or 0),
                    }
                )
            arr = np.asarray(vectors, dtype=np.float32) if vectors else None
            return generation, meta, arr
        finally:
            conn.close()

    async def rebuild(self) -> bool:
        """Rebuild the index in a thread (CPU-bound scan)."""
        if self._building:
            return False
        self._building = True
        try:
            generation, meta, arr = await asyncio.to_thread(self._scan_sync)
            self._vectors = arr
            self._meta = meta
            self._generation = generation
            self._db_path = str(settings.DB_PATH)
            self._loaded = True
            logger.info(
                "DocumentVectorIndex rebuilt: %d chunks generation=%s size=%s",
                len(meta),
                generation,
                len(arr) if arr is not None else 0,
            )
            return True
        finally:
            self._building = False

    async def ensure_loaded(self) -> bool:
        """Reload only when the DOCUMENT generation counter changed."""
        current_db_path = str(settings.DB_PATH)
        current_generation = await asyncio.to_thread(self._read_generation_sync)
        if (
            self._loaded
            and self._vectors is not None
            and self._generation == current_generation
            and self._db_path == current_db_path
        ):
            return True
        if self._building:
            for _ in range(100):
                await asyncio.sleep(0.1)
                if not self._building:
                    break
            if self._loaded and self._generation == current_generation:
                return True
        return await self.rebuild()

    async def search(
        self,
        query_vec: np.ndarray,
        *,
        scope_filter: Optional[str] = None,
        scope_filters: Optional[list[str]] = None,
        top_k: int = 5,
    ) -> list[tuple[str, float]]:
        """Return top_k [(chunk_id, score)] against the document vectors.

        ``query_vec`` is compared by cosine (vectors are pre-normalized). The
        generation is checked before and after so a concurrent document
        mutation triggers one authoritative rebuild (same pattern as TurboVec).
        """
        if not await self.ensure_loaded():
            return []
        if self._vectors is None or len(self._vectors) == 0:
            return []
        if query_vec.ndim != 1 or query_vec.shape[0] != settings.EMBED_DIM:
            return []
        if not np.all(np.isfinite(query_vec)) or float(np.linalg.norm(query_vec)) <= 0:
            return []

        query_norm = query_vec / np.linalg.norm(query_vec)
        allowed_scopes: Optional[set[str]] = None
        if scope_filters is not None:
            allowed_scopes = {s for s in scope_filters if isinstance(s, str)}
        if scope_filter is not None:
            allowed_scopes = (allowed_scopes or {scope_filter}) & {scope_filter}

        snapshot_gen = self._generation
        results = await asyncio.to_thread(
            self._search_sync, query_norm, allowed_scopes, top_k
        )

        latest_gen = await asyncio.to_thread(self._read_generation_sync)
        if latest_gen != snapshot_gen:
            if not await self.rebuild():
                return []
            results = await asyncio.to_thread(
                self._search_sync, query_norm, allowed_scopes, top_k
            )
        return results

    def _search_sync(
        self,
        query_norm: np.ndarray,
        allowed_scopes: Optional[set[str]],
        top_k: int,
    ) -> list[tuple[str, float]]:
        if self._vectors is None or len(self._vectors) == 0:
            return []
        indices = list(range(len(self._meta)))
        if allowed_scopes is not None:
            indices = [
                i
                for i in indices
                if isinstance(self._meta[i].get("scope"), str)
                and self._meta[i]["scope"] in allowed_scopes
            ]
            if not indices:
                return []
        v = self._vectors[indices]
        scores = v @ query_norm
        k = min(top_k, len(scores))
        if k <= 0:
            return []
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        return [
            (self._meta[indices[int(i)]]["chunk_id"], float(scores[int(i)]))
            for i in top
        ]

    async def get_stats(self) -> dict[str, Any]:
        await self.ensure_loaded()
        return {
            "loaded": self._loaded,
            "size": len(self._meta),
            "dim": settings.EMBED_DIM,
            "generation": self._generation,
            "db_path": self._db_path,
        }

    def metadata_for(self, chunk_id: str) -> Optional[dict[str, Any]]:
        """Return the stored metadata dict for a chunk_id (for assertions)."""
        for m in self._meta:
            if m["chunk_id"] == chunk_id:
                return m
        return None


document_vector_index = DocumentVectorIndex()
