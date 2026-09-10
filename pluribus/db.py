"""Gestor de base de dades SQLite amb aiosqlite i suport per FTS5."""

import aiosqlite
from contextlib import asynccontextmanager
from pathlib import Path

from pluribus.config import settings


# ── D3-C corrective: canonical audit_log action allowlist ────────────────
# The five legacy actions (CREATE/READ/UPDATE/DELETE/SEARCH) are preserved
# verbatim. RECALL is emitted by pluribus/recall.py, and the directive
# lifecycle (CLAIM/COMPLETE/FAIL/REJECT/CANCEL) is emitted by
# pluribus/directives.py. SQLite cannot alter a CHECK constraint in place,
# so a legacy table (5-action CHECK, or no CHECK at all) is rebuilt by
# ``_migrate_audit_log_actions`` below, preserving every row and id.
CANONICAL_AUDIT_ACTIONS: tuple[str, ...] = (
    "CREATE", "READ", "UPDATE", "DELETE", "SEARCH",   # 5 legacy
    "RECALL",                                          # recall.py
    "CLAIM", "COMPLETE", "FAIL", "REJECT", "CANCEL",   # directive lifecycle
)

# Actions that the tree ALREADY emits through log_audit() outside the D3-C
# canonical scope. They were enumerated by an AST scan of EVERY
# log_audit()/_audit() call site (pluribus/**), not by hand. The CHECK is
# an allowlist over what the code writes: rejecting them would raise
# sqlite3.IntegrityError at runtime and regress 27 existing tests, because
# CI runs ``python -m unittest discover -s tests`` (see .github/workflows/
# ci.yml). Keeping them is strictly additive w.r.t. the legacy 5-action
# CHECK, so this phase never rejects an action that used to be accepted.
EXTENDED_AUDIT_ACTIONS: tuple[str, ...] = (
    # admin_config.py:238/255 (_audit(agent_id, action, payload))
    "CONFIG_UPDATE", "SERVICE_RESTART",
    # pluribus/xerrameca/service.py + claim.py + dialogue.py (_audit(db, ...))
    "XERRAMECA_ASSIGN_TURN", "XERRAMECA_CANCEL", "XERRAMECA_CLAIM",
    "XERRAMECA_CREATE", "XERRAMECA_DIALOGUE_REPLY", "XERRAMECA_DIALOGUE_START",
    "XERRAMECA_FINISH", "XERRAMECA_PARTICIPANT_UPDATE", "XERRAMECA_PAUSE",
    "XERRAMECA_REPLY", "XERRAMECA_RESUME", "XERRAMECA_SETTINGS",
    "XERRAMECA_SKIP_TURN", "XERRAMECA_START", "XERRAMECA_SYSTEM_UPDATE",
    # pluribus/xerrameca/runner.py
    "XERRAMECA_RUNNER_CONFIG", "XERRAMECA_RUNNER_DELETE",
    "XERRAMECA_RUNNER_DISPATCH", "XERRAMECA_RUNNER_FAILED",
    "XERRAMECA_RUNNER_ROTATE_SECRET", "XERRAMECA_RUNNER_SYSTEM",
)

# Effective allowlist enforced by the audit_log CHECK constraint.
AUDIT_ACTIONS: tuple[str, ...] = CANONICAL_AUDIT_ACTIONS + EXTENDED_AUDIT_ACTIONS

# Columns copied verbatim by the rebuild (id included: ids are preserved).
_AUDIT_LOG_COLUMNS = (
    "id", "agent_id", "action", "resource_type", "resource_id", "payload", "timestamp",
)

# Transient table used by the rebuild. Deliberately NOT "IF NOT EXISTS": a
# stale audit_log_new must never silently absorb the row copy.
_AUDIT_LOG_NEW_CREATE_SQL = f"""CREATE TABLE audit_log_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT REFERENCES agents(id),
    action TEXT NOT NULL
        CHECK (action IN ({", ".join(f"'{a}'" for a in AUDIT_ACTIONS)})),
    resource_type TEXT NOT NULL,
    resource_id TEXT,
    payload TEXT,
    timestamp TEXT DEFAULT (datetime('now'))
)"""


