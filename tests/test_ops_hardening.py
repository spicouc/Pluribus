"""Regression tests for reproducible dependencies and verified scheduled backups."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from pluribus.backup import backup_database


ROOT = Path(__file__).resolve().parent.parent


class DependencyLockTests(unittest.TestCase):
    def test_ci_uses_immutable_actions_and_locked_dependencies(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertIn("actions/checkout@11d5960a326750d5838078e36cf38b85af677262", workflow)
        self.assertIn("actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065", workflow)
        self.assertIn("python-version: '3.12.13'", workflow)
        self.assertIn("-r requirements.lock", workflow)
        self.assertIn("python -m pip check", workflow)
        self.assertNotIn("actions/checkout@v", workflow)
        self.assertNotIn("actions/setup-python@v", workflow)

    def test_lock_file_contains_only_exact_pins(self) -> None:
        lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
        packages = []
        for raw_line in lock.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            packages.append(line)
            self.assertIn("==", line)
            self.assertNotIn(">=", line)
            self.assertNotIn("<=", line)
        self.assertGreaterEqual(len(packages), 25)
        self.assertTrue(any(line.startswith("fastapi==") for line in packages))
        self.assertTrue(any(line.startswith("turbovec==") for line in packages))

    def test_dependabot_covers_pip_and_github_actions(self) -> None:
        config = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
        self.assertIn('package-ecosystem: "pip"', config)
        self.assertIn('package-ecosystem: "github-actions"', config)


class ScheduledBackupTests(unittest.TestCase):
    def test_backup_service_is_local_only_and_sandboxed(self) -> None:
        service = (ROOT / "systemd" / "pluribus-backup.service").read_text(encoding="utf-8")
        required = [
            "User=pluribus",
            "Group=pluribus",
            "UMask=0077",
            "NoNewPrivileges=yes",
            "ProtectSystem=strict",
            "ProtectHome=yes",
            "CapabilityBoundingSet=",
            "AmbientCapabilities=",
            "RestrictAddressFamilies=AF_UNIX",
            "ReadWritePaths=/opt/pluribus/data",
            "ExecStart=/opt/pluribus/venv/bin/python -m pluribus.backup",
        ]
        for directive in required:
            with self.subTest(directive=directive):
                self.assertIn(directive, service)
        self.assertNotIn("User=root", service)
        self.assertNotIn("AF_INET", service)

    def test_backup_timer_is_daily_persistent_and_jittered(self) -> None:
        timer = (ROOT / "systemd" / "pluribus-backup.timer").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* 03:30:00", timer)
        self.assertIn("RandomizedDelaySec=30m", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("WantedBy=timers.target", timer)

    def test_failed_compressed_restore_check_never_publishes_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "live.db"
            backup_dir = root / "backups"
            with sqlite3.connect(source) as db:
                db.execute("CREATE TABLE items(value TEXT)")
                db.execute("INSERT INTO items VALUES ('safe')")
                db.commit()

            with patch(
                "pluribus.backup._verify_gzip_archive",
                side_effect=RuntimeError("restore check failed"),
            ):
                with self.assertRaises(RuntimeError):
                    backup_database(str(source), str(backup_dir), retention_days=14)

            self.assertEqual(list(backup_dir.glob("pluribus_*.db.gz")), [])
            self.assertEqual(list(backup_dir.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
