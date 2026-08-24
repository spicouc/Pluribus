# Pluribus — Document Library (L0–L7) changelog

Scope-safe, durable, versioned Markdown document library that lives **next to**
(never inside) the `facts` memory schema. Documents are first-class records and
are **not** written into `facts`, `facts_fts` or `chunks`. The Fact
VectorIndex, Recall v2, facts FTS and notion_cache are untouched.

Per-phase commits, tests, regression, quick_check, migration test and rollback
notes are recorded below at the moment each phase is merged.

---

## ROLLBACK (general)

Roll back any phase by reverting the corresponding commit:

```
git -C /opt/pluribus revert <L_commit_sha> --no-edit
# or hard reset to a known-good SHA, then restart the service:
systemctl stop pluribus && systemctl start pluribus
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8790/health   # expect 200
```

Because every phase is a purely **additive schema/code change**, reverting one
commit removes the new tables/endpoints/code and leaves the running `facts`
memory intact. A `pluribus.db.bak.*` snapshot is taken before the first
migration (see below); restore with:

```
systemctl stop pluribus
cp /opt/pluribus/data/pluribus.db.bak.<timestamp> /opt/pluribus/data/pluribus.db
systemctl start pluribus
```

DB snapshot before L0: `pluribus.db.bak.20260824_195853`

---

## L0 — Schema / models (migration, additive)

**Tables added** (all new, none alters `facts`/`facts_fts`/`chunks`/notion_cache):
- `documents` (master record: title, scope, category, tags, description, metadata, current_version, soft-delete)
- `document_versions` (immutable per-version snapshot: content, content_hash, change_note, author, timestamps)
- `document_chunks` (markdown-aware chunks per version; `embedding_blob` nullable)
- `documents_fts` (FTS5 mirror over chunk content; separate from `facts_fts`)
- `document_vector_index_state` (dedicated generation counter + sync triggers `docvec_*`, decoupled from `vector_index_state`)
- `document_fact_provenance` (L5: provenance edges doc→fact, metadata only)

**Changes**: `pluribus/db.py` gained `_migrate_documents()` called from
`_migrate_db()`. Fully idempotent; no ALTER on facts tables.

**Tests**: `tests/test_document_schema.py` (4 tests). Regression:
`178 passed`. Migration test on a copy of the production DB: clean (`quick_check
ok`, `active facts=14 facts_fts=14`, facts `vector_index_state.generation`
unchanged, all new tables present). Service restarted; health `200 ok`,
`embedding_ready true`.

**Rollback L0**: `git revert <sha>`; or restore the DB snapshot above. Because
the migration is idempotent/additive, simply reverting the code is sufficient;
the extra empty tables are harmless if left behind.