def _audit_log_is_current(table_sql: str) -> bool:
    """True when ``table_sql`` already carries the full action allowlist."""
    return all(f"'{action}'" in table_sql for action in AUDIT_ACTIONS)


async def _restore_audit_log_sequence(db) -> None:
    """Keep ``audit_log``'s AUTOINCREMENT counter from regressing.

    The rebuild copies ids explicitly, so SQLite normally keeps the
    sequence at max(id); this is the explicit belt-and-braces guarantee
    required by the spec (a new row must never reuse a historical id).
    """
    cursor = await db.execute("SELECT MAX(id) AS max_id FROM audit_log")
    row = await cursor.fetchone()
    max_id = row["max_id"] if row is not None else None
    if max_id is None:
        return
    cursor = await db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'audit_log'")
    seq_row = await cursor.fetchone()
    if seq_row is None:
        await db.execute(
            "INSERT INTO sqlite_sequence(name, seq) VALUES ('audit_log', ?)", (max_id,)
        )
    elif (seq_row["seq"] or 0) < max_id:
        await db.execute(
            "UPDATE sqlite_sequence SET seq = ? WHERE name = 'audit_log'", (max_id,)
        )


async def _migrate_audit_log_actions(db) -> None:
    """Rebuild ``audit_log`` when its action CHECK is not the full allowlist.

    Idempotent detection: the stored ``sqlite_master.sql`` of the table is
    inspected. If every action of ``AUDIT_ACTIONS`` is already present the
    constraint is current and NOTHING is done. Otherwise the table is
    rebuilt (covers both the legacy 5-action CHECK and a legacy table with
    no CHECK at all → uniform state).

    ``PRAGMA foreign_keys=OFF`` is mandatory and MUST be issued outside a
    transaction: ``audit_log.agent_id`` references ``agents(id)`` and the
    historical rows are copied verbatim, so a row pointing at an agent id
    that no longer exists would make ``INSERT ... SELECT`` fail with FK
    enforcement on. Rows are never rewritten or dropped, and no other
    table (``facts`` included) is touched.
    """
    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='audit_log'"
    )
    row = await cursor.fetchone()
    table_sql = ((row[0] if row is not None else "") or "")
    if not table_sql:
        # Table absent (fresh schema not created yet): nothing to migrate.
        return
    if _audit_log_is_current(table_sql):
        return

    # Indexes are detected before the rebuild and recreated identically.
    cursor = await db.execute(
        """SELECT sql FROM sqlite_master
           WHERE type='index' AND tbl_name='audit_log' AND sql IS NOT NULL"""
    )
    index_sqls = [r[0] for r in await cursor.fetchall()]

    columns = ", ".join(_AUDIT_LOG_COLUMNS)
    await db.execute("PRAGMA foreign_keys=OFF")
    try:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(_AUDIT_LOG_NEW_CREATE_SQL)
        await db.execute(
            f"""INSERT INTO audit_log_new ({columns})
                SELECT {columns} FROM audit_log"""
        )
        await db.execute("DROP TABLE audit_log")
        await db.execute("ALTER TABLE audit_log_new RENAME TO audit_log")
        for index_sql in index_sqls:
            await db.execute(index_sql)
        await _restore_audit_log_sequence(db)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.execute("PRAGMA foreign_keys=ON")


