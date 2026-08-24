"""CRUD + versioning regression tests for the Markdown document library (L1).

These tests exercise the real FastAPI app through an HTTP async client
(``httpx.ASGITransport`` over ``main.app``), authenticating through the actual
``X-API-Key`` middleware by patching ``security._authenticate_agent`` — the
same pattern used by ``test_admin_config.py``. This yields full end-to-end
coverage of the document routes, authorization and versioning, while the
"unauthenticated" test exercises the real middleware rejection path.
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


class DocumentLibraryCrudTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        security._bcrypt_cache.clear()
        security._legacy_scan_by_client.clear()
        security._legacy_scan_global.clear()

        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "documents-crud.db"
        self.settings_patch = patch.object(settings, "DB_PATH", str(self.db_path))
        self.settings_patch.start()
        await init_db()

        self.transport = httpx.ASGITransport(app=main.app)

        self.admin = make_agent("admin-1", admin=True, scopes=["shared", "private"])
        self.writer = make_agent("writer-1", scopes=["shared"])
        self.reader = make_agent("reader-1", read=True, write=False, delete=False)
        self.no_read = make_agent("noread-1", read=False, write=False, delete=False)
        self.private_only = make_agent("priv-1", scopes=["private"])

        # Persist the test agents so ``audit_log.agent_id`` (FK -> agents.id)
        # is satisfied under PRAGMA foreign_keys=ON. Authentication is mocked
        # at the security layer, but the audit trail still references the row.
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
            # Explicitly bypass the agent authentication -> middleware 401.
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
        title: str = "Getting Started",
        content: str = "# Getting Started\n\nWelcome to the handbook.",
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
                "tags": tags if tags is not None else ["guide", "onboarding"],
                "description": "First run guide",
                "metadata": {"owner": "docs"},
            },
        )

    # ── Permission / authentication ─────────────────────────────────────
    async def test_unauthenticated_request_is_rejected_401(self) -> None:
        response = await self.request(None, "GET", "/v1/documents")
        self.assertEqual(response.status_code, 401)
        self.assertIn("X-API-Key", response.json()["detail"])

    async def test_read_without_read_permission_is_403(self) -> None:
        await self.create_doc()
        response = await self.request(
            self.no_read, "GET", "/v1/documents?scope=shared"
        )
        self.assertEqual(response.status_code, 403)

    async def test_write_without_write_permission_is_403(self) -> None:
        response = await self.create_doc(agent=self.reader)
        self.assertEqual(response.status_code, 403)

    async def test_read_outside_allowed_scope_is_403(self) -> None:
        await self.create_doc()
        response = await self.request(
            self.private_only, "GET", "/v1/documents?scope=shared"
        )
        self.assertEqual(response.status_code, 403)

    # ── Create / get ────────────────────────────────────────────────────
    async def test_create_document_returns_document_with_content(self) -> None:
        response = await self.create_doc()
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["title"], "Getting Started")
        self.assertEqual(body["scope"], "shared")
        self.assertEqual(body["category"], "events")
        self.assertEqual(body["tags"], ["guide", "onboarding"])
        self.assertEqual(body["current_version"], 1)
        self.assertEqual(body["content"], "# Getting Started\n\nWelcome to the handbook.")
        self.assertIn("id", body)

    async def test_create_persists_version_1_with_content_hash(self) -> None:
        create = await self.create_doc()
        document_id = create.json()["id"]
        async with get_db() as db:
            cursor = await db.execute(
                """SELECT version, content_hash, content FROM document_versions
                   WHERE document_id = ? """,
                (document_id,),
            )
            rows = await cursor.fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["version"], 1)
        expected_hash = __import__("hashlib").sha256(
            "# Getting Started\n\nWelcome to the handbook.".encode("utf-8")
        ).hexdigest()
        self.assertEqual(rows[0]["content_hash"], expected_hash)

    async def test_get_by_id_returns_current_document(self) -> None:
        create = await self.create_doc()
        document_id = create.json()["id"]
        response = await self.request(self.reader, "GET", f"/v1/documents/{document_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], document_id)
        self.assertEqual(response.json()["content"], "# Getting Started\n\nWelcome to the handbook.")

    async def test_get_missing_document_is_404(self) -> None:
        response = await self.request(
            self.writer, "GET", "/v1/documents/does-not-exist-id"
        )
        self.assertEqual(response.status_code, 404)

    async def test_get_by_title_and_scope(self) -> None:
        await self.create_doc()
        response = await self.request(
            self.reader,
            "GET",
            "/v1/documents/lookup?title=Getting Started&scope=shared",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], "Getting Started")
        self.assertEqual(response.json()["scope"], "shared")

    async def test_get_by_title_missing_is_404(self) -> None:
        response = await self.request(
            self.reader,
            "GET",
            "/v1/documents/lookup?title=Nope&scope=shared",
        )
        self.assertEqual(response.status_code, 404)

    # ── Update / versioning ─────────────────────────────────────────────
    async def test_update_creates_new_version_and_bumps_current_version(self) -> None:
        create = await self.create_doc()
        document_id = create.json()["id"]
        self.assertEqual(create.json()["current_version"], 1)

        response = await self.request(
            self.writer,
            "PUT",
            f"/v1/documents/{document_id}",
            json={
                "content": "# Getting Started\n\nUpdated body.",
                "change_note": "improve intro",
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["current_version"], 2)
        self.assertEqual(body["content"], "# Getting Started\n\nUpdated body.")

        async with get_db() as db:
            cursor = await db.execute(
                "SELECT version, content FROM document_versions WHERE document_id = ? ORDER BY version",
                (document_id,),
            )
            rows = await cursor.fetchall()
        self.assertEqual([r["version"] for r in rows], [1, 2])
        self.assertEqual(rows[1]["content"], "# Getting Started\n\nUpdated body.")

    async def test_update_metadata_only_does_not_mint_empty_version(self) -> None:
        create = await self.create_doc()
        document_id = create.json()["id"]
        response = await self.request(
            self.writer,
            "PUT",
            f"/v1/documents/{document_id}",
            json={"tags": ["guides", "updated"]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["current_version"], 1)
        self.assertEqual(response.json()["tags"], ["guides", "updated"])

    async def test_update_requires_write_permission(self) -> None:
        create = await self.create_doc()
        document_id = create.json()["id"]
        response = await self.request(
            self.reader,
            "PUT",
            f"/v1/documents/{document_id}",
            json={"content": "blocked"},
        )
        self.assertEqual(response.status_code, 403)

    # ── List / search ──────────────────────────────────────────────────
    async def test_list_filters_by_scope(self) -> None:
        await self.create_doc(title="Team Guide", scope="shared")
        await self.create_doc(title="Secret Note", content="private details")
        async with get_db() as db:
            await db.execute(
                """UPDATE documents SET scope = 'private' WHERE title = 'Secret Note'"""
            )
            await db.commit()

        response = await self.request(
            self.writer, "GET", "/v1/documents?scope=shared"
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["scope"], "shared")
        titles = [item["title"] for item in body["items"]]
        self.assertIn("Team Guide", titles)
        self.assertNotIn("Secret Note", titles)

    async def test_list_filters_by_tag(self) -> None:
        await self.create_doc(title="Doc A", content="alpha body")
        await self.create_doc(title="Doc B", content="beta body")
        async with get_db() as db:
            await db.execute(
                """UPDATE documents SET tags = '["release"]' WHERE title = 'Doc B'"""
            )
            await db.commit()

        response = await self.request(
            self.writer, "GET", "/v1/documents?scope=shared&tag=release"
        )
        body = response.json()
        titles = [item["title"] for item in body["items"]]
        self.assertEqual(titles, ["Doc B"])

    async def test_list_filters_by_text_query(self) -> None:
        await self.create_doc(title="Alpha Handbook", content="alpha body")
        await self.create_doc(title="Beta Handbook", content="beta body")
        response = await self.request(
            self.writer, "GET", "/v1/documents?scope=shared&q=Alpha"
        )
        body = response.json()
        titles = [item["title"] for item in body["items"]]
        self.assertEqual(titles, ["Alpha Handbook"])

    # ── Soft delete ────────────────────────────────────────────────────
    async def test_soft_delete_sets_deleted_at_and_hides_document(self) -> None:
        create = await self.create_doc()
        document_id = create.json()["id"]

        response = await self.request(self.writer, "DELETE", f"/v1/documents/{document_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["document_id"], document_id)

        async with get_db() as db:
            cursor = await db.execute(
                "SELECT deleted_at FROM documents WHERE id = ?", (document_id,)
            )
            row = await cursor.fetchone()
        self.assertIsNotNone(row["deleted_at"])

        # Hidden from get-by-id and from list.
        get_response = await self.request(self.reader, "GET", f"/v1/documents/{document_id}")
        self.assertEqual(get_response.status_code, 404)
        list_response = await self.request(
            self.reader, "GET", "/v1/documents?scope=shared"
        )
        self.assertEqual(list_response.status_code, 200)
        self.assertNotIn(document_id, [i["id"] for i in list_response.json()["items"]])

    async def test_delete_requires_delete_permission(self) -> None:
        create = await self.create_doc()
        document_id = create.json()["id"]
        reader = make_agent("reader-del", read=True, write=True, delete=False)
        response = await self.request(reader, "DELETE", f"/v1/documents/{document_id}")
        self.assertEqual(response.status_code, 403)

    # ── Version history ────────────────────────────────────────────────
    async def test_version_history_lists_snapshots(self) -> None:
        create = await self.create_doc()
        document_id = create.json()["id"]
        await self.request(
            self.writer,
            "PUT",
            f"/v1/documents/{document_id}",
            json={"content": "v2 body", "change_note": "second"},
        )
        await self.request(
            self.writer,
            "PUT",
            f"/v1/documents/{document_id}",
            json={"content": "v3 body", "change_note": "third"},
        )

        response = await self.request(
            self.reader, "GET", f"/v1/documents/{document_id}/versions"
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 3)
        self.assertEqual([v["version"] for v in body["versions"]], [3, 2, 1])

    async def test_get_specific_version_returns_snapshot(self) -> None:
        create = await self.create_doc(content="original body")
        document_id = create.json()["id"]
        await self.request(
            self.writer,
            "PUT",
            f"/v1/documents/{document_id}",
            json={"content": "updated body", "change_note": "edit"},
        )

        v1 = await self.request(
            self.reader, "GET", f"/v1/documents/{document_id}/versions/1"
        )
        self.assertEqual(v1.status_code, 200)
        self.assertEqual(v1.json()["content"], "original body")
        self.assertEqual(v1.json()["version"], 1)

        v2 = await self.request(
            self.reader, "GET", f"/v1/documents/{document_id}/versions/2"
        )
        self.assertEqual(v2.json()["content"], "updated body")

        missing = await self.request(
            self.reader, "GET", f"/v1/documents/{document_id}/versions/99"
        )
        self.assertEqual(missing.status_code, 404)

    # ── Facts isolation (L1 hard rule) ──────────────────────────────────
    async def test_document_operations_never_write_to_facts(self) -> None:
        create = await self.create_doc()
        document_id = create.json()["id"]
        await self.request(
            self.writer,
            "PUT",
            f"/v1/documents/{document_id}",
            json={"content": "another version"},
        )
        async with get_db() as db:
            cursor = await db.execute("SELECT COUNT(*) AS total FROM facts")
            facts = (await cursor.fetchone())["total"]
            cursor = await db.execute("SELECT COUNT(*) AS total FROM chunks")
            chunks = (await cursor.fetchone())["total"]
            cursor = await db.execute("SELECT COUNT(*) AS total FROM documents")
            docs = (await cursor.fetchone())["total"]
        self.assertEqual(facts, 0, "documents must never become facts")
        self.assertEqual(chunks, 0, "documents must never reuse facts.chunks")
        self.assertEqual(docs, 1)


if __name__ == "__main__":
    unittest.main()
