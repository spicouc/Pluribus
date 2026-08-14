"""Regression tests for query-save scope isolation."""

from __future__ import annotations

import unittest

import aiosqlite

from pluribus.query_save import _visible_source_ids


class QuerySaveScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript(
            """
            CREATE TABLE facts (
                id TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                deleted_at TEXT
            );
            INSERT INTO facts(id, scope, deleted_at) VALUES
                ('shared-1', 'shared', NULL),
                ('local-1', 'local', NULL),
                ('shared-deleted', 'shared', '2026-01-01');
            """
        )

    async def asyncTearDown(self) -> None:
        await self.db.close()

    async def test_only_same_scope_active_sources_are_visible(self) -> None:
        visible = await _visible_source_ids(
            self.db,
            ['local-1', 'shared-1', 'shared-deleted', 'shared-1'],
            'shared',
        )
        self.assertEqual(visible, ['shared-1'])

    async def test_empty_source_list_is_safe(self) -> None:
        self.assertEqual(await _visible_source_ids(self.db, [], 'shared'), [])


if __name__ == "__main__":
    unittest.main()
