"""D3-B — Safe ASSIGN control from dashboard tests.

Covers:
  - test_d3b_01..24: authorization (read-only vs write vs admin),
    shared-service semantics (issuer/target/scope/grants/inactive),
    idempotency replay + conflict, validation 422s, CSRF/origin
    cookie protection, content-type contract (implemented), audit,
    no-memory-facts, telemetry untouched, pending != current_task,
    grant revocation, legacy POST /v1/directives compatibility.
  - test_d3b_25..31 (D3-B corrective): REAL concurrent idempotency
    (identical → both 201 same id; conflicting → {201, 409}),
    TTL in the idempotency signature (different ttl → 409),
    auth-source binding (cookie + bogus key never bypasses Origin,
    pure validated API key exempt from Origin, cookie identity beats
    a different actor's valid key), non-JSON content type → 415.

Scaffold mirrors test_dashboard_observability.py: fresh temp DB_PATH
set BEFORE importing pluribus, bcrypt hashes precomputed at import
(rounds=4) so setUp stays fast, security state reset between tests.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime as _dt
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parent.parent

_TMP = tempfile.TemporaryDirectory()
_DB = Path(_TMP.name) / "d3b.db"
os.environ["DB_PATH"] = str(_DB)
os.environ.setdefault("PLURIBUS_API_KEY", "d3b-suite-key-AAAAAAAAAAAAAAAAAAAAAAAAA")

import sys
sys.path.insert(0, str(REPO_ROOT))

import pluribus.security as security  # noqa: E402
import bcrypt  # noqa: E402
from pluribus.api_keys import fingerprint_api_key  # noqa: E402
from pluribus.config import settings  # noqa: E402
from pluribus.db import get_db, init_db  # noqa: E402


# --- Fixed fake API keys (one per agent, pre-hashed at import) ----------
# These are NOT real credentials: they only exist inside the throwaway
# test database. bcrypt rounds=4 keeps module import fast while the
# runtime verifier (_verify_candidate) still exercises the real path.
KEY_ADMIN = "d3b-admin-key-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
KEY_OP = "d3b-op-key-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
KEY_RO = "d3b-ro-key-CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
KEY_WORKER = "d3b-worker-key-DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD"
KEY_INACTIVE = "d3b-inactive-key-EEEEEEEEEEEEEEEEEEEEEEEEEEEEEE"
KEY_NOSCOPE = "d3b-noscope-key-FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF"

_KEYS = {
    "d3b-admin": KEY_ADMIN,
    "d3b-op": KEY_OP,
    "d3b-ro": KEY_RO,
    "d3b-worker": KEY_WORKER,
    "d3b-inactive": KEY_INACTIVE,
    "d3b-noscope": KEY_NOSCOPE,
}
_HASHES = {
    agent_id: bcrypt.hashpw(key.encode("utf-8"), bcrypt.gensalt(rounds=4)).decode("utf-8")
    for agent_id, key in _KEYS.items()
}

# (agent_id, name, perms, scopes, is_active)
_AGENTS = [
    ("d3b-admin", "d3b-admin", {"read": True, "write": True, "delete": False, "admin": True}, ["shared", "local"], 1),
    ("d3b-op", "d3b-op", {"read": True, "write": True, "delete": False, "admin": False}, ["shared", "local"], 1),
    ("d3b-ro", "d3b-ro", {"read": True, "write": False, "delete": False, "admin": False}, ["shared"], 1),
    ("d3b-worker", "d3b-worker", {"read": True, "write": False, "delete": False, "admin": False}, ["shared"], 1),
    ("d3b-inactive", "d3b-inactive", {"read": True, "write": True, "delete": False, "admin": False}, ["shared"], 0),
    ("d3b-noscope", "d3b-noscope", {"read": True, "write": True, "delete": False, "admin": False}, ["shared"], 1),
]


def _init_db_sync() -> None:
    if _DB.exists():
        _DB.unlink()
    for suffix in ("-wal", "-shm"):
        side = Path(str(_DB) + suffix)
        if side.exists():
            side.unlink()

    async def _go():
        await init_db()
        from pluribus.directives_schema import init_directives_db
        await init_directives_db()

    asyncio.run(_go())


def _seed_agent_sync(agent_id: str) -> None:
    info = next(item for item in _AGENTS if item[0] == agent_id)
    _, name, perms, scopes, is_active = info
    key = _KEYS[agent_id]
    fp = fingerprint_api_key(key)

    async def _do():
        async with get_db() as db:
            await db.execute(
                """INSERT OR REPLACE INTO agents
                   (id, name, api_key_hash, api_key_fingerprint, permissions,
                    allowed_scopes, is_active)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (agent_id, name, _HASHES[agent_id], fp,
                 json.dumps(perms), json.dumps(scopes), is_active),
            )
            await db.commit()

    asyncio.run(_do())


def _seed_all_agents_sync() -> None:
    for agent_id, *_ in _AGENTS:
        _seed_agent_sync(agent_id)


def _seed_grants_sync(rows: list[tuple[str, str, int, int]]) -> None:
    """rows: (agent_id, capability, can_execute, can_delegate)."""

    async def _do():
        async with get_db() as db:
            for agent_id, capability, can_execute, can_delegate in rows:
                await db.execute(
                    """INSERT OR REPLACE INTO directive_grants
                       (agent_id, capability, can_execute, can_delegate)
                       VALUES (?, ?, ?, ?)""",
                    (agent_id, capability, int(can_execute), int(can_delegate)),
                )
            await db.commit()

    asyncio.run(_do())


def _grant_success_path() -> None:
    """Default grants so d3b-op can delegate and d3b-worker can execute."""
    _seed_grants_sync([
        ("d3b-op", "d3b.run", 0, 1),
        ("d3b-worker", "d3b.run", 1, 0),
    ])


def _db_rows(sql: str, params: tuple = ()) -> list[dict]:
    async def _go():
        async with get_db() as db:
            cur = await db.execute(sql, params)
            return [dict(r) for r in await cur.fetchall()]

    return asyncio.run(_go())


def _db_exec(sql: str, params: tuple = ()) -> None:
    async def _go():
        async with get_db() as db:
            await db.execute(sql, params)
            await db.commit()

    asyncio.run(_go())


def _setup_client():
    _init_db_sync()
    _seed_all_agents_sync()
    from fastapi.testclient import TestClient
    from pluribus.main import app
    return TestClient(app)


def _login(client, key: str = KEY_OP):
    r = client.post("/v1/dashboard/login", headers={"X-API-Key": key})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    return dict(r.cookies)


def _assign_body(**overrides) -> dict:
    body: dict = {
        "target_agent_id": "d3b-worker",
        "scope": "shared",
        "action": "d3b.run",
        "arguments": {"suite": "d3b"},
        "required_capability": "d3b.run",
        "ttl_seconds": 3600,
        "idempotency_key": uuid.uuid4().hex,
    }
    body.update(overrides)
    return body


def _hdr(key: str) -> dict:
    return {"X-API-Key": key}


_ASSIGN_URL = "/v1/dashboard/control/assign"
_OPTIONS_URL = "/v1/dashboard/control/options"