@asynccontextmanager
async def get_db() -> aiosqlite.Connection:
    """Obté una connexió a SQLite dins d'un context manager asíncron."""
    db_path = Path(settings.DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA busy_timeout=5000")
    await conn.execute("PRAGMA cache_size=-4000")
    try:
        yield conn
    finally:
        await conn.close()


async def _migrate_db() -> None:
    """Aplica migracions idempotents; qualsevol error inesperat es propaga."""
    async with get_db() as db:
        cursor = await db.execute("PRAGMA table_info(facts)")
        fact_columns = {row["name"] for row in await cursor.fetchall()}

        if "category" not in fact_columns:
            await db.execute(
                "ALTER TABLE facts ADD COLUMN category TEXT NOT NULL DEFAULT 'events'"
            )
        if "ttl_days" not in fact_columns:
            await db.execute("ALTER TABLE facts ADD COLUMN ttl_days INTEGER DEFAULT NULL")
        if "expires_at" not in fact_columns:
            await db.execute("ALTER TABLE facts ADD COLUMN expires_at TEXT DEFAULT NULL")

        await db.execute("CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category)")

        # API-key lookup migration. Existing agents remain NULL until they
        # authenticate once with their legacy key or are rotated/recreated.
        cursor = await db.execute("PRAGMA table_info(agents)")
        agent_columns = {row["name"] for row in await cursor.fetchall()}
        if "api_key_fingerprint" not in agent_columns:
            await db.execute("ALTER TABLE agents ADD COLUMN api_key_fingerprint TEXT")
        await db.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_api_key_fingerprint
               ON agents(api_key_fingerprint)
               WHERE api_key_fingerprint IS NOT NULL"""
        )

        # D2-B: current-state telemetry columns (additive, idempotent).
        # These are explicit CURRENT-STATE fields, not historical
        # memory. They overwrite on each heartbeat. Do NOT use them
        # as authoritative sources for past results — see directives.
        agent_telemetry_cols = {
            "work_state":               "ALTER TABLE agents ADD COLUMN work_state TEXT DEFAULT 'UNKNOWN'",
            "current_task_id":          "ALTER TABLE agents ADD COLUMN current_task_id TEXT",
            "current_project":          "ALTER TABLE agents ADD COLUMN current_project TEXT",
            "current_blocker":          "ALTER TABLE agents ADD COLUMN current_blocker TEXT",
            "current_blocker_reported": "ALTER TABLE agents ADD COLUMN current_blocker_reported INTEGER NOT NULL DEFAULT 0",
        }
        for col, sql in agent_telemetry_cols.items():
            if col not in agent_columns:
                await db.execute(sql)

        # Webhook security/delivery columns are migrated before the app starts,
        # avoiding concurrent first-request ALTER TABLE races. Legacy helper
        # migration in webhooks.py remains as a defensive compatibility layer.
        cursor = await db.execute("PRAGMA table_info(webhooks)")
        webhook_columns = {row["name"] for row in await cursor.fetchall()}
        webhook_migrations = {
            "secret": "ALTER TABLE webhooks ADD COLUMN secret TEXT",
            "last_attempted_at": "ALTER TABLE webhooks ADD COLUMN last_attempted_at TEXT",
            "last_status": "ALTER TABLE webhooks ADD COLUMN last_status INTEGER",
            "last_error": "ALTER TABLE webhooks ADD COLUMN last_error TEXT",
        }
        for name, sql in webhook_migrations.items():
            if name not in webhook_columns:
                await db.execute(sql)

        await db.executescript("""
            DROP TRIGGER IF EXISTS facts_ai;
            DROP TRIGGER IF EXISTS facts_ad;
            DROP TRIGGER IF EXISTS facts_au;

            CREATE TRIGGER facts_ai AFTER INSERT ON facts BEGIN
                INSERT INTO facts_fts(fact_id, content, scope)
                VALUES (new.id, new.content, new.scope);
            END;

            CREATE TRIGGER facts_ad AFTER DELETE ON facts BEGIN
                DELETE FROM facts_fts WHERE fact_id = old.id;
            END;

            CREATE TRIGGER facts_au AFTER UPDATE ON facts
            WHEN old.content != new.content BEGIN
                DELETE FROM facts_fts WHERE fact_id = old.id;
                INSERT INTO facts_fts(fact_id, content, scope)
                VALUES (new.id, new.content, new.scope);
            END;
        """)

        # ── facts_fts schema migration (legacy FTS without `scope`) ───────
        # Older databases created facts_fts as (fact_id, content) without the
        # `scope` column that the current triggers/queries expect. Recreating
        # the FTS with scope is the only way to add a column to a virtual
        # FTS5 table, and must happen before the triggers are recreated.
        cursor = await db.execute("PRAGMA table_info(facts_fts)")
        fts_cols = {row["name"] for row in await cursor.fetchall()}
        if "scope" not in fts_cols:
            await db.executescript(
                """
                DROP TRIGGER IF EXISTS facts_ai;
                DROP TRIGGER IF EXISTS facts_ad;
                DROP TRIGGER IF EXISTS facts_au;
                DROP TABLE IF EXISTS facts_fts;
                """
            )
            await db.execute(
                """
                CREATE VIRTUAL TABLE facts_fts USING fts5(
                    fact_id UNINDEXED,
                    content,
                    scope UNINDEXED,
                    tokenize="unicode61 categories 'L* N*'"
                );
                """
            )
            cursor = await db.execute(
                "SELECT id, content, scope FROM facts WHERE deleted_at IS NULL"
            )
            for row in await cursor.fetchall():
                await db.execute(
                    "INSERT INTO facts_fts(fact_id, content, scope) VALUES (?, ?, ?)",
                    (row["id"], row["content"], row["scope"] or ""),
                )
            await db.execute("PRAGMA quick_check")
            # Recreate FTS sync triggers (dropped above) against the new schema
            await db.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
                    INSERT INTO facts_fts(fact_id, content, scope)
                    VALUES (new.id, new.content, new.scope);
                END;

                CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
                    DELETE FROM facts_fts WHERE fact_id = old.id;
                END;

                CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts
                WHEN old.content != new.content BEGIN
                    DELETE FROM facts_fts WHERE fact_id = old.id;
                    INSERT INTO facts_fts(fact_id, content, scope)
                    VALUES (new.id, new.content, new.scope);
                END;
                """
            )
            await db.commit()

        cursor = await db.execute(
            "SELECT COUNT(*) AS total FROM facts WHERE deleted_at IS NULL"
        )
        active_fact_count = (await cursor.fetchone())["total"]
        cursor = await db.execute("SELECT COUNT(*) AS total FROM facts_fts")
        fts_fact_count = (await cursor.fetchone())["total"]

        if active_fact_count != fts_fact_count:
            await db.execute("DELETE FROM facts_fts")
            await db.execute("""
                INSERT INTO facts_fts(fact_id, content, scope)
                SELECT id, content, scope
                FROM facts
                WHERE deleted_at IS NULL
            """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS entities (
                id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
                name TEXT NOT NULL,
                type TEXT DEFAULT '',
                aliases TEXT DEFAULT '[]',
                description TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                deleted_at TEXT
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type)")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS triples (
                id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
                subject_id TEXT NOT NULL REFERENCES entities(id),
                predicate TEXT NOT NULL,
                object_id TEXT NOT NULL REFERENCES entities(id),
                confidence REAL DEFAULT 1.0,
                source_agent_id TEXT,
                metadata TEXT DEFAULT '{}',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                expires_at TEXT,
                deleted_at TEXT
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_triples_subject ON triples(subject_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_triples_object ON triples(object_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_triples_predicate ON triples(predicate)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_triples_deleted ON triples(deleted_at)")

        await _migrate_documents(db)

        await db.commit()


async def _migrate_documents(db) -> None:
    """Idempotent migration for the Markdown document library (L0-L7).

    Purely additive: these tables live next to (never inside) the `facts`
    memory schema. Documents are stored as their own first-class records and
    are NOT written into `facts`, `facts_fts` or `chunks`. Facts semantics,
    Recall v2, the Fact VectorIndex and notion_cache are untouched.

    L0 creates the base schema: ``documents``, ``document_versions``,
    ``document_chunks``, the ``documents_fts`` FTS5 mirror and a dedicated
    ``document_vector_index_state`` generation counter with its own sync
    triggers. Later phases build on top without further ALTERs to facts.
    """
    await db.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
            title TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'shared',
            category TEXT DEFAULT '',
            tags TEXT DEFAULT '[]',
            description TEXT DEFAULT '',
            metadata TEXT DEFAULT '{}',
            current_version INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            deleted_at TEXT
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_documents_scope ON documents(scope)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_documents_title ON documents(title)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_documents_deleted ON documents(deleted_at)")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS document_versions (
            id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
            document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            version INTEGER NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            change_note TEXT DEFAULT '',
            author_agent_id TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(document_id, version)
        )
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_document_versions_doc ON document_versions(document_id)"
    )

    await db.execute("""
        CREATE TABLE IF NOT EXISTS document_chunks (
            id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
            version_id TEXT NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
            document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL DEFAULT 0,
            section TEXT DEFAULT '',
            heading_path TEXT DEFAULT '',
            chunk_text TEXT NOT NULL,
            line_start INTEGER NOT NULL DEFAULT 0,
            line_end INTEGER NOT NULL DEFAULT 0,
            chunk_sha TEXT DEFAULT '',
            embedding_state TEXT NOT NULL DEFAULT 'pending',
            embedding_blob BLOB,
            embedding_model TEXT,
            embedding_dim INTEGER,
            embedding_attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_document_chunks_version ON document_chunks(version_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_document_chunks_document ON document_chunks(document_id)"
    )

    # ── Provenance + embedding-state columns (L2-CERT/L3, additive) ──────
    # Existing databases created document_chunks with only (section, chunk_text,
    # embedding_blob). We add heading_path/line ranges (L2 provenance), the
    # chunk content SHA + embedding state columns (L3) idempotently so older
    # DBs upgrade in place without ever touching the facts tables.
    _chunks_cols = await db.execute("PRAGMA table_info(document_chunks)")
    _chunk_col_names = {row["name"] for row in await _chunks_cols.fetchall()}
    _chunk_additives = {
        "heading_path": "ALTER TABLE document_chunks ADD COLUMN heading_path TEXT DEFAULT ''",
        "line_start": "ALTER TABLE document_chunks ADD COLUMN line_start INTEGER NOT NULL DEFAULT 0",
        "line_end": "ALTER TABLE document_chunks ADD COLUMN line_end INTEGER NOT NULL DEFAULT 0",
        "chunk_sha": "ALTER TABLE document_chunks ADD COLUMN chunk_sha TEXT DEFAULT ''",
        "embedding_state": "ALTER TABLE document_chunks ADD COLUMN embedding_state TEXT NOT NULL DEFAULT 'pending'",
        "embedding_model": "ALTER TABLE document_chunks ADD COLUMN embedding_model TEXT",
        "embedding_dim": "ALTER TABLE document_chunks ADD COLUMN embedding_dim INTEGER",
        "embedding_attempts": "ALTER TABLE document_chunks ADD COLUMN embedding_attempts INTEGER NOT NULL DEFAULT 0",
    }
    for _name, _sql in _chunk_additives.items():
        if _name not in _chunk_col_names:
            await db.execute(_sql)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_document_chunks_state ON document_chunks(embedding_state)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_document_chunks_sha ON document_chunks(chunk_sha)"
    )

    # FTS5 mirror over the latest chunked content of each version. The content
    # column is the only indexed field; id/scope fields are UNINDEXED markers.
    await db.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
            chunk_id UNINDEXED,
            document_id UNINDEXED,
            version UNINDEXED,
            scope UNINDEXED,
            content,
            tokenize="unicode61 categories 'L* N*'"
        )
    """)

    # Generation counter for the derived DocumentVectorIndex (L3/L4). This is a
    # dedicated counter, separate from vector_index_state.generation which
    # tracks the *facts* TurboVec index. Its only triggers are on
    # document_chunks.embedding_blob shape changes, never on facts tables.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS document_vector_index_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            generation INTEGER NOT NULL DEFAULT 0
        )
    """)
    await db.execute(
        "INSERT OR IGNORE INTO document_vector_index_state(singleton, generation) VALUES (1, 0)"
    )
    await db.executescript("""
        DROP TRIGGER IF EXISTS docvec_chunks_ai;
        DROP TRIGGER IF EXISTS docvec_chunks_au;
        DROP TRIGGER IF EXISTS docvec_chunks_ad;
        DROP TRIGGER IF EXISTS docvec_docs_au;
        DROP TRIGGER IF EXISTS docvec_docs_ad;

        CREATE TRIGGER docvec_chunks_ai AFTER INSERT ON document_chunks BEGIN
            UPDATE document_vector_index_state SET generation = generation + 1 WHERE singleton = 1;
        END;
        CREATE TRIGGER docvec_chunks_au
        AFTER UPDATE OF embedding_blob, document_id, chunk_text, version_id ON document_chunks BEGIN
            UPDATE document_vector_index_state SET generation = generation + 1 WHERE singleton = 1;
        END;
        CREATE TRIGGER docvec_chunks_ad AFTER DELETE ON document_chunks BEGIN
            UPDATE document_vector_index_state SET generation = generation + 1 WHERE singleton = 1;
        END;
        CREATE TRIGGER docvec_docs_au
        AFTER UPDATE OF scope, deleted_at ON documents BEGIN
            UPDATE document_vector_index_state SET generation = generation + 1 WHERE singleton = 1;
        END;
        CREATE TRIGGER docvec_docs_ad AFTER DELETE ON documents BEGIN
            UPDATE document_vector_index_state SET generation = generation + 1 WHERE singleton = 1;
        END;
    """)

    # L5: provenance edges between documents and derived facts.
    # Metadata-only provenance; it never writes or alters facts themselves.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS document_fact_provenance (
            id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
            document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            fact_id TEXT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
            document_version INTEGER,
            reason TEXT DEFAULT '',
            confidence REAL DEFAULT 1.0,
            created_at TEXT DEFAULT (datetime('now')),
            created_by_agent_id TEXT,
            UNIQUE(document_id, fact_id)
        )
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_doc_prov_document ON document_fact_provenance(document_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_doc_prov_fact ON document_fact_provenance(fact_id)"
    )

    # L3: embedding-reuse cache for document chunks, keyed on
    # (chunk_sha, embedding_model, embedding_dim). A model/dim change presents a
    # different key so incompatible vectors are never silently reused. Purely
    # additive, independent of the facts schema.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS document_embedding_cache (
            sha TEXT NOT NULL,
            model TEXT NOT NULL,
            dim INTEGER NOT NULL,
            embedding_blob BLOB NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (sha, model, dim)
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_doc_emb_cache_sha ON document_embedding_cache(sha)")


async def init_db() -> None:
    """Inicialitza l'esquema de la base de dades executant init_db.sql."""
    sql_path = Path(__file__).resolve().parent.parent / "scripts" / "init_db.sql"
    if not sql_path.exists():
        raise FileNotFoundError(f"No es troba l'script SQL: {sql_path}")

    sql = sql_path.read_text(encoding="utf-8")
    async with get_db() as db:
        await db.executescript(sql)
        await db.commit()
        # D3-C corrective: widen the audit_log action CHECK in place for
        # databases created before it (same connection, after the SQL and
        # before/without interfering with _migrate_db()).
        await _migrate_audit_log_actions(db)

    await _migrate_db()