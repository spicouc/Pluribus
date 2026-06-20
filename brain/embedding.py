"""Servei d'embeddings amb Ollama API, async-friendly amb cache."""

from __future__ import annotations

import hashlib
import time
from threading import Lock
from typing import Optional

import numpy as np
import requests

from brain.config import settings


class EmbeddingService:
    """Servei que genera embeddings via Ollama API, amb cache en memòria.

    Fa servir /api/embed d'Ollama amb el model configurat.
    Si Ollama no està disponible, retorna vectors buits i is_ready=False.
    """

    def __init__(self) -> None:
        self._cache: dict[str, np.ndarray] = {}
        self._cache_lock: Lock = Lock()
        self._ready: Optional[bool] = None
        self._last_check: float = 0
        self._check_interval: float = 60.0  # segons entre checks

    def _check_ollama(self) -> bool:
        """Verifica que Ollama respongui i tingui el model."""
        now = time.time()
        if self._ready is not None and (now - self._last_check) < self._check_interval:
            return self._ready

        try:
            resp = requests.get(
                f"{settings.OLLAMA_BASE_URL}/api/tags",
                timeout=5,
            )
            if resp.status_code != 200:
                self._ready = False
                return False

            models = resp.json().get("models", [])
            available = any(m["name"].startswith(settings.OLLAMA_MODEL.rsplit(":", 1)[0]) for m in models)
            self._ready = available
        except Exception:
            self._ready = False

        self._last_check = time.time()
        return self._ready

    @staticmethod
    def _normalize(v: np.ndarray) -> np.ndarray:
        """Normalitza L2 un vector."""
        norm = np.linalg.norm(v)
        if norm > 0:
            v = v / norm
        return v

    @staticmethod
    def _sha256(text: str) -> str:
        """Calcula el hash SHA256 d'un text normalitzat."""
        normalized = text.lower().strip()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def split_into_chunks(self, text: str) -> list[str]:
        """Divideix un text en fragments de mida màxima MAX_CHUNK_SIZE amb solapament."""
        if len(text) <= settings.MAX_CHUNK_SIZE:
            return [text]

        chunks: list[str] = []
        start = 0
        while start < len(text):
            end = start + settings.MAX_CHUNK_SIZE
            if end >= len(text):
                chunks.append(text[start:])
                break
            split_pos = text.rfind(" ", start + settings.MAX_CHUNK_SIZE - settings.CHUNK_OVERLAP, end)
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
        """Obté l'embedding per a un text, usant cache en memòria.

        Ollama no requereix prefix 'query:' o 'passage:' com E5.
        """
        full_text = f"{prefix}{text}" if prefix else text
        text_hash = self._sha256(full_text)

        # Comprova cache
        with self._cache_lock:
            cached = self._cache.get(text_hash)
            if cached is not None:
                return cached

        # Comprova disponibilitat d'Ollama
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

            # Desa a cache
            with self._cache_lock:
                self._cache[text_hash] = vec

            return vec
        except Exception:
            return np.zeros(settings.EMBED_DIM, dtype=np.float32)

    def get_embedding_batch(self, texts: list[str]) -> list[tuple[str, np.ndarray]]:
        """Obté embeddings per a múltiples textos en un sol call d'Ollama."""
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

    async def semantic_search(
        self,
        query_vec: np.ndarray,
        scope_filter: Optional[str] = None,
        agent_id_filter: Optional[str] = None,
        top_k: int = 5,
    ) -> list[tuple[str, float]]:
        """Cerca semàntica usant TurboVec index.

        Returns list of (chunk_id, score) tuples.
        Falls back to numpy dot products if TurboVec fails.
        """
        try:
            from brain.vector_index import vector_index
            results = await vector_index.search(
                query_vec,
                scope_filter=scope_filter,
                agent_id_filter=agent_id_filter,
                top_k=top_k,
            )
            if results:
                return results
        except Exception as exc:
            # Log and fall through to numpy fallback
            import logging
            logging.getLogger(__name__).warning(
                "TurboVec search failed, falling back to numpy: %s", exc
            )

        # Numpy fallback (legacy behavior) - requires chunks_with_ids parameter
        # This path is kept for backward compatibility but should not be used
        # when TurboVec is available
        return []

    def semantic_search_numpy(
        self,
        query_vec: np.ndarray,
        chunks_with_ids: list[tuple[str, np.ndarray]],
        top_k: int = 5,
    ) -> list[tuple[str, float]]:
        """Legacy numpy dot product search (kept as fallback).

        Tots els vectors han d'estar normalitzats L2.
        """
        if not chunks_with_ids:
            return []

        chunk_ids = [c[0] for c in chunks_with_ids]
        vectors = np.array([c[1] for c in chunks_with_ids], dtype=np.float32)

        scores = np.dot(vectors, query_vec)

        k = min(top_k, len(scores))
        if k == 0:
            return []

        top_indices = np.argpartition(scores, -k)[-k:]
        top_indices = top_indices[np.argsort(-scores[top_indices])]

        results = []
        for idx in top_indices:
            results.append((chunk_ids[idx], float(scores[idx])))

        return results

    @property
    def is_ready(self) -> bool:
        """Indica si Ollama està disponible i el model existeix."""
        return self._check_ollama()


embedding_service = EmbeddingService()
