"""Servei d'embeddings amb Ollama API, cache i checks async-friendly."""

from __future__ import annotations

import asyncio
import hashlib
import time
from threading import Lock
from typing import Optional

import numpy as np
import requests

from pluribus.config import settings


class EmbeddingService:
    """Servei que genera embeddings via Ollama API, amb cache en memòria."""

    def __init__(self) -> None:
        self._cache: dict[str, np.ndarray] = {}
        self._cache_lock: Lock = Lock()
        self._ready: Optional[bool] = None
        self._last_check: float = 0
        self._check_interval: float = 60.0

    def _check_ollama(self, force: bool = False) -> bool:
        """Comprovació síncrona d'Ollama; cridar en thread des de codi async."""
        now = time.time()
        if (
            not force
            and self._ready is not None
            and (now - self._last_check) < self._check_interval
        ):
            return self._ready

        try:
            resp = requests.get(
                f"{settings.OLLAMA_BASE_URL}/api/tags",
                timeout=5,
            )
            if resp.status_code != 200:
                self._ready = False
            else:
                models = resp.json().get("models", [])
                expected = settings.OLLAMA_MODEL.rsplit(":", 1)[0]
                self._ready = any(
                    isinstance(m, dict)
                    and isinstance(m.get("name"), str)
                    and m["name"].startswith(expected)
                    for m in models
                )
        except Exception:
            self._ready = False

        self._last_check = time.time()
        return bool(self._ready)

    async def check_ready(self, force: bool = False) -> bool:
        """Comprova Ollama sense bloquejar l'event loop."""
        return await asyncio.to_thread(self._check_ollama, force)

    @staticmethod
    def _normalize(v: np.ndarray) -> np.ndarray:
        norm = np.linalg.norm(v)
        if norm > 0:
            v = v / norm
        return v

    @staticmethod
    def _sha256(text: str) -> str:
        normalized = text.lower().strip()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def split_into_chunks(self, text: str) -> list[str]:
        if len(text) <= settings.MAX_CHUNK_SIZE:
            return [text]

        chunks: list[str] = []
        start = 0
        while start < len(text):
            end = start + settings.MAX_CHUNK_SIZE
            if end >= len(text):
                chunks.append(text[start:])
                break
            split_pos = text.rfind(
                " ",
                start + settings.MAX_CHUNK_SIZE - settings.CHUNK_OVERLAP,
                end,
            )
            if split_pos > start:
                end = split_pos
            chunk = text[start:end]
            if chunk.strip():
                chunks.append(chunk)
            start = end - settings.CHUNK_OVERLAP if end > settings.CHUNK_OVERLAP else end
            if start >= len(text):
                break
        return chunks

    def get_embedding(self, text: str, prefix: str = "") -> np.ndarray:
        """Obté un embedding. Aquesta API és síncrona per compatibilitat legacy."""
        full_text = f"{prefix}{text}" if prefix else text
        text_hash = self._sha256(full_text)

        with self._cache_lock:
            cached = self._cache.get(text_hash)
            if cached is not None:
                return cached

        if not self._check_ollama():
            return np.zeros(settings.EMBED_DIM, dtype=np.float32)

        try:
            resp = requests.post(
                f"{settings.OLLAMA_BASE_URL}/api/embed",
                json={"model": settings.OLLAMA_MODEL, "input": full_text},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            vec = np.array(data["embeddings"][0], dtype=np.float32)
            if len(vec.shape) > 1:
                vec = vec.flatten()
            vec = self._normalize(vec)
            with self._cache_lock:
                self._cache[text_hash] = vec
            return vec
        except Exception:
            return np.zeros(settings.EMBED_DIM, dtype=np.float32)

    async def get_embedding_async(self, text: str, prefix: str = "") -> np.ndarray:
        """Obté un embedding sense bloquejar l'event loop."""
        return await asyncio.to_thread(self.get_embedding, text, prefix)

    def get_embedding_batch(self, texts: list[str]) -> list[tuple[str, np.ndarray]]:
        if not texts:
            return []
        if not self._check_ollama():
            return [(t, np.zeros(settings.EMBED_DIM, dtype=np.float32)) for t in texts]

        uncached: list[str] = []
        result: list[tuple[str, np.ndarray]] = []
        with self._cache_lock:
            for t in texts:
                h = self._sha256(t)
                cached = self._cache.get(h)
                if cached is not None:
                    result.append((t, cached))
                else:
                    uncached.append(t)

        if uncached:
            try:
                resp = requests.post(
                    f"{settings.OLLAMA_BASE_URL}/api/embed",
                    json={"model": settings.OLLAMA_MODEL, "input": uncached},
                    timeout=60,
                )
                resp.raise_for_status()
                data = resp.json()
                with self._cache_lock:
                    for i, t in enumerate(uncached):
                        vec = np.array(data["embeddings"][i], dtype=np.float32)
                        vec = self._normalize(vec)
                        self._cache[self._sha256(t)] = vec
                        result.append((t, vec))
            except Exception:
                for t in uncached:
                    result.append((t, np.zeros(settings.EMBED_DIM, dtype=np.float32)))
        return result

    def semantic_search(
        self,
        query_vec: np.ndarray,
        chunks_with_ids: list[tuple[str, np.ndarray]],
        top_k: int = 5,
    ) -> list[tuple[str, float]]:
        return self.semantic_search_numpy(query_vec, chunks_with_ids, top_k)

    async def semantic_search_index(
        self,
        query_vec: np.ndarray,
        scope_filter: Optional[str] = None,
        category_filter: Optional[str] = None,
        agent_id_filter: Optional[str] = None,
        top_k: int = 5,
    ) -> list[tuple[str, float]]:
        try:
            from pluribus.vector_index import vector_index

            return await vector_index.search(
                query_vec,
                scope_filter=scope_filter,
                category_filter=category_filter,
                agent_id_filter=agent_id_filter,
                top_k=top_k,
            )
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning("TurboVec search failed: %s", exc)
            return []

    def semantic_search_numpy(
        self,
        query_vec: np.ndarray,
        chunks_with_ids: list[tuple[str, np.ndarray]],
        top_k: int = 5,
    ) -> list[tuple[str, float]]:
        if not chunks_with_ids:
            return []
        if query_vec.ndim != 1 or not np.all(np.isfinite(query_vec)):
            return []
        query_norm = float(np.linalg.norm(query_vec))
        if query_norm <= 0:
            return []

        valid_chunks = [
            (chunk_id, vec)
            for chunk_id, vec in chunks_with_ids
            if vec.ndim == 1
            and len(vec) == len(query_vec)
            and np.all(np.isfinite(vec))
            and float(np.linalg.norm(vec)) > 0
        ]
        if not valid_chunks:
            return []

        chunk_ids = [c[0] for c in valid_chunks]
        vectors = np.array([c[1] for c in valid_chunks], dtype=np.float32)
        scores = np.dot(vectors, query_vec)
        k = min(top_k, len(scores))
        if k == 0:
            return []
        top_indices = np.argpartition(scores, -k)[-k:]
        top_indices = top_indices[np.argsort(-scores[top_indices])]
        return [(chunk_ids[idx], float(scores[idx])) for idx in top_indices]

    @property
    def is_ready(self) -> bool:
        """Retorna només l'últim estat conegut; mai fa I/O de xarxa."""
        return bool(self._ready)


embedding_service = EmbeddingService()
