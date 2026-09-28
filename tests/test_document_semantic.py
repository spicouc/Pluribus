"""L3 regression tests: semantic document vector index + embeddings.

These tests exercise the complete L3 document-semantic pipeline against the real
FastAPI app (httpx.ASGITransport over ``main.app``) plus direct calls to the L3
library indexer and the independent ``DocumentVectorIndex``:

* embedding lifecycle pending -> ready (and error/retryable on failure),
* Ollama-down degradation (ingest/FTS/API keep working, semantic degrades,
  no corruption),
* zero / wrong-dim / NaN vectors are never committed,
* embedding reuse keyed on (chunk_sha, model, dim) — incl. model-change safety,
* new-version / soft-delete exclusion, scope isolation, rebuild-from-SQLite,
* restart persistence of ready state,
* document vs. fact generation independence (L3-12 / L3-13),
* bounded idempotent retries.

Ollama is simulated by patching ``embedding_service.get_embedding`` (sync) and
``embedding_service.check_ready`` (async) so no external Ollama is needed. Facts
and the facts TurboVec index are never touched except to prove independence.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
import httpx

from pluribus.config import settings
from pluribus.db import get_db, init_db
from pluribus.embedding import embedding_service
from pluribus.document_chunks import chunk_sha
from pluribus.document_vector_index import DocumentVectorIndex
from pluribus.library_indexer import run_library_indexer, reset_to_pending
import pluribus.main as main
import pluribus.security as security

EMBED = "<a zebra reference>"


def make_agent(
    agent_id: str,
    *,
    read: bool = True,
    write: bool = True,
    delete: bool = True,
    admin: bool = False,
    scopes: list[str] | None = None,
) -> dict:
    return {
        "id": agent_id,
        "name": agent_id,
        "permissions": {"read": read, "write": write, "delete": delete, "admin": admin},
        "allowed_scopes": scopes or ["shared"],
    }


HANDLER_TEXT = "La zebra galopa per la sabana."
# A single logical Markdown block: one heading + one body paragraph. The L2
# Markdown-aware chunker emits one chunk per heading-block, so this content
# yields EXACTLY ONE chunk (the semantic tests reason in "1 doc -> 1 chunk ->
# 1 vector" terms). (A version with two headings would produce two chunks and
# would not match the fixtures' 1-chunk assumption.)
SIMPLE_CONTENT = (
    "# Etologia\n\n"
    f"{HANDLER_TEXT}\n"
    "La zebra viu a les sabanes bafades de sol."
)


def vec(coordinate: int = 0) -> np.ndarray:
    v = np.zeros(settings.EMBED_DIM, dtype=np.float32)
    v[coordinate] = 1.0
    return v


class DocumentSemanticIndexTests(unittest.IsolatedAsyncioTestCase):
    """Harness + the 15 mandatory L3 tests."""

    async def asyncSetUp(self) -> None:
        security._bcrypt_cache.clear()
        security._legacy_scan_by_client.clear()
        security._legacy_scan_global.clear()

        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "document-semantic.db"
        self.settings_patch = patch.object(settings, "DB_PATH", str(self.db_path))
        self.settings_patch.start()
        await init_db()

        # A fresh, independent DocumentVectorIndex per test (never the module
        # singleton) so no state leaks between tests.
        self.index = DocumentVectorIndex()

        self.transport = httpx.ASGITransport(app=main.app)
        self.writer = make_agent("writer-1", scopes=["shared"])
        self.admin = make_agent("admin-1", admin=True, scopes=["shared", "private"])

        _all_agents = {"writer-1": self.writer, "admin-1": self.admin}
        async with get_db() as db:
            for agent_id, agent in _all_agents.items():
                await db.execute(
                    """INSERT INTO agents
                       (id, name, api_key_hash, permissions, allowed_scopes)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        agent_id,
                        agent["name"],
                        "unused-test-hash",
                        json.dumps(agent["permissions"]),
                        json.dumps(agent["allowed_scopes"]),
                    ),
                )
            await db.commit()

    async def asyncTearDown(self) -> None:
        self.settings_patch.stop()
        self.temp_dir.cleanup()

    async def request(self, agent, method, url, **kwargs) -> httpx.Response:
        with patch(
            "pluribus.security._authenticate_agent",
            new=AsyncMock(return_value=agent),
        ):
            async with httpx.AsyncClient(
                transport=self.transport,
                base_url="http://testserver",
                headers={"X-API-Key": "fake-key-that-is-long-enough"},
            ) as client:
                return await client.request(method, url, **kwargs)

    async def create_doc(
        self,
        *,
        title: str = "Zebra Handbook",
        content: str = SIMPLE_CONTENT,
        scope: str = "shared",
        agent=None,
    ) -> tuple[str, str]:
        resp = await self.request(
            agent or self.writer,
            "POST",
            "/v1/documents",
            json={
                "title": title,
                "content": content,
                "scope": scope,
                "category": "events",
                "tags": ["guide"],
                "description": "A zebra reference",
                "metadata": {"owner": "docs"},
            },
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()["id"], resp.json().get("current_version") or 1

    async def update_doc(self, document_id: str, content: str, agent=None) -> None:
        resp = await self.request(
            agent or self.writer,
            "PUT",
            f"/v1/documents/{document_id}",
            json={"content": content},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    async def delete_doc(self, document_id: str, agent=None) -> None:
        resp = await self.request(
            agent or self.writer,
            "DELETE",
            f"/v1/documents/{document_id}",
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    # ── DB helpers ─────────────────────────────────────────────────────
    async def fetch_chunks(self):
        """Return all document_chunks rows as a list of dicts."""
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT id, document_id, version_id, chunk_index, chunk_sha, "
                "chunk_text, embedding_state, embedding_model, embedding_dim, "
                "embedding_attempts, COALESCE(length(embedding_blob), 0) AS blob_len "
                "FROM document_chunks ORDER BY document_id, chunk_index"
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def doc_generation(self) -> int:
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT generation FROM document_vector_index_state WHERE singleton = 1"
            )
            row = await cursor.fetchone()
            return int(row["generation"]) if row else 0

    async def fact_generation(self) -> int:
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT generation FROM vector_index_state WHERE singleton = 1"
            )
            row = await cursor.fetchone()
            return int(row["generation"]) if row else 0

    async def insert_fact(self, fact_id: str, chunk_id: str, blob: bytes) -> None:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO facts(id, scope, category, content) "
                "VALUES (?, 'shared', 'events', ?)",
                (fact_id, fact_id),
            )
            await db.execute(
                "INSERT INTO chunks(id, fact_id, chunk_text, embedding_blob) "
                "VALUES (?, ?, ?, ?)",
                (chunk_id, fact_id, chunk_id, blob),
            )
            await db.commit()

    # ── Ollama mocking helpers ─────────────────────────────────────────
    def _ollama_ok(self, vector=None, ready=True):
        """Return context managers that simulate a healthy (or degraded) Ollama."""
        getter = Mock(side_effect=lambda _text: (vector if vector is not None else vec(0)))
        ready_mock = AsyncMock(return_value=ready)
        return (
            patch.object(embedding_service, "get_embedding", getter),
            patch.object(embedding_service, "check_ready", ready_mock),
        )

    async def _run(self, vector=None, ready=True):
        cm1, cm2 = self._ollama_ok(vector=vector, ready=ready)
        async with get_db() as db:
            with cm1, cm2:
                return await run_library_indexer(db)

    # ── L3-01: pending -> ready ────────────────────────────────────────
    async def test_l301_pending_to_ready(self) -> None:
        doc_id, version = await self.create_doc()
        chunks = await self.fetch_chunks()
        self.assertTrue(chunks)
        for c in chunks:
            self.assertEqual(c["embedding_state"], "pending")
            self.assertEqual(c["blob_len"], 0)

        stats = await self._run(vector=vec(0))
        self.assertGreaterEqual(stats["ready"], 1)
        self.assertEqual(stats["failed"], 0)
        self.assertFalse(stats["ollama_down"])

        chunks = await self.fetch_chunks()
        self.assertEqual(len(chunks), 1)
        c = chunks[0]
        self.assertEqual(c["embedding_state"], "ready")
        self.assertEqual(c["embedding_model"], settings.OLLAMA_MODEL)
        self.assertEqual(c["embedding_dim"], settings.EMBED_DIM)
        self.assertEqual(c["blob_len"], settings.EMBED_DIM * 4)

        # The independent DocumentVectorIndex sees exactly the one ready chunk.
        stats_idx = await self.index.get_stats()
        self.assertEqual(stats_idx["size"], 1)

    # ── L3-02: Ollama down -> FTS continues, semantic degraded ────────
    async def test_l302_ollama_down_degrades_but_never_corrupts(self) -> None:
        doc_id, version = await self.create_doc()
        chunks_before = await self.fetch_chunks()
        self.assertTrue(chunks_before)

        # Ingest+FTS+API already passed (create succeeded). Simulate Ollama
        # being down and run the indexer.
        stats = await self._run(ready=False)
        self.assertTrue(stats["ollama_down"])
        self.assertEqual(stats["ready"], 0)

        # Semantic is degraded: nothing was committed, chunk is retryable.
        chunks = await self.fetch_chunks()
        for c in chunks:
            self.assertIn(c["embedding_state"], ("retryable", "pending"))
            self.assertEqual(c["blob_len"], 0)  # never a zero vector committed
        idx_stats = await self.index.get_stats()
        self.assertEqual(idx_stats["size"], 0)  # degraded: no vectors indexed

        # Ingest + FTS + API are unharmed and document still retrievable.
        resp = await self.request(self.writer, "GET", f"/v1/documents/{doc_id}")
        self.assertEqual(resp.status_code, 200)
        search = await self.request(
            self.writer, "GET", "/v1/documents/search", params={"q": "zebra", "scope": "shared"}
        )
        self.assertEqual(search.status_code, 200)
        body = search.json()
        self.assertGreaterEqual(
            body["total"], 1, "FTS must keep working while Ollama is down"
        )
        self.assertEqual(body["items"][0]["document_id"], doc_id)

        # No corruption: chunk text intact.
        self.assertIn(HANDLER_TEXT, chunks[0]["chunk_text"])

    # ── L3-03: zero vector is never committed ─────────────────────────
    async def test_l303_zero_vector_excluded(self) -> None:
        await self.create_doc()
        stats = await self._run(vector=np.zeros(settings.EMBED_DIM, dtype=np.float32))
        self.assertEqual(stats["ready"], 0)
        chunks = await self.fetch_chunks()
        for c in chunks:
            self.assertNotEqual(c["embedding_state"], "ready")
            self.assertEqual(c["blob_len"], 0)
        idx_stats = await self.index.get_stats()
        self.assertEqual(idx_stats["size"], 0)

    # ── L3-04: invalid dimension excluded ─────────────────────────────
    async def test_l304_invalid_dimension_excluded(self) -> None:
        await self.create_doc()
        wrong = np.zeros(8, dtype=np.float32)
        wrong[0] = 1.0
        stats = await self._run(vector=wrong)
        self.assertEqual(stats["ready"], 0)
        chunks = await self.fetch_chunks()
        for c in chunks:
            self.assertNotEqual(c["embedding_state"], "ready")
            self.assertEqual(c["blob_len"], 0)
        self.assertEqual((await self.index.get_stats())["size"], 0)

    # ── L3-05: NaN/Inf excluded ───────────────────────────────────────
    async def test_l305_nan_inf_excluded(self) -> None:
        await self.create_doc()
        nan_vec = np.full(settings.EMBED_DIM, np.nan, dtype=np.float32)
        stats = await self._run(vector=nan_vec)
        self.assertEqual(stats["ready"], 0)
        chunks = await self.fetch_chunks()
        for c in chunks:
            self.assertNotEqual(c["embedding_state"], "ready")
            self.assertEqual(c["blob_len"], 0)

        # Inf variant.
        inf_vec = np.full(settings.EMBED_DIM, np.inf, dtype=np.float32)
        stats = await self._run(vector=inf_vec)
        self.assertEqual(stats["ready"], 0)
        chunks = await self.fetch_chunks()
        for c in chunks:
            self.assertNotEqual(c["embedding_state"], "ready")
        self.assertEqual((await self.index.get_stats())["size"], 0)

    # ── L3-06: identical chunk reuse (same sha) ───────────────────────
    async def test_l306_identical_chunk_reuse(self) -> None:
        await self.create_doc(title="Doc A")
        await self.create_doc(title="Doc B")
        chunks = await self.fetch_chunks()
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0]["chunk_sha"], chunks[1]["chunk_sha"])

        stats = await self._run(vector=vec(3))
        self.assertEqual(stats["ready"], 2)
        self.assertGreaterEqual(stats["reused"], 1)  # second chunk reused

        after = await self.fetch_chunks()
        blobs = {c["document_id"]: c["blob_len"] for c in after}
        self.assertEqual(set(blobs.values()), {settings.EMBED_DIM * 4})
        # Same content -> identical vector blob stored in both rows.
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT embedding_blob FROM document_chunks ORDER BY document_id"
            )
            rows = await cursor.fetchall()
        self.assertEqual(rows[0]["embedding_blob"], rows[1]["embedding_blob"])

    # ── L3-07: new current version excludes previous ──────────────────
    async def test_l307_new_current_version_excludes_previous(self) -> None:
        doc_id, v1 = await self.create_doc()
        await self._run(vector=vec(0))
        stats_v1 = await self.index.get_stats()
        self.assertEqual(stats_v1["size"], 1)
        # Capture the single v1 chunk id.
        self.assertEqual(len(self.index._meta), 1)
        old_chunk_id = self.index._meta[0]["chunk_id"]

        # New version with different content.
        await self.update_doc(doc_id, "# Nova\n\nLa zebra ara viu a les muntanyes.")
        await self._run(vector=vec(5))
        chunks = await self.fetch_chunks()
        # document_chunks keeps historical rows per version (provenance), so
        # the table holds the stale v1 row plus the new v2 row. What matters
        # for the semantic index is that the CURRENT version replaces the
        # previous one (asserted below on self.index after a fresh rebuild).
        self.assertEqual(len(chunks), 2)  # stale v1 row + current v2 row
        self.assertEqual(chunks[0]["document_id"], doc_id)

        # Fresh rebuild from SQLite: index must only contain the new version.
        self.index.invalidate()
        await self.index.ensure_loaded()
        metas = self.index._meta
        self.assertEqual(len(metas), 1)
        self.assertNotEqual(metas[0]["chunk_id"], old_chunk_id)

    # ── L3-08: soft-delete excludes vectors ───────────────────────────
    async def test_l308_soft_delete_excludes_vectors(self) -> None:
        doc_id, _ = await self.create_doc()
        await self._run(vector=vec(0))
        self.assertEqual((await self.index.get_stats())["size"], 1)

        await self.delete_doc(doc_id)
        self.index.invalidate()
        stats = await self.index.get_stats()
        self.assertEqual(stats["size"], 0)

        # History preserved: chunk rows still exist but are excluded by the
        # live-document filter in the index scan.
        chunks = await self.fetch_chunks()
        self.assertEqual(len(chunks), 1)

    # ── L3-09: scope isolation ────────────────────────────────────────
    async def test_l309_scope_isolation(self) -> None:
        await self.create_doc(title="Public", content=SIMPLE_CONTENT, scope="shared")
        await self.create_doc(
            title="Private",
            content="# Confidencial\n\nsecret de la companyia",
            scope="private",
            agent=self.admin,
        )
        await self._run(vector=vec(1))
        stats = await self.index.get_stats()
        self.assertEqual(stats["size"], 2)

        # Search scoped to private only returns the private chunk.
        priv = await self.index.search(vec(1), scope_filter="private", top_k=5)
        self.assertEqual(len(priv), 1)
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT c.id AS cid FROM document_chunks c "
                "JOIN documents d ON d.id=c.document_id WHERE d.scope='private'"
            )
            priv_cid = (await cursor.fetchone())["cid"]
        self.assertEqual(priv[0][0], priv_cid)

        shared = await self.index.search(vec(1), scope_filter="shared", top_k=5)
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT c.id AS cid FROM document_chunks c "
                "JOIN documents d ON d.id=c.document_id WHERE d.scope='shared'"
            )
            shared_cid = (await cursor.fetchone())["cid"]
        self.assertEqual([x[0] for x in shared], [shared_cid])

    # ── L3-10: rebuild from SQLite ────────────────────────────────────
    async def test_l310_rebuild_from_sqlite(self) -> None:
        await self.create_doc()
        await self._run(vector=vec(2))
        self.assertEqual((await self.index.get_stats())["size"], 1)

        # A brand-new instance rebuilds purely from SQLite state.
        fresh = DocumentVectorIndex()
        await fresh.rebuild()
        self.assertEqual((await fresh.get_stats())["size"], 1)
        m = fresh._meta[0]
        self.assertIn("chunk_id", m)
        self.assertEqual(m["filename"], "Zebra Handbook")
        self.assertEqual(m["scope"], "shared")
        self.assertGreaterEqual(m["line_end"], m["line_start"])

    # ── L3-11: ready state persists across re-init ────────────────────
    async def test_l311_restart_persistence(self) -> None:
        doc_id, _ = await self.create_doc()
        await self._run(vector=vec(0))
        before = await self.fetch_chunks()
        self.assertEqual(before[0]["embedding_state"], "ready")

        # Simulate service restart: re-run init_db on the same file + a fresh
        # index instance + fresh indexer run.
        await init_db()
        fresh = DocumentVectorIndex()
        await fresh.ensure_loaded()
        self.assertEqual((await fresh.get_stats())["size"], 1)

        # ready state persisted; a re-run is idempotent (nothing left pending).
        stats = await self._run(vector=vec(0))
        self.assertEqual(stats["processed"], 0)
        after = await self.fetch_chunks()
        self.assertEqual(after[0]["embedding_state"], "ready")
        self.assertEqual(after[0]["blob_len"], before[0]["blob_len"])

    # ── L3-12: document mutation does not alter fact generation ───────
    async def test_l312_doc_mutation_does_not_bump_fact_generation(self) -> None:
        fact_before = await self.fact_generation()
        doc_before = await self.doc_generation()

        await self.create_doc()

        fact_after = await self.fact_generation()
        doc_after = await self.doc_generation()
        self.assertEqual(fact_after, fact_before, "facts generation must not change")
        self.assertGreaterEqual(doc_after, doc_before)

    # ── L3-13: fact mutation does not alter document generation ───────
    async def test_l313_fact_mutation_does_not_bump_doc_generation(self) -> None:
        doc_before = await self.doc_generation()
        fact_before = await self.fact_generation()

        await self.insert_fact("fact-l3", "chunk-l3", vec(9).tobytes())

        doc_after = await self.doc_generation()
        fact_after = await self.fact_generation()
        self.assertEqual(
            doc_after, doc_before, "document generation must not change on fact writes"
        )
        self.assertGreaterEqual(fact_after, fact_before)

    # ── L3-14: bounded, idempotent retries ────────────────────────────
    async def test_l314_bounded_idempotent_retries(self) -> None:
        await self.create_doc()

        # Phase 1: Ollama repeatedly returns an invalid (zero) vector.
        for _ in range(3):
            stats = await self._run(vector=np.zeros(settings.EMBED_DIM, dtype=np.float32))
            self.assertEqual(stats["ready"], 0)
        chunks = await self.fetch_chunks()
        self.assertEqual(chunks[0]["embedding_state"], "error")
        self.assertGreaterEqual(chunks[0]["embedding_attempts"], 3)
        self.assertEqual(chunks[0]["blob_len"], 0)

        # idempotent: retries never fabricated a ready embedding.
        self.assertEqual((await self.index.get_stats())["size"], 0)

        # Phase 2: an operator restores Ollama and resets bounded state.
        async with get_db() as db:
            await reset_to_pending(db)
        stats = await self._run(vector=vec(0))
        self.assertEqual(stats["ready"], 1)
        chunks = await self.fetch_chunks()
        self.assertEqual(chunks[0]["embedding_state"], "ready")

        # Idempotent re-run: nothing left pending, blob unchanged.
        blob_before = chunks[0]["blob_len"]
        stats = await self._run(vector=vec(0))
        self.assertEqual(stats["processed"], 0)
        chunks = await self.fetch_chunks()
        self.assertEqual(chunks[0]["blob_len"], blob_before)

    # ── L3-15: model change does not reuse stale vectors ──────────────
    async def test_l315_model_change_does_not_reuse_stale_vectors(self) -> None:
        model_a = "nomic-embed-text-v2-moe:latest"
        model_b = "other-embedding-model:latest"
        with patch.object(settings, "OLLAMA_MODEL", model_a):
            await self.create_doc(title="Doc A", content=SIMPLE_CONTENT)
            await self._run(vector=vec(0))
        chunks = await self.fetch_chunks()
        self.assertEqual(chunks[0]["embedding_model"], model_a)

        # A second doc with IDENTICAL content, but the embedding model changed.
        with patch.object(settings, "OLLAMA_MODEL", model_b):
            await self.create_doc(title="Doc B", content=SIMPLE_CONTENT)
            await self._run(vector=vec(1))

        chunks = await self.fetch_chunks()
        self.assertEqual(len(chunks), 2)
        by_doc = {c["document_id"]: c for c in chunks}
        a_model_chunk = next(c for c in chunks if c["embedding_model"] == model_a)
        b_model_chunk = next(c for c in chunks if c["embedding_model"] == model_b)

        # Same content sha -> would be a cache hit, but the key includes the
        # model, so the model-B chunk must NOT reuse the model-A vector.
        self.assertEqual(b_model_chunk["embedding_model"], model_b)
        self.assertEqual(
            a_model_chunk["chunk_sha"], b_model_chunk["chunk_sha"],
            "identical content implies identical sha (the reuse-key baseline)",
        )
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT embedding_blob FROM document_chunks WHERE embedding_model=? ",
                (model_a,),
            )
            blob_a = (await cursor.fetchone())["embedding_blob"]
            cursor = await db.execute(
                "SELECT embedding_blob FROM document_chunks WHERE embedding_model=?",
                (model_b,),
            )
            blob_b = (await cursor.fetchone())["embedding_blob"]
        # vec(0) vs vec(1) -> different vectors, proving no stale reuse.
        self.assertNotEqual(blob_a, blob_b)
        v_b = np.frombuffer(blob_b, dtype=np.float32)
        self.assertEqual(int(v_b.argmax()), 1)


if __name__ == "__main__":
    unittest.main()
