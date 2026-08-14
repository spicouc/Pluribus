"""Idempotent persistence for Xerrameca Runner v1."""

from __future__ import annotations

from pluribus.db import get_db


RUNNER_SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS xerrameca_runner_runtime (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    poll_interval_seconds REAL NOT NULL DEFAULT 2.0
        CHECK (poll_interval_seconds BETWEEN 0.25 AND 60.0),
    max_dispatches_per_tick INTEGER NOT NULL DEFAULT 4
        CHECK (max_dispatches_per_tick BETWEEN 1 AND 100),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT OR IGNORE INTO xerrameca_runner_runtime
(singleton, enabled, poll_interval_seconds, max_dispatches_per_tick)
VALUES (1, 0, 2.0, 4);

CREATE TABLE IF NOT EXISTS xerrameca_runners (
    agent_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    endpoint_url TEXT NOT NULL,
    secret TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    request_timeout_seconds INTEGER NOT NULL DEFAULT 30
        CHECK (request_timeout_seconds BETWEEN 2 AND 120),
    max_failures INTEGER NOT NULL DEFAULT 3
        CHECK (max_failures BETWEEN 1 AND 20),
    cooldown_seconds INTEGER NOT NULL DEFAULT 60
        CHECK (cooldown_seconds BETWEEN 10 AND 3600),
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    circuit_open_until TEXT,
    last_attempted_at TEXT,
    last_success_at TEXT,
    last_status INTEGER,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_xerrameca_runners_enabled
    ON xerrameca_runners(enabled, circuit_open_until);
"""


async def init_xerrameca_runner_db() -> None:
    async with get_db() as db:
        await db.executescript(RUNNER_SCHEMA_SQL)
        await db.commit()