class DashboardControlTests(unittest.TestCase):
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
        _seed_all_agents_sync()
        if self._client is not None:
            self._client.cookies.clear()
        # Reset process-global mutable security state (rate limiters and
        # bcrypt cache survive DB recreation across tests in the same
        # pytest process).
        from pluribus import security as _sec
        _sec._rate_limiter.clear()
        _sec._bcrypt_cache.clear()
        _sec._last_rate_cleanup = 0.0
        if hasattr(_sec, "_legacy_scan_by_client"):
            _sec._legacy_scan_by_client.clear()
        if hasattr(_sec, "_legacy_scan_global"):
            _sec._legacy_scan_global.clear()

    # ====== test_d3b_01..24 ============================================

    def test_d3b_01_readonly_agent_cannot_assign(self) -> None:
        """d3b-ro (read-only) → POST /assign → 403."""
        c = self._client
        r = c.post(_ASSIGN_URL, json=_assign_body(), headers=_hdr(KEY_RO))
        self.assertEqual(r.status_code, 403, r.text[:300])
        self.assertIn("write", r.text)

    def test_d3b_02_write_agent_can_assign(self) -> None:
        """d3b-op (read+write) with grants → 201 and row exists in DB."""
        _grant_success_path()
        c = self._client
        r = c.post(_ASSIGN_URL, json=_assign_body(), headers=_hdr(KEY_OP))
        self.assertEqual(r.status_code, 201, r.text[:300])
        rows = _db_rows("SELECT id, status FROM directives")
        self.assertEqual(len(rows), 1)

    def test_d3b_03_issuer_is_the_dashboard_actor(self) -> None:
        _grant_success_path()
        c = self._client
        r = c.post(_ASSIGN_URL, json=_assign_body(), headers=_hdr(KEY_OP))
        self.assertEqual(r.status_code, 201, r.text[:300])
        self.assertEqual(r.json()["issuer_agent_id"], "d3b-op")

    def test_d3b_04_target_is_the_selected_worker(self) -> None:
        _grant_success_path()
        c = self._client
        r = c.post(_ASSIGN_URL, json=_assign_body(), headers=_hdr(KEY_OP))
        self.assertEqual(r.status_code, 201, r.text[:300])
        self.assertEqual(r.json()["target_agent_id"], "d3b-worker")

    def test_d3b_05_created_directive_status_is_pending(self) -> None:
        _grant_success_path()
        c = self._client
        r = c.post(_ASSIGN_URL, json=_assign_body(), headers=_hdr(KEY_OP))
        self.assertEqual(r.status_code, 201, r.text[:300])
        self.assertEqual(r.json()["status"], "pending")

    def test_d3b_06_actor_scope_not_permitted(self) -> None:
        """Actor whose allowed_scopes exclude the requested scope → 403."""
        c = self._client
        r = c.post(
            _ASSIGN_URL,
            json=_assign_body(scope="local"),
            headers=_hdr(KEY_NOSCOPE),
        )
        self.assertEqual(r.status_code, 403, r.text[:300])

    def test_d3b_07_target_scope_incompatible(self) -> None:
        """Actor may use scope 'local' but worker cannot → 403."""
        _grant_success_path()
        c = self._client
        r = c.post(
            _ASSIGN_URL,
            json=_assign_body(scope="local"),
            headers=_hdr(KEY_OP),
        )
        self.assertEqual(r.status_code, 403, r.text[:300])

    def test_d3b_08_issuer_without_delegation_grant(self) -> None:
        """d3b-op has no can_delegate for the capability → 403."""
        _seed_grants_sync([("d3b-worker", "d3b.run", 1, 0)])
        c = self._client
        r = c.post(_ASSIGN_URL, json=_assign_body(), headers=_hdr(KEY_OP))
        self.assertEqual(r.status_code, 403, r.text[:300])

    def test_d3b_09_target_without_execution_grant(self) -> None:
        """Worker lacks can_execute for the capability → 403."""
        _seed_grants_sync([("d3b-op", "d3b.run", 0, 1)])
        c = self._client
        r = c.post(_ASSIGN_URL, json=_assign_body(), headers=_hdr(KEY_OP))
        self.assertEqual(r.status_code, 403, r.text[:300])

    def test_d3b_10_inactive_target_rejected(self) -> None:
        """Assigning to an inactive agent → 404."""
        c = self._client
        r = c.post(
            _ASSIGN_URL,
            json=_assign_body(target_agent_id="d3b-inactive"),
            headers=_hdr(KEY_OP),
        )
        self.assertEqual(r.status_code, 404, r.text[:300])

    def test_d3b_11_same_key_and_payload_replays_same_directive(self) -> None:
        """Replaying identical key+payload returns the SAME directive id."""
        _grant_success_path()
        c = self._client
        body = _assign_body(idempotency_key="d3b-replay-0001")
        r1 = c.post(_ASSIGN_URL, json=body, headers=_hdr(KEY_OP))
        r2 = c.post(_ASSIGN_URL, json=body, headers=_hdr(KEY_OP))
        self.assertEqual(r1.status_code, 201, r1.text[:300])
        self.assertEqual(r2.status_code, 201, r2.text[:300])
        self.assertEqual(r1.json()["id"], r2.json()["id"])
        rows = _db_rows("SELECT id FROM directives")
        self.assertEqual(len(rows), 1)

    def test_d3b_12_same_key_different_payload_conflicts(self) -> None:
        """Same idempotency_key with a different payload → 409."""
        _grant_success_path()
        c = self._client
        body1 = _assign_body(idempotency_key="d3b-conflict-0001")
        r1 = c.post(_ASSIGN_URL, json=body1, headers=_hdr(KEY_OP))
        self.assertEqual(r1.status_code, 201, r1.text[:300])
        body2 = _assign_body(
            idempotency_key="d3b-conflict-0001",
            action="d3b.run.other",
        )
        r2 = c.post(_ASSIGN_URL, json=body2, headers=_hdr(KEY_OP))
        self.assertEqual(r2.status_code, 409, r2.text[:300])

    def test_d3b_13_invalid_ttl_rejected(self) -> None:
        """ttl_seconds=10 (< 60) → 422."""
        c = self._client
        r = c.post(
            _ASSIGN_URL,
            json=_assign_body(ttl_seconds=10),
            headers=_hdr(KEY_OP),
        )
        self.assertEqual(r.status_code, 422, r.text[:300])

    def test_d3b_14_invalid_action_and_idempotency_rejected(self) -> None:
        """Malformed action / absent key / malformed key → 422."""
        c = self._client
        # action with characters outside the identifier grammar
        r = c.post(
            _ASSIGN_URL,
            json=_assign_body(action="run task!"),
            headers=_hdr(KEY_OP),
        )
        self.assertEqual(r.status_code, 422, r.text[:300])
        # idempotency_key is REQUIRED on dashboard assigns
        body = _assign_body()
        body.pop("idempotency_key", None)
        r = c.post(_ASSIGN_URL, json=body, headers=_hdr(KEY_OP))
        self.assertEqual(r.status_code, 422, r.text[:300])
        # malformed idempotency_key (spaces/! outside identifier grammar)
        r = c.post(
            _ASSIGN_URL,
            json=_assign_body(idempotency_key="not a valid key!"),
            headers=_hdr(KEY_OP),
        )
        self.assertEqual(r.status_code, 422, r.text[:300])

    def test_d3b_15_cookie_assign_with_evil_origin_rejected(self) -> None:
        """Browser (cookie) mutation with foreign Origin → 403 (CSRF)."""
        _grant_success_path()
        c = self._client
        _login(c, KEY_OP)
        r = c.post(
            _ASSIGN_URL,
            json=_assign_body(),
            headers={"Origin": "http://evil.example"},
        )
        self.assertEqual(r.status_code, 403, r.text[:300])
        rows = _db_rows("SELECT id FROM directives")
        self.assertEqual(len(rows), 0)

    def test_d3b_16_cookie_assign_with_same_origin_allowed(self) -> None:
        """Browser (cookie) mutation with same-origin header → 201."""
        _grant_success_path()
        c = self._client
        _login(c, KEY_OP)
        r = c.post(
            _ASSIGN_URL,
            json=_assign_body(),
            headers={"Origin": "http://testserver"},
        )
        self.assertEqual(r.status_code, 201, r.text[:300])
        rows = _db_rows("SELECT id FROM directives")
        self.assertEqual(len(rows), 1)

    def test_d3b_17_key_assign_without_origin_allowed(self) -> None:
        """Server-to-server (X-API-Key, no Origin) → 201."""
        _grant_success_path()
        c = self._client
        r = c.post(_ASSIGN_URL, json=_assign_body(), headers=_hdr(KEY_OP))
        self.assertEqual(r.status_code, 201, r.text[:300])

    def test_d3b_18_audit_row_created_on_assign(self) -> None:
        """Successful assign writes an audit_log CREATE/directive row."""
        _grant_success_path()
        c = self._client
        body = _assign_body(arguments={"suite": "d3b-audit"})
        r = c.post(_ASSIGN_URL, json=body, headers=_hdr(KEY_OP))
        self.assertEqual(r.status_code, 201, r.text[:300])
        rows = _db_rows(
            """SELECT agent_id, action, resource_type, resource_id, payload
               FROM audit_log WHERE action = 'CREATE' AND resource_type = 'directive'"""
        )
        self.assertEqual(len(rows), 1, "expected exactly one CREATE/directive audit row")
        audit = rows[0]
        self.assertEqual(audit["agent_id"], "d3b-op")
        self.assertEqual(audit["resource_id"], r.json()["id"])
        payload = json.loads(audit["payload"])
        self.assertEqual(payload["target_agent_id"], "d3b-worker")
        self.assertEqual(payload["scope"], "shared")
        self.assertEqual(payload["action"], "d3b.run")
        self.assertEqual(payload["required_capability"], "d3b.run")

    def test_d3b_19_no_memory_facts_created(self) -> None:
        """ASSIGN must not create Memory facts (facts count unchanged)."""
        _grant_success_path()
        before = _db_rows("SELECT COUNT(*) AS n FROM facts")[0]["n"]
        c = self._client
        r = c.post(_ASSIGN_URL, json=_assign_body(), headers=_hdr(KEY_OP))
        self.assertEqual(r.status_code, 201, r.text[:300])
        after = _db_rows("SELECT COUNT(*) AS n FROM facts")[0]["n"]
        self.assertEqual(before, after)

    def test_d3b_20_worker_telemetry_untouched(self) -> None:
        """work_state / current_task_id of the worker must not change."""
        _grant_success_path()
        _db_exec(
            """UPDATE agents SET work_state = 'busy', current_task_id = 'pre-task-42'
               WHERE id = 'd3b-worker'"""
        )
        c = self._client
        r = c.post(_ASSIGN_URL, json=_assign_body(), headers=_hdr(KEY_OP))
        self.assertEqual(r.status_code, 201, r.text[:300])
        worker = _db_rows(
            "SELECT work_state, current_task_id FROM agents WHERE id = 'd3b-worker'"
        )[0]
        self.assertEqual(worker["work_state"], "busy")
        self.assertEqual(worker["current_task_id"], "pre-task-42")

    def test_d3b_21_directive_pending_worker_not_current_task(self) -> None:
        """Directive stays pending and is NOT the worker's current_task."""
        _grant_success_path()
        c = self._client
        r = c.post(_ASSIGN_URL, json=_assign_body(), headers=_hdr(KEY_OP))
        self.assertEqual(r.status_code, 201, r.text[:300])
        directive = _db_rows(
            "SELECT status, target_agent_id FROM directives"
        )[0]
        self.assertEqual(directive["status"], "pending")
        self.assertEqual(directive["target_agent_id"], "d3b-worker")
        worker = _db_rows(
            "SELECT current_task_id FROM agents WHERE id = 'd3b-worker'"
        )[0]
        self.assertIsNone(worker["current_task_id"])

    def test_d3b_22_double_submit_is_idempotent(self) -> None:
        """Two rapid POSTs with the same key+payload → exactly 1 directive."""
        _grant_success_path()
        c = self._client
        body = _assign_body(idempotency_key="d3b-double-0001")
        r1 = c.post(_ASSIGN_URL, json=body, headers=_hdr(KEY_OP))
        r2 = c.post(_ASSIGN_URL, json=body, headers=_hdr(KEY_OP))
        self.assertEqual(r1.status_code, 201, r1.text[:300])
        self.assertEqual(r2.status_code, 201, r2.text[:300])
        self.assertEqual(
            _db_rows("SELECT COUNT(*) AS n FROM directives")[0]["n"],
            1,
        )

    def test_d3b_23_grant_revoked_between_options_and_assign(self) -> None:
        """Options may look fine but revocation is enforced at assign time."""
        _grant_success_path()
        c = self._client
        opt = c.get(_OPTIONS_URL, headers=_hdr(KEY_OP))
        self.assertEqual(opt.status_code, 200, opt.text[:300])
        self.assertTrue(opt.json()["can_assign"])
        self.assertIn("d3b.run", opt.json()["capabilities"])
        # Revoke the delegation grant after the options were rendered.
        _db_exec(
            """UPDATE directive_grants SET can_delegate = 0
               WHERE agent_id = 'd3b-op' AND capability = 'd3b.run'"""
        )
        r = c.post(_ASSIGN_URL, json=_assign_body(), headers=_hdr(KEY_OP))
        self.assertEqual(r.status_code, 403, r.text[:300])

    def test_d3b_24_legacy_directives_endpoint_backward_compatible(self) -> None:
        """POST /v1/directives with admin still creates directives (201)."""
        _seed_grants_sync([("d3b-worker", "d3b.run", 1, 0)])
        c = self._client
        body = {
            "target_agent_id": "d3b-worker",
            "scope": "shared",
            "action": "d3b.run",
            "arguments": {"suite": "legacy"},
            "required_capability": "d3b.run",
            "ttl_seconds": 3600,
        }
        r = c.post("/v1/directives", json=body, headers=_hdr(KEY_ADMIN))
        self.assertEqual(r.status_code, 201, r.text[:300])
        self.assertEqual(r.json()["status"], "pending")
        rows = _db_rows("SELECT id FROM directives")
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            _db_rows("SELECT COUNT(*) AS n FROM facts")[0]["n"],
            0,
        )

    # ====== test_d3b_25..31 (D3-B corrective) =========================

    def test_d3b_25_concurrent_identical_assign(self) -> None:
        """Two SIMULTANEOUS identical POSTs never double-create.

        Both requests race past the fast-path SELECT; the loser's INSERT
        hits the partial UNIQUE idx_directives_idempotency and the
        IntegrityError handler replays the winner atomically → both
        201 with the SAME directive id and exactly one row per key.
        """
        _grant_success_path()
        c = self._client

        def _post(payload: dict):
            return c.post(_ASSIGN_URL, json=payload, headers=_hdr(KEY_OP))

        with ThreadPoolExecutor(max_workers=2) as pool:
            for i in range(5):
                body = _assign_body(idempotency_key=f"d3b-conc-ident-{i:04d}")
                f1 = pool.submit(_post, body)
                f2 = pool.submit(_post, body)
                r1 = f1.result()  # propagates any uncaught exception
                r2 = f2.result()
                self.assertEqual(r1.status_code, 201, r1.text[:300])
                self.assertEqual(r2.status_code, 201, r2.text[:300])
                self.assertEqual(r1.json()["id"], r2.json()["id"])
        rows = _db_rows("SELECT COUNT(*) AS n FROM directives")
        self.assertEqual(rows[0]["n"], 5, "one directive per idempotency key")

    def test_d3b_26_concurrent_conflicting_assign(self) -> None:
        """Simultaneous POSTs sharing a key with DIFFERENT payloads.

        Exactly one request wins (201); the loser answers 409 — via the
        fast-path replay check or via the IntegrityError race handler.
        Never 500/422, and only one row per contested key.
        """
        _grant_success_path()
        c = self._client

        def _post(payload: dict):
            return c.post(_ASSIGN_URL, json=payload, headers=_hdr(KEY_OP))

        with ThreadPoolExecutor(max_workers=2) as pool:
            for i in range(3):
                key = f"d3b-conc-conf-{i:04d}"
                f1 = pool.submit(
                    _post,
                    _assign_body(idempotency_key=key, action="d3b.run"),
                )
                f2 = pool.submit(
                    _post,
                    _assign_body(idempotency_key=key, action="d3b.run.other"),
                )
                statuses = {f1.result().status_code, f2.result().status_code}
                self.assertEqual(statuses, {201, 409})
        rows = _db_rows("SELECT COUNT(*) AS n FROM directives")
        self.assertEqual(rows[0]["n"], 3, "one directive per contested key")

    def test_d3b_27_ttl_difference_conflicts(self) -> None:
        """TTL is part of the idempotency signature.

        Same key+payload but a different ttl_seconds → 409, never a
        silent replay of a differently-scoped lifetime. The stored row
        keeps the ORIGINAL exact TTL (created_at == expires_at from the
        same server clock).
        """
        _grant_success_path()
        c = self._client
        key = "d3b-ttl-diff-0001"
        r1 = c.post(
            _ASSIGN_URL,
            json=_assign_body(idempotency_key=key, ttl_seconds=3600),
            headers=_hdr(KEY_OP),
        )
        self.assertEqual(r1.status_code, 201, r1.text[:300])
        r2 = c.post(
            _ASSIGN_URL,
            json=_assign_body(idempotency_key=key, ttl_seconds=7200),
            headers=_hdr(KEY_OP),
        )
        self.assertEqual(r2.status_code, 409, r2.text[:300])
        rows = _db_rows("SELECT id, created_at, expires_at FROM directives")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        ttl = (
            _dt.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S")
            - _dt.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
        ).total_seconds()
        self.assertEqual(int(ttl), 3600)

    def test_d3b_28_cookie_plus_bogus_api_key_does_not_bypass_origin(self) -> None:
        """A bogus X-API-Key next to a VALID session cookie changes nothing.

        The middleware must NOT answer 401 early (dashboard route with a
        session cookie present): the request reaches the per-route guard,
        which authenticates the COOKIE (auth_method="cookie") — a failed
        key never becomes an API-key Origin exemption → missing/foreign
        Origin answers 403 and nothing is created.
        """
        _grant_success_path()
        c = self._client
        _login(c, KEY_OP)  # stores the session cookie in the client jar
        hdrs = {"X-API-Key": "d3b-bogus-key-zzzzzzzzzzzzzzzzzzzzzzzzzz"}
        r1 = c.post(_ASSIGN_URL, json=_assign_body(), headers=hdrs)
        self.assertEqual(r1.status_code, 403, r1.text[:300])
        r2 = c.post(
            _ASSIGN_URL,
            json=_assign_body(),
            headers={**hdrs, "Origin": "http://evil.example"},
        )
        self.assertEqual(r2.status_code, 403, r2.text[:300])
        rows = _db_rows("SELECT id FROM directives")
        self.assertEqual(len(rows), 0)

    def test_d3b_29_pure_valid_api_key_origin_exempt(self) -> None:
        """Server-to-server call: valid key, NO cookie, NO Origin → 201.

        The Origin exemption applies ONLY to validated API-key auth
        (auth_method == "api_key"), so a pure key call needs no Origin.
        """
        _grant_success_path()
        c = self._client
        r = c.post(_ASSIGN_URL, json=_assign_body(), headers=_hdr(KEY_OP))
        self.assertEqual(r.status_code, 201, r.text[:300])
        rows = _db_rows("SELECT id FROM directives")
        self.assertEqual(len(rows), 1)

    def test_d3b_30_mixed_different_identities_safe(self) -> None:
        """Cookie identity beats a DIFFERENT actor's valid API key.

        d3b-op session cookie + VALID d3b-admin X-API-Key + missing or
        foreign Origin → 403. Precedence verified: a valid session
        cookie SELECTS the cookie identity (auth_method="cookie"); the
        X-API-Key is only consulted when no valid cookie exists, so the
        valid admin key NEVER converts this request into an API-key
        Origin exemption (no silent identity switch, no CSRF bypass).
        """
        _grant_success_path()
        c = self._client
        _login(c, KEY_OP)  # valid d3b-op session cookie
        hdrs = {"X-API-Key": KEY_ADMIN}  # valid key of a DIFFERENT actor
        r1 = c.post(_ASSIGN_URL, json=_assign_body(), headers=hdrs)
        self.assertEqual(r1.status_code, 403, r1.text[:300])
        r2 = c.post(
            _ASSIGN_URL,
            json=_assign_body(),
            headers={**hdrs, "Origin": "http://evil.example"},
        )
        self.assertEqual(r2.status_code, 403, r2.text[:300])
        rows = _db_rows("SELECT id FROM directives")
        self.assertEqual(len(rows), 0)

    def test_d3b_31_non_json_content_type_rejected(self) -> None:
        """Non-JSON content type on /assign → 415 (never 422/500).

        The content-type check runs as a route dependency resolved
        BEFORE body parsing/validation, so even a text/plain body that
        is not valid JSON answers the documented 415 contract and
        nothing is created.
        """
        _grant_success_path()
        c = self._client
        # raw non-JSON body under text/plain
        r1 = c.post(
            _ASSIGN_URL,
            content="this is not json",
            headers={**_hdr(KEY_OP), "Content-Type": "text/plain"},
        )
        self.assertEqual(r1.status_code, 415, r1.text[:300])
        # valid JSON payload but WRONG content type → still 415
        r2 = c.post(
            _ASSIGN_URL,
            content=json.dumps(_assign_body()),
            headers={**_hdr(KEY_OP), "Content-Type": "text/plain"},
        )
        self.assertEqual(r2.status_code, 415, r2.text[:300])
        rows = _db_rows("SELECT id FROM directives")
        self.assertEqual(len(rows), 0)


# ==========================================================================
# D3-C — CANONICAL SAFE CANCEL (tests) — GO §8
# ==========================================================================
#
# Aïllament de DB (REQUISIT I VERIFICAT):
#   - Mateix patró que D3-B: patch.object(settings, "DB_PATH", <fitxer dins
#     d'un tempfile.TemporaryDirectory>). MAI la DB de producció.
#   - _d3c_assert_temp_db() s'executa a setUp() de cada test i a CADA helper
#     que escriu a la DB: si settings.DB_PATH no apunta al directori temporal
#     (o apunta a /opt/pluribus/data/pluribus.db) el test s'atura amb error.
#   - Els 31 tests D3-B (class DashboardControlTests) no es modifiquen.
#
# Cobertura GO §8 (mapa):
#   migració (1,2,3) -> test_d3c_01..03
#   pending/claimed (4,5) -> 04,05 ; terminals (6) -> 06
#   authz issuer-or-admin (7) -> 07,08,09,10 ; scope (8) -> 11
#   stale expected_status (9) -> 12
#   curses (10-13) -> 13,14,15,16 ; replay (14,15) -> 17,18,19 ; audit (16) -> 20
#   no Memory (17) -> 21 ; no telemetria (18) -> 22
#   exclusions current_task/pending/last_result (19-21) -> 23,24,25
#   CSRF/Origin (22) -> 26,27,28 ; API-key (23) -> 29 ; 415 (24) -> 30
#   can_cancel (25) -> 31,32,33,34,43 ; 404 (26) -> 35 ; altre issuer (27) -> 36
#   CANCEL != REJECT (28) -> 37 ; camps persistits (29) -> 38
#   enduriment complete/fail/reject (30) -> 39,40,41
#   _cleanup_queue no toca cancelled (31) -> 42 ; can_cancel terminal (32) -> 34,43
#   extra: validació del cos (422) -> 44

import sqlite3  # noqa: E402  (fixtures de migració D3-C)

_PROD_DB = "/opt/pluribus/data/pluribus.db"
_DB_D3C = Path(_TMP.name) / "d3c.db"
_CANCEL_URL_TMPL = "/v1/dashboard/control/{directive_id}/cancel"

NEW_CANCELLED_COLUMNS = {"cancelled_at", "cancelled_by_agent_id", "cancellation_reason"}
ALL_DIRECTIVE_STATUSES = ("pending", "claimed", "completed", "failed", "rejected", "expired")
TERMINAL_STATUSES = ("completed", "failed", "rejected", "expired")
OLD_DIRECTIVE_INDEXES = {
    "idx_directives_target_status",
    "idx_directives_issuer",
    "idx_directives_scope",
    "idx_directives_idempotency",
}

# Esquema PRE-D3-C exacte (sense 'cancelled' i sense les 3 columnes noves).
_OLD_DIRECTIVES_SCHEMA = """
CREATE TABLE directives (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    issuer_agent_id TEXT NOT NULL,
    target_agent_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    action TEXT NOT NULL,
    arguments TEXT NOT NULL DEFAULT '{}',
    required_capability TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','claimed','completed','failed','rejected','expired')),
    idempotency_key TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    claimed_at TEXT,
    claimed_by_agent_id TEXT,
    lease_until TEXT,
    completed_at TEXT,
    result TEXT,
    error TEXT
);
CREATE INDEX idx_directives_target_status
    ON directives(target_agent_id, status, created_at);
CREATE INDEX idx_directives_issuer
    ON directives(issuer_agent_id, created_at);
CREATE INDEX idx_directives_scope
    ON directives(scope, created_at);
CREATE UNIQUE INDEX idx_directives_idempotency
    ON directives(issuer_agent_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
"""

_OLD_GRANTS_SCHEMA = """
CREATE TABLE directive_grants (
    agent_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    can_execute INTEGER NOT NULL DEFAULT 0 CHECK (can_execute IN (0, 1)),
    can_delegate INTEGER NOT NULL DEFAULT 0 CHECK (can_delegate IN (0, 1)),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (agent_id, capability)
);
CREATE INDEX idx_directive_grants_capability ON directive_grants(capability);
"""

_OLD_INSERT_SQL = """
INSERT INTO directives (
    id, issuer_agent_id, target_agent_id, scope, action, arguments,
    required_capability, status, idempotency_key, created_at, expires_at,
    claimed_at, claimed_by_agent_id, lease_until, completed_at, result, error
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


# --- Guardes d'aïllament ---------------------------------------------------


def _d3c_assert_temp_db_path(path: object) -> None:
    """ATURA'T si el path indicat no és dins del directori temporal."""
    resolved = os.path.realpath(str(path))
    tmp_root = os.path.realpath(tempfile.gettempdir())
    if resolved == os.path.realpath(_PROD_DB):
        raise RuntimeError(
            "D3-C DB ISOLATION VIOLATION: el path apunta a la DB de PRODUCCIÓ "
            f"({_PROD_DB})"
        )
    if not resolved.startswith(tmp_root + os.sep):
        raise RuntimeError(
            "D3-C DB ISOLATION VIOLATION: "
            f"{resolved!r} no és dins el directori temporal {tmp_root!r}"
        )


def _d3c_assert_temp_db() -> str:
    """VERIFICA que settings.DB_PATH apunta al directori temporal.

    S'ha de cridar ABANS de qualsevol escriptura a la DB (tests i helpers).
    Retorna la ruta resolta (per al report).
    """
    _d3c_assert_temp_db_path(settings.DB_PATH)
    return os.path.realpath(str(settings.DB_PATH))


# --- Helpers de DB / API (D3-C) -------------------------------------------


def _d3c_init_db_sync() -> None:
    _d3c_assert_temp_db()
    if _DB_D3C.exists():
        _DB_D3C.unlink()
    for suffix in ("-wal", "-shm"):
        side = Path(str(_DB_D3C) + suffix)
        if side.exists():
            side.unlink()

    async def _go():
        await init_db()
        from pluribus.directives_schema import init_directives_db
        await init_directives_db()

    asyncio.run(_go())
    _d3c_assert_temp_db()


def _d3c_seed_all_agents_sync() -> None:
    _d3c_assert_temp_db()
    _seed_all_agents_sync()


def _d3c_seed_grants(rows: list[tuple[str, str, int, int]]) -> None:
    _d3c_assert_temp_db()
    _seed_grants_sync(rows)


def _d3c_grant_success_path() -> None:
    _d3c_assert_temp_db()
    _grant_success_path()


