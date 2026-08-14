-- Esquema complet de la base de dades Brain v2
-- Includes Phase 1 (fact_relations) + Phase 2 (agent enhancements)

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    api_key_hash TEXT NOT NULL,
    api_key_fingerprint TEXT,
    permissions TEXT DEFAULT '{}',
    allowed_scopes TEXT DEFAULT '["shared"]',
    capabilities TEXT DEFAULT '{}',
    metadata TEXT DEFAULT '{}',
    last_active_at TEXT,
    last_ip TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_api_key_fingerprint
    ON agents(api_key_fingerprint)
    WHERE api_key_fingerprint IS NOT NULL;

CREATE TABLE IF NOT EXISTS facts (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    scope TEXT NOT NULL DEFAULT 'shared',
    agent_id TEXT REFERENCES agents(id),
    category TEXT NOT NULL DEFAULT 'events',
    key TEXT,
    content TEXT NOT NULL,
    metadata TEXT DEFAULT '{}',
    version INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_facts_scope ON facts(scope);
CREATE INDEX IF NOT EXISTS idx_facts_agent ON facts(agent_id);
CREATE INDEX IF NOT EXISTS idx_facts_key ON facts(key);
CREATE INDEX IF NOT EXISTS idx_facts_deleted ON facts(deleted_at);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    fact_id UNINDEXED,
    content,
    scope UNINDEXED,
    tokenize='unicode61 categories ''L* N*'''
);

CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(fact_id, content, scope) VALUES (new.id, new.content, new.scope);
END;

CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    DELETE FROM facts_fts WHERE fact_id = old.id;
END;

CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts WHEN old.content != new.content BEGIN
    DELETE FROM facts_fts WHERE fact_id = old.id;
    INSERT INTO facts_fts(fact_id, content, scope) VALUES (new.id, new.content, new.scope);
END;

CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    fact_id TEXT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    chunk_text TEXT NOT NULL,
    embedding_blob BLOB,
    metadata TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_chunks_fact_id ON chunks(fact_id);

CREATE TABLE IF NOT EXISTS embedding_cache (
    hash TEXT PRIMARY KEY,
    embedding_blob BLOB,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT REFERENCES agents(id),
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT,
    payload TEXT,
    timestamp TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_agent ON audit_log(agent_id);

CREATE TABLE IF NOT EXISTS consolidated (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    session_id TEXT,
    agent_id TEXT,
    summary TEXT NOT NULL,
    source_facts TEXT,
    model TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_consolidated_session ON consolidated(session_id);
CREATE INDEX IF NOT EXISTS idx_consolidated_agent ON consolidated(agent_id);

CREATE TABLE IF NOT EXISTS notion_cache (
    id TEXT PRIMARY KEY,
    title TEXT,
    markdown TEXT,
    url TEXT,
    embedding_blob BLOB,
    last_synced TEXT,
    parent_db TEXT
);

CREATE TABLE IF NOT EXISTS notion_links (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    fact_id TEXT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    notion_page_id TEXT NOT NULL REFERENCES notion_cache(id),
    relevance REAL DEFAULT 0.0,
    match_type TEXT DEFAULT 'auto',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_notion_links_fact ON notion_links(fact_id);
CREATE INDEX IF NOT EXISTS idx_notion_links_page ON notion_links(notion_page_id);

CREATE TABLE IF NOT EXISTS notion_sync_log (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    page_id TEXT,
    action TEXT,
    error TEXT,
    synced_at TEXT DEFAULT (datetime('now'))
);

-- Phase 1: Knowledge graph
CREATE TABLE IF NOT EXISTS fact_relations (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    source_fact_id TEXT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    target_fact_id TEXT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL DEFAULT 'related_to',
    relation_strength REAL DEFAULT 0.5,
    discovered_by TEXT DEFAULT 'worker',
    metadata TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_fact_relations_source ON fact_relations(source_fact_id);
CREATE INDEX IF NOT EXISTS idx_fact_relations_target ON fact_relations(target_fact_id);
CREATE INDEX IF NOT EXISTS idx_fact_relations_type ON fact_relations(relation_type);

-- Webhooks per notificacions de fets nous
CREATE TABLE IF NOT EXISTS webhooks (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    url TEXT NOT NULL,
    scope TEXT,
    category TEXT,
    events TEXT NOT NULL DEFAULT '["fact.created"]',
    created_at TEXT DEFAULT (datetime('now')),
    last_triggered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_webhooks_scope ON webhooks(scope);
CREATE INDEX IF NOT EXISTS idx_webhooks_category ON webhooks(category);
