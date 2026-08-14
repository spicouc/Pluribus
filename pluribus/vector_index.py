"""TurboVec-based vector index for semantic search.

Wraps turbovec.IdMapIndex to provide fast vector search with metadata filtering.
All TurboVec operations run in asyncio.to_thread to avoid blocking the event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import struct
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np
from turbovec import IdMapIndex

from pluribus.config import settings

logger = logging.getLogger(__name__)

# Path to persist the index
INDEX_DIR = Path(settings.DB_PATH).parent
INDEX_PATH = INDEX_DIR / "turbovec_index.tvim"
META_PATH = INDEX_DIR / "turbovec_meta.json"


def _chunk_id_to_u64(chunk_id: str) -> int:
    """Convert a string chunk_id to a uint64 for TurboVec external IDs.

    Uses the first 8 bytes of SHA256 hash to get a deterministic uint64.
    """
    h = hashlib.sha256(chunk_id.encode()).digest()
    return struct.unpack("<Q", h[:8])[0]


class VectorIndex:
    """Thread-safe TurboVec vector index with metadata tracking.

    Stores vectors in a TurboVec IdMapIndex with external IDs derived from
    chunk_id strings. Maintains Python dicts for metadata lookup.
    """

    def __init__(self) -> None:
        self._index: Optional[IdMapIndex] = None
        self._loaded = False
        self._building = False

        # Metadata mappings: external_id (u64) -> metadata dict
        self._meta_by_ext: dict[int, dict[str, Any]] = {}
        # Reverse mapping: chunk_id (str) -> external_id (u64)
        self._ext_by_chunk: dict[str, int] = {}
        # All known external IDs for allowlist filtering
        self._all_ext_ids: list[int] = []
        self._all_ext_ids_arr: Optional[np.ndarray] = None

    async def ensure_loaded(self) -> bool:
        """Ensure the index is loaded. Auto-builds from DB if needed."""
        if self._loaded and self._index is not None:
            return True
        if self._building:
            # Wait for build in progress
            for _ in range(100):
                await asyncio.sleep(0.1)
                if self._loaded and self._index is not None:
                    return True
            return False
        return await self.rebuild()

    async def rebuild(self) -> bool:
        """Drop existing index and rebuild from SQLite."""
        if self._building:
            return False
        self._building = True
        try:
            t0 = time.time()
            count = await asyncio.to_thread(self._rebuild_sync)
            elapsed = time.time() - t0
            logger.info("TurboVec index rebuilt: %d chunks in %.2fs", count, elapsed)
            return count > 0
        finally:
            self._building = False

    def _rebuild_sync(self) -> int:
        """Synchronous rebuild from SQLite (runs in thread)."""
        import sqlite3

        db_path = settings.DB_PATH
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        try:
            # Load all active chunks with valid embeddings
            cursor = conn.execute("""
                SELECT c.id as chunk_id, c.fact_id, c.embedding_blob,
                       f.scope, f.category, f.agent_id, f.key
                FROM chunks c
                JOIN facts f ON c.fact_id = f.id
                WHERE f.deleted_at IS NULL
                  AND c.embedding_blob IS NOT NULL
            """)
            rows = cursor.fetchall()

            if not rows:
                self._index = IdMapIndex(dim=settings.EMBED_DIM, bit_width=4)
                self._meta_by_ext = {}
                self._ext_by_chunk = {}
                self._all_ext_ids = []
                self._all_ext_ids_arr = None
                self._loaded = True
                return 0

            # Parse vectors and metadata
            chunk_ids = []
            vectors = []
            meta_list = []

            for row in rows:
                blob = row["embedding_blob"]
                if blob is None or len(blob) < settings.EMBED_DIM * 4:
                    continue
                vec = np.frombuffer(blob, dtype=np.float32)
                if len(vec) != settings.EMBED_DIM:
                    continue
                # Normalize
                norm = np.linalg.norm(vec)
                if norm > 0:
                    vec = vec / norm

                ext_id = _chunk_id_to_u64(row["chunk_id"])
                chunk_ids.append(row["chunk_id"])
                vectors.append(vec)
                meta_list.append({
                    "chunk_id": row["chunk_id"],
                    "fact_id": row["fact_id"],
                    "scope": row["scope"],
                    "category": row["category"],
                    "agent_id": row["agent_id"],
                    "key": row["key"],
                })

            if not vectors:
                self._index = IdMapIndex(dim=settings.EMBED_DIM, bit_width=4)
                self._meta_by_ext = {}
                self._ext_by_chunk = {}
                self._all_ext_ids = []
                self._all_ext_ids_arr = None
                self._loaded = True
                return 0

            # Build index
            vectors_arr = np.array(vectors, dtype=np.float32)
            ext_ids_arr = np.array(
                [_chunk_id_to_u64(cid) for cid in chunk_ids], dtype=np.uint64
            )

            index = IdMapIndex(dim=settings.EMBED_DIM, bit_width=4)
            index.add_with_ids(vectors_arr, ext_ids_arr)
            index.prepare()

            # Build metadata mappings
            meta_by_ext = {}
            ext_by_chunk = {}
            for i, cid in enumerate(chunk_ids):
                ext_id = int(ext_ids_arr[i])
                meta_by_ext[ext_id] = meta_list[i]
                ext_by_chunk[cid] = ext_id

            self._index = index
            self._meta_by_ext = meta_by_ext
            self._ext_by_chunk = ext_by_chunk
            self._all_ext_ids = list(ext_ids_arr)
            self._all_ext_ids_arr = ext_ids_arr
            self._loaded = True

            # Persist to disk
            try:
                INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
                index.write(str(INDEX_PATH))
                logger.info("TurboVec index persisted to %s", INDEX_PATH)
            except Exception as exc:
                logger.warning("Failed to persist TurboVec index: %s", exc)

            return len(chunk_ids)
        finally:
            conn.close()

    async def search(
        self,
        query_vec: np.ndarray,
        scope_filter: Optional[str] = None,
        category_filter: Optional[str] = None,
        agent_id_filter: Optional[str] = None,
        top_k: int = 5,
    ) -> list[tuple[str, float]]:
        """Search for similar vectors with optional metadata filtering.

        Returns list of (chunk_id, score) tuples.
        """
        if not await self.ensure_loaded():
            return []

        if self._index is None or len(self._index) == 0:
            return []

        return await asyncio.to_thread(
            self._search_sync,
            query_vec,
            scope_filter,
            category_filter,
            agent_id_filter,
            top_k,
        )

    def _search_sync(
        self,
        query_vec: np.ndarray,
        scope_filter: Optional[str],
        category_filter: Optional[str],
        agent_id_filter: Optional[str],
        top_k: int,
    ) -> list[tuple[str, float]]:
        """Synchronous search (runs in thread)."""
        if self._index is None or self._all_ext_ids_arr is None:
            return []

        # Build allowlist based on filters
        allowlist = self._build_allowlist(
            scope_filter,
            category_filter,
            agent_id_filter,
        )

        if allowlist is not None and len(allowlist) == 0:
            return []

        # Reshape query for single query
        q = query_vec.reshape(1, -1).astype(np.float32)

        # Search with allowlist
        k = min(top_k, len(self._index))
        if allowlist is not None:
            k = min(k, len(allowlist))

        if k == 0:
            return []

        try:
            scores, ids = self._index.search(q, k, allowlist=allowlist)
        except Exception as exc:
            logger.warning("TurboVec search failed: %s", exc)
            return []

        # Map external IDs back to chunk_ids
        results = []
        for score_val, ext_id in zip(scores[0], ids[0]):
            ext_id_int = int(ext_id)
            meta = self._meta_by_ext.get(ext_id_int)
            if meta:
                chunk_id = meta["chunk_id"]
                results.append((chunk_id, float(score_val)))

        return results

    def _build_allowlist(
        self,
        scope_filter: Optional[str],
        category_filter: Optional[str],
        agent_id_filter: Optional[str],
    ) -> Optional[np.ndarray]:
        """Build an allowlist array based on metadata filters."""
        if (
            scope_filter is None
            and category_filter is None
            and agent_id_filter is None
        ):
            return self._all_ext_ids_arr

        filtered = []
        for ext_id in self._all_ext_ids:
            meta = self._meta_by_ext.get(ext_id)
            if meta is None:
                continue
            if scope_filter is not None and meta.get("scope") != scope_filter:
                continue
            if category_filter is not None and meta.get("category") != category_filter:
                continue
            if agent_id_filter is not None and meta.get("agent_id") != agent_id_filter:
                continue
            filtered.append(ext_id)

        if not filtered:
            return np.array([], dtype=np.uint64)
        return np.array(filtered, dtype=np.uint64)

    async def add_vectors(
        self,
        chunk_ids: list[str],
        vectors: list[np.ndarray],
        metadata_list: list[dict[str, Any]],
    ) -> None:
        """Add new vectors to the index."""
        if not await self.ensure_loaded():
            return

        if self._index is None:
            return

        await asyncio.to_thread(
            self._add_vectors_sync, chunk_ids, vectors, metadata_list
        )

    def _add_vectors_sync(
        self,
        chunk_ids: list[str],
        vectors: list[np.ndarray],
        metadata_list: list[dict[str, Any]],
    ) -> None:
        """Synchronous add (runs in thread)."""
        if self._index is None or not chunk_ids:
            return

        ext_ids = []
        vecs = []
        new_metas = []
        new_ext_ids = []

        for cid, vec, meta in zip(chunk_ids, vectors, metadata_list):
            if cid in self._ext_by_chunk:
                continue  # Already in index
            ext_id = _chunk_id_to_u64(cid)
            ext_ids.append(ext_id)
            vecs.append(vec)
            new_metas.append(meta)
            new_ext_ids.append(ext_id)

        if not ext_ids:
            return

        vectors_arr = np.array(vecs, dtype=np.float32)
        ext_ids_arr = np.array(ext_ids, dtype=np.uint64)

        try:
            self._index.add_with_ids(vectors_arr, ext_ids_arr)
            self._index.prepare()

            # Update mappings
            for i, cid in enumerate(chunk_ids):
                if cid not in self._ext_by_chunk:
                    eid = _chunk_id_to_u64(cid)
                    self._meta_by_ext[eid] = metadata_list[i]
                    self._ext_by_chunk[cid] = eid
                    self._all_ext_ids.append(eid)

            self._all_ext_ids_arr = np.array(self._all_ext_ids, dtype=np.uint64)

            # Persist
            try:
                INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
                self._index.write(str(INDEX_PATH))
            except Exception as exc:
                logger.warning("Failed to persist index after add: %s", exc)

        except Exception as exc:
            logger.error("Failed to add vectors to TurboVec: %s", exc)

    async def remove_vector(self, chunk_id: str) -> bool:
        """Remove a vector by chunk_id."""
        if not self._loaded or self._index is None:
            return False

        return await asyncio.to_thread(self._remove_vector_sync, chunk_id)

    def _remove_vector_sync(self, chunk_id: str) -> bool:
        """Synchronous remove (runs in thread)."""
        ext_id = self._ext_by_chunk.get(chunk_id)
        if ext_id is None:
            return False

        try:
            # Get the internal index position from the IdMapIndex
            # We need to find the internal position of this external ID
            # IdMapIndex.remove() takes the external ID directly
            self._index.remove(ext_id)

            # Update mappings
            del self._ext_by_chunk[chunk_id]
            if ext_id in self._meta_by_ext:
                del self._meta_by_ext[ext_id]
            if ext_id in self._all_ext_ids:
                self._all_ext_ids.remove(ext_id)
                self._all_ext_ids_arr = np.array(self._all_ext_ids, dtype=np.uint64) if self._all_ext_ids else None

            return True
        except Exception as exc:
            logger.warning("Failed to remove vector %s: %s", chunk_id, exc)
            return False

    async def get_stats(self) -> dict[str, Any]:
        """Get index statistics."""
        if not self._loaded or self._index is None:
            return {"loaded": False, "size": 0}

        return {
            "loaded": True,
            "size": len(self._index),
            "dim": self._index.dim,
            "bit_width": self._index.bit_width,
            "metadata_count": len(self._meta_by_ext),
        }


# Singleton instance
vector_index = VectorIndex()
