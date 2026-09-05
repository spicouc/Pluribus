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
    async def _go():
        await init_db()
        from pluribus.directives_schema import init_directives_db
        await init_directives_db()
    asyncio.run(_go())


def _clear_directives_sync() -> None:
    """Wipe any directives left over from a previous test. We do
    this at the start of each D2-C test because the setUp wipes
    the DB on disk and re-creates the schema, but it is possible
    for an in-process connection to retain rows. Defensive."""
    async def _go():
        from pluribus.db import get_db
        async with get_db() as db:
            await db.execute("DELETE FROM directives")
            await db.execute("DELETE FROM facts")
            await db.commit()
    asyncio.run(_go())


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
        # Reset Pluribus process-global mutable security state. These
        # are module-level dicts that survive DB recreation across
        # tests in the same pytest process. Without this reset, a
        # test that consumes the per-agent rate limit bleeds into
        # the next test, which sees HTTP 429 from /v1/dashboard/login.
        from pluribus import security as _sec
        _sec._rate_limiter.clear()
        _sec._bcrypt_cache.clear()
        _sec._last_rate_cleanup = 0.0
        if hasattr(_sec, "_legacy_scan_by_client"):
            _sec._legacy_scan_by_client.clear()
        if hasattr(_sec, "_legacy_scan_global"):
            _sec._legacy_scan_global.clear()

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
            for k in ("name", "identity", "active_flag", "allowed_scopes", "presence"):
                self.assertIn(k, a, f"agent missing {k}: {a}")
            self.assertIn(a["presence"], ("ONLINE", "STALE", "OFFLINE", "UNKNOWN"))

    def test_d1_04_unknown_telemetry_when_no_heartbeat(self) -> None:
        """When the agent has not sent a heartbeat, the dashboard
        reports presence=UNKNOWN, work_state=UNKNOWN, current_task=UNKNOWN,
        project=UNKNOWN, last_known_activity=UNKNOWN. This is the
        D1 truthfulness rule carried into D2-B: telemetry is only
        real when the agent has explicitly reported it."""
        c = self._client
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        self.assertEqual(r.status_code, 200)
        j = r.json()
        for a in j["agents"]:
            self.assertEqual(a.get("presence"), "UNKNOWN")
            self.assertEqual(a.get("work_state"), "UNKNOWN")
            self.assertEqual(a.get("current_task"), "UNKNOWN")
            self.assertEqual(a.get("project"), "UNKNOWN")
            self.assertEqual(a.get("blocker"), "UNKNOWN")
            self.assertEqual(a.get("last_result"), "UNKNOWN")
            self.assertEqual(a.get("last_known_activity"), "UNKNOWN")
            self.assertIsNone(a.get("pending_directive"))

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
        self.assertLessEqual(len(code), 64)
        # Verify code carries >= 128 bits of entropy (D1-SEC-10).
        # token_urlsafe(16) produces 22 base64-url chars = 128 bits.
        import base64
        # Reconstruct the bytes from the urlsafe string (with padding
        # stripped) and check that the original 16 bytes are present.
        # urlsafe-base64 without padding: each char encodes 6 bits.
        # For 16 bytes we expect a 22-char string.
        try:
            padded = code + "=" * (-len(code) % 4)
            decoded = base64.urlsafe_b64decode(padded)
            self.assertGreaterEqual(len(decoded) * 8, 128,
                f"login code entropy {len(decoded) * 8} bits < 128")
        except Exception as e:
            self.fail(f"login code not decodeable as urlsafe base64: {e}")

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

    # ====== D1-SEC-09..12: FINAL SECURITY CORRECTIVE =================
    def test_d1_sec_09_login_code_rate_limit(self) -> None:
        """Repeated invalid login attempts are rate-limited per IP.
        After 5 failed attempts, the 6th returns 429 (Too Many Requests).
        Successful logins are not counted against the limit."""
        # Mint a code so we have a valid one to use later
        r = self._client.post("/v1/dashboard/login-code", headers={"X-API-Key": KEY})
        valid_code = r.json()["code"]

        c = self._fresh_client()
        # 5 failed attempts
        for i in range(5):
            r = c.post("/dashboard/login", json={"code": "WRONGCODE" + str(i)})
            # Each may be 401 (invalid code) but NOT 429 yet
            self.assertIn(r.status_code, (401, 429),
                          f"attempt {i+1} unexpected status {r.status_code}: {r.text[:100]}")

        # 6th attempt should be 429 (rate-limited)
        r = c.post("/dashboard/login", json={"code": "STILLWRONG"})
        # The 5th might have already triggered it; either way the
        # rate limit must kick in somewhere around here.
        self.assertIn(r.status_code, (429, 401),
                      f"after 5+ failed attempts expected rate limit, got {r.status_code}: {r.text[:100]}")
        # And the next one must definitely be 429
        r = c.post("/dashboard/login", json={"code": "AGAIN"})
        self.assertEqual(r.status_code, 429, f"expected 429, got {r.status_code}: {r.text[:100]}")

    def test_d1_sec_10_login_code_entropy(self) -> None:
        """Login code carries >= 128 bits of entropy."""
        import base64
        codes = set()
        for _ in range(10):
            r = self._client.post("/v1/dashboard/login-code", headers={"X-API-Key": KEY})
            self.assertEqual(r.status_code, 200)
            code = r.json()["code"]
            codes.add(code)
            # Decode the urlsafe base64 string
            padded = code + "=" * (-len(code) % 4)
            decoded = base64.urlsafe_b64decode(padded)
            self.assertGreaterEqual(len(decoded) * 8, 128,
                f"login code entropy {len(decoded) * 8} bits < 128 (code={code!r})")
        # 10 codes must all be unique
        self.assertEqual(len(codes), 10, "login codes collided — randomness broken")

    def test_d1_sec_11_raw_code_not_persisted(self) -> None:
        """The raw login code is never stored in the DB — only its SHA-256 digest."""
        r = self._client.post("/v1/dashboard/login-code", headers={"X-API-Key": KEY})
        raw_code = r.json()["code"]
        # Inspect the database
        import asyncio
        from pluribus.db import get_db
        async def _check():
            async with get_db() as db:
                cur = await db.execute("SELECT * FROM dashboard_login_codes")
                rows = await cur.fetchall()
                return rows
        rows = asyncio.run(_check())
        # No row may contain the raw code as a substring
        for row in rows:
            row_str = " ".join(str(c) for c in row)
            self.assertNotIn(raw_code, row_str,
                f"raw login code leaked into DB row: {row!r}")
            # The code_hash must be a 64-hex string (SHA-256 digest)
            # which is the standard format
            code_hash = row[0]  # code_hash is the primary key
            self.assertEqual(len(code_hash), 64, f"code_hash not 64-hex: {code_hash!r}")
            self.assertTrue(all(c in "0123456789abcdef" for c in code_hash),
                f"code_hash not hex: {code_hash!r}")

    def test_d1_sec_12_api_key_normal_middleware_path(self) -> None:
        """X-API-Key calls to /v1/dashboard/* go through the normal
        global middleware (request.state.agent is set, rate-limited).
        Verified indirectly: a valid X-API-Key succeeds; an invalid
        one fails with 'Clau API invalida' (which is the normal
        middleware's message), NOT 'Falta la capçalera X-API-Key'."""
        # Valid X-API-Key: success
        r = self._client.get("/v1/dashboard/summary", headers={"X-API-Key": KEY})
        self.assertEqual(r.status_code, 200, r.text[:200])
        # Invalid X-API-Key: the normal middleware path applies
        r = self._client.get("/v1/dashboard/summary", headers={"X-API-Key": "wrong-key-12345678901234567890"})
        self.assertEqual(r.status_code, 401, r.text[:200])
        self.assertIn("Clau API inv", r.json().get("detail", "") or r.text)

    # ====== D2-B: AGENT TELEMETRY FOUNDATION =========================
    def test_d2_01_heartbeat_updates_last_active_at(self) -> None:
        """Heartbeat updates last_active_at with SERVER time (not a
        client-supplied value)."""
        # Pre-seed: no last_active_at
        c = self._client
        # The setUp() has already created the agent
        # Issue a heartbeat via the agents router
        r = c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY})
        self.assertEqual(r.status_code, 204, r.text[:200])
        # The dashboard now shows the agent with a real presence
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        self.assertEqual(r.status_code, 200)
        agents = {a["identity"]: a for a in r.json()["agents"]}
        self.assertIn("d1-test-agent", agents)
        a = agents["d1-test-agent"]
        self.assertIn(a["presence"], ("ONLINE", "STALE"))
        # last_known_activity is a real timestamp, not UNKNOWN
        self.assertNotEqual(a["last_known_activity"], "UNKNOWN")
        self.assertIsNotNone(a["last_known_activity"])
        self.assertIsNotNone(a["age_seconds"])

    def test_d2_02_agent_can_update_own_telemetry(self) -> None:
        """An agent can PATCH its own heartbeat with telemetry fields."""
        c = self._client
        r = c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY},
                    json={"work_state": "WORKING",
                          "current_project": "D2-B",
                          "current_blocker": "",
                          "current_task_id": "dir-1"})
        self.assertEqual(r.status_code, 204, r.text[:200])
        # Verify via dashboard
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        agents = {a["identity"]: a for a in r.json()["agents"]}
        a = agents["d1-test-agent"]
        self.assertEqual(a["work_state"], "WORKING")
        self.assertEqual(a["project"], "D2-B")
        # current_blocker = "" means "explicit NONE" (D2-B semantics)
        self.assertEqual(a["blocker"], "NONE")

    def test_d2_03_agent_cannot_update_another_agent_telemetry(self) -> None:
        """Self-only rule: agent A cannot heartbeat for agent B."""
        c = self._client
        r = c.patch("/v1/agents/d1-noread-agent/heartbeat", headers={"X-API-Key": KEY},
                    json={"work_state": "WORKING"})
        self.assertEqual(r.status_code, 403, r.text[:200])
        # Verify the other agent is unchanged
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        agents = {a["identity"]: a for a in r.json()["agents"]}
        # The no-read agent should still show UNKNOWN
        noread = agents.get("d1-noread-agent")
        if noread:
            self.assertEqual(noread["presence"], "UNKNOWN")

    def test_d2_04_omitted_work_state_preserves_existing(self) -> None:
        """If a heartbeat body omits work_state, the previous value
        is preserved (do NOT default to IDLE)."""
        c = self._client
        # First heartbeat: WORKING
        r = c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY},
                    json={"work_state": "WORKING"})
        self.assertEqual(r.status_code, 204, r.text[:200])
        # Second heartbeat: no body
        r = c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY})
        self.assertEqual(r.status_code, 204, r.text[:200])
        # Verify work_state is still WORKING (preserved)
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        agents = {a["identity"]: a for a in r.json()["agents"]}
        a = agents["d1-test-agent"]
        self.assertEqual(a["work_state"], "WORKING",
                         f"omitted work_state should preserve prior; got {a['work_state']!r}")

    def test_d2_05_explicit_idle_sets_idle(self) -> None:
        """An explicit work_state=IDLE sets IDLE (overrides previous)."""
        c = self._client
        # Set to WORKING
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY},
               json={"work_state": "WORKING"})
        # Then set to IDLE explicitly
        r = c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY},
                    json={"work_state": "IDLE"})
        self.assertEqual(r.status_code, 204, r.text[:200])
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        agents = {a["identity"]: a for a in r.json()["agents"]}
        self.assertEqual(agents["d1-test-agent"]["work_state"], "IDLE")

    def test_d2_06_invalid_work_state_rejected(self) -> None:
        """An invalid work_state is rejected with 422."""
        c = self._client
        r = c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY},
                    json={"work_state": "BUSY"})  # not in the allowed list
        self.assertEqual(r.status_code, 422, r.text[:200])
        # The value is NOT saved
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        agents = {a["identity"]: a for a in r.json()["agents"]}
        a = agents["d1-test-agent"]
        # work_state was UNKNOWN and is still UNKNOWN (no successful save)
        self.assertNotEqual(a["work_state"], "BUSY")

    def test_d2_07_presence_unknown_without_heartbeat(self) -> None:
        """An agent that has never sent a heartbeat shows presence=UNKNOWN."""
        c = self._client
        # Create a new agent
        import asyncio
        async def _seed():
            from pluribus.db import get_db
            new_key = "fresh-key-CCCCCCCCCCCCCCCCCCCCCCC"
            new_hash = bcrypt.hashpw(new_key.encode("utf-8"),
                                     bcrypt.gensalt(rounds=4)).decode("utf-8")
            async with get_db() as db:
                await db.execute(
                    "INSERT OR REPLACE INTO agents (id, name, api_key_hash, api_key_fingerprint, permissions, allowed_scopes, is_active) VALUES (?, ?, ?, ?, ?, ?, 1)",
                    ("fresh-agent", "fresh", new_hash,
                     fingerprint_api_key(new_key),
                     json.dumps({"read": True}), json.dumps(["shared"])),
                )
                await db.commit()
        asyncio.run(_seed())
        # No heartbeat sent — presence should be UNKNOWN
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        agents = {a["identity"]: a for a in r.json()["agents"]}
        self.assertEqual(agents["fresh-agent"]["presence"], "UNKNOWN")
        self.assertEqual(agents["fresh-agent"]["work_state"], "UNKNOWN")
        self.assertEqual(agents["fresh-agent"]["last_known_activity"], "UNKNOWN")

    def test_d2_08_presence_online(self) -> None:
        """A recent heartbeat (age <= 60s) -> presence=ONLINE."""
        c = self._client
        r = c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY})
        self.assertEqual(r.status_code, 204)
        # Force the last_active_at to a very recent time (now)
        # (the patch already did this with server time)
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        self.assertEqual(a["presence"], "ONLINE",
                         f"age_seconds={a['age_seconds']} -> presence {a['presence']}")

    def test_d2_09_presence_stale(self) -> None:
        """last_active_at between 60s and 300s ago -> presence=STALE."""
        c = self._client
        # Send a heartbeat to set last_active_at = now
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY})
        # Then forcibly set last_active_at to 120 seconds ago
        import asyncio
        async def _set_stale():
            from pluribus.db import get_db
            from datetime import datetime, timezone, timedelta
            t = (datetime.now(timezone.utc) - timedelta(seconds=120)).strftime(
                "%Y-%m-%d %H:%M:%S")
            async with get_db() as db:
                await db.execute(
                    "UPDATE agents SET last_active_at = ? WHERE id = ?",
                    (t, "d1-test-agent"),
                )
                await db.commit()
        asyncio.run(_set_stale())
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        self.assertEqual(a["presence"], "STALE",
                         f"expected STALE (age={a['age_seconds']}s); got {a['presence']}")
        # Work state still works (STALE is not OFFLINE)
        # We did not set work_state so it should be UNKNOWN
        self.assertEqual(a["work_state"], "UNKNOWN")

    def test_d2_10_presence_offline(self) -> None:
        """last_active_at > 300s ago -> presence=OFFLINE."""
        c = self._client
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY})
        import asyncio
        async def _set_offline():
            from pluribus.db import get_db
            from datetime import datetime, timezone, timedelta
            t = (datetime.now(timezone.utc) - timedelta(seconds=600)).strftime(
                "%Y-%m-%d %H:%M:%S")
            async with get_db() as db:
                await db.execute(
                    "UPDATE agents SET last_active_at = ? WHERE id = ?",
                    (t, "d1-test-agent"),
                )
                await db.commit()
        asyncio.run(_set_offline())
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        self.assertEqual(a["presence"], "OFFLINE",
                         f"expected OFFLINE (age={a['age_seconds']}s); got {a['presence']}")

    def test_d2_11_is_active_does_not_imply_online(self) -> None:
        """D1 regression rule: an active agent that has never sent
        a heartbeat is NOT online. is_active == 1 but presence == UNKNOWN."""
        c = self._client
        # Create a fresh agent (is_active=1, no heartbeat)
        import asyncio
        async def _seed():
            from pluribus.db import get_db
            new_key = "active-only-key-EEEEEEEEEEEEEEEEEEE"
            new_hash = bcrypt.hashpw(new_key.encode("utf-8"),
                                     bcrypt.gensalt(rounds=4)).decode("utf-8")
            async with get_db() as db:
                await db.execute(
                    "INSERT OR REPLACE INTO agents (id, name, api_key_hash, api_key_fingerprint, permissions, allowed_scopes, is_active) VALUES (?, ?, ?, ?, ?, ?, 1)",
                    ("active-only", "active-only", new_hash,
                     fingerprint_api_key(new_key),
                     json.dumps({"read": True}), json.dumps(["shared"])),
                )
                await db.commit()
        asyncio.run(_seed())
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["active-only"]
        self.assertTrue(a["active_flag"])
        self.assertEqual(a["presence"], "UNKNOWN")  # NOT online

    def test_d2_12_pending_directive_not_current_task(self) -> None:
        """D1 truthfulness: a PENDING directive is shown as
        pending_directive, NEVER as current_task."""
        c = self._client
        # Send a heartbeat so the agent is not UNKNOWN
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY})
        # Insert a pending directive targeting the agent
        import asyncio
        async def _seed_directive():
            from pluribus.db import get_db
            async with get_db() as db:
                await db.execute(
                    """INSERT INTO directives
                       (id, issuer_agent_id, target_agent_id, scope, action,
                        arguments, required_capability, status, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', datetime('now', '+1 day'))""",
                    ("dir-pending-1", "issuer", "d1-test-agent",
                     "shared", "test_action", "{}", "test_cap"),
                )
                await db.commit()
        asyncio.run(_seed_directive())
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        # current_task remains UNKNOWN
        self.assertEqual(a["current_task"], "UNKNOWN")
        # pending_directive shows the pending one
        self.assertIsNotNone(a["pending_directive"])
        self.assertEqual(a["pending_directive"]["id"], "dir-pending-1")

    def test_d2_13_historical_memory_cannot_populate_state(self) -> None:
        """D1 regression: a fact mentioning 'BLOCKED' or 'PASS' or
        'project=foo' in historical memory must NOT populate
        blocker / last_result / project on the agent."""
        c = self._client
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY})
        # Insert noisy historical facts
        import asyncio
        async def _seed_facts():
            from pluribus.db import get_db
            async with get_db() as db:
                await db.execute(
                    "INSERT INTO facts (scope, category, key, content, agent_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    ("shared", "events", "old_blocked",
                     "BLOCKED in 2025 (long since resolved)", None,
                     "2025-01-01 00:00:00"),
                )
                await db.execute(
                    "INSERT INTO facts (scope, category, key, content, agent_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    ("shared", "events", "old_pass",
                     "PASS last week", None, "2025-01-02 00:00:00"),
                )
                await db.execute(
                    "INSERT INTO facts (scope, category, key, content, agent_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    ("shared", "events", "old_project",
                     "project=xerrameca mentioned here", None,
                     "2025-01-03 00:00:00"),
                )
                await db.commit()
        asyncio.run(_seed_facts())
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        # None of the historical text became authoritative state
        self.assertEqual(a["blocker"], "UNKNOWN")  # No reported current_blocker
        self.assertNotIn("xerrameca", (a["project"] or "").lower())
        self.assertEqual(a["last_result"], "UNKNOWN")

    def test_d2_14_offline_old_working_not_presented_as_fresh(self) -> None:
        """D2 freshness: an OFFLINE agent that last reported WORKING
        must NOT show work_state=WORKING as fresh. effective
        work_state = UNKNOWN; last_reported_work_state preserves
        the historical value."""
        c = self._client
        # Report WORKING, then go offline
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY},
               json={"work_state": "WORKING"})
        import asyncio
        async def _offline():
            from pluribus.db import get_db
            from datetime import datetime, timezone, timedelta
            t = (datetime.now(timezone.utc) - timedelta(seconds=600)).strftime(
                "%Y-%m-%d %H:%M:%S")
            async with get_db() as db:
                await db.execute(
                    "UPDATE agents SET last_active_at = ? WHERE id = ?",
                    (t, "d1-test-agent"),
                )
                await db.commit()
        asyncio.run(_offline())
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        self.assertEqual(a["presence"], "OFFLINE")
        # effective work_state is UNKNOWN (NOT WORKING as fresh)
        self.assertEqual(a["work_state"], "UNKNOWN")
        # but the last reported is preserved
        self.assertEqual(a["reported_work_state"], "WORKING")
        self.assertEqual(a["telemetry_freshness"], "OFFLINE")

    def test_d2_15_current_project_explicit_only(self) -> None:
        """project is set ONLY when the agent reports current_project
        explicitly. Otherwise UNKNOWN."""
        c = self._client
        # No heartbeat yet
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        self.assertEqual(a["project"], "UNKNOWN")
        # Heartbeat with project
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY},
               json={"current_project": "D2-B"})
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        self.assertEqual(a["project"], "D2-B")

    def test_d2_16_blocker_distinguishes_none_from_unknown(self) -> None:
        """D2-B corrective:
        current_blocker_reported = 0 -> blocker = UNKNOWN (never reported)
        current_blocker_reported = 1 + blocker NULL -> blocker = NONE
        current_blocker_reported = 1 + blocker string -> blocker = string

        Heartbeats WITHOUT a current_blocker field preserve the prior
        reported flag (no inference from heartbeat itself)."""
        c = self._client
        import asyncio
        async def _reset():
            from pluribus.db import get_db
            async with get_db() as db:
                await db.execute(
                    "UPDATE agents SET current_blocker = NULL, "
                    "current_blocker_reported = 0, last_active_at = NULL "
                    "WHERE id = ?",
                    ("d1-test-agent",),
                )
                await db.commit()
        asyncio.run(_reset())
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        # Never reported: blocker = UNKNOWN
        self.assertEqual(a["blocker"], "UNKNOWN")

        # Heartbeat WITHOUT current_blocker field (omitted) keeps
        # reported = 0. The dashboard must still show UNKNOWN.
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY},
               json={"work_state": "IDLE"})
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        self.assertEqual(a["blocker"], "UNKNOWN")

        # Heartbeat with empty current_blocker -> reported=1, blocker=NULL
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY},
               json={"current_blocker": ""})
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        self.assertEqual(a["blocker"], "NONE")

        # Heartbeat with explicit blocker string -> blocker = that string
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY},
               json={"current_blocker": "API rate limited"})
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        self.assertEqual(a["blocker"], "API rate limited")


    def test_d2_17_heartbeat_does_not_create_memory_fact(self) -> None:
        """D2 architectural invariant: heartbeats do not create
        facts in Memory."""
        c = self._client
        # Count facts before
        import asyncio
        async def _count_facts():
            from pluribus.db import get_db
            async with get_db() as db:
                cur = await db.execute("SELECT count(*) FROM facts")
                row = await cur.fetchone()
                return row[0]
        before = asyncio.run(_count_facts())
        # Send a heartbeat
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY},
               json={"work_state": "WORKING"})
        after = asyncio.run(_count_facts())
        self.assertEqual(after, before, "heartbeat must not create a fact")

    def test_d2_18_server_timestamp_controls_heartbeat_freshness(self) -> None:
        """The server controls last_active_at with datetime('now'),
        not the client. We verify by sending a heartbeat and
        confirming the timestamp is recent (not some old value)."""
        c = self._client
        from datetime import datetime, timezone
        before = datetime.now(timezone.utc)
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY},
               json={"work_state": "WORKING"})
        after = datetime.now(timezone.utc)
        # Verify the last_active_at is between before and after
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        last = datetime.strptime(a["last_known_activity"],
                                 "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        # last_active_at should be very close to "now"
        self.assertLessEqual((last - before).total_seconds(), 5,
                             f"server timestamp not in [before, after] window; diff={(last-before).total_seconds()}")
        self.assertLessEqual((after - last).total_seconds(), 5,
                             f"server timestamp not in [before, after] window; diff={(after-last).total_seconds()}")
    def test_d2_19_heartbeat_without_blocker_remains_unknown(self) -> None:
        """D2-B corrective: a heartbeat with no current_blocker field
        keeps the prior reported flag. New agent with reported=0
        stays UNKNOWN even after a heartbeat with only work_state."""
        c = self._client
        import asyncio
        import bcrypt
        new_key = "d2-19-key-CCCCCCCCCCCCCCCCCCC"
        new_hash = bcrypt.hashpw(new_key.encode("utf-8"),
                                 bcrypt.gensalt(rounds=4)).decode("utf-8")
        from pluribus.api_keys import fingerprint_api_key
        async def _seed():
            from pluribus.db import get_db
            async with get_db() as db:
                await db.execute(
                    "INSERT OR REPLACE INTO agents (id, name, api_key_hash, api_key_fingerprint, permissions, allowed_scopes, is_active) "
                    "VALUES (?, ?, ?, ?, ?, ?, 1)",
                    ("d2-19-agent", "d2-19", new_hash,
                     fingerprint_api_key(new_key),
                     json.dumps({"read": True}), json.dumps(["shared"])),
                )
                await db.commit()
        asyncio.run(_seed())
        # Heartbeat with only work_state — NO current_blocker
        c.patch("/v1/agents/d2-19-agent/heartbeat", headers={"X-API-Key": new_key},
               json={"work_state": "IDLE"})
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d2-19-agent"]
        self.assertEqual(a["blocker"], "UNKNOWN",
                         "heartbeat without current_blocker must keep blocker=UNKNOWN")
        # Now heartbeat with current_blocker: "" -> NONE
        c.patch("/v1/agents/d2-19-agent/heartbeat", headers={"X-API-Key": new_key},
               json={"current_blocker": ""})
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d2-19-agent"]
        self.assertEqual(a["blocker"], "NONE")
        # And a real string
        c.patch("/v1/agents/d2-19-agent/heartbeat", headers={"X-API-Key": new_key},
               json={"current_blocker": "Waiting for CI"})
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d2-19-agent"]
        self.assertEqual(a["blocker"], "Waiting for CI")

    def _seed_claimed_directives(self, agent_id, n: int, *,
                                 lease_valid: bool = True,
                                 expires_valid: bool = True) -> list[str]:
        """Insert n claimed directives targeting agent_id. Returns
        the list of directive IDs in deterministic order (claimed
        DESC, id DESC). D2-C: by default the lease and expires are
        valid (in the future) so the directive is a valid current_task
        source. Pass lease_valid=False / expires_valid=False to seed
        expired variants."""
        import asyncio
        ids = [f"dir-{agent_id}-{i}" for i in range(n)]
        async def _do():
            from pluribus.db import get_db
            from datetime import datetime, timezone, timedelta
            base = datetime.now(timezone.utc)
            async with get_db() as db:
                for i, did in enumerate(ids):
                    claimed = (base - timedelta(seconds=i)).strftime(
                        "%Y-%m-%d %H:%M:%S")
                    created = (base - timedelta(seconds=10 + i)).strftime(
                        "%Y-%m-%d %H:%M:%S")
                    lease = (base + (timedelta(hours=1) if lease_valid
                                      else timedelta(seconds=-10))).strftime(
                        "%Y-%m-%d %H:%M:%S")
                    expires = (base + (timedelta(days=1) if expires_valid
                                        else timedelta(seconds=-10))).strftime(
                        "%Y-%m-%d %H:%M:%S")
                    await db.execute(
                        """INSERT INTO directives
                           (id, issuer_agent_id, target_agent_id, scope, action,
                            arguments, required_capability, status, created_at,
                            expires_at, claimed_at, claimed_by_agent_id,
                            lease_until)
                           VALUES (?, ?, ?, ?, ?, ?, ?, 'claimed', ?, ?, ?, ?, ?)""",
                        (did, "issuer", agent_id, "shared", f"act_{i}", "{}",
                         "test_cap", created, expires, claimed, agent_id,
                         lease),
                    )
                await db.commit()
        asyncio.run(_do())
        return ids

    def _seed_terminal_directives(self, agent_id, *,
                                   status: str = "completed",
                                   n: int = 1,
                                   result: str | None = "ok",
                                   error: str | None = None,
                                   completed_offsets: tuple[int, ...] = (60,),
                                   id_prefix: str = "term") -> list[str]:
        """Insert n completed/failed directives. completed_offsets
        is the seconds-ago for each directive (so we can verify
        ordering); the LAST entry is the newest."""
        import asyncio
        ids = []
        async def _do():
            from pluribus.db import get_db
            from datetime import datetime, timezone, timedelta
            now = datetime.now(timezone.utc)
            async with get_db() as db:
                for i in range(n):
                    did = f"{id_prefix}-{agent_id}-{i}"
                    ids.append(did)
                    secs_ago = completed_offsets[i] if i < len(completed_offsets) else 60 + i
                    completed = (now - timedelta(seconds=secs_ago)).strftime(
                        "%Y-%m-%d %H:%M:%S")
                    try:
                        await db.execute(
                            """INSERT INTO directives
                               (id, issuer_agent_id, target_agent_id, scope, action,
                                arguments, required_capability, status, created_at,
                                expires_at, completed_at, claimed_by_agent_id,
                                result, error)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (did, "issuer", agent_id, "shared", f"act_{i}",
                             "{}", "test_cap", status,
                             (now - timedelta(seconds=600 + secs_ago)).strftime(
                                 "%Y-%m-%d %H:%M:%S"),
                             (now + timedelta(days=1)).strftime(
                                 "%Y-%m-%d %H:%M:%S"),
                             completed, agent_id, result, error),
                        )
                        await db.commit()
                    except Exception as e:
                        raise
                await db.commit()
        asyncio.run(_do())
        return ids

    def test_d2_20_single_claimed_is_current_task(self) -> None:
        """D2-B corrective: with exactly one claimed directive, that
        directive is the current_task."""
        c = self._client
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY})
        self._seed_claimed_directives("d1-test-agent", 1)
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        self.assertEqual(a["current_task"], "directive:dir-d1-test-agent-0")
        self.assertEqual(a["claimed_directive_count"], 1)

    def test_d2_21_multiple_claimed_without_explicit_task_is_unknown(self) -> None:
        """D2-B corrective: with multiple claimed directives and no
        valid explicit current_task_id, current_task = UNKNOWN. We
        do NOT pick one arbitrarily."""
        c = self._client
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY})
        self._seed_claimed_directives("d1-test-agent", 2)
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        self.assertEqual(a["current_task"], "UNKNOWN")
        self.assertEqual(a["current_task_id"], "UNKNOWN")
        self.assertEqual(a["claimed_directive_count"], 2)

    def test_d2_22_multiple_claimed_with_valid_explicit_task(self) -> None:
        """D2-B corrective: with multiple claimed directives, an
        explicit current_task_id that matches one of them picks
        exactly that one."""
        c = self._client
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY})
        self._seed_claimed_directives("d1-test-agent", 3)
        target = "dir-d1-test-agent-1"
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY},
               json={"current_task_id": target})
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        self.assertEqual(a["current_task"], f"directive:{target}")
        self.assertEqual(a["current_task_id"], target)
        self.assertEqual(a["claimed_directive_count"], 3)

    def test_d2_23_explicit_task_must_match_claimed_directive(self) -> None:
        """D2-B final corrective: an explicit current_task_id that
        does NOT match a CURRENTLY CLAIMED directive for this agent
        forces current_task = UNKNOWN. We do NOT fall back to
        another claimed directive.

        Scenarios:
          A) one claimed + heartbeat with nonexistent task -> UNKNOWN
          B) one claimed + one pending + heartbeat pointing to pending -> UNKNOWN
          C) omitted current_task_id + one claimed -> that claimed
             (fallback ONLY when no explicit reference is reported)
        """
        c = self._client
        import asyncio
        # A) one claimed, heartbeat with bogus current_task_id
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY})
        self._seed_claimed_directives("d1-test-agent", 1)
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY},
               json={"current_task_id": "dir-bogus-nonexistent"})
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        # Explicit invalid reference -> UNKNOWN. No fallback.
        self.assertEqual(a["current_task"], "UNKNOWN")
        self.assertEqual(a["current_task_id"], "UNKNOWN")

        # B) one claimed + add a pending directive. Heartbeat points
        # at the pending one. Pending != claimed. Result: UNKNOWN.
        async def _add_pending():
            from pluribus.db import get_db
            async with get_db() as db:
                await db.execute(
                    """INSERT INTO directives
                       (id, issuer_agent_id, target_agent_id, scope, action,
                        arguments, required_capability, status, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', datetime('now', '+1 day'))""",
                    ("dir-pending-bogus", "issuer", "d1-test-agent",
                     "shared", "act", "{}", "test_cap"),
                )
                await db.commit()
        asyncio.run(_add_pending())
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY},
               json={"current_task_id": "dir-pending-bogus"})
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        # Still UNKNOWN. Pending is NEVER current_task, and we do
        # not fall back to the claimed when the reference is invalid.
        self.assertEqual(a["current_task"], "UNKNOWN")
        self.assertEqual(a["current_task_id"], "UNKNOWN")

        # C) Heartbeat OMITS current_task_id. Fallback applies.
        # First, remove the pending and clear the bogus heartbeat
        # so the test only has the single claimed directive.
        async def _cleanup():
            from pluribus.db import get_db
            async with get_db() as db:
                await db.execute(
                    "DELETE FROM directives WHERE id IN (?, ?)",
                    ("dir-pending-bogus", "dir-d1-test-agent-0"),
                )
                await db.execute(
                    "UPDATE agents SET current_task_id = NULL WHERE id = ?",
                    ("d1-test-agent",),
                )
                await db.commit()
        asyncio.run(_cleanup())
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY},
               json={"work_state": "IDLE"})
        # Re-seed exactly one claimed directive
        self._seed_claimed_directives("d1-test-agent", 1)
        # Heartbeat WITHOUT current_task_id
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY},
               json={"work_state": "IDLE"})
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        # Omitted current_task_id with exactly one claimed: that claimed
        self.assertEqual(a["current_task"], "directive:dir-d1-test-agent-0")
        self.assertEqual(a["current_task_id"], "dir-d1-test-agent-0")


    def test_d2_24_malformed_json_rejected(self) -> None:
        """D2-B corrective: a heartbeat with Content-Type
        application/json and an unparseable body must be REJECTED
        (400 or 422). A plain heartbeat with no body must still work."""
        c = self._client
        # Plain heartbeat (no body) -> 204
        r = c.patch("/v1/agents/d1-test-agent/heartbeat",
                   headers={"X-API-Key": KEY})
        self.assertEqual(r.status_code, 204, r.text[:200])
        # Malformed JSON -> 400
        r = c.patch("/v1/agents/d1-test-agent/heartbeat",
                   headers={"X-API-Key": KEY, "Content-Type": "application/json"},
                   content=b"{not-json")
        self.assertIn(r.status_code, (400, 422),
                      f"malformed JSON expected 400/422, got {r.status_code}: {r.text[:200]}")
        # A valid heartbeat (after the bad one) still works
        r = c.patch("/v1/agents/d1-test-agent/heartbeat",
                   headers={"X-API-Key": KEY},
                   json={"work_state": "WORKING"})
        self.assertEqual(r.status_code, 204, r.text[:200])
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        self.assertEqual(a["work_state"], "WORKING")
    # ====== D2-C: AUTHORITATIVE TASK + RESULT LIFECYCLE =================
    def test_d2c_01_completed_result_from_directives(self) -> None:
        """D2C-01: a completed directive produces last_result=COMPLETED
        with detail from the directive row."""
        c = self._client
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY})
        self._seed_terminal_directives(
            "d1-test-agent", status="completed",
            result='{"output": "ok"}', completed_offsets=(60,))
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        self.assertEqual(a["last_result"], "COMPLETED")
        self.assertIsNotNone(a["last_result_detail"])
        self.assertEqual(a["last_result_detail"]["status"], "COMPLETED")
        self.assertEqual(a["last_result_detail"]["result"], {"output": "ok"})
        self.assertEqual(a["last_result_detail"]["action"], "act_0")

    def test_d2c_02_failed_result_from_directives(self) -> None:
        """D2C-02: a failed directive produces last_result=FAILED
        with the error from the directive row."""
        c = self._client
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY})
        self._seed_terminal_directives(
            "d1-test-agent", status="failed",
            result=None, error="boom: connection refused",
            completed_offsets=(30,))
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        self.assertEqual(a["last_result"], "FAILED")
        self.assertIsNotNone(a["last_result_detail"])
        self.assertEqual(a["last_result_detail"]["status"], "FAILED")
        self.assertEqual(a["last_result_detail"]["error"], "boom: connection refused")

    def test_d2c_03_latest_completed_at_wins(self) -> None:
        """D2C-03: with two completed directives, the one with
        the latest completed_at is the last_result."""
        c = self._client
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY})
        # Newer one is 30s ago; older one is 600s ago. Newer wins.
        # Use distinct result payloads to verify which one was selected.
        self._seed_terminal_directives(
            "d1-test-agent", status="completed", n=2,
            result="older-result", completed_offsets=(600, 30),
            id_prefix="term33")
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        self.assertEqual(a["last_result"], "COMPLETED")
        # The newer directive (i=1) wins, not the older (i=0).
        self.assertEqual(a["last_result_detail"]["result"], "older-result")
        self.assertEqual(a["last_result_detail"]["directive_id"], "term33-d1-test-agent-1")

    def test_d2c_04_result_tie_deterministic(self) -> None:
        """D2C-04: when two completed directives have the same
        completed_at, id DESC determines the winner."""
        c = self._client
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY})
        # same completed_at (both 60s ago). id DESC -> last one wins.
        self._seed_terminal_directives(
            "d1-test-agent", status="completed", n=2,
            result="first-result", completed_offsets=(60, 60),
            id_prefix="term44")
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        # id DESC -> term44-d1-test-agent-1 wins (NOT 0)
        self.assertEqual(a["last_result_detail"]["result"], "first-result")
        self.assertEqual(a["last_result_detail"]["directive_id"], "term44-d1-test-agent-1")

    def test_d2c_05_rejected_not_last_result(self) -> None:
        """D2C-05: a rejected directive is NOT an execution result.
        last_result = UNKNOWN."""
        c = self._client
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY})
        self._seed_terminal_directives(
            "d1-test-agent", status="rejected",
            result=None, error="not my job",
            completed_offsets=(60,), id_prefix="rej")
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        self.assertEqual(a["last_result"], "UNKNOWN")
        self.assertIsNone(a["last_result_detail"])

    def test_d2c_06_expired_not_last_result(self) -> None:
        """D2C-06: an expired directive is NOT an execution result.
        last_result = UNKNOWN."""
        c = self._client
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY})
        self._seed_terminal_directives(
            "d1-test-agent", status="expired",
            result=None, error="ttl",
            completed_offsets=(60,), id_prefix="exp")
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        self.assertEqual(a["last_result"], "UNKNOWN")
        self.assertIsNone(a["last_result_detail"])

    def test_d2c_07_memory_cannot_populate_last_result(self) -> None:
        """D2C-07: historical PASS/FAIL memory facts are NOT used
        to populate last_result."""
        c = self._client
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY})
        import asyncio
        async def _seed_facts():
            from pluribus.db import get_db
            async with get_db() as db:
                await db.execute(
                    "INSERT INTO facts (scope, category, key, content, agent_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    ("shared", "events", "old_pass",
                     "PASS last week (memory fact)", None, "2025-01-02 00:00:00"),
                )
                await db.execute(
                    "INSERT INTO facts (scope, category, key, content, agent_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    ("shared", "events", "old_fail",
                     "FAIL last month (memory fact)", None, "2024-12-15 00:00:00"),
                )
                await db.commit()
        asyncio.run(_seed_facts())
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        # No terminal directives seeded for this agent. Memory
        # PASS/FAIL must NOT populate last_result.
        self.assertEqual(a["last_result"], "UNKNOWN")
        self.assertIsNone(a["last_result_detail"])

    def test_d2c_08_expired_lease_not_current_task(self) -> None:
        """D2C-08: a claimed directive with an expired lease is NOT
        current_task. current_task = UNKNOWN."""
        c = self._client
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY})
        # Claimed but lease_until is in the past.
        self._seed_claimed_directives("d1-test-agent", 1,
                                     lease_valid=False,
                                     expires_valid=True)
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        # claimed_directive_count = 1 (storage-level), but
        # valid_claimed_count = 0 (no current_task).
        self.assertEqual(a["claimed_directive_count"], 1)
        self.assertEqual(a["valid_claimed_count"], 0)
        self.assertEqual(a["current_task"], "UNKNOWN")
        self.assertIsNone(a["current_task_detail"])

    def test_d2c_09_expired_directive_not_current_task(self) -> None:
        """D2C-09: a claimed directive with an expired directive
        TTL is NOT current_task."""
        c = self._client
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY})
        self._seed_claimed_directives("d1-test-agent", 1,
                                     lease_valid=True,
                                     expires_valid=False)
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        self.assertEqual(a["claimed_directive_count"], 1)
        self.assertEqual(a["valid_claimed_count"], 0)
        self.assertEqual(a["current_task"], "UNKNOWN")
        self.assertIsNone(a["current_task_detail"])

    def test_d2c_10_valid_lease_current_task(self) -> None:
        """D2C-10: a valid claimed directive (lease + expires in
        future) IS current_task with full detail."""
        c = self._client
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY})
        self._seed_claimed_directives("d1-test-agent", 1,
                                     lease_valid=True,
                                     expires_valid=True)
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        self.assertEqual(a["claimed_directive_count"], 1)
        self.assertEqual(a["valid_claimed_count"], 1)
        self.assertEqual(a["current_task"], "directive:dir-d1-test-agent-0")
        self.assertIsNotNone(a["current_task_detail"])
        d = a["current_task_detail"]
        self.assertEqual(d["id"], "dir-d1-test-agent-0")
        self.assertEqual(d["action"], "act_0")
        self.assertIsNotNone(d["claimed_at"])
        self.assertIsNotNone(d["lease_until"])
        self.assertIsNotNone(d["expires_at"])

    def test_d2c_11_explicit_task_cannot_resurrect_expired_lease(self) -> None:
        """D2C-11: an explicit current_task_id pointing to a claimed
        directive with an EXPIRED lease is UNKNOWN (no resurrection)."""
        c = self._client
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY})
        # Seed a claimed directive with an expired lease.
        target = "dir-d1-test-agent-0"
        self._seed_claimed_directives("d1-test-agent", 1,
                                     lease_valid=False,
                                     expires_valid=True)
        # Heartbeat with explicit current_task_id pointing to it.
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY},
               json={"current_task_id": target})
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        # Expired lease -> current_task = UNKNOWN. No resurrection.
        self.assertEqual(a["current_task"], "UNKNOWN")
        self.assertEqual(a["current_task_id"], "UNKNOWN")
        self.assertIsNone(a["current_task_detail"])

    def test_d2c_12_expired_pending_not_shown(self) -> None:
        """D2C-12: a pending directive with expires_at in the past
        is not surfaced as pending_directive."""
        c = self._client
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY})
        import asyncio
        async def _seed_pending_expired():
            from pluribus.db import get_db
            from datetime import datetime, timezone, timedelta
            now = datetime.now(timezone.utc)
            async with get_db() as db:
                await db.execute(
                    """INSERT INTO directives
                       (id, issuer_agent_id, target_agent_id, scope, action,
                        arguments, required_capability, status, created_at,
                        expires_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                    ("dir-pending-expired", "issuer", "d1-test-agent",
                     "shared", "act_x", "{}", "test_cap",
                     (now - timedelta(seconds=600)).strftime(
                         "%Y-%m-%d %H:%M:%S"),
                     (now - timedelta(seconds=10)).strftime(
                         "%Y-%m-%d %H:%M:%S"),
                     ),
                )
                # Also a valid pending so we know the filter is
                # selective, not "show none".
                await db.execute(
                    """INSERT INTO directives
                       (id, issuer_agent_id, target_agent_id, scope, action,
                        arguments, required_capability, status, created_at,
                        expires_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                    ("dir-pending-valid", "issuer", "d1-test-agent",
                     "shared", "act_y", "{}", "test_cap",
                     (now - timedelta(seconds=10)).strftime(
                         "%Y-%m-%d %H:%M:%S"),
                     (now + timedelta(hours=1)).strftime(
                         "%Y-%m-%d %H:%M:%S"),
                     ),
                )
                await db.commit()
        asyncio.run(_seed_pending_expired())
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        # The expired pending is filtered out. The valid one is shown.
        self.assertIsNotNone(a["pending_directive"])
        self.assertEqual(a["pending_directive"]["id"], "dir-pending-valid")

    def test_d2c_13_result_secrets_redacted(self) -> None:
        """D2C-13: nested secret-like result/error is REDACTED in the
        browser payload. The original DB row is unchanged."""
        c = self._client
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY})
        # Completed directive with a NESTED secret in result.
        nested_secret_result = {
            "summary": "all good",
            "creds":   {"password": "hunter2", "token": "Bearer eyJabc"},
            "list":    [{"api_key": "sk-abc123"}],
        }
        self._seed_terminal_directives(
            "d1-test-agent", status="completed",
            result=json.dumps(nested_secret_result),
            completed_offsets=(60,))
        # Capture original DB content for "unchanged" check
        import asyncio
        async def _get_original():
            from pluribus.db import get_db
            async with get_db() as db:
                cur = await db.execute(
                    "SELECT result FROM directives WHERE id LIKE 'term%' AND target_agent_id = ?",
                    ("d1-test-agent",),
                )
                row = await cur.fetchone()
                return row[0] if row else None
        original_result = asyncio.run(_get_original())
        cookies = _login(c)
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        a = {x["identity"]: x for x in r.json()["agents"]}["d1-test-agent"]
        # The browser payload has REDACTED markers.
        body = r.text
        self.assertNotIn("hunter2", body)
        self.assertNotIn("eyJabc", body)
        self.assertNotIn("sk-abc123", body)
        # And the original DB is unchanged.
        self.assertEqual(original_result, json.dumps(nested_secret_result))
        # Plus the dashboard's last_result_detail has REDACTED.
        self.assertIn("REDACTED",
                      str(a["last_result_detail"]["result"]))

    def test_d2c_14_dashboard_read_causes_no_mutation(self) -> None:
        """D2C-14: GET /v1/dashboard/agents does NOT mutate directives
        or facts. State is read-only."""
        c = self._client
        c.patch("/v1/agents/d1-test-agent/heartbeat", headers={"X-API-Key": KEY})
        self._seed_claimed_directives("d1-test-agent", 1)
        self._seed_terminal_directives(
            "d1-test-agent", status="completed",
            result="ok", completed_offsets=(60,))
        cookies = _login(c)
        # Capture state before
        import asyncio
        async def _count():
            from pluribus.db import get_db
            async with get_db() as db:
                c1 = (await (await db.execute(
                    "SELECT count(*) FROM directives")).fetchone())[0]
                c2 = (await (await db.execute(
                    "SELECT count(*) FROM facts")).fetchone())[0]
                return c1, c2
        d_before, f_before = asyncio.run(_count())
        # Read
        r = c.get("/v1/dashboard/agents", cookies=cookies)
        self.assertEqual(r.status_code, 200)
        d_after, f_after = asyncio.run(_count())
        # Unchanged
        self.assertEqual(d_before, d_after, "directives count changed")
        self.assertEqual(f_before, f_after, "facts count changed")

    # ====== D2C-15: TEST ISOLATION — rate limit must reset between tests
    def test_d2c_15_security_state_isolation(self) -> None:
        """D2C-15 (regression): setUp must clear Pluribus process-
        global security state so a previous test that saturated
        the per-agent rate limit does not 429-block the next one.

        This test does NOT need to receive 429 from a sanity check;
        it directly inspects the module-level _rate_limiter dict.
        """
        from pluribus import security as _sec
        c = self._client
        # Heartbeat once to register the agent in the rate limiter
        c.patch("/v1/agents/d1-test-agent/heartbeat",
                 headers={"X-API-Key": KEY})
        # The middleware records the request. The agent id key in
        # _rate_limiter is set.
        self.assertIn("d1-test-agent", _sec._rate_limiter,
                      "heartbeat did not record the request in the rate limiter")
        # Manually invoke setUp() to simulate the next test starting.
        self.setUp()
        # After setUp(), the per-agent rate limit entry is gone.
        self.assertNotIn("d1-test-agent", _sec._rate_limiter,
                         "setUp did not clear the rate limiter; "
                         "cross-test pollution will return as 429 in later tests")
