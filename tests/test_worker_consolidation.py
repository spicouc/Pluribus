"""Regression tests for worker consolidation bookkeeping."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import aiosqlite

from pluribus.worker import consolidate_facts, ensure_worker_tables


class WorkerConsolidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript("""
            CREATE TABLE facts (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                agent_id TEXT,
                created_at TEXT NOT NULL,
                deleted_at TEXT
            );
            CREATE TABLE consolidated (
                id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
                agent_id TEXT,
                summary TEXT NOT NULL,
                source_facts TEXT,
                model TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
        """)
        await ensure_worker_tables(self.db)

    async def asyncTearDown(self) -> None:
        await self.db.close()

    async def test_failed_old_fact_remains_pending_for_next_round(self) -> None:
        await self.db.executemany(
            "INSERT INTO facts(id, content, created_at) VALUES (?, ?, ?)",
            [
                ("old-1", "first", "2020-01-01 00:00:00"),
                ("old-2", "second", "2020-01-02 00:00:00"),
                ("new-1", "third", "2026-01-01 00:00:00"),
            ],
        )
        await self.db.commit()

        calls = {"first": 0}

        def flaky_summary(content: str):
            if content == "first" and calls["first"] == 0:
                calls["first"] += 1
                raise RuntimeError("temporary failure")
            return (f"summary:{content}", "test-model")

        with patch("pluribus.worker.BATCH_SIZE", 2), patch(
            "pluribus.worker._summarize_sync", side_effect=flaky_summary
        ):
            first_round = await consolidate_facts(self.db)
            self.assertEqual(first_round["errors"], 1)
            second_round = await consolidate_facts(self.db)
            self.assertEqual(second_round["errors"], 0)
            third_round = await consolidate_facts(self.db)
            self.assertEqual(third_round["errors"], 0)

        cursor = await self.db.execute(
            "SELECT fact_id FROM consolidated_facts ORDER BY fact_id"
        )
        mapped = [row["fact_id"] for row in await cursor.fetchall()]
        self.assertEqual(mapped, ["new-1", "old-1", "old-2"])

    async def test_legacy_source_facts_are_backfilled_exactly(self) -> None:
        await self.db.execute(
            "INSERT INTO facts(id, content, created_at) VALUES ('abc', 'x', '2020-01-01')"
        )
        await self.db.execute(
            "INSERT INTO consolidated(id, summary, source_facts) VALUES ('c1', 's', '[\"abc\"]')"
        )
        await self.db.commit()
        await ensure_worker_tables(self.db)
        cursor = await self.db.execute(
            "SELECT consolidated_id FROM consolidated_facts WHERE fact_id = 'abc'"
        )
        row = await cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["consolidated_id"], "c1")


if __name__ == "__main__":
    unittest.main()
