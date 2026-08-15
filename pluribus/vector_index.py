"""TurboVec index derived from SQLite with generation-based invalidation."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
import struct
import time
from typing import Any, Optional

import numpy as np
from turbovec import IdMapIndex

from pluribus.config import settings

logger = logging.getLogger(__name__)


def _chunk_id_to_u64(chunk_id: str) -> int:
    """Convert a chunk ID to a deterministic uint64 external ID."""
    digest = hashlib.sha256(chunk_id.encode()).digest()
    return struct.unpack("<Q", digest[:8])[0]


class VectorIndex:
    """Derived vector index that treats SQLite as the source of truth.

    SQLite triggers increment ``vector_index_state.generation`` whenever chunks
    or fact metadata relevant to filtering changes. Before every search we
    compare that generation with the snapshot used to build this index.
    """

    def __init__(self) -> None:
        self._index: Optional[IdMapIndex] = None
        self._loaded = False
        self._building = False
        self._generation: int | None = None
        self._meta_by_ext: dict[int, dict[str, Any]] = {}
        self._ext_by_chunk: dict[str, int] = {}
        self._all_ext_ids: list[int] = []
        self._all_ext_ids_arr: Optional[np.ndarray] = None
        self._db_path: str | None = None

    def invalidate(self) -> None:
        """Force the next search to rebuild from SQLite."""
        self._generation = None

    def _read_generation_sync(self) -> int:
        conn = sqlite3.connect(settings.DB_PATH)
        try:
            row = conn.execute(
                "SELECT generation FROM vector_index_state WHERE singleton = 1"
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    async def ensure_loaded(self) -> bool:
        """Load/rebuild when the SQLite generation or database path changed."""
        current_db_path = str(settings.DB_PATH)
        current_generation = await asyncio.to_thread(self._read_generation_sync)
        if (
            self._loaded
            and self._index is not None
            and self._generation == current_generation
            and self._db_path == current_db_path
        ):
            return True

        if self._building:
            for _ in range(100):
                await asyncio.sleep(0.1)
                if not self._building:
                    break
            current_generation = await asyncio.to_thread(self._read_generation_sync)
            if (
                self._loaded
                and self._index is not None
                and self._generation == current_generation
                and self._db_path == current_db_path
            ):
                return True

        return await self.rebuild()

    async def rebuild(self) -> bool:
        if self._building:
            return False
        self._building = True
        try:
            t0 = time.time()
            count = await asyncio.to_thread(self._rebuild_sync)
            logger.info(
                "TurboVec index rebuilt: %d chunks generation=%s in %.2fs",
                count,
                self._generation,
                time.time() - t0,
            )
            return self._loaded and self._index is not None
        finally:
            self._building = False

    def _set_empty_index(self, generation: int, db_path: str) -> None:
        self._index = IdMapIndex(dim=settings.EMBED_DIM, bit_width=4)
        self._meta_by_ext = {}
        self._ext_by_chunk = {}
        self._all_ext_ids = []
        self._all_ext_ids_arr = None
        self._generation = generation
        self._db_path = db_path
        self._loaded = True

    def _rebuild_sync(self) -> int:
        """Build from one consistent SQLite read snapshot."""
        db_path = str(settings.DB_PATH)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN")
            generation_row = conn.execute(
                "SELECT generation FROM vector_index_state WHERE singleton = 1"
            ).fetchone()
            generation = int(generation_row["generation"]) if generation_row else 0

            rows = conn.execute(
                """SELECT c.id AS chunk_id, c.fact_id, c.embedding_blob,
                          f.scope, f.category, f.agent_id, f.key
                   FROM chunks c
                   JOIN facts f ON c.fact_id = f.id
                   WHERE f.deleted_at IS NULL
                     AND c.embedding_blob IS NOT NULL"""
            ).fetchall()

            chunk_ids: list[str] = []
            vectors: list[np.ndarray] = []
            metadata: list[dict[str, Any]] = []

            for row in rows:
                blob = row["embedding_blob"]
                if blob is None or len(blob) != settings.EMBED_DIM * 4:
                    continue
                vec = np.frombuffer(blob, dtype=np.float32)
                if len(vec) != settings.EMBED_DIM or not np.all(np.isfinite(vec)):
                    continue
                norm = float(np.linalg.norm(vec))
                if norm <= 0:
                    continue
                vec = vec / norm
                chunk_ids.append(row["chunk_id"])
                vectors.append(vec)
                metadata.append(
                    {
                        "chunk_id": row["chunk_id"],
                        "fact_id": row["fact_id"],
                        "scope": row["scope"],
                        "category": row["category"],
                        "agent_id": row["agent_id"],
                        "key": row["key"],
                    }
                )

            if not vectors:
                self._set_empty_index(generation, db_path)
                return 0

            vectors_arr = np.asarray(vectors, dtype=np.float32)
            ext_ids_arr = np.asarray(
                [_chunk_id_to_u64(cid) for cid in chunk_ids], dtype=np.uint64
            )
            index = IdMapIndex(dim=settings.EMBED_DIM, bit_width=4)
            index.add_with_ids(vectors_arr, ext_ids_arr)
            index.prepare()

            meta_by_ext: dict[int, dict[str, Any]] = {}
            ext_by_chunk: dict[str, int] = {}
            all_ext_ids: list[int] = []
            for i, cid in enumerate(chunk_ids):
                ext_id = int(ext_ids_arr[i])
                meta_by_ext[ext_id] = metadata[i]
                ext_by_chunk[cid] = ext_id
                all_ext_ids.append(ext_id)

            self._index = index
            self._meta_by_ext = meta_by_ext
            self._ext_by_chunk = ext_by_chunk
            self._all_ext_ids = all_ext_ids
            self._all_ext_ids_arr = np.asarray(all_ext_ids, dtype=np.uint64)
            self._generation = generation
            self._db_path = db_path
            self._loaded = True
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
        scope_filters: Optional[list[str]] = None,
    ) -> list[tuple[str, float]]:
        """Search the current index with optional single- or multi-scope filters.

        ``scope_filter`` is retained for backward compatibility. If both forms
        are supplied they are intersected, which fails closed on disagreement.
        """
        if not await self.ensure_loaded():
            return []
        if self._index is None or len(self._index) == 0:
            return []

        snapshot_generation = self._generation
        results = await asyncio.to_thread(
            self._search_sync,
            query_vec,
            scope_filter,
            category_filter,
            agent_id_filter,
            top_k,
            scope_filters,
        )

        latest_generation = await asyncio.to_thread(self._read_generation_sync)
        if latest_generation != snapshot_generation:
            if not await self.rebuild():
                return []
            if self._index is None or len(self._index) == 0:
                return []
            results = await asyncio.to_thread(
                self._search_sync,
                query_vec,
                scope_filter,
                category_filter,
                agent_id_filter,
                top_k,
                scope_filters,
            )
        return results

    def _search_sync(
        self,
        query_vec: np.ndarray,
        scope_filter: Optional[str],
        category_filter: Optional[str],
        agent_id_filter: Optional[str],
        top_k: int,
        scope_filters: Optional[list[str]] = None,
    ) -> list[tuple[str, float]]:
        if self._index is None or self._all_ext_ids_arr is None:
            return []
        if query_vec.ndim != 1 or len(query_vec) != settings.EMBED_DIM:
            return []
        if not np.all(np.isfinite(query_vec)) or float(np.linalg.norm(query_vec)) <= 0:
            return []

        allowlist = self._build_allowlist(
            scope_filter,
            category_filter,
            agent_id_filter,
            scope_filters=scope_filters,
        )
        if allowlist is not None and len(allowlist) == 0:
            return []

        q = query_vec.reshape(1, -1).astype(np.float32)
        k = min(top_k, len(self._index))
        if allowlist is not None:
            k = min(k, len(allowlist))
        if k <= 0:
            return []

        try:
            scores, ids = self._index.search(q, k, allowlist=allowlist)
        except Exception as exc:
            logger.warning("TurboVec search failed: %s", exc)
            return []

        results: list[tuple[str, float]] = []
        for score_val, ext_id in zip(scores[0], ids[0]):
            meta = self._meta_by_ext.get(int(ext_id))
            if meta:
                results.append((meta["chunk_id"], float(score_val)))
        return results

    def _build_allowlist(
        self,
        scope_filter: Optional[str],
        category_filter: Optional[str],
        agent_id_filter: Optional[str],
        scope_filters: Optional[list[str]] = None,
    ) -> Optional[np.ndarray]:
        allowed_scopes: set[str] | None = None
        if scope_filters is not None:
            allowed_scopes = {scope for scope in scope_filters if isinstance(scope, str)}
        if scope_filter is not None:
            if allowed_scopes is None:
                allowed_scopes = {scope_filter}
            else:
                allowed_scopes &= {scope_filter}

        if (
            allowed_scopes is None
            and category_filter is None
            and agent_id_filter is None
        ):
            return self._all_ext_ids_arr

        if allowed_scopes is not None and not allowed_scopes:
            return np.asarray([], dtype=np.uint64)

        filtered: list[int] = []
        for ext_id in self._all_ext_ids:
            meta = self._meta_by_ext.get(ext_id)
            if meta is None:
                continue
            if allowed_scopes is not None and meta.get("scope") not in allowed_scopes:
                continue
            if category_filter is not None and meta.get("category") != category_filter:
                continue
            if agent_id_filter is not None and meta.get("agent_id") != agent_id_filter:
                continue
            filtered.append(ext_id)
        return np.asarray(filtered, dtype=np.uint64)

    async def add_vectors(
        self,
        chunk_ids: list[str],
        vectors: list[np.ndarray],
        metadata_list: list[dict[str, Any]],
    ) -> None:
        """Compatibility hook: SQLite is authoritative, so invalidate only."""
        self.invalidate()

    async def remove_vector(self, chunk_id: str) -> bool:
        """Compatibility hook: invalidate and let the next search rebuild."""
        self.invalidate()
        return True

    async def get_stats(self) -> dict[str, Any]:
        await self.ensure_loaded()
        return {
            "loaded": self._loaded,
            "size": len(self._index) if self._index is not None else 0,
            "dim": self._index.dim if self._index is not None else settings.EMBED_DIM,
            "bit_width": self._index.bit_width if self._index is not None else 4,
            "metadata_count": len(self._meta_by_ext),
            "generation": self._generation,
        }


vector_index = VectorIndex()
