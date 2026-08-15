"""Static cadence guardrails for Memory Sync deployment."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class MemorySyncCadenceTests(unittest.TestCase):
    def test_worker_runs_five_minutes_after_previous_run_finishes(self) -> None:
        timer = (ROOT / "systemd" / "pluribus-worker.timer").read_text(encoding="utf-8")
        self.assertIn("OnUnitInactiveSec=5min", timer)
        self.assertNotIn("OnUnitActiveSec=15min", timer)

    def test_memory_sync_defaults_are_fast_but_bounded(self) -> None:
        source = (ROOT / "pluribus" / "memory_sync.py").read_text(encoding="utf-8")
        self.assertIn("DEFAULT_ACTIVE_POLL_SECONDS = 5", source)
        self.assertIn("DEFAULT_IDLE_POLL_SECONDS = 30", source)
        self.assertIn("DEFAULT_WRITE_DEBOUNCE_SECONDS = 2", source)
        self.assertIn("DEFAULT_MAX_WRITE_DELAY_SECONDS = 5", source)
        self.assertIn("_MAX_SCAN_EVENTS = 2000", source)


if __name__ == "__main__":
    unittest.main()
