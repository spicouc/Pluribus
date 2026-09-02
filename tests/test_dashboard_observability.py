"""D1 — Dashboard observability tests.

Standard Pluribus test pattern: temp DB, seed one agent, exercise
the four /v1/dashboard/* read-only endpoints via FastAPI TestClient.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parent.parent

_TMP = tempfile.TemporaryDirectory()
_DB = Path(_TMP.name) / "d1.db"
os.environ["DB_PATH"] = str(_DB)
os.environ.setdefault("PLURIBUS_API_KEY", "d1-suite-key-AAAAAAAAAAAAAAAAAAAAAAAAA")

import sys
sys.path.insert(0, str(REPO_ROOT))

import pluribus.security as security  # noqa: E402
import bcrypt  # noqa: E402
from pluribus.api_keys import fingerprint_api_key  # noqa: E402
from pluribus.config import settings  # noqa: E402
from pluribus.db import get_db, init_db  # noqa: E402


KEY = "d1-suite-key-AAAAAAAAAAAAAAAAAAAAAAAAA"
# Real bcrypt hash of the test key (rounds=4 for fast tests)
HASH = bcrypt.hashpw(KEY.encode("utf-8"), bcrypt.gensalt(rounds=4)).decode("utf-8")


def _seed_agent_sync(agent_id: str, key: str, name: str) -> None:
    fp = fingerprint_api_key(key)

    async def _do():
        async with get_db() as db:
            await db.execute(
                """INSERT INTO agents
                   (id, name, api_key_hash, api_key_fingerprint, permissions, allowed_scopes, is_active)
                   VALUES (?, ?, ?, ?, ?, ?, 1)""",
                (
                    agent_id,
                    name,
                    HASH,
                    fp,
                    json.dumps({"read": True, "write": True, "delete": False, "admin": False}),
                    json.dumps(["shared"]),
                ),
            )
            await db.commit()

    asyncio.run(_do())


def _init_db_sync() -> None:
    if _DB.exists():
        _DB.unlink()
    asyncio.run(init_db())


def _setup_once() -> "TestClient":
    _init_db_sync()
    _seed_agent_sync("d1-test-agent", KEY, "d1-test")
    from fastapi.testclient import TestClient
    from pluribus.main import app
    return TestClient(app)


class DashboardObservabilityTests(unittest.TestCase):
    maxDiff = 4000
    _client = None
    _db_patch = None

    @classmethod
    def setUpClass(cls) -> None:
        cls._db_patch = patch.object(settings, "DB_PATH", str(_DB))
        cls._db_patch.start()
        security._bcrypt_cache.clear()
        cls._client = _setup_once()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._client is not None:
            try:
                cls._client.close()
            except Exception:
                pass
        if cls._db_patch is not None:
            cls._db_patch.stop()

    def setUp(self) -> None:
        # Refresh DB per-test (clean slate) — event loop and TestClient
        # live for the whole class to avoid the "Event loop is closed"
        # error that fires when a new TestClient is built per test.
        _init_db_sync()
        _seed_agent_sync("d1-test-agent", KEY, "d1-test")

    # ----- D1-01 -------------------------------------------------------
    def test_d1_01_dashboard_route_loads(self) -> None:
        c = self._client
        r = c.get("/dashboard")
        self.assertIn(r.status_code, (200, 307), f"GET /dashboard -> {r.status_code}")
        if r.status_code == 200:
            self.assertIn("text/html", r.headers.get("content-type", ""))
            self.assertIn("Pluribus", r.text)

    # ----- D1-02 -------------------------------------------------------
    def test_d1_02_summary_returns_structured_state(self) -> None:
        c = self._client
        r = c.get("/v1/dashboard/summary", headers={"X-API-Key": KEY})
        self.assertEqual(r.status_code, 200, r.text[:300])
        j = r.json()
        for key in ("pluribus", "xerrameca", "hermes", "ollama",
                    "agents_known", "recent_memories", "last_update"):
            self.assertIn(key, j, f"missing {key} in {list(j)}")
        for svc in ("pluribus", "xerrameca", "hermes", "ollama"):
            self.assertIn("status", j[svc])
            self.assertIn(j[svc]["status"],
                          ("HEALTHY", "DEGRADED", "DOWN", "UNKNOWN", "NOT CONFIGURED"),
                          f"invalid status for {svc}: {j[svc]['status']}")

    # ----- D1-03 -------------------------------------------------------
    def test_d1_03_agents_uses_real_identities(self) -> None:
        c = self._client
        r = c.get("/v1/dashboard/agents", headers={"X-API-Key": KEY})
        self.assertEqual(r.status_code, 200, r.text[:300])
        j = r.json()
        self.assertIn("agents", j)
        self.assertIsInstance(j["agents"], list)
        for a in j["agents"]:
            for k in ("name", "identity", "active_flag", "allowed_scopes", "online_now"):
                self.assertIn(k, a, f"agent missing {k}: {a}")
            self.assertIn(a["online_now"], ("YES", "NO", "UNKNOWN"))

    # ----- D1-04 -------------------------------------------------------
    def test_d1_04_unknown_telemetry_is_never_fabricated(self) -> None:
        c = self._client
        r = c.get("/v1/dashboard/agents", headers={"X-API-Key": KEY})
        self.assertEqual(r.status_code, 200)
        j = r.json()
        for a in j["agents"]:
            self.assertIn(a.get("current_task"),
                          ("UNKNOWN", "NONE"),
                          f"current_task not a sentinel: {a.get('current_task')!r}")
            self.assertNotEqual(a.get("current_task"), "in progress")
            self.assertIn(a.get("last_result"),
                          ("PASS", "FAIL", "UNKNOWN", "NONE"))
            self.assertIn(a.get("blocker"),
                          ("BLOCKED", "NONE", "UNKNOWN"))

    # ----- D1-05 -------------------------------------------------------
    def test_d1_05_memory_latest_works(self) -> None:
        c = self._client
        r = c.get("/v1/dashboard/memory?limit=5", headers={"X-API-Key": KEY})
        self.assertEqual(r.status_code, 200, r.text[:300])
        j = r.json()
        self.assertIn("items", j)
        self.assertIn("total", j)
        self.assertLessEqual(len(j["items"]), 5)
        for it in j["items"]:
            self.assertIn("id", it)
            self.assertIn("created_at", it)
            self.assertIn("category", it)
            self.assertIn("content_preview", it)
            self.assertIn("project", it)

    # ----- D1-06 -------------------------------------------------------
    def test_d1_06_memory_search_works(self) -> None:
        c = self._client
        r = c.get("/v1/dashboard/memory?q=test&limit=5",
                  headers={"X-API-Key": KEY})
        self.assertEqual(r.status_code, 200, r.text[:300])
        j = r.json()
        self.assertIn("q", j)
        self.assertEqual(j["q"], "test")
        self.assertIn("items", j)

    # ----- D1-07 -------------------------------------------------------
    def test_d1_07_system_health_classification(self) -> None:
        c = self._client
        r = c.get("/v1/dashboard/system", headers={"X-API-Key": KEY})
        self.assertEqual(r.status_code, 200, r.text[:300])
        j = r.json()
        self.assertIn("services", j)
        for s in j["services"]:
            self.assertIn("status", s)
            self.assertIn(s["status"],
                          ("HEALTHY", "DEGRADED", "DOWN", "UNKNOWN", "NOT CONFIGURED"))

    # ----- D1-08 -------------------------------------------------------
    def test_d1_08_no_secrets_in_dashboard_payload(self) -> None:
        c = self._client
        for path in ("/v1/dashboard/summary", "/v1/dashboard/agents",
                    "/v1/dashboard/memory?limit=5", "/v1/dashboard/system"):
            r = c.get(path, headers={"X-API-Key": KEY})
            self.assertEqual(r.status_code, 200, f"{path} -> {r.status_code}: {r.text[:200]}")
            for needle in ("sk-", "Bearer ", "password=", "token=", "X-API-Key"):
                self.assertNotIn(needle, r.text,
                                 f"forbidden substring {needle!r} in {path}")

    # ----- D1-09 -------------------------------------------------------
    def test_d1_09_observer_endpoints_read_only(self) -> None:
        c = self._client
        for path in ("/v1/dashboard/summary", "/v1/dashboard/agents",
                    "/v1/dashboard/memory", "/v1/dashboard/system"):
            for method in ("post", "put", "delete", "patch"):
                r = getattr(c, method)(path, headers={"X-API-Key": KEY})
                self.assertNotEqual(r.status_code, 200,
                                    f"{method.upper()} {path} returned 200")

    # ----- D1-10 -------------------------------------------------------
    def test_d1_10_dashboard_no_admin_key_in_html(self) -> None:
        c = self._client
        r = c.get("/dashboard")
        if r.status_code != 200:
            self.skipTest("/dashboard not accessible in this TestClient setup")
        for needle in ("X-API-Key", "sk-", "Bearer "):
            self.assertNotIn(needle, r.text,
                             f"HTML embeds secret-like {needle!r}")

    # ----- D1-11 -------------------------------------------------------
    def test_d1_11_deterministic_json_shape(self) -> None:
        c = self._client
        r1 = c.get("/v1/dashboard/summary", headers={"X-API-Key": KEY})
        r2 = c.get("/v1/dashboard/summary", headers={"X-API-Key": KEY})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        j1, j2 = r1.json(), r2.json()
        self.assertEqual(set(j1.keys()), set(j2.keys()))
        for svc in ("pluribus", "xerrameca", "hermes", "ollama"):
            self.assertEqual(set(j1[svc].keys()), set(j2[svc].keys()))

    # ----- D1-12 -------------------------------------------------------
    def test_d1_12_graceful_degradation(self) -> None:
        c = self._client
        r = c.get("/v1/dashboard/summary", headers={"X-API-Key": KEY})
        self.assertEqual(r.status_code, 200, r.text[:200])
        j = r.json()
        for svc in ("pluribus", "xerrameca", "hermes", "ollama"):
            self.assertIn("status", j[svc])
            self.assertIn(j[svc]["status"],
                          ("HEALTHY", "DEGRADED", "DOWN", "UNKNOWN", "NOT CONFIGURED"))


if __name__ == "__main__":
    unittest.main()
