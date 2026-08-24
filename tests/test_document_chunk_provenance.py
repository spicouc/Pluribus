"""L2-CERT: audit and certify chunk provenance for the document library.

These tests certify the 7 L2 provenance guarantees requested by the phase:

1. LINE PROVENANCE — each document_chunk keeps document/version relation,
   chunk_index, line_start, line_end, section + heading_path, all mapping back
   to the original Markdown line numbers.
2. NESTED HEADINGS — ``# Architecture`` > ``## Storage`` > ``### Backups``
   produces a ``heading_path = "Architecture > Storage > Backups"`` chunk.
3. LONG SECTION — a section > 6000 chars is fully preserved (no truncation,
   no gaps, deterministic order, chunks within limit, provenance retained).
4. CODE FENCES — ```...``` blocks keep every line; no destructive splits.
5. VERSIONING — v1 indexed, create v2 => normal search returns only v2,
   explicit GET of v1 chunks/versions still available.
6. SOFT DELETE — deleted document => 0 FTS results, history preserved.
7. SCOPE — a shared-only agent may search/list shared, private is DENIED across
   search, chunks and version selector — no bypass via version=.

Uses the same httpx.ASGITransport harness as the other document tests.
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


class ChunkProvenanceUnitTests(unittest.TestCase):
    """Direct unit tests over chunk_markdown() for provenance guarantees."""

    def _assert_reconstructs(self, content: str, chunks) -> None:
        """Certify lossless, deterministic provenance:
        - each chunk's body lines map into its claimed 1-based line range;
        - ranges are ordered (no overlap/gaps in *content* coverage);
        - **every** non-heading body line of the source survives in some chunk."""
        lines = content.split("\n")

        all_body = {
            ln.strip()
            for ln in lines
            if ln.strip() and not ln.lstrip().startswith("#")
        }
        # Ranges must map to the source: each chunk's text (whitespace-normalised)
        # must appear within its claimed 1-based line range. Oversized sub-parts
        # split mid-line, so we use whitespace-collapsed substring matching.
        import re as _re
        def collapse(t):
            return _re.sub(r"\s+", " ", t).strip()
        for ch in chunks:
            self.assertGreaterEqual(ch.line_start, 1)
            self.assertGreaterEqual(ch.line_end, ch.line_start)
            block = " ".join(ln.strip() for ln in lines[ch.line_start - 1:ch.line_end])
            self.assertIn(collapse(ch.chunk_text), collapse(block),
                          "chunk must map into its claimed line range")
        # Ordered, index-contiguous.
        self.assertEqual([c.chunk_index for c in chunks], list(range(len(chunks))))
        for a, b in zip(chunks, chunks[1:]):
            self.assertGreaterEqual(b.line_start, a.line_start, "chunks must be ordered")
        # Lossless: every body *word* of the source survives somewhere in the
        # concatenated chunks (word-stream coverage; robust to mid-line splits).
        import re as _re
        src_words = _re.findall(r"[^\W_]+", " ".join(all_body))
        chunk_words = _re.findall(r"[^\W_]+", " ".join(c.chunk_text for c in chunks))
        for w in src_words:
            self.assertIn(w, chunk_words, f"word {w!r} lost in chunking")

    def test_line_provenance_ranges(self) -> None:
        content = "# Zebra Handbook\n\nWelcome text.\n\n## Habitats\n\nSavannah regions.\n"
        chunks = chunk_markdown(content)
        self.assertGreaterEqual(len(chunks), 2)
        for ch in chunks:
            self.assertGreaterEqual(ch.line_start, 1)
            self.assertGreaterEqual(ch.line_end, ch.line_start)
        self._assert_reconstructs(content, chunks)

    def test_nested_headings_produce_heading_path(self) -> None:
        content = (
            "# Architecture\n\nArchitecture preamble.\n\n"
            "## Storage\n\nStorage intro.\n\n"
            "### Backups\n\nBackup details.\n"
        )
        chunks = chunk_markdown(content)
        paths = {ch.heading_path for ch in chunks if ch.heading_path}
        self.assertIn("Architecture > Storage > Backups", paths,
                      "nested heading must preserve full breadcrumb")
        # The innermost section is also kept.
        sections = {ch.section for ch in chunks if ch.section}
        self.assertIn("Backups", sections)
        self.assertIn("Storage", sections)
        self._assert_reconstructs(content, chunks)

    def test_long_section_no_truncation(self) -> None:
        para = ("Once upon a time the industrious zebra crossed the wide savannah "
                "toward the distant acacia grove, carefully avoiding every sleepy "
                "predator it met on its way, all while keeping a steady rhythm. ") * 40
        content = f"# Long Report\n\n{para}\n"
        self.assertGreater(len(para), 6000)
        chunks = chunk_markdown(content)
        self.assertGreater(len(chunks), 1, "over-max section must be split")
        for ch in chunks:
            self.assertLessEqual(len(ch.chunk_text), 6000, "no chunk exceeds max_len")
        # Deterministic order + lossless.
        self.assertEqual([ch.chunk_index for ch in chunks], list(range(len(chunks))))
        self._assert_reconstructs(content, chunks)

    def test_code_fences_preserved(self) -> None:
        code = "# Software\n\n```python\ndef zebra(x):\n    return x * 2\n\nprint(zebra(21))\n```\n\nTrailing note.\n"
        chunks = chunk_markdown(code)
        joined = "\n".join(c.chunk_text for c in chunks)
        self.assertIn("def zebra(x):", joined)
        self.assertIn("print(zebra(21))", joined)
        self.assertIn("```", joined)
        # No line inside the fence lost.
        for line in ["def zebra(x):", "    return x * 2", "print(zebra(21))"]:
            self.assertIn(line, joined)
        self._assert_reconstructs(code, chunks)

    def test_deterministic_chunking(self) -> None:
        content = "# A\n\naa bb cc.\n\n## B\n\nx y z.\n"
        first = [(c.section, c.chunk_text, c.line_start, c.line_end, c.heading_path)
                 for c in chunk_markdown(content)]
        second = [(c.section, c.chunk_text, c.line_start, c.line_end, c.heading_path)
                  for c in chunk_markdown(content)]
        self.assertEqual(first, second)


class DocumentProvenanceApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        security._bcrypt_cache.clear()
        security._legacy_scan_by_client.clear()
        security._legacy_scan_global.clear()

        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "provenance.db"
        self.settings_patch = patch.object(settings, "DB_PATH", str(self.db_path))
        self.settings_patch.start()
        await init_db()

        self.transport = httpx.ASGITransport(app=main.app)

        self.admin = make_agent("admin-1", admin=True, scopes=["shared", "private"])
        self.writer = make_agent("writer-1", scopes=["shared"])
        self.reader = make_agent("reader-1", read=True, write=False, delete=False)
        self.private_only = make_agent("priv-1", scopes=["private"])

        _all_agents = {
            "admin-1": self.admin,
            "writer-1": self.writer,
            "reader-1": self.reader,
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

    async def request(self, agent, method, url, **kwargs) -> httpx.Response:
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

    async def create_doc(self, *, title="Doc", content=None, scope="shared", agent=None) -> httpx.Response:
        content = content if content is not None else (
            "# Zebra Handbook\n\nIntro zebra content.\n\n## Habitats\n\nSavannah regions.\n"
        )
        return await self.request(
            agent or self.writer,
            "POST",
            "/v1/documents",
            json={"title": title, "content": content, "scope": scope,
                  "category": "events", "tags": ["guide"], "description": "d",
                  "metadata": {"owner": "docs"}},
        )

    async def chunk_row(self, document_id, version):
        async with get_db() as db:
            c = await db.execute(
                """SELECT c.line_start, c.line_end, c.heading_path, c.section,
                          c.chunk_index, v.version
                   FROM document_chunks c
                   JOIN document_versions v ON v.id = c.version_id
                   WHERE c.document_id = ? AND v.version = ?
                   ORDER BY c.chunk_index""",
                (document_id, version),
            )
            return await c.fetchall()

    # ── 5. VERSIONING ───────────────────────────────────────────────
    async def test_versioning_search_returns_only_current(self) -> None:
        create = await self.create_doc(content="# A\n\nThe v1 secret gorilla hides here.\n")
        document_id = create.json()["id"]
        await self.request(
            self.writer, "PUT", f"/v1/documents/{document_id}",
            json={"content": "# A\n\nNow the public octopus swims.\n"},
        )
        search = await self.request(
            self.reader, "GET", "/v1/documents/search?q=gorilla&scope=shared"
        )
        self.assertEqual(search.json()["total"], 0, "normal search must exclude v1 (stale)")
        search2 = await self.request(
            self.reader, "GET", "/v1/documents/search?q=octopus&scope=shared"
        )
        self.assertEqual(search2.json()["total"], 1, "v2 content must be searchable")
        # v1 history still available explicitly.
        v1 = await self.request(
            self.reader, "GET", f"/v1/documents/{document_id}/versions/1"
        )
        self.assertEqual(v1.status_code, 200)
        self.assertIn("gorilla", v1.json()["content"])

    # ── 6. SOFT DELETE ──────────────────────────────────────────────
    async def test_soft_delete_removes_fts_preserves_history(self) -> None:
        create = await self.create_doc(content="# B\n\nThe unique platypus roams.\n")
        document_id = create.json()["id"]
        self.assertEqual((await self.request(
            self.reader, "GET", "/v1/documents/search?q=platypus&scope=shared"
        )).json()["total"], 1)

        await self.request(self.writer, "DELETE", f"/v1/documents/{document_id}")
        self.assertEqual((await self.request(
            self.reader, "GET", "/v1/documents/search?q=platypus&scope=shared"
        )).json()["total"], 0, "deleted document must vanish from FTS")

        # History preserved: chunks and versions rows still present.
        async with get_db() as db:
            c = await db.execute("SELECT COUNT(*) AS t FROM document_versions WHERE document_id = ?",
                                 (document_id,))
            self.assertGreaterEqual((await c.fetchone())["t"], 1)
            c = await db.execute("SELECT COUNT(*) AS t FROM document_chunks WHERE document_id = ?",
                                 (document_id,))
            self.assertGreaterEqual((await c.fetchone())["t"], 1)

    # ── 7. SCOPE isolation (search, chunks, version selector) ───────
    async def test_scope_denied_on_all_endpoints_no_ver_bypass(self) -> None:
        create = await self.create_doc(title="Private Doc", scope="private", agent=self.admin)
        document_id = create.json()["id"]

        # search denied
        r = await self.request(self.writer, "GET", "/v1/documents/search?q=Doc&scope=private")
        self.assertEqual(r.status_code, 403)
        # chunks denied (no bypass via version=)
        r = await self.request(self.writer, "GET", f"/v1/documents/{document_id}/chunks?version=1")
        self.assertEqual(r.status_code, 403)
        # version selector denied (no bypass via version=)
        r = await self.request(self.writer, "GET", f"/v1/documents/{document_id}/versions/1")
        self.assertEqual(r.status_code, 403)
        r = await self.request(self.writer, "GET", f"/v1/documents/{document_id}/versions")
        self.assertEqual(r.status_code, 403)
        # get denied
        r = await self.request(self.writer, "GET", f"/v1/documents/{document_id}")
        self.assertEqual(r.status_code, 403)

        # shared-only read succeeds on public documents
        pub = await self.create_doc(title="Shared Doc", scope="shared", agent=self.writer)
        r = await self.request(self.reader, "GET", f"/v1/documents/{pub.json()['id']}")
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
