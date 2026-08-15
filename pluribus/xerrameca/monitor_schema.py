"""Persistent state for the Xerrameca conversation monitor."""

from __future__ import annotations

from pluribus.db import get_db


SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS xerrameca_monitor_runtime (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    poll_interval_seconds REAL NOT NULL DEFAULT 30.0 CHECK (poll_interval_seconds BETWEEN 2 AND 3600),
    stalled_after_seconds INTEGER NOT NULL DEFAULT 900 CHECK (stalled_after_seconds BETWEEN 30 AND 86400),
    near_rounds_threshold INTEGER NOT NULL DEFAULT 2 CHECK (near_rounds_threshold BETWEEN 1 AND 20),
    loop_window INTEGER NOT NULL DEFAULT 4 CHECK (loop_window BETWEEN 3 AND 12),
    auto_pause_stalled INTEGER NOT NULL DEFAULT 0 CHECK (auto_pause_stalled IN (0,1)),
    auto_pause_loop INTEGER NOT NULL DEFAULT 0 CHECK (auto_pause_loop IN (0,1)),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT OR IGNORE INTO xerrameca_monitor_runtime
(singleton, enabled, poll_interval_seconds, stalled_after_seconds,
 near_rounds_threshold, loop_window, auto_pause_stalled, auto_pause_loop)
VALUES (1, 1, 30.0, 900, 2, 4, 0, 0);

CREATE TABLE IF NOT EXISTS xerrameca_monitor_alerts (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES xerrameca_conversations(id) ON DELETE CASCADE,
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','acknowledged','resolved')),
    message TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '{}',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1,
    acknowledged_at TEXT,
    acknowledged_by_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_xerrameca_monitor_alerts_status
    ON xerrameca_monitor_alerts(status, severity, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_xerrameca_monitor_alerts_conversation
    ON xerrameca_monitor_alerts(conversation_id, status, alert_type);
"""


async def init_xerrameca_monitor_db() -> None:
    async with get_db() as db:
        await db.executescript(SCHEMA_SQL)
        await db.commit()
