"""Regression tests for indexed API-key authentication."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pluribus.api_keys import fingerprint_api_key, generate_api_key
from pluribus.config import settings
from pluribus.db import get_db, init_db
import pluribus.security as security


class ApiKeyAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "auth.db"
        self.settings_patch = patch.object(settings, "DB_PATH", str(self.db_path))
        self.settings_patch.start()
        await init_db()

        security._bcrypt_cache.clear()
        security._legacy_scan_by_client.clear()
        security._legacy_scan_global.clear()

    async def asyncTearDown(self) -> None:
        self.settings_patch.stop()
        self.temp_dir.cleanup()

    async def _insert_agent(
        self,
        agent_id: str,
        api_key_hash: str = "fake-hash",
        fingerprint: str | None = None,
    ) -> None:
        async with get_db() as db:
            await db.execute(
                """INSERT INTO agents
                   (id, name, api_key_hash, api_key_fingerprint, permissions, allowed_scopes)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    agent_id,
                    agent_id,
                    api_key_hash,
                    fingerprint,
                    json.dumps({"read": True}),
                    json.dumps(["shared"]),
                ),
            )
            await db.commit()

    async def test_schema_has_indexed_fingerprint_column(self) -> None:
        async with get_db() as db:
            cursor = await db.execute("PRAGMA table_info(agents)")
            columns = {row["name"] for row in await cursor.fetchall()}
            self.assertIn("api_key_fingerprint", columns)

            cursor = await db.execute("PRAGMA index_list(agents)")
            indexes = {row["name"] for row in await cursor.fetchall()}
            self.assertIn("idx_agents_api_key_fingerprint", indexes)

    async def test_current_key_authenticates_with_one_bcrypt_check(self) -> None:
        api_key = generate_api_key()
        fingerprint = fingerprint_api_key(api_key)
        await self._insert_agent("current", fingerprint=fingerprint)

        with patch("pluribus.security.bcrypt.checkpw", return_value=True) as checkpw:
            result = await security._authenticate_agent(api_key, "127.0.0.1")

        self.assertIsNotNone(result)
        self.assertEqual(result["id"], "current")
        self.assertEqual(checkpw.call_count, 1)

    async def test_unknown_current_key_never_scans_legacy_bcrypt_hashes(self) -> None:
        await self._insert_agent("legacy", fingerprint=None)
        unknown_key = generate_api_key()

        with patch("pluribus.security.bcrypt.checkpw", return_value=False) as checkpw:
            result = await security._authenticate_agent(unknown_key, "127.0.0.1")

        self.assertIsNone(result)
        self.assertEqual(checkpw.call_count, 0)

    async def test_successful_legacy_login_self_migrates_fingerprint(self) -> None:
        legacy_key = "legacy-key-material-that-is-long-enough"
        await self._insert_agent("legacy", fingerprint=None)

        with patch("pluribus.security.bcrypt.checkpw", return_value=True) as checkpw:
            result = await security._authenticate_agent(legacy_key, "127.0.0.1")

        self.assertIsNotNone(result)
        self.assertEqual(result["id"], "legacy")
        self.assertEqual(checkpw.call_count, 1)

        expected = fingerprint_api_key(legacy_key)
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT api_key_fingerprint FROM agents WHERE id = 'legacy'"
            )
            row = await cursor.fetchone()
        self.assertEqual(row["api_key_fingerprint"], expected)

        # The second login uses the indexed fast path rather than a legacy scan.
        security._legacy_scan_by_client.clear()
        security._legacy_scan_global.clear()
        with patch("pluribus.security._allow_legacy_scan", return_value=False) as legacy_scan:
            result2 = await security._authenticate_agent(legacy_key, "127.0.0.1")
        self.assertIsNotNone(result2)
        legacy_scan.assert_not_called()

    async def test_legacy_scan_is_bounded_before_bcrypt_loop(self) -> None:
        await self._insert_agent("legacy", fingerprint=None)
        with patch("pluribus.security._allow_legacy_scan", return_value=False), patch(
            "pluribus.security.bcrypt.checkpw", return_value=False
        ) as checkpw:
            result = await security._authenticate_agent(
                "another-legacy-key-material-long-enough", "203.0.113.9"
            )
        self.assertIsNone(result)
        self.assertEqual(checkpw.call_count, 0)


if __name__ == "__main__":
    unittest.main()
