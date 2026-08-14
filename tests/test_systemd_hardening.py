"""Regression tests for least-privilege systemd deployment."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from pluribus import admin_config
from pluribus.config import settings


ROOT = Path(__file__).resolve().parent.parent


class SystemdUnitTests(unittest.TestCase):
    def _unit(self, name: str) -> str:
        return (ROOT / "systemd" / name).read_text(encoding="utf-8")

    def test_api_service_is_unprivileged_and_sandboxed(self) -> None:
        unit = self._unit("pluribus.service")
        required = [
            "User=pluribus",
            "Group=pluribus",
            "NoNewPrivileges=yes",
            "ProtectSystem=strict",
            "ProtectHome=yes",
            "PrivateDevices=yes",
            "CapabilityBoundingSet=",
            "AmbientCapabilities=",
            "ReadWritePaths=/opt/pluribus/data",
            "EnvironmentFile=-/opt/pluribus/data/pluribus.env",
            "Restart=always",
        ]
        for directive in required:
            with self.subTest(directive=directive):
                self.assertIn(directive, unit)
        self.assertNotIn("User=root", unit)

    def test_worker_is_unprivileged_and_sandboxed(self) -> None:
        unit = self._unit("pluribus-worker.service")
        self.assertIn("User=pluribus", unit)
        self.assertIn("NoNewPrivileges=yes", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("ReadWritePaths=/opt/pluribus/data", unit)
        self.assertNotIn("User=root", unit)

    def test_mutable_env_path_is_inside_writable_state_directory(self) -> None:
        self.assertEqual(settings.ENV_PATH, "/opt/pluribus/data/pluribus.env")


class RestartMechanismTests(unittest.TestCase):
    def test_restart_schedules_self_sigterm_without_privileged_subprocess(self) -> None:
        instances = []

        class FakeTimer:
            def __init__(self, delay, callback):
                self.delay = delay
                self.callback = callback
                self.daemon = False
                self.started = False
                instances.append(self)

            def start(self):
                self.started = True

        with patch("pluribus.admin_config.threading.Timer", FakeTimer):
            timer = admin_config._restart_service(0.25)

        self.assertIs(timer, instances[0])
        self.assertEqual(timer.delay, 0.25)
        self.assertTrue(timer.daemon)
        self.assertTrue(timer.started)

    def test_admin_config_no_longer_imports_or_calls_subprocess(self) -> None:
        source = (ROOT / "pluribus" / "admin_config.py").read_text(encoding="utf-8")
        self.assertNotIn("import subprocess", source)
        self.assertNotIn("subprocess.Popen", source)


if __name__ == "__main__":
    unittest.main()
