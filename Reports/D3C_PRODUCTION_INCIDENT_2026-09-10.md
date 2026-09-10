# D3-C — Production database incident report (2026-09-10)

**Severity:** SEV-2 (unintended write to the production database; no legitimate data loss)
**Component:** Pluribus directives / D3-C safe CANCEL lifecycle
**Branch:** `feature/dashboard-d3c-safe-cancel` (base `bd4b78bdac2a5ec97521395b480a1ea7b277f9a5`)
**Status:** contained, cleaned up, corrective controls merged in this branch. Production freeze still in force.
**Production DB:** `/opt/pluribus/data/pluribus.db`

This report contains **no secrets** (no credentials, keys, tokens or password material): only schema
shapes, row counts and paths.

---

## 1. Root cause — `DB_PATH` vs `PLURIBUS_DB_PATH`

`pluribus/config.py` resolves the database path through `pydantic-settings` with
`env_prefix = "PLURIBUS_"`. The setting is therefore read from **`PLURIBUS_DB_PATH`**.

The bare environment variable **`DB_PATH` is silently ignored** — pydantic-settings does not warn
about unknown environment variables, and there is no explicit validation that a "did you mean"
variable was set. When `PLURIBUS_DB_PATH` is absent, the setting falls back to its default, which is
the production path `/opt/pluribus/data/pluribus.db`.

Failure mode in one line: *a mistyped environment variable does not fail loudly, it silently selects
production.*

## 2. Accidental production path

During phase 1 a throwaway verification script launched by the phase executor exported **`DB_PATH`**
(instead of `PLURIBUS_DB_PATH`) to point at what was believed to be a scratch database. Because the
variable was ignored, the configuration resolved the **production default**
`/opt/pluribus/data/pluribus.db`, and the script wrote rows into the live database (agents, grants,
directives, a dashboard session and one audit row).

## 3. Data impact — no legitimate data loss

| observation | value |
|---|---|
| legitimate rows lost | **0** |
| `directives` rows before / after | 0 / 0 |
| `directives` rows in the 2026-08-24 backup | 0 |
| `audit_log` rows with `resource_type='directive'` (entire history) | 0 |
| `facts` | 302 active / 327 total (untouched) |
| `agents` | the 7 legitimate agents, `0` with a `d3c-` name |

No legitimate data was destroyed, overwritten or rewritten. The `directives` feature had never been
used in production (no row and no directive audit in its whole history), so the accidental writes
were strictly additive and confined to synthetic `d3c-*` test fixtures.

## 4. Cleanup

Every row created by the accidental run was deleted and re-verified as absent:

- 5 agents with a `d3c-` name → 0
- 2 `directive_grants` rows → 0
- 3 `directives` rows → 0
- 1 `dashboard_sessions` row → 0
- 1 `audit_log` row (action `CREATE`, `d3c-*` actor) → 0

Verified afterwards by **content** (see §8): `agents_d3c_prefix = 0`, `directives_total = 0`,
`directive_grants = 0`, `dashboard_sessions = 0`, `audit_d3c_actor = 0`.
File `mtime` is **not** used as evidence anywhere in this report: the live service continuously
writes `SEARCH` audit rows (e.g. actor `dindjarin` every ~10 s), so timestamps carry no signal.

## 5. Residual `directives` schema change

The D3-C migration ran against the production `directives` table before the incident was detected.
The table therefore keeps the D3-C shape:

```
status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','claimed','completed','failed','rejected','expired','cancelled'))
...
cancelled_at TEXT,
cancelled_by_agent_id TEXT,
cancellation_reason TEXT
```

This is **forward-compatible and harmless**:

- widening a `CHECK` constraint never invalidates existing rows (the table is empty);
- adding nullable columns is a purely additive change;
- pre-D3-C code never references `cancelled_at` / `cancelled_by_agent_id` / `cancellation_reason`.

No rollback migration is required or planned; reverting the constraint would itself be a production
migration, which the freeze forbids.

## 6. Backup created

`/opt/pluribus/data/pluribus.db.bak-d3c-incident-20260910_155059` — **6,307,840 bytes**, taken before
any cleanup statement was executed. Kept in place as the forensic snapshot of the incident window.

## 7. Integrity check — `quick_check`

```
PRAGMA quick_check;  ->  ok
```

Run on the production database after cleanup. The database file is not corrupt.

## 8. Production freeze (still in force)

```
PRODUCTION_DB_WRITE / MIGRATION / RESTART / DEPLOY : FORBIDDEN
MAIN : HOLD
```