def _d3c_db_rows(sql: str, params: tuple = ()) -> list[dict]:
    _d3c_assert_temp_db()
    return _db_rows(sql, params)


def _d3c_db_exec(sql: str, params: tuple = ()) -> None:
    _d3c_assert_temp_db()
    _db_exec(sql, params)


def _d3c_setup_client():
    _d3c_init_db_sync()
    _d3c_seed_all_agents_sync()
    from fastapi.testclient import TestClient
    from pluribus.main import app
    return TestClient(app)


def _hdr_c(key: str) -> dict:
    return {"X-API-Key": key}


def _d3c_cancel_url(directive_id: str) -> str:
    return _CANCEL_URL_TMPL.format(directive_id=directive_id)


def _d3c_assign(
    client,
    key: str = KEY_OP,
    target: str = "d3b-worker",
    scope: str = "shared",
    action: str = "d3b.run",
    capability: str = "d3b.run",
    idem: str | None = None,
    ttl: int = 3600,
) -> dict:
    """Crea una directiva via el camí real (POST /v1/dashboard/control/assign)."""
    body = {
        "target_agent_id": target,
        "scope": scope,
        "action": action,
        "arguments": {"suite": "d3c"},
        "required_capability": capability,
        "ttl_seconds": ttl,
        "idempotency_key": idem or uuid.uuid4().hex,
    }
    r = client.post(_ASSIGN_URL, json=body, headers=_hdr_c(key))
    assert r.status_code == 201, f"assign failed: {r.status_code} {r.text[:300]}"
    return r.json()


def _d3c_cancel(
    client,
    directive_id: str,
    *,
    key: str = KEY_OP,
    reason: str = "d3c-cancel-reason",
    expected: str = "pending",
    headers: dict | None = None,
    **kwargs,
):
    hdrs = dict(headers) if headers is not None else _hdr_c(key)
    return client.post(
        _d3c_cancel_url(directive_id),
        json={"reason": reason, "expected_status": expected},
        headers=hdrs,
        **kwargs,
    )


def _d3c_claim(client, directive_id: str, key: str = KEY_WORKER, lease: int = 300):
    return client.post(
        f"/v1/directives/{directive_id}/claim",
        json={"lease_seconds": lease},
        headers=_hdr_c(key),
    )


def _d3c_complete(client, directive_id: str, key: str = KEY_WORKER, result: dict | None = None):
    return client.post(
        f"/v1/directives/{directive_id}/complete",
        json={"result": result if result is not None else {"suite": "d3c"}},
        headers=_hdr_c(key),
    )


def _d3c_fail(client, directive_id: str, key: str = KEY_WORKER, error: str = "d3c-boom"):
    return client.post(
        f"/v1/directives/{directive_id}/fail",
        json={"error": error},
        headers=_hdr_c(key),
    )


def _d3c_reject(client, directive_id: str, key: str = KEY_WORKER, reason: str = "d3c-nope"):
    return client.post(
        f"/v1/directives/{directive_id}/reject",
        json={"reason": reason},
        headers=_hdr_c(key),
    )


def _d3c_row(directive_id: str) -> dict:
    rows = _d3c_db_rows("SELECT * FROM directives WHERE id = ?", (directive_id,))
    assert len(rows) == 1, f"expected exactly one directive {directive_id}, got {len(rows)}"
    return rows[0]


def _d3c_audit_count(action: str, resource_id: str) -> int:
    return _d3c_db_rows(
        "SELECT COUNT(*) AS n FROM audit_log WHERE action = ? AND resource_id = ?",
        (action, resource_id),
    )[0]["n"]


def _d3c_audit_rows(action: str, resource_id: str) -> list[dict]:
    return _d3c_db_rows(
        """SELECT agent_id, action, resource_type, resource_id, payload
           FROM audit_log WHERE action = ? AND resource_id = ?""",
        (action, resource_id),
    )


def _d3c_agents(client, key: str = KEY_OP) -> dict:
    r = client.get("/v1/dashboard/agents", headers=_hdr_c(key))
    assert r.status_code == 200, f"agents failed: {r.status_code} {r.text[:300]}"
    return r.json()


def _d3c_agent(payload: dict, identity: str = "d3b-worker") -> dict:
    for entry in payload.get("agents", []):
        if entry.get("identity") == identity:
            return entry
    raise AssertionError(f"agent {identity} not found in payload")


def _d3c_make_old_db(path: Path) -> list[tuple[str, str, str | None]]:
    """Crea una DB temporal amb l'esquema PRE-D3-C + files en tots els estats."""
    _d3c_assert_temp_db_path(path)
    if path.exists():
        path.unlink()
    for suffix in ("-wal", "-shm"):
        side = Path(str(path) + suffix)
        if side.exists():
            side.unlink()

    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_OLD_DIRECTIVES_SCHEMA)
        conn.executescript(_OLD_GRANTS_SCHEMA)
        seeded: list[tuple[str, str, str | None]] = []
        for i, status in enumerate(ALL_DIRECTIVE_STATUSES):
            did = f"d3c-old-{status}-{i}"
            idem = f"d3c-old-idem-{i}"
            conn.execute(
                _OLD_INSERT_SQL,
                (
                    did, "issuer-old", "worker-old", "shared", f"act.{status}",
                    '{"a":1}', "cap.x", status, idem, "2026-01-01 00:00:00",
                    "2026-12-31 00:00:00",
                    "2026-01-01 00:00:10" if status != "pending" else None,
                    "worker-old" if status != "pending" else None,
                    "2026-01-01 00:10:00" if status == "claimed" else None,
                    "2026-01-01 00:05:00" if status in ("completed", "failed") else None,
                    '{"ok":true}' if status == "completed" else None,
                    "boom" if status == "failed" else None,
                ),
            )
            seeded.append((did, status, idem))
        # idempotency_key NULL ha de continuar essent permesa (índex parcial)
        conn.execute(
            _OLD_INSERT_SQL,
            (
                "d3c-old-nullkey", "issuer-old", "worker-old", "shared", "act.null",
                "{}", "cap.x", "pending", None, "2026-01-01 00:00:00",
                "2026-12-31 00:00:00", None, None, None, None, None, None,
            ),
        )
        seeded.append(("d3c-old-nullkey", "pending", None))
        conn.commit()
    finally:
        conn.close()
    return seeded


def _d3c_snapshot(path: Path) -> dict:
    """Instantània completa (files, esquema de taula, índexs, columnes, grants)."""
    _d3c_assert_temp_db_path(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        rows = [
            (r["id"], r["status"], r["idempotency_key"])
            for r in conn.execute(
                "SELECT id, status, idempotency_key FROM directives ORDER BY id"
            ).fetchall()
        ]
        indexes = {
            r["name"]: r["sql"]
            for r in conn.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='index' AND tbl_name='directives'"
            ).fetchall()
        }
        table_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='directives'"
        ).fetchone()
        columns = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(directives)").fetchall()
        }
        grants = [
            (r["agent_id"], r["capability"], r["can_execute"], r["can_delegate"])
            for r in conn.execute(
                "SELECT agent_id, capability, can_execute, can_delegate "
                "FROM directive_grants ORDER BY agent_id, capability"
            ).fetchall()
        ]
        leftovers = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'directives_migrated'"
            ).fetchall()
        ]
    finally:
        conn.close()
    return {
        "rows": rows,
        "indexes": indexes,
        "table_sql": (table_row[0] if table_row else ""),
        "columns": columns,
        "grants": grants,
        "leftovers": leftovers,
    }


async def _d3c_init_directives_only() -> None:
    from pluribus.directives_schema import init_directives_db
    await init_directives_db()


def _d3c_run_migration(path: Path) -> None:
    """Executa init_directives_db() contra la DB temporal indicada."""
    _d3c_assert_temp_db_path(path)
    with patch.object(settings, "DB_PATH", str(path)):
        _d3c_assert_temp_db()
        asyncio.run(_d3c_init_directives_only())


