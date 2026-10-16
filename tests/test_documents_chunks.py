"""L2 regression tests: Markdown-aware chunking + FTS full-text search.

These tests exercise the real FastAPI app through ``httpx.ASGITransport`` over
``main.app`` (the same harness as ``test_documents_crud.py``) plus direct unit
tests for the ``chunk_markdown`` splitting logic. They cover:

- Markdown chunking: heading-aware split, blank-line paragraph fallback.
- Chunk generation on create and re-chunking on content update (new version).
- ``documents_fts`` population and FTS exact-phrase search over chunks.
- Search filters (scope), auth (401), out-of-scope (403) and pagination.
- The chunk→document ``GET /{id}/chunks`` reverse lookup.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from pluribus.config import settings
from pluribus.db import get_db, init_db
from pluribus.document_chunks import chunk_markdown
import pluribus.main as main
import pluribus.security as security


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


class DocumentChunkingUnitTests(unittest.TestCase):
    """Direct unit tests for the Markdown-aware chunker."""

    def test_heading_document_produces_multiple_chunks_with_sections(self) -> None:
        content = (
            "# Getting Started\n\n"
            "Welcome to the handbook.\n\n"
            "## Installation\n\n"
            "Run the installer command.\n\n"
            "## Configuration\n\n"
            "Edit the config file.\n"
        )
        chunks = chunk_markdown(content)
        self.assertGreaterEqual(len(chunks), 3)
        sections = [c.section for c in chunks]
        self.assertIn("Getting Started", sections)
        self.assertIn("Installation", sections)
        self.assertIn("Configuration", sections)
        # Each chunk carries its own body and a stable index.
        for idx, chunk in enumerate(chunks):
            self.assertEqual(chunk.chunk_index, idx)
            self.assertTrue(chunk.chunk_text.strip())

    def test_paragraph_only_document_falls_back_to_paragraphs(self) -> None:
        content = "First paragraph only.\n\nSecond paragraph about zebras.\n\nThird one."
        chunks = chunk_markdown(content)
        self.assertGreaterEqual(len(chunks), 1)
        joined = " ".join(c.chunk_text for c in chunks)
        self.assertIn("zebras", joined)

    def test_empty_content_produces_no_chunks(self) -> None:
        self.assertEqual(chunk_markdown(""), [])

    def test_degenerate_single_paragraph_still_produces_one_chunk(self) -> None:
        content = "One long paragraph with no blank lines whatsoever anywhere in it."
        chunks = chunk_markdown(content)
        self.assertGreaterEqual(len(chunks), 1)


class DocumentLibraryChunksTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        security._bcrypt_cache.clear()
        security._legacy_scan_by_client.clear()
        security._legacy_scan_global.clear()

        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "documents-chunks.db"
        self.settings_patch = patch.object(settings, "DB_PATH", str(self.db_path))
        self.settings_patch.start()
        await init_db()

        self.transport = httpx.ASGITransport(app=main.app)

        self.admin = make_agent("admin-1", admin=True, scopes=["shared", "private"])
        self.writer = make_agent("writer-1", scopes=["shared"])
        self.reader = make_agent("reader-1", read=True, write=False, delete=False)
        self.no_read = make_agent("noread-1", read=False, write=False, delete=False)
        self.private_only = make_agent("priv-1", scopes=["private"])

        _all_agents = {
            "admin-1": self.admin,
            "writer-1": self.writer,
            "reader-1": self.reader,
            "noread-1": self.no_read,
            "priv-1": self.private_only,
        }
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

    async def request(
        self,
        agent: dict | None,
        method: str,
        url: str,
        **kwargs,
    ) -> httpx.Response:
        if agent is None:
            async with httpx.AsyncClient(transport=self.transport, base_url="http://testserver") as client:
                return await client.request(method, url, **kwargs)
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
        content: str = (
            "# Zebra Handbook\n\n"
            "The zebra is the reference animal of this library.\n\n"
            "## Habitats\n\nThe unique zebra protocol lives in savannah regions.\n\n"
            "## Diet\n\nZebras graze on grassland.\n"
        ),
        scope: str = "shared",
        category: str = "events",
        tags: list[str] | None = None,
        agent: dict | None = None,
    ) -> httpx.Response:
        return await self.request(
            agent or self.writer,
            "POST",
            "/v1/documents",
            json={
                "title": title,
                "content": content,
                "scope": scope,
                "category": category,
                "tags": tags if tags is not None else ["guide", "animals"],
                "description": "A zebra reference",
                "metadata": {"owner": "docs"},
            },
        )

    async def chunk_counts(self) -> tuple[int, int]:
        async with get_db() as db:
            cursor = await db.execute("SELECT COUNT(*) AS total FROM document_chunks")
            rows = (await cursor.fetchone())["total"]
            cursor = await db.execute("SELECT COUNT(*) AS total FROM documents_fts")
            fts = (await cursor.fetchone())["total"]
        return rows, fts

    # ── Chunk generation on create ────────────────────────────────────
    async def test_create_generates_chunks_and_fts_rows(self) -> None:
        create = await self.create_doc()
        self.assertEqual(create.status_code, 201)
        document_id = create.json()["id"]

        async with get_db() as db:
            cursor = await db.execute(
                "SELECT chunk_index, section, chunk_text, version_id, embedding_blob "
                "FROM document_chunks WHERE document_id = ? ORDER BY chunk_index",
                (document_id,),
            )
            chunk_rows = await cursor.fetchall()
            cursor = await db.execute(
                "SELECT chunk_id, document_id, version, scope FROM documents_fts "
                "WHERE document_id = ?",
                (document_id,),
            )
            fts_rows = await cursor.fetchall()

        sections = [r["section"] for r in chunk_rows]
        self.assertGreaterEqual(len(chunk_rows), 3, "multiple headings => multiple chunks")
        self.assertIn("Habitats", sections)
        self.assertIn("Diet", sections)
        # embedding_blob stays NULL (embeddings are L3).
        self.assertEqual(len(fts_rows), len(chunk_rows), "each chunk mirrored to FTS")
        self.assertTrue(all(r["version"] == 1 for r in fts_rows))
        self.assertTrue(all(r["scope"] == "shared" for r in fts_rows))
        self.assertFalse(any(r["embedding_blob"] is not None for r in chunk_rows))

    # ── Re-chunk on update (new version) ──────────────────────────────
    async def test_update_rechunks_new_version_and_refreshes_fts(self) -> None:
        create = await self.create_doc()
        document_id = create.json()["id"]

        response = await self.request(
            self.writer,
            "PUT",
            f"/v1/documents/{document_id}",
            json={"content": "# Zebra Handbook\n\n## Migration\n\nThe zebra herd migrates north.\n"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["current_version"], 2)

        async with get_db() as db:
            cursor = await db.execute(
                "SELECT c.chunk_index, c.section, c.chunk_text, v.version "
                "FROM document_chunks c JOIN document_versions v ON v.id = c.version_id "
                "WHERE c.document_id = ? ORDER BY v.version, c.chunk_index",
                (document_id,),
            )
            chunks = await cursor.fetchall()
            cursor = await db.execute(
                "SELECT DISTINCT version FROM documents_fts WHERE document_id = ?",
                (document_id,),
            )
            fts_versions = [r["version"] for r in await cursor.fetchall()]

        latest_sections = [r["section"] for r in chunks if r["version"] == 2]
        self.assertIn("Migration", latest_sections)
        self.assertNotIn("Habitats", latest_sections, "old content must be re-chunked away")
        # FTS mirrors the latest version only.
        self.assertEqual(set(fts_versions), {2})

    # ── FTS search: exact phrase in a chunk ───────────────────────────
    async def test_search_exact_phrase_returns_correct_document(self) -> None:
        await self.create_doc(title="Zebra Handbook")
        await self.create_doc(
            title="Other Notes",
            content="# Notes\n\nCompletely unrelated text about cooking.\n",
        )

        response = await self.request(
            self.reader, "GET", "/v1/documents/search?q=%22unique+zebra+protocol%22&scope=shared"
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["items"][0]["title"], "Zebra Handbook")
        self.assertGreaterEqual(len(body["items"][0]["hits"]), 1)
        self.assertIn("zebra", body["items"][0]["hits"][0]["snippet"].lower())
        # relevance is a finite score
        self.assertIsInstance(body["items"][0]["relevance"], float)

    # ── Search scope filter ───────────────────────────────────────────
    async def test_search_filters_by_scope(self) -> None:
        await self.create_doc(title="Shared Zebras", scope="shared")
        await self.create_doc(title="Private Zebras", scope="private", agent=self.admin)

        shared = await self.request(
            self.writer, "GET", "/v1/documents/search?q=zebra&scope=shared"
        )
        self.assertEqual(shared.json()["total"], 1)
        self.assertEqual(shared.json()["items"][0]["title"], "Shared Zebras")

        both = await self.request(
            self.admin, "GET", "/v1/documents/search?q=zebra&scope=private"
        )
        self.assertEqual(both.json()["total"], 1)
        self.assertEqual(both.json()["items"][0]["title"], "Private Zebras")

    # ── Search authz ──────────────────────────────────────────────────
    async def test_search_rejects_unauthenticated_401(self) -> None:
        response = await self.request(None, "GET", "/v1/documents/search?q=zebra&scope=shared")
        self.assertEqual(response.status_code, 401)

    async def test_search_rejects_no_read_permission_403(self) -> None:
        response = await self.request(
            self.no_read, "GET", "/v1/documents/search?q=zebra&scope=shared"
        )
        self.assertEqual(response.status_code, 403)

    async def test_search_rejects_out_of_scope_403(self) -> None:
        response = await self.request(
            self.private_only, "GET", "/v1/documents/search?q=zebra&scope=shared"
        )
        self.assertEqual(response.status_code, 403)

    # ── Search pagination ─────────────────────────────────────────────
    async def test_search_pagination(self) -> None:
        for i in range(5):
            await self.create_doc(title=f"Paged Zebra {i}", content=f"# Doc {i}\n\nzebra fragment {i}\n")
        page1 = await self.request(
            self.reader, "GET", "/v1/documents/search?q=zebra&scope=shared&limit=2&offset=0"
        )
        body1 = page1.json()
        self.assertEqual(body1["total"], 5)
        self.assertEqual(len(body1["items"]), 2)

        page2 = await self.request(
            self.reader, "GET", "/v1/documents/search?q=zebra&scope=shared&limit=2&offset=2"
        )
        body2 = page2.json()
        self.assertEqual(len(body2["items"]), 2)
        ids1 = {item["document_id"] for item in body1["items"]}
        ids2 = {item["document_id"] for item in body2["items"]}
        self.assertEqual(len(ids1 & ids2), 0, "pages must be disjoint")

    # ── Chunk → document reverse lookup ───────────────────────────────
    async def test_list_document_chunks(self) -> None:
        create = await self.create_doc()
        document_id = create.json()["id"]
        response = await self.request(self.reader, "GET", f"/v1/documents/{document_id}/chunks")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["document_id"], document_id)
        self.assertEqual(body["version"], 1)
        self.assertGreaterEqual(body["total"], 3)
        sections = [c["section"] for c in body["chunks"]]
        self.assertIn("Habitats", sections)
        self.assertIn("Diet", sections)

    async def test_list_document_chunks_requires_scope(self) -> None:
        create = await self.create_doc(scope="private", agent=self.admin)
        document_id = create.json()["id"]
        response = await self.request(self.writer, "GET", f"/v1/documents/{document_id}/chunks")
        self.assertEqual(response.status_code, 403)

    # ── Search reflects updates and soft-deletes ──────────────────────
    async def test_search_uses_latest_version_not_stale_chunks(self) -> None:
        create = await self.create_doc(content="# A\n\nThe radioactive unicorn glows.\n")
        document_id = create.json()["id"]
        await self.request(
            self.writer,
            "PUT",
            f"/v1/documents/{document_id}",
            json={"content": "# A\n\nNow it says baking soda everywhere.\n"},
        )
        stale = await self.request(
            self.reader, "GET", "/v1/documents/search?q=unicorn&scope=shared"
        )
        self.assertEqual(stale.json()["total"], 0, "old content must no longer match")

    async def test_soft_deleted_document_stops_matching_search(self) -> None:
        create = await self.create_doc(content="# B\n\nThe elusive platypus appears.\n")
        document_id = create.json()["id"]
        before = await self.request(
            self.reader, "GET", "/v1/documents/search?q=platypus&scope=shared"
        )
        self.assertEqual(before.json()["total"], 1)

        await self.request(self.writer, "DELETE", f"/v1/documents/{document_id}")
        after = await self.request(
            self.reader, "GET", "/v1/documents/search?q=platypus&scope=shared"
        )
        self.assertEqual(after.json()["total"], 0, "deleted document must not be searchable")


if __name__ == "__main__":
    unittest.main()
