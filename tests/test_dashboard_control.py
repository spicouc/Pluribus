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
