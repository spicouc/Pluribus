"""D1 — Dashboard observability tests.

Covers:
  - D1-01..D1-12: contract (real data, no secrets, read-only, etc.)
  - D1-SEC-01..08: security — auth (cookie OR X-API-Key), read perm,
    scope, no admin, secret redaction, URL sanitization
  - D1-TRUE-01..06: truthfulness — no fabricated online/busy/task/
    project/blocker; historical facts NOT current telemetry
  - D1-CFG-01: service endpoints configurable, graceful when missing
  - D1-E2E-01: real browser flow (cookie login, then data calls)
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
                """INSERT OR REPLACE INTO agents
                   (id, name, api_key_hash, api_key_fingerprint, permissions, allowed_scopes, is_active)
                   VALUES (?, ?, ?, ?, ?, ?, 1)""",
                (
                    agent_id, name,
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


def _seed_secret_fixtures() -> None:
    """Seed facts containing realistic secret-like material to verify
    redaction. None of these are real credentials."""
    fixtures = [
        "sk-abcdef1234567890abcdef1234567890abcdef",
        "Authorization: Bearer eyJabc.def.ghi",
        "X-API-Key: live-prod-12345-secret",
        "password=hunter2-secret",
        "token=ghp_AbCdEf123456",
        "api-key: my-test-key-12345",
        "url with https://user:pass@host.example.com/path",
    ]
    async def _do():
        async with get_db() as db:
            for i, content in enumerate(fixtures):
                await db.execute(
                    """INSERT INTO facts
                       (scope, category, key, content, agent_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    ("shared", "events", f"fixture_secret_{i}",
                     content, None, "2026-09-01 10:00:00"),
                )
            await db.commit()
    asyncio.run(_do())


def _setup_client():
    _init_db_sync()
    _seed_agent_sync("d1-test-agent", KEY, "d1-test")
    _seed_agent_sync(
        "d1-noread-agent", KEY_NOREAD, "d1-noread",
        perms={"read": False, "write": False, "delete": False, "admin": False},
        scopes=["shared"],
    )
    from fastapi.testclient import TestClient
    from pluribus.main import app
    return TestClient(app)


def _cookie_name() -> str:
    from pluribus.dashboard_session import SESSION_COOKIE_NAME
    return SESSION_COOKIE_NAME


