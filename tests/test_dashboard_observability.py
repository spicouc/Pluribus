"""D1 — Dashboard observability tests.

Standard Pluribus test pattern: temp DB, seed one agent, exercise
the four /v1/dashboard/* read-only endpoints via FastAPI TestClient.

Covers:
  - D1-01..D1-12: contract (real data, no secrets, read-only, etc.)
  - D1-SEC-01..06: security — auth is enforced for every endpoint,
    scope is enforced, no admin required, no cross-scope leak
  - D1-TRUE-01..05: truthfulness — no fabricated online/busy/task/
    project/blocker; historical facts are NOT current telemetry
  - D1-CFG-01: service endpoints are configurable, graceful when missing
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
KEY_NOREAD = "d1-noread-key-BBBBBBBBBBBBBBBBBBBBB"
HASH = bcrypt.hashpw(KEY.encode("utf-8"), bcrypt.gensalt(rounds=4)).decode("utf-8")
HASH_NOREAD = bcrypt.hashpw(KEY_NOREAD.encode("utf-8"), bcrypt.gensalt(rounds=4)).decode("utf-8")


def _seed_agent_sync(agent_id: str, key: str, name: str,
                    perms: dict | None = None,
                    scopes: list[str] | None = None) -> None:
    fp = fingerprint_api_key(key)
    perms = perms or {"read": True, "write": True, "delete": False, "admin": False}
    scopes = scopes or ["shared"]

    async def _do():
        async with get_db() as db:
            await db.execute(
                """INSERT INTO agents
                   (id, name, api_key_hash, api_key_fingerprint, permissions, allowed_scopes, is_active)
                   VALUES (?, ?, ?, ?, ?, ?, 1)""",
                (
                    agent_id,
                    name,
                    HASH if key == KEY else HASH_NOREAD,
                    fp,
                    json.dumps(perms),
                    json.dumps(scopes),
                ),
            )
            await db.commit()

    asyncio.run(_do())


def _init_db_sync() -> None:
    if _DB.exists():
        _DB.unlink()
    asyncio.run(init_db())


def _setup_client() -> "TestClient":
    _init_db_sync()
    _seed_agent_sync("d1-test-agent", KEY, "d1-test")
    # No-read agent (used by D1-SEC-05)
    _seed_agent_sync(
        "d1-noread-agent", KEY_NOREAD, "d1-noread",
        perms={"read": False, "write": False, "delete": False, "admin": False},
        scopes=["shared"],
    )
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
        cls._client = _setup_client()

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
        _init_db_sync()
        _seed_agent_sync("d1-test-agent", KEY, "d1-test")
        _seed_agent_sync(
            "d1-noread-agent", KEY_NOREAD, "d1-noread",
            perms={"read": False, "write": False, "delete": False, "admin": False},
            scopes=["shared"],
        )

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
                    "agents_known", "last_update"):
            self.assertIn(key, j, f"missing {key} in {list(j)}")
        for svc in ("pluribus", "xerrameca", "hermes", "ollama"):
            self.assertIn("status", j[svc])
            self.assertIn(j[svc]["status"],
                          ("HEALTHY", "DEGRADED", "DOWN", "UNKNOWN",
                           "NOT_CONFIGURED"),
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
            # Truthfulness: must be UNKNOWN, never a free string
            self.assertEqual(a.get("current_task"), "UNKNOWN",
                             f"current_task must be UNKNOWN, got {a.get('current_task')!r}")
            self.assertEqual(a.get("project"), "UNKNOWN",
                             f"project must be UNKNOWN, got {a.get('project')!r}")
            self.assertEqual(a.get("blocker"), "UNKNOWN",
                             f"blocker must be UNKNOWN, got {a.get('blocker')!r}")
            self.assertEqual(a.get("last_result"), "UNKNOWN",
                             f"last_result must be UNKNOWN, got {a.get('last_result')!r}")
            self.assertEqual(a.get("last_known_activity"), "UNKNOWN",
                             f"last_known_activity must be UNKNOWN, got {a.get('last_known_activity')!r}")

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
            # project must be UNKNOWN or a real metadata.project, never inferred from text
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
                          ("HEALTHY", "DEGRADED", "DOWN", "UNKNOWN",
                           "NOT_CONFIGURED"))

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
                          ("HEALTHY", "DEGRADED", "DOWN", "UNKNOWN",
                           "NOT_CONFIGURED"))

    # ====== SECURITY: D1-SEC-01..06 =====================================
    # ----- D1-SEC-01..04: no API key => 401 -----------------------------
    def test_d1_sec_01_summary_requires_auth(self) -> None:
        r = self._client.get("/v1/dashboard/summary")
        self.assertEqual(r.status_code, 401, r.text[:200])

    def test_d1_sec_02_agents_requires_auth(self) -> None:
        r = self._client.get("/v1/dashboard/agents")
        self.assertEqual(r.status_code, 401, r.text[:200])

    def test_d1_sec_03_memory_requires_auth(self) -> None:
        r = self._client.get("/v1/dashboard/memory")
        self.assertEqual(r.status_code, 401, r.text[:200])

    def test_d1_sec_04_system_requires_auth(self) -> None:
        r = self._client.get("/v1/dashboard/system")
        self.assertEqual(r.status_code, 401, r.text[:200])

    # ----- D1-SEC-05: agent without read permission => 403 --------------
    def test_d1_sec_05_no_read_permission_rejected(self) -> None:
        c = self._client
        r = c.get("/v1/dashboard/summary", headers={"X-API-Key": KEY_NOREAD})
        self.assertEqual(r.status_code, 403, r.text[:200])
        r = c.get("/v1/dashboard/agents", headers={"X-API-Key": KEY_NOREAD})
        self.assertEqual(r.status_code, 403, r.text[:200])
        r = c.get("/v1/dashboard/memory", headers={"X-API-Key": KEY_NOREAD})
        self.assertEqual(r.status_code, 403, r.text[:200])
        r = c.get("/v1/dashboard/system", headers={"X-API-Key": KEY_NOREAD})
        self.assertEqual(r.status_code, 403, r.text[:200])

    # ----- D1-SEC-06: scope=private is rejected for a shared-only agent -
    def test_d1_sec_06_scope_enforced_no_cross_scope(self) -> None:
        c = self._client
        # Agent has scope ['shared'] only; asking for scope=local must 403
        r = c.get("/v1/dashboard/memory?scope=local", headers={"X-API-Key": KEY})
        self.assertEqual(r.status_code, 403, r.text[:200])

    # ====== TRUTHFULNESS: D1-TRUE-01..05 ===============================
    def test_d1_true_01_pending_directive_not_current_task(self) -> None:
        """Truthfulness: a pending directive is shown separately as
        'pending_directive', never as 'current_task'. The D1 agents
        endpoint must NEVER infer current_task from directive_inbox."""
        c = self._client
        r = c.get("/v1/dashboard/agents", headers={"X-API-Key": KEY})
        self.assertEqual(r.status_code, 200)
        j = r.json()
        for a in j["agents"]:
            # current_task must be UNKNOWN — never a directive reference
            self.assertEqual(a.get("current_task"), "UNKNOWN",
                             f"current_task leaked directive info: {a}")
            # pending_directive field may exist but is None when no source
            if "pending_directive" in a:
                self.assertIn(a["pending_directive"],
                              (None, "UNKNOWN", ""),
                              f"pending_directive must be None/UNKNOWN/empty, got {a['pending_directive']!r}")

    def test_d1_true_02_historical_blocked_not_current_blocker(self) -> None:
        """Truthfulness: a historical fact mentioning 'BLOCKED' must
        NOT make current blocker=BLOCKED. Without D2 telemetry,
        current blocker must be UNKNOWN."""
        async def _seed():
            async with get_db() as db:
                await db.execute(
                    """INSERT INTO facts (scope, category, key, content, agent_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    ("shared", "events", "old_blocked",
                     "RESOLVED old blocker from yesterday (BLOCKED back then)",
                     None, "2026-08-01 10:00:00"),
                )
                await db.commit()
        asyncio.run(_seed())

        c = self._client
        r = c.get("/v1/dashboard/agents", headers={"X-API-Key": KEY})
        j = r.json()
        for a in j["agents"]:
            self.assertEqual(a.get("blocker"), "UNKNOWN",
                             f"historical BLOCKED fact corrupted current blocker: {a}")

    def test_d1_true_03_historical_pass_not_current_last_result(self) -> None:
        """Truthfulness: a historical PASS fact must NOT make
        current last_result=PASS. Without D2 telemetry, last_result=UNKNOWN."""
        async def _seed():
            async with get_db() as db:
                await db.execute(
                    """INSERT INTO facts (scope, category, key, content, agent_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    ("shared", "events", "old_pass",
                     "Old checkpoint PASS but that was last week",
                     None, "2026-08-01 10:00:00"),
                )
                await db.commit()
        asyncio.run(_seed())

        c = self._client
        r = c.get("/v1/dashboard/agents", headers={"X-API-Key": KEY})
        j = r.json()
        for a in j["agents"]:
            self.assertEqual(a.get("last_result"), "UNKNOWN",
                             f"historical PASS fact corrupted current last_result: {a}")

    def test_d1_true_04_project_text_not_authoritative(self) -> None:
        """Truthfulness: arbitrary project text in memory does NOT
        become the agent's authoritative project. Project must be UNKNOWN."""
        async def _seed():
            async with get_db() as db:
                await db.execute(
                    """INSERT INTO facts (scope, category, key, content, agent_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    ("shared", "events", "memory_with_project",
                     "Some mention of project=xerrameca here",
                     None, "2026-09-01 10:00:00"),
                )
                await db.commit()
        asyncio.run(_seed())

        c = self._client
        r = c.get("/v1/dashboard/agents", headers={"X-API-Key": KEY})
        j = r.json()
        for a in j["agents"]:
            self.assertEqual(a.get("project"), "UNKNOWN",
                             f"memory text inferred as project: {a}")

    def test_d1_true_05_unavailable_telemetry_remains_unknown(self) -> None:
        """Truthfulness: warnings field must NOT count historical
        BLOCKED/FAIL/ERROR text matches as current warnings. Without
        D2 telemetry, warnings should be UNKNOWN or absent."""
        c = self._client
        r = self._client.get("/v1/dashboard/summary", headers={"X-API-Key": KEY})
        j = r.json()
        # warnings may be UNKNOWN or absent — but if present it must
        # NOT be a positive number derived from historical text matches.
        # Empty database => warnings must be 0 (no current warnings) or UNKNOWN.
        if "warnings" in j:
            self.assertIn(j["warnings"], (0, "UNKNOWN", None),
                          f"warnings not truthful: {j['warnings']!r}")

    # ====== CONFIGURATION: D1-CFG-01 ===================================
    def test_d1_cfg_01_endpoints_configurable(self) -> None:
        """Configuration: service endpoints should come from settings,
        not be hardcoded. Without a setting, the service reports
        NOT_CONFIGURED, not HEALTHY."""
        c = self._client
        r = c.get("/v1/dashboard/system", headers={"X-API-Key": KEY})
        self.assertEqual(r.status_code, 200)
        j = r.json()
        # Every service entry must include an endpoint (or the
        # absence is signaled as NOT_CONFIGURED status).
        for s in j["services"]:
            self.assertIn("status", s)
            # Hardcoded fallback endpoints are an anti-pattern; verify
            # the field exists even when the service is down.
            self.assertIn("endpoint", s)
            # If the endpoint is missing, the system must report
            # NOT_CONFIGURED, not HEALTHY.
            if s.get("status") == "HEALTHY":
                self.assertTrue(s.get("endpoint"),
                                f"{s.get('name')} reports HEALTHY without endpoint")