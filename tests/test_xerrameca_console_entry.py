"""Focused regression tests for Xerrameca dashboard entry behavior."""

from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from pluribus.xerrameca.console_entry import router


class XerramecaConsoleEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()

    def test_default_dashboard_contains_visible_xerrameca_link(self) -> None:
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIn('/dashboard?view=xerrameca', response.text)
        self.assertIn('Xerrameca', response.text)

    def test_xerrameca_view_contains_manual_finish_control(self) -> None:
        response = self.client.get("/dashboard?view=xerrameca")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Finalitza", response.text)
        self.assertIn("finishConv", response.text)
        self.assertIn("/finish`,{method:'POST'", response.text)


if __name__ == "__main__":
    unittest.main()