def _login(client, key: str = KEY):
    """Server-to-server login: POST with X-API-Key, return cookies."""
    r = client.post("/v1/dashboard/login", headers={"X-API-Key": key})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return dict(r.cookies)


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
        # Clear any cookies left over from a previous test
        if self._client is not None:
            self._client.cookies.clear()

    # ====== D1-01..D1-12: contract =====================================
    def test_d1_01_dashboard_route_loads(self) -> None:
        c = self._client
        r = c.get("/dashboard")
        self.assertIn(r.status_code, (200, 307), f"GET /dashboard -> {r.status_code}")
        if r.status_code == 200:
            self.assertIn("text/html", r.headers.get("content-type", ""))
            self.assertIn("Pluribus", r.text)

    def test_d1_02_summary_returns_structured_state(self) -> None:
        c = self._client
        cookies = _login(c)
        r = c.get("/v1/dashboard/summary", cookies=cookies)
        self.assertEqual(r.status_code, 200, r.text[:300])
        j = r.json()
        for key in ("pluribus", "xerrameca", "hermes", "ollama",
                    "agents_known", "recent_memories", "last_update"):
            self.assertIn(key, j, f"missing {key} in {list(j)}")
        for svc in ("pluribus", "xerrameca", "hermes", "ollama"):
            self.assertIn("status", j[svc])
            self.assertIn(j[svc]["status"],
                          ("HEALTHY", "DEGRADED", "DOWN", "UNKNOWN", "NOT_CONFIGURED"))

    def test_d1_03_agents_uses_real_identities(self) -> None:
        c = self._client
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        self.assertEqual(r.status_code, 200, r.text[:300])
        j = r.json()
        self.assertIn("agents", j)
        self.assertIsInstance(j["agents"], list)
        for a in j["agents"]:
            for k in ("name", "identity", "active_flag", "allowed_scopes", "online_now"):
                self.assertIn(k, a, f"agent missing {k}: {a}")
            self.assertIn(a["online_now"], ("YES", "NO", "UNKNOWN"))

    def test_d1_04_unknown_telemetry_is_never_fabricated(self) -> None:
        c = self._client
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        self.assertEqual(r.status_code, 200)
        j = r.json()
        for a in j["agents"]:
            self.assertEqual(a.get("current_task"), "UNKNOWN")
            self.assertEqual(a.get("project"), "UNKNOWN")
            self.assertEqual(a.get("blocker"), "UNKNOWN")
            self.assertEqual(a.get("last_result"), "UNKNOWN")
            self.assertEqual(a.get("last_known_activity"), "UNKNOWN")

    def test_d1_05_memory_latest_works(self) -> None:
        c = self._client
        cookies = _login(c)
        r = c.get("/v1/dashboard/memory?limit=5", cookies=cookies)
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

    def test_d1_06_memory_search_works(self) -> None:
        c = self._client
        cookies = _login(c)
        r = c.get("/v1/dashboard/memory?q=test&limit=5", cookies=cookies)
        self.assertEqual(r.status_code, 200, r.text[:300])
        j = r.json()
        self.assertIn("q", j)
        self.assertEqual(j["q"], "test")
        self.assertIn("items", j)

    def test_d1_07_system_health_classification(self) -> None:
        c = self._client
        cookies = _login(c)
        r = c.get("/v1/dashboard/system", cookies=cookies)
        self.assertEqual(r.status_code, 200, r.text[:300])
        j = r.json()
        self.assertIn("services", j)
        for s in j["services"]:
            self.assertIn("status", s)
            self.assertIn(s["status"],
                          ("HEALTHY", "DEGRADED", "DOWN", "UNKNOWN", "NOT_CONFIGURED"))

    def test_d1_08_no_secrets_in_dashboard_payload(self) -> None:
        c = self._client
        cookies = _login(c)
        for path in ("/v1/dashboard/summary", "/v1/dashboard/agents",
                    "/v1/dashboard/memory?limit=5", "/v1/dashboard/system"):
            r = c.get(path, cookies=cookies)
            self.assertEqual(r.status_code, 200, f"{path} -> {r.status_code}: {r.text[:200]}")
            for needle in ("sk-abcdef", "Bearer eyJ", "X-API-Key", "password=",
                           "token=", "Bearer eyJhbGciOi"):
                self.assertNotIn(needle, r.text,
                                 f"forbidden substring {needle!r} in {path}")

    def test_d1_09_observer_endpoints_read_only(self) -> None:
        c = self._client
        cookies = _login(c)
        for path in ("/v1/dashboard/summary", "/v1/dashboard/agents",
                    "/v1/dashboard/memory", "/v1/dashboard/system"):
            for method in ("post", "put", "delete", "patch"):
                r = getattr(c, method)(path, cookies=cookies)
                self.assertNotEqual(r.status_code, 200,
                                    f"{method.upper()} {path} returned 200")

    def test_d1_10_dashboard_no_admin_key_in_html(self) -> None:
        c = self._client
        r = c.get("/dashboard")
        if r.status_code != 200:
            self.skipTest("/dashboard not accessible in this TestClient setup")
        for needle in ("X-API-Key", "sk-", "Bearer "):
            self.assertNotIn(needle, r.text,
                             f"HTML embeds secret-like {needle!r}")

    def test_d1_11_deterministic_json_shape(self) -> None:
        c = self._client
        cookies = _login(c)
        r1 = c.get("/v1/dashboard/summary", cookies=cookies)
        r2 = c.get("/v1/dashboard/summary", cookies=cookies)
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        j1, j2 = r1.json(), r2.json()
        self.assertEqual(set(j1.keys()), set(j2.keys()))
        for svc in ("pluribus", "xerrameca", "hermes", "ollama"):
            self.assertEqual(set(j1[svc].keys()), set(j2[svc].keys()))

    def test_d1_12_graceful_degradation(self) -> None:
        c = self._client
        cookies = _login(c)
        r = c.get("/v1/dashboard/summary", cookies=cookies)
        self.assertEqual(r.status_code, 200, r.text[:200])
        j = r.json()
        for svc in ("pluribus", "xerrameca", "hermes", "ollama"):
            self.assertIn("status", j[svc])
            self.assertIn(j[svc]["status"],
                          ("HEALTHY", "DEGRADED", "DOWN", "UNKNOWN", "NOT_CONFIGURED"))

    # ====== SECURITY: D1-SEC-01..08 ======================================
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

    def test_d1_sec_05_no_read_permission_rejected(self) -> None:
        c = self._client
        # No-read agent: even with X-API-Key must be rejected
        for path in ("/v1/dashboard/summary", "/v1/dashboard/agents",
                     "/v1/dashboard/memory", "/v1/dashboard/system"):
            r = c.get(path, headers={"X-API-Key": KEY_NOREAD})
            self.assertEqual(r.status_code, 403, f"{path} -> {r.status_code}: {r.text[:200]}")

    def test_d1_sec_06_scope_enforced_no_cross_scope(self) -> None:
        c = self._client
        cookies = _login(c)
        r = c.get("/v1/dashboard/memory?scope=local", cookies=cookies)
        self.assertEqual(r.status_code, 403, r.text[:200])

    def test_d1_sec_07_secret_fixtures_redacted(self) -> None:
        """Real secret-like material is REDACTED in memory payloads."""
        _seed_secret_fixtures()
        c = self._client
        cookies = _login(c)
        r = c.get("/v1/dashboard/memory?limit=20", cookies=cookies)
        self.assertEqual(r.status_code, 200)
        body = r.text
        # None of the secret fragments should be present verbatim
        for secret in ("sk-abcdef1234567890", "eyJabc.def.ghi",
                       "live-prod-12345-secret", "hunter2-secret",
                       "ghp_AbCdEf123456", "my-test-key-12345",
                       "user:pass@host.example.com"):
            self.assertNotIn(secret, body,
                             f"secret fragment leaked: {secret!r}")
        # At least one [REDACTED] marker present
        self.assertIn("[REDACTED", body,
                      "no REDACTED marker in any memory payload")

    def test_d1_sec_08_service_url_sanitized(self) -> None:
        """Service endpoint URLs never expose userinfo / query / fragment."""
        c = self._client
        cookies = _login(c)
        r = c.get("/v1/dashboard/system", cookies=cookies)
        self.assertEqual(r.status_code, 200)
        j = r.json()
        for s in j["services"]:
            ep = s.get("endpoint")
            if ep in (None, "UNKNOWN"):
                continue
            # Endpoint must be scheme://host[:port] only
            self.assertNotIn("@", ep, f"endpoint has userinfo: {ep}")
            self.assertNotIn("?", ep, f"endpoint has query: {ep}")
            self.assertNotIn("#", ep, f"endpoint has fragment: {ep}")

    # ====== TRUTHFULNESS: D1-TRUE-01..06 ===============================
    def test_d1_true_01_pending_directive_not_current_task(self) -> None:
        c = self._client
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        j = r.json()
        for a in j["agents"]:
            self.assertEqual(a.get("current_task"), "UNKNOWN")
            if "pending_directive" in a:
                self.assertIn(a["pending_directive"], (None, "UNKNOWN", ""))

    def test_d1_true_02_historical_blocked_not_current_blocker(self) -> None:
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
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        j = r.json()
        for a in j["agents"]:
            self.assertEqual(a.get("blocker"), "UNKNOWN")

    def test_d1_true_03_historical_pass_not_current_last_result(self) -> None:
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
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        j = r.json()
        for a in j["agents"]:
            self.assertEqual(a.get("last_result"), "UNKNOWN")

    def test_d1_true_04_project_text_not_authoritative(self) -> None:
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
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        j = r.json()
        for a in j["agents"]:
            self.assertEqual(a.get("project"), "UNKNOWN")

    def test_d1_true_05_unavailable_telemetry_remains_unknown(self) -> None:
        c = self._client
        cookies = _login(c)
        r = c.get("/v1/dashboard/summary", cookies=cookies)
        j = r.json()
        if "warnings" in j:
            self.assertIn(j["warnings"], (0, "UNKNOWN", None))

    def test_d1_true_06_recent_memory_not_fake_zero(self) -> None:
        """recent_memories must be a REAL count, not a fake 0."""
        async def _seed():
            async with get_db() as db:
                for i in range(7):
                    await db.execute(
                        """INSERT INTO facts (scope, category, key, content, agent_id, created_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        ("shared", "events", f"recent_{i}",
                         f"recent memory fact {i}", None, "2026-09-02 10:00:00"),
                    )
                await db.commit()
        asyncio.run(_seed())

        c = self._client
        cookies = _login(c)
        r = c.get("/v1/dashboard/summary", cookies=cookies)
        j = r.json()
        self.assertIn("recent_memories", j)
        # Empty DB test would give 0, but here we have 7 facts seeded
        self.assertEqual(j["recent_memories"], 7,
                         f"recent_memories should reflect real count, got {j['recent_memories']!r}")

    # ====== CONFIGURATION: D1-CFG-01 ===================================
    def test_d1_cfg_01_endpoints_configurable(self) -> None:
        c = self._client
        cookies = _login(c)
        r = c.get("/v1/dashboard/system", cookies=cookies)
        self.assertEqual(r.status_code, 200)
        j = r.json()
        for s in j["services"]:
            self.assertIn("status", s)
            self.assertIn("endpoint", s)
            if s.get("status") == "HEALTHY":
                self.assertTrue(s.get("endpoint") and s["endpoint"] != "UNKNOWN",
                                f"{s.get('name')} reports HEALTHY without sanitized endpoint")

    # ====== E2E BROWSER FLOW: D1-E2E-01 =================================
    def test_d1_e2e_01_browser_flow(self) -> None:
        """Real browser flow: login with X-API-Key, then call the four
        data endpoints with the cookie. No X-API-Key in subsequent
        calls. No admin permission used."""
        c = self._client

        # 1. Unauthenticated data endpoints are 401
        for path in ("/v1/dashboard/summary", "/v1/dashboard/agents",
                     "/v1/dashboard/memory", "/v1/dashboard/system"):
            r = c.get(path)
            self.assertEqual(r.status_code, 401, f"{path} expected 401 unauth")

        # 2. Login: server-to-server with X-API-Key
        r = c.post("/v1/dashboard/login", headers={"X-API-Key": KEY})
        self.assertEqual(r.status_code, 200, r.text[:200])
        cname = _cookie_name()
        # The response cookies may be a dict[str, str] or a
        # RequestsCookieJar (varies by httpx version). We just need
        # the token value.
        token = r.cookies.get(cname) if hasattr(r.cookies, "get") else None
        if token is None:
            # Fallback: scan the raw Set-Cookie header
            for k, v in r.cookies.items():
                if k == cname:
                    token = v
                    break
        self.assertIsNotNone(token, f"login did not set {cname!r} cookie: {r.cookies!r}")
        cookies = {cname: str(token)}

        # 3. The four data endpoints now succeed with the cookie
        # (NO X-API-Key header — that's the whole point)
        for path in ("/v1/dashboard/summary", "/v1/dashboard/agents",
                     "/v1/dashboard/memory?limit=5", "/v1/dashboard/system"):
            r = c.get(path, cookies=cookies)
            self.assertEqual(r.status_code, 200,
                             f"{path} with cookie expected 200, got {r.status_code}: {r.text[:200]}")

        # 4. No-read user is rejected even with their own login
        r = c.post("/v1/dashboard/login", headers={"X-API-Key": KEY_NOREAD})
        # The login itself requires read, so a no-read agent gets 403 here
        self.assertEqual(r.status_code, 403, r.text[:200])

        # 5. Logout invalidates the session
        r = c.post("/v1/dashboard/logout", cookies=cookies)
        self.assertEqual(r.status_code, 200, r.text[:200])
        r = c.get("/v1/dashboard/summary", cookies=cookies)
        self.assertEqual(r.status_code, 401, "session still valid after logout")

    def test_d1_e2e_02_x_api_key_fallback(self) -> None:
        """Server-to-server path: X-API-Key still works (CI/tests)."""
        c = self._client
        r = c.get("/v1/dashboard/summary", headers={"X-API-Key": KEY})
        self.assertEqual(r.status_code, 200, r.text[:200])
        j = r.json()
        self.assertIn("pluribus", j)

    # ====== D1-E2E-02..07: HUMAN LOGIN FLOW (one-time code) ==========
    def _fresh_client(self):
        from fastapi.testclient import TestClient
        return TestClient(self._client.app)

    def test_d1_e2e_02_human_login_flow(self) -> None:
        """Real human login flow: operator mints a one-time code via
        X-API-Key, browser submits the code via POST /dashboard/login
        and receives a session cookie. Then /v1/dashboard/* works
        from the browser with NO X-API-Key."""
        c = self._client

        # 1. /dashboard/login page is public
        r = c.get("/dashboard/login")
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertIn("text/html", r.headers.get("content-type", ""))

        # 2. Operator (server-to-server) mints a one-time code
        r = c.post("/v1/dashboard/login-code", headers={"X-API-Key": KEY})
        self.assertEqual(r.status_code, 200, r.text[:300])
        body = r.json()
        self.assertTrue(body.get("ok"))
        self.assertIn("code", body)
        code = body["code"]
        self.assertNotEqual(code, KEY)
        self.assertNotIn(KEY, code)
        self.assertLessEqual(len(code), 16)

        # 3. Browser-equivalent client WITHOUT X-API-Key submits the code
        c2 = self._fresh_client()
        r = c2.post("/dashboard/login", json={"code": code})
        self.assertEqual(r.status_code, 200, r.text[:200])
        cname = _cookie_name()
        token = r.cookies.get(cname) if hasattr(r.cookies, "get") else None
        if token is None:
            sc = r.headers.get("set-cookie", "")
            if cname + "=" in sc:
                token = sc.split(cname + "=")[1].split(";")[0]
        self.assertIsNotNone(token, f"login did not set {cname!r}")

        # 4. Browser (fresh client) can now hit the dashboard
        for path in ("/v1/dashboard/summary", "/v1/dashboard/agents",
                     "/v1/dashboard/memory?limit=5", "/v1/dashboard/system"):
            r = c2.get(path, cookies={cname: str(token)})
            self.assertEqual(r.status_code, 200,
                             f"{path} with cookie expected 200, got {r.status_code}: {r.text[:200]}")

        # 5. /dashboard also loads
        r = c2.get("/dashboard")
        self.assertIn(r.status_code, (200, 307), f"GET /dashboard -> {r.status_code}")

    def test_d1_e2e_03_one_time_code_rejected_on_reuse(self) -> None:
        c = self._client
        r = c.post("/v1/dashboard/login-code", headers={"X-API-Key": KEY})
        code = r.json()["code"]
        c2 = self._fresh_client()
        r1 = c2.post("/dashboard/login", json={"code": code})
        self.assertEqual(r1.status_code, 200)
        c3 = self._fresh_client()
        r2 = c3.post("/dashboard/login", json={"code": code})
        self.assertEqual(r2.status_code, 401, r2.text[:200])

    def test_d1_e2e_04_expired_code_rejected(self) -> None:
        c = self._client
        r = c.post("/v1/dashboard/login-code", headers={"X-API-Key": KEY})
        code = r.json()["code"]
        import asyncio
        async def _expire():
            from pluribus.db import get_db
            from pluribus.dashboard_session import _hash_code
            async with get_db() as db:
                await db.execute(
                    "UPDATE dashboard_login_codes SET expires_at = 0 WHERE code_hash = ?",
                    (_hash_code(code),),
                )
                await db.commit()
        asyncio.run(_expire())
        c2 = self._fresh_client()
        r = c2.post("/dashboard/login", json={"code": code})
        self.assertEqual(r.status_code, 401, r.text[:200])

    def test_d1_e2e_05_invalid_code_rejected(self) -> None:
        c2 = self._fresh_client()
        r = c2.post("/dashboard/login", json={"code": "DEADBE"})
        self.assertEqual(r.status_code, 401, r.text[:200])
        r = c2.post("/dashboard/login", json={"code": ""})
        self.assertIn(r.status_code, (400, 401))

    def test_d1_e2e_06_logout_invalidates_session(self) -> None:
        c = self._client
        r = c.post("/v1/dashboard/login-code", headers={"X-API-Key": KEY})
        code = r.json()["code"]
        c2 = self._fresh_client()
        r = c2.post("/dashboard/login", json={"code": code})
        cname = _cookie_name()
        token = r.cookies.get(cname) if hasattr(r.cookies, "get") else None
        if token is None:
            sc = r.headers.get("set-cookie", "")
            token = sc.split(cname + "=")[1].split(";")[0] if cname + "=" in sc else None
        r = c2.post("/v1/dashboard/logout", cookies={cname: str(token)})
        self.assertEqual(r.status_code, 200, r.text[:200])
        r = c2.get("/v1/dashboard/summary", cookies={cname: str(token)})
        self.assertEqual(r.status_code, 401, r.text[:200])

    def test_d1_e2e_07_session_lifetime_fixed(self) -> None:
        c = self._client
        r = c.post("/v1/dashboard/login", headers={"X-API-Key": KEY})
        self.assertEqual(r.status_code, 200, r.text[:200])
        body = r.json()
        from pluribus.dashboard_session import SESSION_TTL_SECONDS
        self.assertEqual(body.get("ttl_seconds"), SESSION_TTL_SECONDS,
                         f"session ttl_seconds={body.get('ttl_seconds')!r} != SESSION_TTL_SECONDS={SESSION_TTL_SECONDS}")
        import time
        expected = int(time.time()) + SESSION_TTL_SECONDS
        self.assertLess(abs(body.get("expires_at", 0) - expected), 5)