class DashboardCancelTests(unittest.TestCase):
    """D3-C — CANONICAL SAFE CANCEL (endpoint + servei + migració + read model)."""

    maxDiff = 6000
    _client = None
    _db_patch = None

    @classmethod
    def setUpClass(cls) -> None:
        cls._db_patch = patch.object(settings, "DB_PATH", str(_DB_D3C))
        cls._db_patch.start()
        # Abans de qualsevol escriptura: la DB HA de ser temporal.
        _d3c_assert_temp_db()
        security._bcrypt_cache.clear()
        cls._client = _d3c_setup_client()

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
        # GUARDA D'AÏLLAMENT: cap test no escriu fora del directori temporal.
        _d3c_assert_temp_db()
        _d3c_init_db_sync()
        _d3c_seed_all_agents_sync()
        if self._client is not None:
            self._client.cookies.clear()
        from pluribus import security as _sec
        _sec._rate_limiter.clear()
        _sec._bcrypt_cache.clear()
        _sec._last_rate_cleanup = 0.0
        if hasattr(_sec, "_legacy_scan_by_client"):
            _sec._legacy_scan_by_client.clear()
        if hasattr(_sec, "_legacy_scan_global"):
            _sec._legacy_scan_global.clear()

    # ====== 1-3: MIGRACIÓ =============================================

    def test_d3c_01_migration_preserves_rows_ids_status(self) -> None:
        """Esquema antic + files en tots els estats → init_directives_db()
        preserva mateixos ids/status/idempotency_keys i admet 'cancelled'."""
        mig = Path(_TMP.name) / "d3c_migration_preserve.db"
        seeded = _d3c_make_old_db(mig)
        before = _d3c_snapshot(mig)
        self.assertNotIn("cancelled", before["table_sql"])
        self.assertFalse(NEW_CANCELLED_COLUMNS <= before["columns"])

        # L'esquema antic REBUTJA 'cancelled' (fixture realment pre-D3-C).
        conn = sqlite3.connect(str(mig))
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    _OLD_INSERT_SQL,
                    (
                        "d3c-pre-cancel", "issuer-old", "worker-old", "shared",
                        "act.pre", "{}", "cap.x", "cancelled", "idem-pre",
                        "2026-01-01 00:00:00", "2026-12-31 00:00:00",
                        None, None, None, None, None, None,
                    ),
                )
        finally:
            conn.close()

        _d3c_run_migration(mig)
        after = _d3c_snapshot(mig)

        self.assertEqual(after["rows"], before["rows"], "ids/status/keys preservats")
        self.assertEqual(len(after["rows"]), len(seeded))
        self.assertEqual(dict((r[0], r[1]) for r in after["rows"]),
                         dict((r[0], r[1]) for r in seeded))
        self.assertTrue(NEW_CANCELLED_COLUMNS <= after["columns"])
        self.assertIn("cancelled", after["table_sql"])
        self.assertEqual([r for r in after["rows"] if r[0] == "d3c-old-nullkey"][0][2], None)
        self.assertEqual(after["leftovers"], [])

        # El CHECK nou accepta 'cancelled' i els estats antics segueixen bé.
        conn = sqlite3.connect(str(mig))
        try:
            conn.execute(
                _OLD_INSERT_SQL,
                (
                    "d3c-post-cancel", "issuer-old", "worker-old", "shared",
                    "act.post", "{}", "cap.x", "cancelled", "idem-post",
                    "2026-01-01 00:00:00", "2026-12-31 00:00:00",
                    None, None, None, None, None, None,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def test_d3c_02_migration_idempotent(self) -> None:
        """2a i 3a crida de init_directives_db() → cap error, cap canvi."""
        mig = Path(_TMP.name) / "d3c_migration_idempotent.db"
        _d3c_make_old_db(mig)
        _d3c_run_migration(mig)
        first = _d3c_snapshot(mig)
        _d3c_run_migration(mig)
        second = _d3c_snapshot(mig)
        _d3c_run_migration(mig)
        third = _d3c_snapshot(mig)

        self.assertEqual(first, second)
        self.assertEqual(second, third)
        self.assertIn("cancelled", third["table_sql"])
        self.assertEqual(third["table_sql"].count("'cancelled'"), 1)
        self.assertEqual(third["leftovers"], [])
        self.assertEqual(len(third["rows"]), len(ALL_DIRECTIVE_STATUSES) + 1)

    def test_d3c_03_idempotency_unique_index_recreated(self) -> None:
        """Índex únic parcial re-creat: duplicat → IntegrityError; NULL permesos."""
        mig = Path(_TMP.name) / "d3c_migration_unique_index.db"
        _d3c_make_old_db(mig)
        _d3c_run_migration(mig)
        snap = _d3c_snapshot(mig)

        self.assertTrue(OLD_DIRECTIVE_INDEXES <= set(snap["indexes"]))
        idem_sql = (snap["indexes"].get("idx_directives_idempotency") or "").upper()
        self.assertIn("UNIQUE", idem_sql)
        self.assertIn("WHERE", idem_sql)
        self.assertIn("IDEMPOTENCY_KEY IS NOT NULL", idem_sql)

        conn = sqlite3.connect(str(mig))
        try:
            # duplicat (issuer_agent_id, idempotency_key) → IntegrityError
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    _OLD_INSERT_SQL,
                    (
                        "d3c-dup", "issuer-old", "worker-old", "shared", "act.dup",
                        "{}", "cap.x", "pending", "d3c-old-idem-0",
                        "2026-01-01 00:00:00", "2026-12-31 00:00:00",
                        None, None, None, None, None, None,
                    ),
                )
            conn.rollback()
            # NULL keys continuen sense restricció (índex parcial)
            for extra in ("d3c-null-2", "d3c-null-3"):
                conn.execute(
                    _OLD_INSERT_SQL,
                    (
                        extra, "issuer-old", "worker-old", "shared", "act.null",
                        "{}", "cap.x", "pending", None, "2026-01-01 00:00:00",
                        "2026-12-31 00:00:00", None, None, None, None, None, None,
                    ),
                )
            conn.commit()
            n = conn.execute(
                "SELECT COUNT(*) FROM directives WHERE idempotency_key IS NULL"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 3)

    # ====== 4-6: CICLE DE VIDA ========================================

    def test_d3c_04_cancel_pending_200(self) -> None:
        """pending → cancelled: 200, estat a BD = cancelled i camps cancelled_*."""
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, idem="d3c-04-pending")
        r = _d3c_cancel(c, d["id"], reason="motiu-04", expected="pending")
        self.assertEqual(r.status_code, 200, r.text[:300])
        body = r.json()
        self.assertEqual(body["status"], "cancelled")
        self.assertEqual(body["cancelled_by_agent_id"], "d3b-op")
        self.assertEqual(body["cancellation_reason"], "motiu-04")
        row = _d3c_row(d["id"])
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["cancelled_by_agent_id"], "d3b-op")
        self.assertEqual(row["cancellation_reason"], "motiu-04")
        self.assertIsNotNone(row["cancelled_at"])

    def test_d3c_05_cancel_claimed_200(self) -> None:
        """claimed → cancelled: 200 i la traça del claim es preserva."""
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, idem="d3c-05-claimed")
        rc = _d3c_claim(c, d["id"])
        self.assertEqual(rc.status_code, 200, rc.text[:300])
        claimed_row = _d3c_row(d["id"])
        self.assertEqual(claimed_row["status"], "claimed")
        self.assertEqual(claimed_row["claimed_by_agent_id"], "d3b-worker")

        r = _d3c_cancel(c, d["id"], reason="motiu-05", expected="claimed")
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertEqual(r.json()["status"], "cancelled")
        row = _d3c_row(d["id"])
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["claimed_by_agent_id"], "d3b-worker")
        self.assertEqual(row["claimed_at"], claimed_row["claimed_at"])
        self.assertEqual(row["cancellation_reason"], "motiu-05")

    def test_d3c_06_terminal_immutability_409(self) -> None:
        """completed/failed/rejected/expired → 409 i estat intacte (immutable)."""
        _d3c_grant_success_path()
        c = self._client
        for status in TERMINAL_STATUSES:
            with self.subTest(status=status):
                d = _d3c_assign(c, idem=f"d3c-06-{status}")
                _d3c_db_exec(
                    """UPDATE directives SET status = ?, completed_at = datetime('now')
                       WHERE id = ?""",
                    (status, d["id"]),
                )
                r = _d3c_cancel(c, d["id"], reason=f"motiu-06-{status}", expected="pending")
                self.assertEqual(r.status_code, 409, r.text[:300])
                row = _d3c_row(d["id"])
                self.assertEqual(row["status"], status)
                self.assertIsNone(row["cancelled_at"])
                self.assertIsNone(row["cancelled_by_agent_id"])
                self.assertIsNone(row["cancellation_reason"])
                self.assertEqual(_d3c_audit_count("CANCEL", d["id"]), 0)

    # ====== 7-9: AUTORITZACIÓ, SCOPE, EXPECTED_STATUS =================

    def test_d3c_07_issuer_authorized_200(self) -> None:
        """L'emissor de la directiva → 200."""
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, idem="d3c-07-issuer")
        r = _d3c_cancel(c, d["id"], key=KEY_OP, expected="pending")
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertEqual(_d3c_row(d["id"])["status"], "cancelled")

    def test_d3c_08_admin_authorized_200(self) -> None:
        """Un admin (no emissor) → 200 i s'enregistra com a actor."""
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-08-admin")
        r = _d3c_cancel(c, d["id"], key=KEY_ADMIN, reason="motiu-08", expected="pending")
        self.assertEqual(r.status_code, 200, r.text[:300])
        row = _d3c_row(d["id"])
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["cancelled_by_agent_id"], "d3b-admin")

    def test_d3c_09_non_issuer_non_admin_403(self) -> None:
        """Escriu però no és emissor ni admin → 403 i cap mutació."""
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-09-forbidden")
        r = _d3c_cancel(c, d["id"], key=KEY_NOSCOPE, expected="pending")
        self.assertEqual(r.status_code, 403, r.text[:300])
        self.assertIn("emissor", r.text)
        self.assertEqual(_d3c_row(d["id"])["status"], "pending")
        self.assertEqual(_d3c_audit_count("CANCEL", d["id"]), 0)

    def test_d3c_10_readonly_without_write_403(self) -> None:
        """Una sessió read-only no pot cancel·lar (403 abans de la lògica)."""
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-10-readonly")
        r = _d3c_cancel(c, d["id"], key=KEY_RO, expected="pending")
        self.assertEqual(r.status_code, 403, r.text[:300])
        self.assertIn("write", r.text)
        self.assertEqual(_d3c_row(d["id"])["status"], "pending")
        self.assertEqual(_d3c_audit_count("CANCEL", d["id"]), 0)

    def test_d3c_11_scope_revalidated_at_cancel_time(self) -> None:
        """L'emissor que ha perdut l'scope de la fila → 403 (revalidació)."""
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, scope="shared", idem="d3c-11-scope")
        _d3c_db_exec(
            "UPDATE agents SET allowed_scopes = ? WHERE id = 'd3b-op'",
            ('["local"]',),
        )
        r = _d3c_cancel(c, d["id"], key=KEY_OP, expected="pending")
        self.assertEqual(r.status_code, 403, r.text[:300])
        self.assertIn("shared", r.text)
        self.assertEqual(_d3c_row(d["id"])["status"], "pending")
        self.assertEqual(_d3c_audit_count("CANCEL", d["id"]), 0)

    def test_d3c_12_stale_expected_status_409(self) -> None:
        """expected_status obsolet (als dos sentits) → 409, sense mutació."""
        _d3c_grant_success_path()
        c = self._client
        # pending a la BD però el client creu 'claimed'
        d1 = _d3c_assign(c, key=KEY_OP, idem="d3c-12-stale-a")
        r1 = _d3c_cancel(c, d1["id"], reason="stale-a", expected="claimed")
        self.assertEqual(r1.status_code, 409, r1.text[:300])
        self.assertEqual(_d3c_row(d1["id"])["status"], "pending")

        # claimed a la BD però el client creu 'pending'
        d2 = _d3c_assign(c, key=KEY_OP, idem="d3c-12-stale-b")
        self.assertEqual(_d3c_claim(c, d2["id"]).status_code, 200)
        r2 = _d3c_cancel(c, d2["id"], reason="stale-b", expected="pending")
        self.assertEqual(r2.status_code, 409, r2.text[:300])
        self.assertEqual(_d3c_row(d2["id"])["status"], "claimed")
        self.assertEqual(_d3c_audit_count("CANCEL", d1["id"]), 0)
        self.assertEqual(_d3c_audit_count("CANCEL", d2["id"]), 0)

    # ====== 10-13: LES 4 CURSES =======================================

    def test_d3c_13_race_cancel_vs_claim(self) -> None:
        """CANCEL vs CLAIM → exactament un 200, el perdedor 409, mai 2 guanyadors."""
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-13-race-claim")

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_cancel = pool.submit(
                _d3c_cancel, c, d["id"], reason="race-13", expected="pending"
            )
            f_claim = pool.submit(_d3c_claim, c, d["id"])
            r_cancel = f_cancel.result()
            r_claim = f_claim.result()

        statuses = sorted([r_cancel.status_code, r_claim.status_code])
        self.assertEqual(statuses, [200, 409], r_cancel.text[:200] + r_claim.text[:200])
        row = _d3c_row(d["id"])
        self.assertIn(row["status"], {"cancelled", "claimed"})
        if r_cancel.status_code == 200:
            self.assertEqual(row["status"], "cancelled")
            self.assertEqual(r_claim.status_code, 409)
            self.assertEqual(_d3c_audit_count("CANCEL", d["id"]), 1)
            self.assertEqual(_d3c_audit_count("CLAIM", d["id"]), 0)
        else:
            self.assertEqual(row["status"], "claimed")
            self.assertEqual(r_cancel.status_code, 409)
            self.assertEqual(_d3c_audit_count("CANCEL", d["id"]), 0)
            self.assertEqual(_d3c_audit_count("CLAIM", d["id"]), 1)

    def test_d3c_14_race_cancel_vs_complete(self) -> None:
        """CANCEL vs COMPLETE → un guanyador; el perdedor 409 (mai 200 fals)."""
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-14-race-complete")
        self.assertEqual(_d3c_claim(c, d["id"]).status_code, 200)

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_cancel = pool.submit(
                _d3c_cancel, c, d["id"], reason="race-14", expected="claimed"
            )
            f_complete = pool.submit(_d3c_complete, c, d["id"])
            r_cancel = f_cancel.result()
            r_complete = f_complete.result()

        self.assertEqual(
            sorted([r_cancel.status_code, r_complete.status_code]),
            [200, 409],
            r_cancel.text[:200] + r_complete.text[:200],
        )
        row = _d3c_row(d["id"])
        self.assertIn(row["status"], {"cancelled", "completed"})
        if r_cancel.status_code == 200:
            self.assertEqual(row["status"], "cancelled")
            self.assertEqual(_d3c_audit_count("CANCEL", d["id"]), 1)
            self.assertEqual(_d3c_audit_count("COMPLETE", d["id"]), 0)
            self.assertIsNone(row["result"])
        else:
            self.assertEqual(row["status"], "completed")
            self.assertEqual(_d3c_audit_count("CANCEL", d["id"]), 0)
            self.assertEqual(_d3c_audit_count("COMPLETE", d["id"]), 1)

    def test_d3c_15_race_cancel_vs_fail(self) -> None:
        """CANCEL vs FAIL → un guanyador; el perdedor 409."""
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-15-race-fail")
        self.assertEqual(_d3c_claim(c, d["id"]).status_code, 200)

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_cancel = pool.submit(
                _d3c_cancel, c, d["id"], reason="race-15", expected="claimed"
            )
            f_fail = pool.submit(_d3c_fail, c, d["id"])
            r_cancel = f_cancel.result()
            r_fail = f_fail.result()

        self.assertEqual(
            sorted([r_cancel.status_code, r_fail.status_code]),
            [200, 409],
            r_cancel.text[:200] + r_fail.text[:200],
        )
        row = _d3c_row(d["id"])
        self.assertIn(row["status"], {"cancelled", "failed"})
        if r_cancel.status_code == 200:
            self.assertEqual(row["status"], "cancelled")
            self.assertEqual(_d3c_audit_count("CANCEL", d["id"]), 1)
            self.assertEqual(_d3c_audit_count("FAIL", d["id"]), 0)
            self.assertIsNone(row["error"])
        else:
            self.assertEqual(row["status"], "failed")
            self.assertEqual(_d3c_audit_count("CANCEL", d["id"]), 0)
            self.assertEqual(_d3c_audit_count("FAIL", d["id"]), 1)

    def test_d3c_16_race_cancel_vs_reject(self) -> None:
        """CANCEL vs REJECT → un guanyador; el perdedor 409."""
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-16-race-reject")

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_cancel = pool.submit(
                _d3c_cancel, c, d["id"], reason="race-16", expected="pending"
            )
            f_reject = pool.submit(_d3c_reject, c, d["id"])
            r_cancel = f_cancel.result()
            r_reject = f_reject.result()

        self.assertEqual(
            sorted([r_cancel.status_code, r_reject.status_code]),
            [200, 409],
            r_cancel.text[:200] + r_reject.text[:200],
        )
        row = _d3c_row(d["id"])
        self.assertIn(row["status"], {"cancelled", "rejected"})
        if r_cancel.status_code == 200:
            self.assertEqual(row["status"], "cancelled")
            self.assertEqual(_d3c_audit_count("CANCEL", d["id"]), 1)
            self.assertEqual(_d3c_audit_count("REJECT", d["id"]), 0)
        else:
            self.assertEqual(row["status"], "rejected")
            self.assertEqual(_d3c_audit_count("CANCEL", d["id"]), 0)
            self.assertEqual(_d3c_audit_count("REJECT", d["id"]), 1)

    # ====== 14-16: REPLAY I AUDIT =====================================

    def test_d3c_17_replay_idempotent_200_no_new_transition(self) -> None:
        """Mateix cancel repetit → 200, cap transició nova ni audit nou."""
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-17-replay")
        r1 = _d3c_cancel(c, d["id"], reason="replay-motiu", expected="pending")
        self.assertEqual(r1.status_code, 200, r1.text[:300])
        row1 = _d3c_row(d["id"])

        r2 = _d3c_cancel(c, d["id"], reason="replay-motiu", expected="pending")
        self.assertEqual(r2.status_code, 200, r2.text[:300])
        body2 = r2.json()
        self.assertEqual(body2["id"], d["id"])
        self.assertEqual(body2["status"], "cancelled")
        self.assertEqual(body2["cancelled_at"], row1["cancelled_at"])
        self.assertEqual(body2["cancelled_by_agent_id"], "d3b-op")
        self.assertEqual(body2["cancellation_reason"], "replay-motiu")

        row2 = _d3c_row(d["id"])
        self.assertEqual(row2["cancelled_at"], row1["cancelled_at"])
        self.assertEqual(row2["status"], "cancelled")
        self.assertEqual(_d3c_audit_count("CANCEL", d["id"]), 1)
        self.assertEqual(len(_d3c_db_rows("SELECT id FROM directives")), 1)

    def test_d3c_18_replay_conflicting_reason_409(self) -> None:
        """Replay amb una reason diferent → 409 i cap reescriptura."""
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-18-conflict-reason")
        self.assertEqual(
            _d3c_cancel(c, d["id"], reason="motiu-original", expected="pending").status_code,
            200,
        )
        r = _d3c_cancel(c, d["id"], reason="motiu-diferent", expected="pending")
        self.assertEqual(r.status_code, 409, r.text[:300])
        row = _d3c_row(d["id"])
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["cancellation_reason"], "motiu-original")
        self.assertEqual(row["cancelled_by_agent_id"], "d3b-op")
        self.assertEqual(_d3c_audit_count("CANCEL", d["id"]), 1)

    def test_d3c_19_replay_conflicting_other_actor_409(self) -> None:
        """Un altre actor no reescriu la cancel·lació: admin → 409, no-admin → 403."""
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-19-conflict-actor")
        self.assertEqual(
            _d3c_cancel(c, d["id"], key=KEY_OP, reason="motiu-actor", expected="pending").status_code,
            200,
        )
        # No-issuer sense admin: l'autorització va ABANS del replay → 403.
        r_noauth = _d3c_cancel(c, d["id"], key=KEY_NOSCOPE, reason="motiu-actor", expected="pending")
        self.assertEqual(r_noauth.status_code, 403, r_noauth.text[:300])
        # Admin (passa authz) però altre actor → replay conflictiu 409.
        r_admin = _d3c_cancel(c, d["id"], key=KEY_ADMIN, reason="motiu-actor", expected="pending")
        self.assertEqual(r_admin.status_code, 409, r_admin.text[:300])
        row = _d3c_row(d["id"])
        self.assertEqual(row["cancelled_by_agent_id"], "d3b-op")
        self.assertEqual(row["cancellation_reason"], "motiu-actor")
        self.assertEqual(_d3c_audit_count("CANCEL", d["id"]), 1)

    def test_d3c_20_audit_single_row_per_effective_cancel(self) -> None:
        """Exactament 1 audit CANCEL per cancel·lació efectiva; 0 als replays."""
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-20-audit")
        self.assertEqual(_d3c_audit_count("CANCEL", d["id"]), 0)
        self.assertEqual(
            _d3c_cancel(c, d["id"], reason="audit-motiu", expected="pending").status_code,
            200,
        )
        rows = _d3c_audit_rows("CANCEL", d["id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["agent_id"], "d3b-op")
        self.assertEqual(rows[0]["resource_type"], "directive")
        self.assertEqual(json.loads(rows[0]["payload"]), {"expected_status": "pending"})

        # Replays idèntics (200) i conflictiu (409) no afegeixen audit.
        for _ in range(3):
            self.assertEqual(
                _d3c_cancel(c, d["id"], reason="audit-motiu", expected="pending").status_code,
                200,
            )
        self.assertEqual(
            _d3c_cancel(c, d["id"], reason="altra-motiu", expected="pending").status_code,
            409,
        )
        self.assertEqual(_d3c_audit_count("CANCEL", d["id"]), 1)
        self.assertEqual(
            _d3c_db_rows("SELECT COUNT(*) AS n FROM audit_log WHERE action = 'CANCEL'")[0]["n"],
            1,
        )

    # ====== 17-18: CAP EFECTE COL·LATERAL =============================

    def test_d3c_21_no_memory_facts_created(self) -> None:
        """L'endpoint cancel no crea cap fila a facts (Memory intocada)."""
        _d3c_grant_success_path()
        c = self._client
        before = _d3c_db_rows("SELECT COUNT(*) AS n FROM facts")[0]["n"]
        before_fts = _d3c_db_rows("SELECT COUNT(*) AS n FROM facts_fts")[0]["n"]

        d1 = _d3c_assign(c, key=KEY_OP, idem="d3c-21-pending")
        d2 = _d3c_assign(c, key=KEY_OP, idem="d3c-21-claimed")
        self.assertEqual(_d3c_claim(c, d2["id"]).status_code, 200)
        self.assertEqual(
            _d3c_cancel(c, d1["id"], reason="m21a", expected="pending").status_code, 200
        )
        self.assertEqual(
            _d3c_cancel(c, d2["id"], reason="m21b", expected="claimed").status_code, 200
        )

        after = _d3c_db_rows("SELECT COUNT(*) AS n FROM facts")[0]["n"]
        after_fts = _d3c_db_rows("SELECT COUNT(*) AS n FROM facts_fts")[0]["n"]
        self.assertEqual(before, after)
        self.assertEqual(before_fts, after_fts)

    def test_d3c_22_telemetry_untouched(self) -> None:
        """work_state/current_task_id/current_project/current_blocker intactes."""
        _d3c_grant_success_path()
        c = self._client
        _d3c_db_exec(
            """UPDATE agents SET work_state = 'busy', current_task_id = 'd3c-pre-task',
                   current_project = 'proj-x', current_blocker = 'block-y',
                   current_blocker_reported = 1
               WHERE id = 'd3b-worker'"""
        )
        grants_before = _d3c_db_rows(
            "SELECT agent_id, capability, can_execute, can_delegate FROM directive_grants ORDER BY 1,2"
        )
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-22-telemetry")
        self.assertEqual(
            _d3c_cancel(c, d["id"], reason="m22", expected="pending").status_code, 200
        )
        worker = _d3c_db_rows(
            """SELECT work_state, current_task_id, current_project, current_blocker,
                      current_blocker_reported
               FROM agents WHERE id = 'd3b-worker'"""
        )[0]
        self.assertEqual(worker["work_state"], "busy")
        self.assertEqual(worker["current_task_id"], "d3c-pre-task")
        self.assertEqual(worker["current_project"], "proj-x")
        self.assertEqual(worker["current_blocker"], "block-y")
        self.assertEqual(worker["current_blocker_reported"], 1)
        grants_after = _d3c_db_rows(
            "SELECT agent_id, capability, can_execute, can_delegate FROM directive_grants ORDER BY 1,2"
        )
        self.assertEqual(grants_before, grants_after)

    # ====== 19-21: EXCLUSIONS AL READ MODEL ===========================

    def test_d3c_23_cancelled_excluded_from_current_task(self) -> None:
        """'cancelled' desapareix de current_task a /v1/dashboard/agents."""
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-23-current-task")
        self.assertEqual(_d3c_claim(c, d["id"]).status_code, 200)
        _d3c_db_exec(
            "UPDATE agents SET current_task_id = ? WHERE id = 'd3b-worker'", (d["id"],)
        )

        before = _d3c_agent(_d3c_agents(c, KEY_OP), "d3b-worker")
        self.assertEqual(before["current_task"], f"directive:{d['id']}")
        self.assertEqual(before["current_task_id"], d["id"])
        self.assertIsNotNone(before["current_task_detail"])
        self.assertIs(before["current_task_detail"]["can_cancel"], True)

        self.assertEqual(
            _d3c_cancel(c, d["id"], key=KEY_OP, reason="m23", expected="claimed").status_code,
            200,
        )
        after = _d3c_agent(_d3c_agents(c, KEY_OP), "d3b-worker")
        self.assertEqual(after["current_task"], "UNKNOWN")
        self.assertEqual(after["current_task_id"], "UNKNOWN")
        self.assertIsNone(after["current_task_detail"])
        self.assertEqual(after["claimed_directive_count"], 0)
        self.assertNotIn(d["id"], json.dumps(after))

    def test_d3c_24_cancelled_excluded_from_pending_directive(self) -> None:
        """'cancelled' desapareix de pending_directive."""
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-24-pending")
        before = _d3c_agent(_d3c_agents(c, KEY_OP), "d3b-worker")
        self.assertIsNotNone(before["pending_directive"])
        self.assertEqual(before["pending_directive"]["id"], d["id"])
        self.assertIs(before["pending_directive"]["can_cancel"], True)

        self.assertEqual(
            _d3c_cancel(c, d["id"], key=KEY_OP, reason="m24", expected="pending").status_code,
            200,
        )
        after = _d3c_agent(_d3c_agents(c, KEY_OP), "d3b-worker")
        self.assertIsNone(after["pending_directive"])

    def test_d3c_25_cancelled_excluded_from_last_result(self) -> None:
        """'cancelled' no apareix mai com a last_result (només completed/failed)."""
        _d3c_grant_success_path()
        c = self._client
        done = _d3c_assign(c, key=KEY_OP, idem="d3c-25-completed")
        self.assertEqual(_d3c_claim(c, done["id"]).status_code, 200)
        self.assertEqual(_d3c_complete(c, done["id"]).status_code, 200)
        cancelled = _d3c_assign(c, key=KEY_OP, idem="d3c-25-cancelled")
        self.assertEqual(_d3c_claim(c, cancelled["id"]).status_code, 200)
        pre = _d3c_agent(_d3c_agents(c, KEY_OP), "d3b-worker")
        self.assertEqual(pre["last_result"], "COMPLETED")
        self.assertEqual(pre["last_result_detail"]["directive_id"], done["id"])

        self.assertEqual(
            _d3c_cancel(c, cancelled["id"], key=KEY_OP, reason="m25", expected="claimed").status_code,
            200,
        )
        after = _d3c_agent(_d3c_agents(c, KEY_OP), "d3b-worker")
        self.assertEqual(after["last_result"], "COMPLETED")
        self.assertEqual(after["last_result_detail"]["directive_id"], done["id"])
        self.assertNotEqual(after["last_result"], "CANCELLED")
        self.assertNotIn(cancelled["id"], json.dumps(after.get("last_result_detail")))

    # ====== 22-24: TRANSPORT (CSRF/ORIGIN, API-KEY, JSON-ONLY) =========

    def test_d3c_26_cookie_without_origin_403(self) -> None:
        """Cookie sense Origin → 403 i cap directiva cancel·lada."""
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-26-no-origin")
        _login(c, KEY_OP)
        r = _d3c_cancel(c, d["id"], reason="m26", expected="pending", headers={})
        self.assertEqual(r.status_code, 403, r.text[:300])
        self.assertEqual(_d3c_row(d["id"])["status"], "pending")
        self.assertEqual(_d3c_audit_count("CANCEL", d["id"]), 0)

    def test_d3c_27_cookie_with_foreign_origin_403(self) -> None:
        """Cookie amb Origin forani → 403 i cap directiva cancel·lada."""
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-27-evil-origin")
        _login(c, KEY_OP)
        r = _d3c_cancel(
            c, d["id"], reason="m27", expected="pending",
            headers={"Origin": "http://evil.example"},
        )
        self.assertEqual(r.status_code, 403, r.text[:300])
        self.assertEqual(_d3c_row(d["id"])["status"], "pending")
        self.assertEqual(_d3c_audit_count("CANCEL", d["id"]), 0)

    def test_d3c_28_cookie_with_same_origin_200(self) -> None:
        """Cookie amb Origin same-origin → 200."""
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-28-same-origin")
        _login(c, KEY_OP)
        r = _d3c_cancel(
            c, d["id"], reason="m28", expected="pending",
            headers={"Origin": "http://testserver"},
        )
        self.assertEqual(r.status_code, 200, r.text[:300])
        row = _d3c_row(d["id"])
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["cancelled_by_agent_id"], "d3b-op")
        self.assertEqual(_d3c_audit_count("CANCEL", d["id"]), 1)

    def test_d3c_29_api_key_origin_exempt_and_mixed_bound(self) -> None:
        """API key validada, sense cookie ni Origin → 200; cookie + clau
        escombraria NO es converteix en exempció d'Origen → 403."""
        _d3c_grant_success_path()
        c = self._client
        d1 = _d3c_assign(c, key=KEY_OP, idem="d3c-29-key")
        r1 = _d3c_cancel(c, d1["id"], key=KEY_OP, reason="m29a", expected="pending")
        self.assertEqual(r1.status_code, 200, r1.text[:300])
        self.assertEqual(_d3c_row(d1["id"])["status"], "cancelled")

        d2 = _d3c_assign(c, key=KEY_OP, idem="d3c-29-mixed")
        _login(c, KEY_OP)
        r2 = _d3c_cancel(
            c, d2["id"], reason="m29b", expected="pending",
            headers={"X-API-Key": "d3c-bogus-key-zzzzzzzzzzzzzzzzzzzzzzzzz"},
        )
        self.assertEqual(r2.status_code, 403, r2.text[:300])
        self.assertEqual(_d3c_row(d2["id"])["status"], "pending")
        self.assertEqual(_d3c_audit_count("CANCEL", d2["id"]), 0)

    def test_d3c_30_non_json_content_type_415(self) -> None:
        """Content-Type no JSON → 415 abans del parsing i cap cancel·lació."""
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-30-json-only")
        r1 = c.post(
            _d3c_cancel_url(d["id"]),
            content="this is not json",
            headers={**_hdr_c(KEY_OP), "Content-Type": "text/plain"},
        )
        self.assertEqual(r1.status_code, 415, r1.text[:300])
        r2 = c.post(
            _d3c_cancel_url(d["id"]),
            content=json.dumps({"reason": "m30", "expected_status": "pending"}),
            headers={**_hdr_c(KEY_OP), "Content-Type": "text/plain"},
        )
        self.assertEqual(r2.status_code, 415, r2.text[:300])
        self.assertEqual(_d3c_row(d["id"])["status"], "pending")
        self.assertEqual(_d3c_audit_count("CANCEL", d["id"]), 0)

    # ====== 25: can_cancel AUTORITATIU ================================

    def test_d3c_31_can_cancel_true_for_issuer_and_admin(self) -> None:
        """can_cancel=True (issuer i admin) amb estat legal pending/claimed."""
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-31-can-cancel")
        entry_op = _d3c_agent(_d3c_agents(c, KEY_OP), "d3b-worker")
        self.assertIsNotNone(entry_op["pending_directive"])
        self.assertEqual(entry_op["pending_directive"]["id"], d["id"])
        self.assertEqual(entry_op["pending_directive"]["issuer_agent_id"], "d3b-op")
        self.assertIs(entry_op["pending_directive"]["can_cancel"], True)

        entry_admin = _d3c_agent(_d3c_agents(c, KEY_ADMIN), "d3b-worker")
        self.assertIs(entry_admin["pending_directive"]["can_cancel"], True)

        # Estat claimed → current_task_detail també exposat amb can_cancel.
        self.assertEqual(_d3c_claim(c, d["id"]).status_code, 200)
        _d3c_db_exec(
            "UPDATE agents SET current_task_id = ? WHERE id = 'd3b-worker'", (d["id"],)
        )
        entry_claimed = _d3c_agent(_d3c_agents(c, KEY_OP), "d3b-worker")
        self.assertIsNotNone(entry_claimed["current_task_detail"])
        self.assertEqual(entry_claimed["current_task_detail"]["issuer_agent_id"], "d3b-op")
        self.assertIs(entry_claimed["current_task_detail"]["can_cancel"], True)

    def test_d3c_32_can_cancel_false_for_non_issuer(self) -> None:
        """can_cancel=False per a un caller que no és l'emissor ni admin."""
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-32-non-issuer")
        entry_other = _d3c_agent(_d3c_agents(c, KEY_NOSCOPE), "d3b-worker")
        self.assertIsNotNone(entry_other["pending_directive"])
        self.assertEqual(entry_other["pending_directive"]["id"], d["id"])
        self.assertIs(entry_other["pending_directive"]["can_cancel"], False)

        entry_ro = _d3c_agent(_d3c_agents(c, KEY_RO), "d3b-worker")
        self.assertIs(entry_ro["pending_directive"]["can_cancel"], False)

        # El servidor és l'autoritat: el MATEIX payload diu True per a l'issuer.
        entry_issuer = _d3c_agent(_d3c_agents(c, KEY_OP), "d3b-worker")
        self.assertIs(entry_issuer["pending_directive"]["can_cancel"], True)

    def test_d3c_33_can_cancel_false_when_scope_not_permitted(self) -> None:
        """can_cancel=False si l'scope de la directiva no és permès pel caller."""
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, scope="shared", idem="d3c-33-scope")
        _d3c_db_exec(
            "UPDATE agents SET allowed_scopes = ? WHERE id = 'd3b-op'",
            ('["local"]',),
        )
        entry = _d3c_agent(_d3c_agents(c, KEY_OP), "d3b-worker")
        self.assertIsNotNone(entry["pending_directive"])
        self.assertEqual(entry["pending_directive"]["id"], d["id"])
        self.assertEqual(entry["pending_directive"]["issuer_agent_id"], "d3b-op")
        self.assertIs(entry["pending_directive"]["can_cancel"], False)

    def test_d3c_34_can_cancel_false_for_terminal_states_unit(self) -> None:
        """can_cancel és False per a QUALSEVOL estat terminal, fins i tot
        quan el caller és l'emissor (autoritat del servidor)."""
        from pluribus.dashboard_observability import _can_cancel

        for status in ("completed", "failed", "rejected", "expired", "cancelled"):
            with self.subTest(status=status):
                self.assertIs(
                    _can_cancel(
                        status=status,
                        issuer_agent_id="d3b-op",
                        scope="shared",
                        caller_id="d3b-op",
                        caller_is_admin=False,
                        caller_scopes={"shared"},
                    ),
                    False,
                )
        # Estats legals: issuer amb scope → True; no-issuer → False.
        self.assertIs(
            _can_cancel(
                status="pending", issuer_agent_id="d3b-op", scope="shared",
                caller_id="d3b-op", caller_is_admin=False, caller_scopes={"shared"},
            ),
            True,
        )
        self.assertIs(
            _can_cancel(
                status="claimed", issuer_agent_id="d3b-op", scope="shared",
                caller_id="d3b-worker", caller_is_admin=False, caller_scopes={"shared"},
            ),
            False,
        )
        # Admin: True si l'estat és legal, False si és terminal.
        self.assertIs(
            _can_cancel(
                status="pending", issuer_agent_id="d3b-op", scope="shared",
                caller_id="d3b-admin", caller_is_admin=True, caller_scopes=set(),
            ),
            True,
        )
        self.assertIs(
            _can_cancel(
                status="failed", issuer_agent_id="d3b-op", scope="shared",
                caller_id="d3b-admin", caller_is_admin=True, caller_scopes=set(),
            ),
            False,
        )
        # Issuer sense l'scope de la directiva → False.
        self.assertIs(
            _can_cancel(
                status="pending", issuer_agent_id="d3b-op", scope="local",
                caller_id="d3b-op", caller_is_admin=False, caller_scopes={"shared"},
            ),
            False,
        )

    # ====== 26-29: 404 / ALTRE ISSUER / CANCEL != REJECT / CAMPS ======

    def test_d3c_35_cancel_nonexistent_404(self) -> None:
        """Directiva inexistent → 404 i cap audit."""
        _d3c_grant_success_path()
        c = self._client
        missing = "d3c-no-such-directive-0001"
        r = _d3c_cancel(c, missing, key=KEY_OP, reason="m35", expected="pending")
        self.assertEqual(r.status_code, 404, r.text[:300])
        self.assertEqual(_d3c_audit_count("CANCEL", missing), 0)

    def test_d3c_36_cancel_directive_of_other_issuer_403(self) -> None:
        """Cancel·lar una directiva d'un ALTRE emissor → 403 sense mutació."""
        _d3c_seed_grants([
            ("d3b-noscope", "d3b.run", 0, 1),
            ("d3b-worker", "d3b.run", 1, 0),
        ])
        c = self._client
        d = _d3c_assign(c, key=KEY_NOSCOPE, idem="d3c-36-other-issuer")
        self.assertEqual(d["issuer_agent_id"], "d3b-noscope")
        r = _d3c_cancel(c, d["id"], key=KEY_OP, reason="m36", expected="pending")
        self.assertEqual(r.status_code, 403, r.text[:300])
        row = _d3c_row(d["id"])
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["issuer_agent_id"], "d3b-noscope")
        self.assertEqual(_d3c_audit_count("CANCEL", d["id"]), 0)

    def test_d3c_37_cancel_is_not_reject(self) -> None:
        """CANCEL != REJECT: l'estat final és 'cancelled' i error/result no es toquen."""
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-37-not-reject")
        r = _d3c_cancel(c, d["id"], key=KEY_OP, reason="retirada", expected="pending")
        self.assertEqual(r.status_code, 200, r.text[:300])
        body = r.json()
        self.assertEqual(body["status"], "cancelled")
        self.assertNotEqual(body["status"], "rejected")
        self.assertIsNone(body["error"])
        self.assertIsNone(body["result"])
        row = _d3c_row(d["id"])
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNone(row["error"])
        self.assertIsNone(row["result"])
        self.assertEqual(_d3c_audit_count("CANCEL", d["id"]), 1)
        self.assertEqual(_d3c_audit_count("REJECT", d["id"]), 0)

    def test_d3c_38_cancelled_fields_persisted_and_returned(self) -> None:
        """cancelled_at / cancelled_by_agent_id / cancellation_reason
        persistits a la BD i retornats al payload."""
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-38-fields")
        self.assertEqual(_d3c_claim(c, d["id"]).status_code, 200)
        r = _d3c_cancel(
            c, d["id"], key=KEY_OP, reason="motiu-38 complet",
            expected="claimed",
        )
        self.assertEqual(r.status_code, 200, r.text[:300])
        body = r.json()
        row = _d3c_row(d["id"])
        for field in ("cancelled_at", "cancelled_by_agent_id", "cancellation_reason"):
            self.assertIsNotNone(body[field], field)
            self.assertEqual(body[field], row[field], field)
        self.assertEqual(body["cancelled_by_agent_id"], "d3b-op")
        self.assertEqual(body["cancellation_reason"], "motiu-38 complet")
        self.assertIsNotNone(row["completed_at"])
        self.assertRegex(str(body["cancelled_at"]), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    # ====== 30: ENDURIMENT DE TRANSICIONS TERMINALS ===================

    def _prepare_stale_snapshot(self, client, idem: str, expected: str) -> tuple[str, dict]:
        """Crea una directiva, la deixa a l'estat esperat i retorna una
        instantània OBSOLETA (abans de cancel·lar-la) per simular una cursa."""
        d = _d3c_assign(client, key=KEY_OP, idem=idem)
        if expected == "claimed":
            self.assertEqual(_d3c_claim(client, d["id"]).status_code, 200)
        stale = dict(_d3c_row(d["id"]))
        self.assertEqual(stale["status"], expected)
        self.assertEqual(
            _d3c_cancel(client, d["id"], key=KEY_OP, reason=f"stale-{idem}",
                        expected=expected).status_code,
            200,
        )
        self.assertEqual(_d3c_row(d["id"])["status"], "cancelled")
        return d["id"], stale

    def test_d3c_39_hardening_complete_loser_409(self) -> None:
        """complete que perd la cursa (pre-check obsolet) → 409, sense audit
        i sense escriptura de result: mai un 200 fals."""
        from pluribus import directives as directives_mod

        _d3c_grant_success_path()
        c = self._client
        directive_id, stale = self._prepare_stale_snapshot(c, "d3c-39", "claimed")
        # La instantània OBSOLETA passaria el pre-check (_claimed_by_caller):
        # estat reclamat, claimer = caller i lease viva. Per tant l'ÚNIC camí
        # cap al 409 és la comprovació de rowcount de l'UPDATE (D3-C §5).
        self.assertEqual(stale["status"], "claimed")
        self.assertEqual(stale["target_agent_id"], "d3b-worker")
        self.assertEqual(stale["claimed_by_agent_id"], "d3b-worker")
        self.assertGreater(stale["lease_until"], stale["claimed_at"])

        async def _stale_fetch(_directive_id: str):
            return stale

        with patch.object(directives_mod, "_fetch_directive", _stale_fetch):
            r = _d3c_complete(c, directive_id, result={"should": "not-write"})
        self.assertEqual(r.status_code, 409, r.text[:300])
        # Missatge del GUARDA de rowcount, NO el del pre-check
        # ("no està reclamada per aquest agent") → el pre-check va passar.
        self.assertIn("ja no està en estat reclamat", r.text)
        row = _d3c_row(directive_id)
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNone(row["result"])
        self.assertEqual(_d3c_audit_count("COMPLETE", directive_id), 0)
        self.assertEqual(_d3c_audit_count("CANCEL", directive_id), 1)

    def test_d3c_40_hardening_fail_loser_409(self) -> None:
        """fail que perd la cursa → 409, sense audit ni escriptura d'error."""
        from pluribus import directives as directives_mod

        _d3c_grant_success_path()
        c = self._client
        directive_id, stale = self._prepare_stale_snapshot(c, "d3c-40", "claimed")
        self.assertEqual(stale["status"], "claimed")
        self.assertEqual(stale["claimed_by_agent_id"], "d3b-worker")

        async def _stale_fetch(_directive_id: str):
            return stale

        with patch.object(directives_mod, "_fetch_directive", _stale_fetch):
            r = _d3c_fail(c, directive_id, error="should-not-write")
        self.assertEqual(r.status_code, 409, r.text[:300])
        self.assertIn("ja no està en estat reclamat", r.text)
        row = _d3c_row(directive_id)
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNone(row["error"])
        self.assertEqual(_d3c_audit_count("FAIL", directive_id), 0)
        self.assertEqual(_d3c_audit_count("CANCEL", directive_id), 1)

    def test_d3c_41_hardening_reject_loser_409(self) -> None:
        """reject que perd la cursa davant CANCEL → 409, sense audit."""
        from pluribus import directives as directives_mod

        _d3c_grant_success_path()
        c = self._client
        directive_id, stale = self._prepare_stale_snapshot(c, "d3c-41", "pending")
        # La instantània OBSOLETA té estat 'pending' i target = caller, així
        # que el pre-check de reject_directive (status IN pending/claimed) NO
        # pot fallar: l'ÚNIC camí cap al 409 és el rowcount de l'UPDATE.
        self.assertEqual(stale["status"], "pending")
        self.assertEqual(stale["target_agent_id"], "d3b-worker")

        async def _stale_fetch(_directive_id: str):
            return stale

        with patch.object(directives_mod, "_fetch_directive", _stale_fetch):
            r = _d3c_reject(c, directive_id, reason="should-not-write")
        self.assertEqual(r.status_code, 409, r.text[:300])
        row = _d3c_row(directive_id)
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNone(row["error"])
        self.assertEqual(_d3c_audit_count("REJECT", directive_id), 0)
        self.assertEqual(_d3c_audit_count("CANCEL", directive_id), 1)

    # ====== 31: _cleanup_queue NO TOCA 'cancelled' ====================

    def test_d3c_42_cleanup_queue_preserves_cancelled(self) -> None:
        """_cleanup_queue no altera ni ressuscita directives 'cancelled'."""
        _d3c_grant_success_path()
        c = self._client

        # (a) cancelled des de pending amb expires_at ja passat
        dl = _d3c_assign(c, key=KEY_OP, idem="d3c-42-pending")
        self.assertEqual(
            _d3c_cancel(c, dl["id"], key=KEY_OP, reason="m42a", expected="pending").status_code,
            200,
        )
        _d3c_db_exec(
            "UPDATE directives SET expires_at = '2020-01-01 00:00:00' WHERE id = ?",
            (dl["id"],),
        )
        # (b) cancelled des de claimed amb lease_until i expires_at passats
        dc = _d3c_assign(c, key=KEY_OP, idem="d3c-42-claimed")
        self.assertEqual(_d3c_claim(c, dc["id"]).status_code, 200)
        self.assertEqual(
            _d3c_cancel(c, dc["id"], key=KEY_OP, reason="m42b", expected="claimed").status_code,
            200,
        )
        _d3c_db_exec(
            """UPDATE directives SET expires_at = '2020-01-01 00:00:00',
                   lease_until = '2020-01-01 00:00:00' WHERE id = ?""",
            (dc["id"],),
        )
        before = {dl["id"]: _d3c_row(dl["id"]), dc["id"]: _d3c_row(dc["id"])}

        # Camí real: /inbox dispara _cleanup_queue(target_agent_id)
        inbox = c.get("/v1/directives/inbox", headers=_hdr_c(KEY_WORKER))
        self.assertEqual(inbox.status_code, 200, inbox.text[:300])

        async def _cleanup():
            from pluribus.directives import _cleanup_queue
            await _cleanup_queue("d3b-worker")

        asyncio.run(_cleanup())

        for directive_id, snapshot in before.items():
            row = _d3c_row(directive_id)
            self.assertEqual(row["status"], "cancelled", directive_id)
            self.assertEqual(row["cancelled_at"], snapshot["cancelled_at"])
            self.assertEqual(row["cancelled_by_agent_id"], snapshot["cancelled_by_agent_id"])
            self.assertEqual(row["cancellation_reason"], snapshot["cancellation_reason"])
            self.assertNotEqual(row["status"], "expired")
        self.assertEqual(
            _d3c_db_rows("SELECT COUNT(*) AS n FROM directives WHERE status = 'expired'")[0]["n"],
            0,
        )

    def test_d3c_43_can_cancel_false_after_cancel_for_issuer(self) -> None:
        """Després de cancel·lar, cap entrada del panell exposa la directiva
        amb can_cancel=True (ni per a l'emissor)."""
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-43-after-cancel")
        self.assertEqual(_d3c_claim(c, d["id"]).status_code, 200)
        _d3c_db_exec(
            "UPDATE agents SET current_task_id = ? WHERE id = 'd3b-worker'", (d["id"],)
        )
        pre = _d3c_agent(_d3c_agents(c, KEY_OP), "d3b-worker")
        self.assertTrue(pre["current_task_detail"]["can_cancel"])

        self.assertEqual(
            _d3c_cancel(c, d["id"], key=KEY_OP, reason="m43", expected="claimed").status_code,
            200,
        )
        payload = _d3c_agents(c, KEY_OP)
        entry = _d3c_agent(payload, "d3b-worker")
        self.assertIsNone(entry["current_task_detail"])
        self.assertIsNone(entry["pending_directive"])
        for other in payload["agents"]:
            for key in ("current_task_detail", "pending_directive"):
                detail = other.get(key)
                if detail is None:
                    continue
                self.assertNotEqual(detail.get("id"), d["id"])
        self.assertNotIn(d["id"], json.dumps(payload))

    # ====== EXTRA: validació del cos =================================

    def test_d3c_44_cancel_body_validation_422(self) -> None:
        """Cos invàlid (expected_status fora del literal, reason buit/absent) → 422."""
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-44-validation")
        url = _d3c_cancel_url(d["id"])
        invalid_bodies = [
            {"reason": "m44"},                                   # sense expected_status
            {"reason": "m44", "expected_status": "completed"},    # literal prohibit
            {"expected_status": "pending"},                       # sense reason
            {"reason": "", "expected_status": "pending"},         # reason buit
        ]
        for body in invalid_bodies:
            with self.subTest(body=body):
                r = c.post(url, json=body, headers=_hdr_c(KEY_OP))
                self.assertEqual(r.status_code, 422, r.text[:300])
        self.assertEqual(_d3c_row(d["id"])["status"], "pending")
        self.assertEqual(_d3c_audit_count("CANCEL", d["id"]), 0)


# ==========================================================================
# D3-C CORRECTIVE (FASE 2b) — MIGRACIÓ D'AUDIT_LOG (tests)
# ==========================================================================
#
# Addendum vinculant: /root/d3c_spec_audit.md §3.
#
#   a) esquema legacy production-like (audit_log amb CHECK de 5 accions +
#      FK a agents, directives D3-B) → init_db() + init_directives_db()
#      migren audit_log i directives → test_d3c_audit_01
#   b) estat residual de l'incident: directives JA D3-C i audit_log encara
#      legacy → només es migren les peces que falten (audit migrat,
#      directives intacte) i és idempotent → test_d3c_audit_02
#   c) les accions canòniques noves acceptades individualment i les 5
#      legacy segueixen acceptades → 03 / 04
#   d) cap fila perduda amb sqlite_sequence preservat → 05
#   e) guarda de DB temporal activa als tests nous (control negatiu) → 06
#   f) FK OFF durant la reconstrucció, FK ON restaurada, índexs recreats
#      idèntics → 07
#
# Tots els tests nous treballen amb DB TEMPORAL (tempfile.TemporaryDirectory)
# i REUTILITZEN la guarda existent `_d3c_assert_temp_db()` /
# `_d3c_assert_temp_db_path()` — no se'n crea cap de nova. Els 75 tests
# existents (31 D3-B + 44 D3-C) NO es toquen.

LEGACY_AUDIT_ACTIONS = ("CREATE", "READ", "UPDATE", "DELETE", "SEARCH")
_DB_AUDIT = Path(_TMP.name) / "d3c_audit.db"
CANONICAL_AUDIT_ACTIONS_11 = (
    "CREATE", "READ", "UPDATE", "DELETE", "SEARCH",   # 5 legacy
    "RECALL",                                          # recall.py
    "CLAIM", "COMPLETE", "FAIL", "REJECT", "CANCEL",   # cicle de vida directives
)
AUDIT_COLUMNS = ("id", "agent_id", "action", "resource_type", "resource_id", "payload", "timestamp")
AUDIT_LEGACY_INDEXES = ("idx_audit_timestamp", "idx_audit_agent")

# agents mínima (pre-D2-B): _migrate_db() n'afegeix la resta.
_LEGACY_AGENTS_SCHEMA = """
CREATE TABLE agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    api_key_hash TEXT NOT NULL,
    permissions TEXT DEFAULT '{}',
    allowed_scopes TEXT DEFAULT '["shared"]',
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);
"""

# audit_log production-like PRE-correcció: CHECK de 5 accions + FK a agents.
_LEGACY_AUDIT_SCHEMA = """
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT REFERENCES agents(id),
    action TEXT NOT NULL
        CHECK (action IN ('CREATE','READ','UPDATE','DELETE','SEARCH')),
    resource_type TEXT NOT NULL,
    resource_id TEXT,
    payload TEXT,
    timestamp TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX idx_audit_agent ON audit_log(agent_id);
"""

# directives JA migrades a D3-C (estat residual de l'incident).
_D3C_DIRECTIVES_SCHEMA = """
CREATE TABLE directives (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    issuer_agent_id TEXT NOT NULL,
    target_agent_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    action TEXT NOT NULL,
    arguments TEXT NOT NULL DEFAULT '{}',
    required_capability TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','claimed','completed','failed','rejected','expired','cancelled')),
    idempotency_key TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    claimed_at TEXT,
    claimed_by_agent_id TEXT,
    lease_until TEXT,
    completed_at TEXT,
    result TEXT,
    error TEXT,
    cancelled_at TEXT,
    cancelled_by_agent_id TEXT,
    cancellation_reason TEXT
);
CREATE INDEX idx_directives_target_status
    ON directives(target_agent_id, status, created_at);
CREATE INDEX idx_directives_issuer
    ON directives(issuer_agent_id, created_at);
CREATE INDEX idx_directives_scope
    ON directives(scope, created_at);
CREATE UNIQUE INDEX idx_directives_idempotency
    ON directives(issuer_agent_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
"""

_D3C_INSERT_SQL = """
INSERT INTO directives (
    id, issuer_agent_id, target_agent_id, scope, action, arguments,
    required_capability, status, idempotency_key, created_at, expires_at,
    claimed_at, claimed_by_agent_id, lease_until, completed_at, result, error,
    cancelled_at, cancelled_by_agent_id, cancellation_reason
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_LEGACY_AUDIT_INSERT_SQL = """
INSERT INTO audit_log (id, agent_id, action, resource_type, resource_id, payload, timestamp)
VALUES (?, ?, ?, ?, ?, ?, ?)
"""


# --- Helpers d'aïllament i fixtures (D3-C audit) --------------------------


def _d3c_audit_unlink(path: Path) -> None:
    if path.exists():
        path.unlink()
    for suffix in ("-wal", "-shm"):
        side = Path(str(path) + suffix)
        if side.exists():
            side.unlink()


def _d3c_audit_legacy_db(path: Path, *, directives_d3c: bool = False) -> dict:
    """Construeix una DB temporal production-like PRE-correcció d'audit_log.

    - ``audit_log``: CHECK legacy de 5 accions + FK ``agent_id`` → ``agents(id)``
      amb una fila històrica coneguda per a CADA acció legacy, més una fila
      òrfena (``agent_id`` que NO existeix a ``agents``) que només es pot
      copiar amb ``PRAGMA foreign_keys=OFF``.
    - ``directives``: esquema D3-B (default) o ja D3-C (``directives_d3c``).
    """
    _d3c_assert_temp_db_path(path)
    _d3c_audit_unlink(path)

    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_LEGACY_AGENTS_SCHEMA)
        conn.executescript(_LEGACY_AUDIT_SCHEMA)
        conn.executescript(_D3C_DIRECTIVES_SCHEMA if directives_d3c else _OLD_DIRECTIVES_SCHEMA)
        conn.executescript(_OLD_GRANTS_SCHEMA)
        conn.execute(
            "INSERT INTO agents (id, name, api_key_hash) VALUES ('agent-legacy', 'legacy', 'x')"
        )

        audit_rows = []
        for i, action in enumerate(LEGACY_AUDIT_ACTIONS):
            aid = i + 1
            row = (
                aid, "agent-legacy", action, "fact", f"res-{action}",
                json.dumps({"n": aid}), f"2026-01-0{aid} 00:00:0{aid}",
            )
            conn.execute(_LEGACY_AUDIT_INSERT_SQL, row)
            audit_rows.append(row)
        # fila òrfena: agent_id inexistent (pitfall FK de la reconstrucció).
        orphan = (
            6, "agent-ghost", "READ", "fact", "res-ghost",
            json.dumps({"ghost": True}), "2026-01-06 00:00:06",
        )
        conn.execute(_LEGACY_AUDIT_INSERT_SQL, orphan)
        audit_rows.append(orphan)

        directives_rows = []
        for i, status in enumerate(ALL_DIRECTIVE_STATUSES):
            did = f"d3c-audit-old-{status}-{i}"
            values = (
                did, "issuer-old", "worker-old", "shared", f"act.{status}",
                '{"a":1}', "cap.x", status, f"d3c-audit-old-idem-{i}",
                "2026-01-01 00:00:00", "2026-12-31 00:00:00",
                "2026-01-01 00:00:10" if status != "pending" else None,
                "worker-old" if status != "pending" else None,
                "2026-01-01 00:10:00" if status == "claimed" else None,
                "2026-01-01 00:05:00" if status in ("completed", "failed") else None,
                '{"ok":true}' if status == "completed" else None,
                "boom" if status == "failed" else None,
            )
            if directives_d3c:
                conn.execute(_D3C_INSERT_SQL, values + (None, None, None))
            else:
                conn.execute(_OLD_INSERT_SQL, values)
            directives_rows.append((did, status))
        if directives_d3c:
            # Estat residual de l'incident: una directiva JA cancel·lada amb
            # traça completa + una de claimed.
            for did, status, reason in (
                ("d3c-audit-residual-cancelled", "cancelled", "incident-hold"),
                ("d3c-audit-residual-claimed", "claimed", None),
            ):
                conn.execute(
                    _D3C_INSERT_SQL,
                    (
                        did, "issuer-old", "worker-old", "shared", "act.res",
                        "{}", "cap.x", status, f"idem-{did}",
                        "2026-01-01 00:00:00", "2026-12-31 00:00:00",
                        "2026-01-01 00:00:10" if status == "claimed" else None,
                        "worker-old" if status == "claimed" else None,
                        "2026-01-01 00:10:00" if status == "claimed" else None,
                        None, None, None,
                        "2026-01-02 00:00:00" if reason else None,
                        "issuer-old" if reason else None,
                        reason,
                    ),
                )
                directives_rows.append((did, status))
        conn.execute(
            """INSERT INTO directive_grants
               (agent_id, capability, can_execute, can_delegate)
               VALUES ('agent-legacy', 'cap.x', 1, 1)"""
        )
        conn.commit()
    finally:
        conn.close()
    return {"audit_rows": audit_rows, "directives_rows": directives_rows}


def _d3c_audit_snapshot(path: Path) -> dict:
    """Instantània completa de la DB (audit_log + directives + fets)."""
    _d3c_assert_temp_db_path(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        audit_rows = [
            tuple(r)
            for r in conn.execute(
                f"SELECT {', '.join(AUDIT_COLUMNS)} FROM audit_log ORDER BY id"
            ).fetchall()
        ]
        audit_sql = (
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='audit_log'"
            ).fetchone()
            or (None,)
        )[0] or ""
        audit_indexes = {
            r["name"]: r["sql"]
            for r in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='audit_log'"
            ).fetchall()
        }
        try:
            seq_row = conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = 'audit_log'"
            ).fetchone()
            audit_seq = None if seq_row is None else seq_row["seq"]
            sqlite_sequence_exists = True
        except sqlite3.OperationalError:
            audit_seq = None
            sqlite_sequence_exists = False
        directives_rows = [
            tuple(r) for r in conn.execute("SELECT * FROM directives ORDER BY id").fetchall()
        ]
        directives_sql = (
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='directives'"
            ).fetchone()
            or (None,)
        )[0] or ""
        directives_indexes = {
            r["name"]: r["sql"]
            for r in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='directives'"
            ).fetchall()
        }
        leftovers = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE name IN ('audit_log_new','directives_migrated')"
            ).fetchall()
        ]
        try:
            facts_count = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        except sqlite3.OperationalError:
            # La fixture legacy crea només agents/audit_log/directives; la
            # taula facts apareix després d'init_db(). Cap escriptura a facts.
            facts_count = None
        grants = [
            tuple(r)
            for r in conn.execute(
                "SELECT agent_id, capability, can_execute, can_delegate FROM directive_grants"
            ).fetchall()
        ]
    finally:
        conn.close()
    return {
        "audit_rows": audit_rows,
        "audit_sql": audit_sql,
        "audit_indexes": audit_indexes,
        "audit_seq": audit_seq,
        "sqlite_sequence_exists": sqlite_sequence_exists,
        "directives_rows": directives_rows,
        "directives_sql": directives_sql,
        "directives_indexes": directives_indexes,
        "leftovers": leftovers,
        "facts_count": facts_count,
        "grants": grants,
    }


def _d3c_audit_run_migration(path: Path) -> None:
    """Executa init_db() + init_directives_db() contra la DB temporal."""
    _d3c_assert_temp_db_path(path)
    with patch.object(settings, "DB_PATH", str(path)):
        _d3c_assert_temp_db()

        async def _go():
            await init_db()
            from pluribus.directives_schema import init_directives_db
            await init_directives_db()

        asyncio.run(_go())


def _d3c_audit_insert_action(path: Path, action: str, agent_id: object = "agent-legacy"):
    """INSERT d'una fila d'auditoria amb l'acció indicada."""
    _d3c_assert_temp_db_path(path)
    conn = sqlite3.connect(str(path))
    try:
        cursor = conn.execute(
            """INSERT INTO audit_log (agent_id, action, resource_type, resource_id, payload)
               VALUES (?, ?, 'd3c_audit', ?, ?)""",
            (agent_id, action, f"res-{action}", json.dumps({"t": "d3c-audit"})),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def _d3c_audit_query(path: Path, sql: str, params: tuple = ()) -> list[tuple]:
    _d3c_assert_temp_db_path(path)
    conn = sqlite3.connect(str(path))
    try:
        return [tuple(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _d3c_audit_fk_enabled(path: Path) -> int:
    """PRAGMA foreign_keys de la connexió de l'aplicació (get_db)."""
    _d3c_assert_temp_db_path(path)
    with patch.object(settings, "DB_PATH", str(path)):

        async def _go():
            async with get_db() as db:
                cursor = await db.execute("PRAGMA foreign_keys")
                row = await cursor.fetchone()
                return row[0]

        return asyncio.run(_go())


class AuditLogMigrationTests(unittest.TestCase):
    """FASE 2b — migració idempotent d'audit_log (11 accions canòniques)."""

    maxDiff = 8000
    _db_patch = None

    @classmethod
    def setUpClass(cls) -> None:
        # Mateix patró d'aïllament que DashboardCancelTests: settings.DB_PATH
        # queda fixat a un fitxer dins del tempfile de la suite ABANS de
        # qualsevol escriptura.
        cls._db_patch = patch.object(settings, "DB_PATH", str(_DB_AUDIT))
        cls._db_patch.start()
        _d3c_assert_temp_db()
        security._bcrypt_cache.clear()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._db_patch is not None:
            cls._db_patch.stop()

    def setUp(self) -> None:
        # GUARDA D'AÏLLAMENT: cap test nou escriu fora del directori temporal.
        _d3c_assert_temp_db()
        _d3c_assert_temp_db_path(Path(_TMP.name) / "d3c_audit_current.db")

    # ====== §3a: esquema legacy production-like =======================

    def test_d3c_audit_01_legacy_production_schema_migration(self) -> None:
        """Legacy 5-action CHECK + FK → migració completa preservant files."""
        mig = Path(_TMP.name) / "d3c_audit_legacy_production.db"
        fixture = _d3c_audit_legacy_db(mig)
        before = _d3c_audit_snapshot(mig)

        # Fixture realment PRE-correcció.
        for action in ("RECALL", "CLAIM", "COMPLETE", "FAIL", "REJECT", "CANCEL"):
            self.assertNotIn(action, before["audit_sql"])
        self.assertNotIn("cancelled", before["directives_sql"])

        # El CHECK legacy REBUTJA les accions noves (fixture vàlida).
        for action in ("RECALL", "CLAIM", "COMPLETE", "FAIL", "REJECT", "CANCEL"):
            with self.subTest(action=action):
                with self.assertRaises(sqlite3.IntegrityError):
                    _d3c_audit_insert_action(mig, action)

        audit_rows_before = len(before["audit_rows"])
        self.assertEqual(audit_rows_before, len(fixture["audit_rows"]))
        self.assertEqual(before["audit_rows"], [tuple(r) for r in fixture["audit_rows"]])

        _d3c_audit_run_migration(mig)
        after = _d3c_audit_snapshot(mig)

        # AUDIT_ROWS_AFTER == AUDIT_ROWS_BEFORE, files idèntiques (7 columnes).
        self.assertEqual(len(after["audit_rows"]), audit_rows_before)
        self.assertEqual(after["audit_rows"], before["audit_rows"], "files/ids preservats")
        self.assertEqual(
            [r[AUDIT_COLUMNS.index("id")] for r in after["audit_rows"]],
            [r[AUDIT_COLUMNS.index("id")] for r in before["audit_rows"]],
            "ids preservats",
        )
        # La fila òrfena (agent_id inexistent) ha sobreviscut → FK OFF correcte.
        orphans = [r for r in after["audit_rows"] if r[1] == "agent-ghost"]
        self.assertEqual(len(orphans), 1)
        self.assertEqual(orphans[0][2], "READ")

        # CHECK canònic present i les 11 accions s'accepten individualment.
        self.assertIn("CHECK (action IN", after["audit_sql"])
        for action in CANONICAL_AUDIT_ACTIONS_11:
            with self.subTest(action=action):
                self.assertIn(f"'{action}'", after["audit_sql"])
                self.assertIsNotNone(_d3c_audit_insert_action(mig, action))
        # …i una acció fora de l'allowlist SEGUEIX rebutjada (CHECK real).
        with self.assertRaises(sqlite3.IntegrityError):
            _d3c_audit_insert_action(mig, "NOT_AN_ACTION")

        # La taula directives també queda migrada.
        self.assertIn("cancelled", after["directives_sql"])
        self.assertTrue(
            {"cancelled_at", "cancelled_by_agent_id", "cancellation_reason"}
            <= {r[1] for r in _d3c_audit_query(mig, "PRAGMA table_info(directives)")}
        )
        self.assertTrue(OLD_DIRECTIVE_INDEXES <= set(after["directives_indexes"]))

        # Cap resta de taules temporals; cap escriptura a facts.
        self.assertEqual(after["leftovers"], [])
        self.assertEqual(after["facts_count"], 0)
        self.assertIn(before["facts_count"], (None, 0))

        # Segona execució → cap canvi (idempotència real).
        pre_second = _d3c_audit_snapshot(mig)
        _d3c_audit_run_migration(mig)
        again = _d3c_audit_snapshot(mig)
        self.assertEqual(again, pre_second, "segona migració ha de ser NO-OP")

    # ====== §3b: estat residual de l'incident =========================

    def test_d3c_audit_02_incident_state_migration(self) -> None:
        """directives JA D3-C + audit_log legacy → només es migra audit_log."""
        mig = Path(_TMP.name) / "d3c_audit_incident_state.db"
        fixture = _d3c_audit_legacy_db(mig, directives_d3c=True)
        before = _d3c_audit_snapshot(mig)

        self.assertIn("cancelled", before["directives_sql"])
        self.assertNotIn("RECALL", before["audit_sql"])
        self.assertNotIn("CANCEL", before["audit_sql"])
        residual = [
            r for r in before["directives_rows"] if r[0] == "d3c-audit-residual-cancelled"
        ]
        self.assertEqual(len(residual), 1)
        self.assertEqual(residual[0][7], "cancelled")
        directives_sql_before = before["directives_sql"]
        directives_rows_before = before["directives_rows"]

        _d3c_audit_run_migration(mig)
        after = _d3c_audit_snapshot(mig)

        # audit_log migrat i files preservades.
        self.assertIn("CHECK (action IN", after["audit_sql"])
        self.assertIn("'CANCEL'", after["audit_sql"])
        self.assertEqual(after["audit_rows"], before["audit_rows"])
        self.assertEqual(len(after["audit_rows"]), len(fixture["audit_rows"]))
        for action in CANONICAL_AUDIT_ACTIONS_11:
            with self.subTest(action=action):
                self.assertIsNotNone(_d3c_audit_insert_action(mig, action))

        # directives INTACTE (esquema i files byte a byte).
        self.assertEqual(after["directives_sql"], directives_sql_before)
        self.assertEqual(after["directives_rows"], directives_rows_before)
        self.assertEqual(after["directives_indexes"], before["directives_indexes"])
        self.assertEqual(after["grants"], before["grants"])
        residual_after = [
            r for r in after["directives_rows"] if r[0] == "d3c-audit-residual-cancelled"
        ]
        self.assertEqual(len(residual_after), 1)
        self.assertEqual(residual_after[0][7], "cancelled")
        self.assertEqual(residual_after[0][19], "incident-hold")

        # Cap resta; idempotent.
        self.assertEqual(after["leftovers"], [])
        pre_second = _d3c_audit_snapshot(mig)
        _d3c_audit_run_migration(mig)
        self.assertEqual(_d3c_audit_snapshot(mig), pre_second)

    # ====== §3c: accions noves i legacy acceptades ====================

    def test_d3c_audit_03_new_actions_accepted_individually(self) -> None:
        """RECALL + cicle de vida de directives: 1 fila per acció → OK."""
        mig = Path(_TMP.name) / "d3c_audit_new_actions.db"
        _d3c_audit_legacy_db(mig)
        _d3c_audit_run_migration(mig)

        # NOTA: l'espec §3c diu "les 7 accions noves"; el conjunt canònic
        # (11) menys les 5 legacy en deixa 6 → aquí es cobreixen les 6 reals
        # (i les 5 legacy a test_d3c_audit_04).
        new_actions = tuple(a for a in CANONICAL_AUDIT_ACTIONS_11 if a not in LEGACY_AUDIT_ACTIONS)
        self.assertEqual(len(new_actions), 6)
        ids = {}
        for action in new_actions:
            with self.subTest(action=action):
                ids[action] = _d3c_audit_insert_action(mig, action)
                self.assertIsNotNone(ids[action])
        for action in new_actions:
            rows = _d3c_audit_query(
                mig, "SELECT action FROM audit_log WHERE id = ?", (ids[action],)
            )
            self.assertEqual(rows, [(action,)], action)
        with self.assertRaises(sqlite3.IntegrityError):
            _d3c_audit_insert_action(mig, "BOGUS")

    def test_d3c_audit_04_legacy_actions_still_accepted(self) -> None:
        """Les 5 accions legacy segueixen acceptades (i amb les seves files)."""
        mig = Path(_TMP.name) / "d3c_audit_legacy_actions.db"
        fixture = _d3c_audit_legacy_db(mig)
        _d3c_audit_run_migration(mig)

        for i, action in enumerate(LEGACY_AUDIT_ACTIONS):
            with self.subTest(action=action):
                original = tuple(fixture["audit_rows"][i])
                rows = _d3c_audit_query(
                    mig,
                    f"SELECT {', '.join(AUDIT_COLUMNS)} FROM audit_log WHERE id = ?",
                    (original[0],),
                )
                self.assertEqual(rows, [original])
                self.assertIsNotNone(_d3c_audit_insert_action(mig, action))
        self.assertEqual(
            _d3c_audit_query(mig, "SELECT COUNT(*) FROM audit_log")[0][0],
            len(fixture["audit_rows"]) + len(LEGACY_AUDIT_ACTIONS),
        )

    # ====== §3d: cap fila perduda + sqlite_sequence ===================

    def test_d3c_audit_05_no_rows_lost_and_sqlite_sequence_preserved(self) -> None:
        """sqlite_sequence no retrocedeix: una fila nova rep id > max històric."""
        mig = Path(_TMP.name) / "d3c_audit_sequence.db"
        fixture = _d3c_audit_legacy_db(mig)
        before = _d3c_audit_snapshot(mig)
        max_before = max(r[AUDIT_COLUMNS.index("id")] for r in before["audit_rows"])
        self.assertEqual(max_before, 6)
        self.assertTrue(before["sqlite_sequence_exists"])
        self.assertEqual(before["audit_seq"], max_before)

        _d3c_audit_run_migration(mig)
        after = _d3c_audit_snapshot(mig)
        self.assertTrue(after["sqlite_sequence_exists"])
        self.assertEqual(after["audit_seq"], max_before)
        self.assertEqual(len(after["audit_rows"]), len(fixture["audit_rows"]))
        self.assertEqual(
            sorted(r[AUDIT_COLUMNS.index("id")] for r in after["audit_rows"]),
            sorted(r[AUDIT_COLUMNS.index("id")] for r in before["audit_rows"]),
        )

        # Fila nova sense id explícit → id estrictament major que l'històric.
        new_id = _d3c_audit_insert_action(mig, "CANCEL")
        self.assertGreater(new_id, max_before)
        self.assertEqual(new_id, max_before + 1)
        self.assertEqual(
            _d3c_audit_query(
                mig,
                f"SELECT {', '.join(AUDIT_COLUMNS)} FROM audit_log WHERE id = ?",
                (new_id,),
            )[0][2],
            "CANCEL",
        )
        # La segona passada no altera res (ni el comptador).
        _d3c_audit_run_migration(mig)
        final = _d3c_audit_snapshot(mig)
        self.assertEqual(final["audit_seq"], new_id)
        self.assertEqual(len(final["audit_rows"]), len(fixture["audit_rows"]) + 1)

    # ====== §3e: guarda de DB temporal (control negatiu) ==============

    def test_d3c_audit_06_temp_db_guard_active(self) -> None:
        """La guarda existent atura qualsevol ruta fora del directori temporal."""
        # Ruta vàlida (dins tempfile) → no peta.
        _d3c_assert_temp_db_path(Path(_TMP.name) / "d3c_audit_guard_ok.db")
        # Ruta de PRODUCCIÓ → RuntimeError.
        with self.assertRaises(RuntimeError):
            _d3c_assert_temp_db_path(_PROD_DB)
        # settings.DB_PATH apuntant a producció → RuntimeError.
        with patch.object(settings, "DB_PATH", _PROD_DB):
            with self.assertRaises(RuntimeError):
                _d3c_assert_temp_db()
        # Ruta fora del directori temporal → RuntimeError.
        with patch.object(settings, "DB_PATH", "/root/d3c-audit-not-temp.db"):
            with self.assertRaises(RuntimeError):
                _d3c_assert_temp_db()
        # Els helpers nous també hi passen per la guarda.
        with self.assertRaises(RuntimeError):
            _d3c_audit_legacy_db(Path(_PROD_DB))
        with patch.object(settings, "DB_PATH", _PROD_DB):
            with self.assertRaises(RuntimeError):
                _d3c_audit_query(_PROD_DB, "SELECT 1")

    # ====== §2: FK handling, índexs i restauració =====================

    def test_d3c_audit_07_fk_off_handling_and_indexes_recreated(self) -> None:
        """L'INSERT ... SELECT només funciona amb FK OFF; índexs recreats idèntics."""
        mig = Path(_TMP.name) / "d3c_audit_fk.db"
        fixture = _d3c_audit_legacy_db(mig)
        before = _d3c_audit_snapshot(mig)
        self.assertTrue(set(AUDIT_LEGACY_INDEXES) <= set(before["audit_indexes"]))
        self.assertEqual(len([r for r in before["audit_rows"] if r[1] == "agent-ghost"]), 1)

        # Evidència del pitfall: amb FK ON la còpia de les files òrfenes peta.
        conn = sqlite3.connect(str(mig))
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                """CREATE TABLE audit_probe (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       agent_id TEXT REFERENCES agents(id),
                       action TEXT NOT NULL,
                       resource_type TEXT NOT NULL,
                       resource_id TEXT, payload TEXT, timestamp TEXT)"""
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO audit_probe (id, agent_id, action, resource_type, "
                    "resource_id, payload, timestamp) SELECT id, agent_id, action, "
                    "resource_type, resource_id, payload, timestamp FROM audit_log"
                )
            conn.rollback()
            conn.execute("DROP TABLE IF EXISTS audit_probe")
            conn.commit()
        finally:
            conn.close()

        _d3c_audit_run_migration(mig)
        after = _d3c_audit_snapshot(mig)

        # La còpia s'ha fet amb FK OFF i totes les files hi són.
        self.assertEqual(after["audit_rows"], before["audit_rows"])
        self.assertEqual(len(after["audit_rows"]), len(fixture["audit_rows"]))
        # FK restaurada a ON a la connexió de l'aplicació.
        self.assertEqual(_d3c_audit_fk_enabled(mig), 1)
        # Índexs recreats idèntics (nom + SQL).
        for name in AUDIT_LEGACY_INDEXES:
            self.assertIn(name, after["audit_indexes"])
            self.assertEqual(
                after["audit_indexes"][name].split(" ON ")[0],
                before["audit_indexes"][name].split(" ON ")[0],
                name,
            )
        # Cap taula temporal residual.
        self.assertEqual(after["leftovers"], [])
        # La FK torna a estar REALMENT en vigor després de la migració.
        conn = sqlite3.connect(str(mig))
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO audit_log
                       (agent_id, action, resource_type, resource_id, payload)
                       VALUES ('agent-ghost', 'READ', 'fact', 'x', '{}')"""
                )
        finally:
            conn.close()


# ==========================================================================
# D3-C §7 — UI MÍNIMA DE CANCEL (tests d'UI: estàtic servit + API-level)
# ==========================================================================
#
# HONESTEDAT METODOLÒGICA (no sobreafirmar): en aquest entorn NO hi ha
# navegador ni motor JavaScript dins de pytest. Aquests tests NO executen el
# JavaScript al navegador. Es fan servir dues tècniques:
#
#   (a) ESTÀTIC sobre l'HTML/JS REALMENT SERVIT per `GET /dashboard`: es
#       llegeix la resposta HTTP (no el fitxer font) i s'hi fan assercions
#       estructurals — la ruta de cancel al mapa API, la guarda de gating
#       `can_cancel !== true`, el cos del POST (`reason` + `expected_status`),
#       els missatges literals d'error (403/404/409/415/422), la crida de
#       refresc `loadAgents()` després de la resposta, el warning literal de
#       l'estat claimed, l'absència de credencials i l'absència de controls
#       prohibits (RETRY/REPRIORITIZE/PAUSE/RESUME/START/STOP/RESTART).
#
#   (b) API-LEVEL sobre els endpoints reals: `/v1/dashboard/agents` (d'on surt
#       el valor `can_cancel` que la UI NOMÉS pinta) i
#       `/v1/dashboard/control/<id>/cancel` (el contracte d'estats que el JS
#       descriu: 200/403/404/409/415/422 i l'estat post-cancel·lació).
#
# No s'afirma enlloc "provat al navegador". Els 82 tests existents
# (31 D3-B + 51 D3-C) no es modifiquen.

import re  # noqa: E402  (assercions estàtiques §7)

_UI_WARNING = (
    "Cancel·lar retira la directiva de Pluribus. No atura forçosament un "
    "procés que ja s'estigui executant a l'agent."
)
_UI_FORBIDDEN_CONTROLS = (
    "RETRY", "REPRIORITIZE", "PAUSE", "RESUME", "START", "STOP", "RESTART",
)
_UI_CANCEL_ROUTE = "/v1/dashboard/control/{id}/cancel"


def _ui_slice(text: str, start: str, end: str) -> str:
    """Retorna el tros de `text` des de `start` fins a la propera `end`.

    Falla sorollosament (ValueError) si l'àncora ha desaparegut: un refactor
    que esborri la secció de CANCEL no pot passar desapercebut.
    """
    i = text.index(start)
    j = text.index(end, i + len(start))
    return text[i:j]


class DashboardCancelUiTests(unittest.TestCase):
    """D3-C §7 — UI mínima de CANCEL (estàtic sobre l'HTML servit + API-level)."""

    maxDiff = 8000
    _client = None
    _db_patch = None
    _page = None

    @classmethod
    def setUpClass(cls) -> None:
        # Mateix patró d'aïllament que DashboardCancelTests: settings.DB_PATH
        # fixat dins del tempfile de la suite ABANS de qualsevol escriptura.
        cls._db_patch = patch.object(settings, "DB_PATH", str(_DB_D3C))
        cls._db_patch.start()
        _d3c_assert_temp_db()
        security._bcrypt_cache.clear()
        cls._client = _d3c_setup_client()
        # PÀGINA REAL SERVIDA (és el que el navegador rebria).
        r = cls._client.get("/dashboard")
        if r.status_code != 200:
            raise AssertionError(f"GET /dashboard -> {r.status_code}: {r.text[:200]}")
        if "text/html" not in r.headers.get("content-type", ""):
            raise AssertionError(f"GET /dashboard content-type: {r.headers}")
        cls._page = r.text

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
        # GUARDA D'AÏLLAMENT: cap test escriu fora del directori temporal.
        _d3c_assert_temp_db()
        _d3c_init_db_sync()
        _d3c_seed_all_agents_sync()
        if self._client is not None:
            self._client.cookies.clear()
        from pluribus import security as _sec
        _sec._rate_limiter.clear()
        _sec._bcrypt_cache.clear()
        _sec._last_rate_cleanup = 0.0

    # --- extractes estàtics de la pàgina servida --------------------------

    def _cell_fn(self) -> str:
        """Cos de `cancelCell` (builder gatat) de l'HTML servit."""
        return _ui_slice(self._page, "function cancelCell(",
                         "function cancelErrorMessage(")

    def _err_fn(self) -> str:
        """Cos de `cancelErrorMessage` (mapa d'errors) de l'HTML servit."""
        return _ui_slice(self._page, "function cancelErrorMessage(",
                         "function cancelOut(")

    def _post_fn(self) -> str:
        """Cos de `cancelDirective` (POST + refresc) de l'HTML servit."""
        return _ui_slice(self._page, "async function cancelDirective(",
                         "function cancelBind(")

    def _bind_fn(self) -> str:
        """Cos de `cancelBind` (motiu + confirmació) de l'HTML servit."""
        return _ui_slice(self._page, "function cancelBind(",
                         "// ========== MEMORY")

    # ====== §7.1 ========================================================

    def test_d3c_ui_01_can_cancel_false_no_actionable_control(self) -> None:
        """can_cancel=false/absent → cap control accionable (gating estricte).

        Estàtic: el builder està gatat per `can_cancel !== true` i retorna
        cadena buida ABANS de generar cap markup. API: el servidor diu
        can_cancel=False per a un caller que no és l'emissor ni admin.
        """
        cell = self._cell_fn()
        # Guarda estricta (identitat): ni absent, ni false, ni 'true' string.
        self.assertIn("if (!d || d.can_cancel !== true) return '';", cell)
        # El retorn buit va ABANS del markup del control.
        self.assertLess(cell.index("return ''"), cell.index("<button"))
        # Només hi ha UN camí que pinta el control, i és després de la guarda.
        self.assertEqual(cell.count("data-cancel-id"), 1)
        self.assertGreater(cell.index("data-cancel-id"),
                           cell.index("can_cancel !== true"))
        # Cap heurística local de legalitat (status/issuer decidits al client).
        self.assertNotIn("issuer_agent_id", cell)
        self.assertNotIn("status === 'pending'", cell)
        self.assertNotIn("status === 'claimed'", cell)

        # API-level: la directiva és visible per a un tercer, però el
        # servidor diu can_cancel=False → la UI no hi pinta res.
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-ui-01-gate")
        entry_ro = _d3c_agent(_d3c_agents(c, KEY_RO), "d3b-worker")
        self.assertIsNotNone(entry_ro["pending_directive"])
        self.assertEqual(entry_ro["pending_directive"]["id"], d["id"])
        self.assertIs(entry_ro["pending_directive"]["can_cancel"], False)
        entry_noscope = _d3c_agent(_d3c_agents(c, KEY_NOSCOPE), "d3b-worker")
        self.assertIs(entry_noscope["pending_directive"]["can_cancel"], False)

    # ====== §7.2 ========================================================

    def test_d3c_ui_02_can_cancel_true_control_targets_cancel_route(self) -> None:
        """can_cancel=true → el control existeix i envia l'id correcte.

        Estàtic: la ruta viu al mapa `API` i el control porta el directive id
        (que el handler injecta a la ruta substituint `{id}`).
        API: per a l'issuer el servidor diu can_cancel=True.
        """
        page = self._page
        self.assertIn(f"cancel:  '{_UI_CANCEL_ROUTE}'", page)
        cell = self._cell_fn()
        self.assertIn("const rid = esc(d.id);", cell)
        self.assertIn('data-cancel-id="${rid}"', cell)
        post = self._post_fn()
        self.assertIn("API.cancel.replace('{id}', encodeURIComponent(id))", post)

        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-ui-02-route")
        entry = _d3c_agent(_d3c_agents(c, KEY_OP), "d3b-worker")
        self.assertIsNotNone(entry["pending_directive"])
        self.assertEqual(entry["pending_directive"]["id"], d["id"])
        self.assertIs(entry["pending_directive"]["can_cancel"], True)
        self.assertEqual(entry["pending_directive"]["issuer_agent_id"], "d3b-op")

    # ====== §7.3 ========================================================

    def test_d3c_ui_03_reason_required_js_and_api_422(self) -> None:
        """Motiu obligatori: validació JS (no s'envia res) I servidor (422)."""
        bind = self._bind_fn()
        self.assertIn("const reason = input ? input.value.trim() : '';", bind)
        self.assertIn("if (!reason) {", bind)
        # El tall local és ABANS de cridar el POST.
        self.assertLess(bind.index("if (!reason) {"),
                        bind.index("cancelDirective(id, expectedStatus, reason, btn);"))
        self.assertIn("el motiu és obligatori", bind)
        # El motiu viatja al cos del POST.
        self.assertIn("reason: reason", self._post_fn())

        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-ui-03-reason")
        # (b) API: reason buit → 422 i cap mutació.
        r_empty = _d3c_cancel(c, d["id"], reason="", expected="pending")
        self.assertEqual(r_empty.status_code, 422, r_empty.text[:200])
        # reason absent → 422.
        r_missing = c.post(
            _d3c_cancel_url(d["id"]),
            json={"expected_status": "pending"},
            headers=_hdr_c(KEY_OP),
        )
        self.assertEqual(r_missing.status_code, 422, r_missing.text[:200])
        self.assertEqual(_d3c_row(d["id"])["status"], "pending")
        self.assertEqual(_d3c_audit_count("CANCEL", d["id"]), 0)

    # ====== §7.4 ========================================================

    def test_d3c_ui_04_expected_status_in_post_body(self) -> None:
        """`expected_status` s'envia al cos del POST i es deriva del servidor.

        La UI NO decideix legalitat: pren `d.status` (camp que el servidor
        exposeja a cada entrada de directiva) amb fallback a l'origen
        ('claimed' per current_task_detail, 'pending' per pending_directive).
        """
        post = self._post_fn()
        self.assertIn("method: 'POST'", post)
        self.assertIn("headers: { 'Content-Type': 'application/json' }", post)
        self.assertIn("JSON.stringify({ reason: reason, expected_status: expectedStatus })", post)
        cell = self._cell_fn()
        self.assertIn("const expected = d.status || fallbackExpected || '';", cell)
        page = self._page
        self.assertIn("cancelCell(a.current_task_detail, 'claimed')", page)
        self.assertIn("cancelCell(a.pending_directive, 'pending')", page)

        # API-level: el servidor exposa el `status` de cada entrada.
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-ui-04-expected")
        entry_pending = _d3c_agent(_d3c_agents(c, KEY_OP), "d3b-worker")
        self.assertEqual(entry_pending["pending_directive"]["id"], d["id"])
        self.assertEqual(entry_pending["pending_directive"]["status"], "pending")

        self.assertEqual(_d3c_claim(c, d["id"]).status_code, 200)
        _d3c_db_exec(
            "UPDATE agents SET current_task_id = ? WHERE id = 'd3b-worker'",
            (d["id"],),
        )
        entry_claimed = _d3c_agent(_d3c_agents(c, KEY_OP), "d3b-worker")
        self.assertIsNotNone(entry_claimed["current_task_detail"])
        self.assertEqual(entry_claimed["current_task_detail"]["id"], d["id"])
        self.assertEqual(entry_claimed["current_task_detail"]["status"], "claimed")

    # ====== §7.5 ========================================================

    def test_d3c_ui_05_error_messages_and_409_stale_state(self) -> None:
        """Missatges clars 403/404/409/415/422 (literal) + 409 real d'estat.

        Estàtic: cada codi té el seu missatge literal al mapa d'errors.
        API: el servidor retorna exactament aquests codis en els casos que
        el JS descriu (i el 409 no muta res ni audita).
        """
        err = self._err_fn()
        self.assertIn("if (status === 403) return 'No autoritzat per cancel·lar aquesta directiva.';", err)
        self.assertIn("if (status === 404) return 'Directiva no trobada.';", err)
        self.assertIn(
            'if (status === 409) return "L\'estat ha canviat (conflicte). '
            'Recarrega; la directiva ja no és cancel·lable.";',
            err,
        )
        self.assertIn("if (status === 415) return 'Petició no vàlida (cal JSON).';", err)
        self.assertIn(
            'if (status === 422) return "Falta el motiu o l\'estat esperat no és vàlid.";',
            err,
        )
        # El 409 és l'únic que ordena recarregar l'estat.
        self.assertIn("Recarrega", err)

        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-ui-05-errors")

        # 409: expected_status desactualitzat (la fila és 'pending').
        r409 = _d3c_cancel(c, d["id"], expected="claimed")
        self.assertEqual(r409.status_code, 409, r409.text[:200])
        self.assertEqual(_d3c_row(d["id"])["status"], "pending")
        self.assertEqual(_d3c_audit_count("CANCEL", d["id"]), 0)

        # 403: caller que no és l'emissor ni admin.
        r403 = _d3c_cancel(c, d["id"], key=KEY_RO)
        self.assertEqual(r403.status_code, 403, r403.text[:200])

        # 404: directiva inexistent.
        r404 = _d3c_cancel(c, uuid.uuid4().hex)
        self.assertEqual(r404.status_code, 404, r404.text[:200])

        # 415: Content-Type no JSON (abans del parsing).
        r415 = c.post(
            _d3c_cancel_url(d["id"]),
            content="this is not json",
            headers={**_hdr_c(KEY_OP), "Content-Type": "text/plain"},
        )
        self.assertEqual(r415.status_code, 415, r415.text[:200])

        # 422: motiu buit.
        r422 = _d3c_cancel(c, d["id"], reason="", expected="pending")
        self.assertEqual(r422.status_code, 422, r422.text[:200])

        # Cap dels errors ha mutat la directiva.
        self.assertEqual(_d3c_row(d["id"])["status"], "pending")
        self.assertEqual(_d3c_audit_count("CANCEL", d["id"]), 0)

    # ====== §7.6 ========================================================

    def test_d3c_ui_06_claimed_warning_literal(self) -> None:
        """El warning de l'estat claimed apareix LITERAL a la pàgina servida."""
        self.assertIn(_UI_WARNING, self._page)
        self.assertIn(f'const CANCEL_WARNING = "{_UI_WARNING}";', self._page)
        # Es mostra al diàleg de confirmació quan l'estat esperat és claimed.
        bind = self._bind_fn()
        self.assertIn("if (expectedStatus === 'claimed') msg += ' — ' + cancelWarning();", bind)
        # Confirmació obligatòria ABANS de qualsevol POST.
        self.assertIn("if (!window.confirm(msg)) return;", bind)
        self.assertLess(bind.index("window.confirm(msg)"),
                        bind.index("cancelDirective(id, expectedStatus, reason, btn);"))

    # ====== §7.7 ========================================================

    def test_d3c_ui_07_refresh_agents_after_response(self) -> None:
        """El refresc de l'estat passa DESPRÉS de la resposta, èxit i error."""
        post = self._post_fn()
        self.assertIn("finally {", post)
        self.assertLess(post.index("fetch(API.cancel"), post.index("loadAgents();"))
        self.assertGreater(post.index("loadAgents();"), post.index("finally {"))
        # Idempotència visual: botó desactivat durant la petició i reactivat.
        self.assertLess(post.index("btn.disabled = true;"), post.index("fetch(API.cancel"))
        self.assertIn("btn.disabled = false;", post)

        # API-level: després d'un cancel 200, la relectura ja no mostra la
        # directiva (és exactament el que el refresc de la UI pinta).
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-ui-07-refresh")
        before = _d3c_agent(_d3c_agents(c, KEY_OP), "d3b-worker")
        self.assertIsNotNone(before["pending_directive"])
        self.assertEqual(before["pending_directive"]["id"], d["id"])

        r = _d3c_cancel(c, d["id"], reason="d3c-ui-07", expected="pending")
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertEqual(r.json()["status"], "cancelled")

        after = _d3c_agent(_d3c_agents(c, KEY_OP), "d3b-worker")
        self.assertIsNone(after["pending_directive"])
        self.assertIsNone(after["current_task_detail"])

    # ====== §7.8 ========================================================

    def test_d3c_ui_08_terminal_directive_no_actionable_cancel(self) -> None:
        """Directiva terminal → sense CANCEL accionable (servidor + gating)."""
        from pluribus.dashboard_observability import _can_cancel

        for status in ("completed", "failed", "rejected", "expired", "cancelled"):
            with self.subTest(status=status):
                self.assertIs(
                    _can_cancel(
                        status=status,
                        issuer_agent_id="d3b-op",
                        scope="shared",
                        caller_id="d3b-op",
                        caller_is_admin=True,
                        caller_scopes={"shared", "local"},
                    ),
                    False,
                )
        # Control positiu (l'estat legal sí que és cancel·lable).
        self.assertIs(
            _can_cancel(
                status="pending",
                issuer_agent_id="d3b-op",
                scope="shared",
                caller_id="d3b-op",
                caller_is_admin=False,
                caller_scopes={"shared"},
            ),
            True,
        )

        # API-level: una directiva completada no apareix enlloc del payload
        # → la UI no pot pintar cap control per a ella.
        _d3c_grant_success_path()
        c = self._client
        d = _d3c_assign(c, key=KEY_OP, idem="d3c-ui-08-terminal")
        self.assertEqual(_d3c_claim(c, d["id"]).status_code, 200)
        self.assertEqual(_d3c_complete(c, d["id"]).status_code, 200)
        entry = _d3c_agent(_d3c_agents(c, KEY_OP), "d3b-worker")
        self.assertIsNone(entry["pending_directive"])
        self.assertIsNone(entry["current_task_detail"])
        # I el builder continua gatat pel camp autoritatiu.
        self.assertIn("if (!d || d.can_cancel !== true) return '';", self._cell_fn())

    # ====== §7.9 ========================================================

    def test_d3c_ui_09_no_credentials_in_served_page(self) -> None:
        """Cap credencial al JS/HTML servit (cookie same-origin, no API key)."""
        page = self._page
        for needle in ("api_key", "apikey", "X-API-Key", "x-api-key",
                       "Authorization", "Bearer ", "PLURIBUS_API_KEY", "sk-"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, page,
                                 f"credential-like substring {needle!r} in served page")
        lowered = page.lower()
        for needle in ("api_key", "x-api-key", "bearer ", "authorization"):
            self.assertNotIn(needle, lowered)
        # Les mutacions del dashboard viatgen amb la cookie de sessió
        # mateixa-origen (el comentari del JS ho diu explícitament).
        self.assertIn("same-origin", page)

    # ====== §7.10 =======================================================

    def test_d3c_ui_10_no_forbidden_controls(self) -> None:
        """Cap control RETRY/REPRIORITIZE/PAUSE/RESUME/START/STOP/RESTART."""
        page = self._page
        for word in _UI_FORBIDDEN_CONTROLS:
            with self.subTest(word=word):
                self.assertIsNone(
                    re.search(r"\b" + word + r"\b", page, re.IGNORECASE),
                    f"forbidden control {word!r} present in served page",
                )
        # Els únics controls de la pàgina són els preexistents (cerca de
        # memòria, ASSIGN de D3-B) i el CANCEL nou: cap altre botó.
        buttons = re.findall(r"<button[^>]*>", page)
        self.assertTrue(buttons, "no buttons found — extractor broke")
        allowed = ('id="memory-search-btn"', 'id="as-submit"', 'class="cancel-btn"')
        for b in buttons:
            with self.subTest(button=b[:80]):
                self.assertTrue(any(a in b for a in allowed),
                                f"unexpected control rendered: {b[:160]}")
        # El botó de CANCEL ve del builder gatat (una sola aparició).
        self.assertEqual(self._cell_fn().count("data-cancel-id"), 1)