- `/opt/pluribus/data/pluribus.db` is intocable for this workstream.
- All D3-C tests run against a temporary database behind a fail-fast guard (see §9).
- `PRODUCTION_DB_TOUCHED` is decided by **content**, never by `mtime`.

Content evidence collected at the final gate (read-only connection, `mode=ro`):

```
facts_total 327 · facts_active 302 · agents_total 7 · agents_d3c_prefix 0
directives_total 0 · directive_grants 0 · dashboard_sessions 0
audit_log resource_type='directive' 0 · audit_log d3c actor 0
```

→ `PRODUCTION_DB_TOUCHED: NO`.

## 9. Test-isolation corrective

`tests/test_dashboard_control.py` now calls a fail-fast guard, `_d3c_assert_temp_db()`, **before any
write**. The guard resolves `settings.DB_PATH` and raises `RuntimeError` unless it points inside a
`tempfile` directory. Consequences:

- a test can never reach `/opt/pluribus/data/pluribus.db`; if configuration drifts back to
  production, the suite **aborts loudly instead of writing**;
- negative controls are covered (production path, and any non-temporary path → `RuntimeError`).

Root cause of the miss is therefore closed at the test layer: the previous flow tested "where did I
think I was writing?" while the guard tests "where is the process actually writing?".

## 10. `audit_log` schema drift (deployment blocker)

The production `audit_log` CHECK admitted only **5** actions:

```sql
action TEXT NOT NULL CHECK (action IN ('CREATE','READ','UPDATE','DELETE','SEARCH'))
```

while the codebase emits **34** actions — the 11 canonical ones plus 23 already-emitted ones
(`CLAIM`/`COMPLETE`/`FAIL`/`REJECT` for directives, `RECALL`, and the whole `XERRAMECA_*` family).
D3-C adds `CANCEL`.

Any directive audit write (or Xerrameca audit) would therefore have failed with
`IntegrityError: CHECK constraint failed` on deploy. This is a latent production bug discovered as a
side effect of the incident, and it is independent of the accidental writes.

## 11. Migration corrective — idempotent `audit_log` rebuild

`D3-C` ships an idempotent `audit_log` migration (`pluribus/db.py` + `scripts/init_db.sql`):

- **12-step table rebuild** (create new → copy → drop → rename → reindex) with a canonical
  allowlist of **34 actions** (11 canonical + 23 already emitted) — strictly **additive**: no
  previously accepted action becomes rejected, no historical row is lost or rewritten;
- `PRAGMA foreign_keys=OFF` around the rebuild so historical rows with dangling `agent_id`
  references survive (covered by a test that inserts an orphan row);
- indexes recreated, `sqlite_sequence` preserved so `AUTOINCREMENT` keeps counting above the
  historical maximum;
- **no-op when the CHECK is already canonical** (verified by repeated runs).

This migration is **NOT** applied to production by this branch: it ships with the code and will only
run as part of a future, explicitly authorised deploy.

---

## Timeline (UTC)

| time | event |
|---|---|
| ~15:4x | phase 1 verification script exports `DB_PATH` and writes to production |
| 15:50:59 | backup `pluribus.db.bak-d3c-incident-20260910_155059` created |
| ~15:5x | synthetic rows identified and deleted; verified at 0 |
| ~15:5x | `PRAGMA quick_check` → `ok` |
| 16:0x | production freeze declared (write/migration/restart/deploy FORBIDDEN) |
| later | fail-fast test guard + idempotent `audit_log` migration implemented and tested |

## Corrective actions

1. `_d3c_assert_temp_db()` fail-fast guard in the D3-C test module (done, tested).
2. Idempotent `audit_log` allowlist migration closing the 5-vs-34 drift (done, tested).
3. Production freeze honoured for the whole D3-C workstream: no production write, migration, restart
   or deploy (done).
4. No-secrets rule: this report and the diff contain no credential material (verified).
5. Residual risk accepted: the widened `directives` CHECK in production (§5); forward-compatible.

## Out of scope / not authorised

`MERGE_AUTHORIZED: NO` · `DEPLOY_AUTHORIZED: NO` · `PRODUCTION_MIGRATION_AUTHORIZED: NO` ·
`PRODUCTION_RESTART_AUTHORIZED: NO` · D3-D HOLD · XERRAMECA HOLD · M0 HOLD · MEMORY_CAPTURE HOLD ·
DUP_FIX HOLD.
