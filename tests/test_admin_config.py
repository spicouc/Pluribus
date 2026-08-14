"""Regression tests for hardened admin configuration writes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from pluribus.admin_config import _atomic_update_env, _validate_updates
import pluribus.main as main


class ConfigValidationTests(unittest.TestCase):
    def test_rejects_newline_injection(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            _validate_updates(
                {"PLURIBUS_OLLAMA_MODEL": "safe-model\nPLURIBUS_API_PORT=9999"}
            )
        self.assertEqual(ctx.exception.status_code, 400)

    def test_rejects_non_allowlisted_environment_key(self) -> None:
        with self.assertRaises(HTTPException):
            _validate_updates({"PLURIBUS_NOTION_API_KEY": "secret"})

    def test_rejects_invalid_chunk_overlap(self) -> None:
        with self.assertRaises(HTTPException):
            _validate_updates(
                {
                    "PLURIBUS_MAX_CHUNK_SIZE": "100",
                    "PLURIBUS_CHUNK_OVERLAP": "100",
                }
            )

    def test_canonicalizes_integer_values(self) -> None:
        updates, restart = _validate_updates(
            {"PLURIBUS_RATE_LIMIT": "00100", "_restart": "false"}
        )
        self.assertEqual(updates["PLURIBUS_RATE_LIMIT"], "100")
        self.assertFalse(restart)


class AtomicEnvTests(unittest.TestCase):
    def test_atomic_update_preserves_unrelated_values_and_sets_mode(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        try:
            path = Path(temp_dir.name) / ".env"
            path.write_text(
                "# comment\nPLURIBUS_RATE_LIMIT=10\nSECRET_VALUE=keep-me\n",
                encoding="utf-8",
            )
            _atomic_update_env(
                str(path),
                {
                    "PLURIBUS_RATE_LIMIT": "200",
                    "PLURIBUS_API_PORT": "8791",
                },
            )
            content = path.read_text(encoding="utf-8")
            self.assertIn("# comment\n", content)
            self.assertIn("SECRET_VALUE=keep-me\n", content)
            self.assertIn("PLURIBUS_RATE_LIMIT=200\n", content)
            self.assertIn("PLURIBUS_API_PORT=8791\n", content)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        finally:
            temp_dir.cleanup()

    def test_refuses_symlink_target(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        try:
            root = Path(temp_dir.name)
            target = root / "real.env"
            target.write_text("X=1\n", encoding="utf-8")
            link = root / ".env"
            link.symlink_to(target)
            with self.assertRaises(RuntimeError):
                _atomic_update_env(str(link), {"PLURIBUS_RATE_LIMIT": "100"})
        finally:
            temp_dir.cleanup()


class RoutePrecedenceTests(unittest.TestCase):
    def _endpoint_index(self, endpoint_name: str) -> int:
        for index, route in enumerate(main.app.routes):
            endpoint = getattr(route, "endpoint", None)
            if getattr(endpoint, "__name__", None) == endpoint_name:
                return index
        self.fail(f"Endpoint {endpoint_name!r} no registrat")

    def test_hardened_config_routes_are_registered_before_legacy_duplicates(self) -> None:
        self.assertLess(
            self._endpoint_index("secure_save_config"),
            self._endpoint_index("save_config"),
        )
        self.assertLess(
            self._endpoint_index("reject_get_restart"),
            self._endpoint_index("restart_pluribus"),
        )
        self.assertLess(
            self._endpoint_index("secure_restart"),
            self._endpoint_index("restart_pluribus"),
        )


if __name__ == "__main__":
    unittest.main()
