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

---
## L1 — Markdown CRUD + versioning (router)

**Added** `pluribus/documents.py` (APIRouter prefix `/v1/documents`), registered in
`pluribus/main.py`. Endpoints:
- POST /v1/documents (create, v1 snapshot + sha256 content_hash, 201)
- GET /v1/documents/{id} (get by id, 404 if deleted/missing)
- GET /v1/documents/lookup?title=&scope= (slug-equivalent; no slug column in L0 — decision documented for L2)
- GET /v1/documents (list/search by scope/category/tag/q, paginated)
- PUT /v1/documents/{id} (mints new document_versions row + bumps current_version ONLY when content_hash changes; metadata-only edits don't mint empty versions)
- DELETE /v1/documents/{id} (soft-delete, sets deleted_at)
- GET /v1/documents/{id}/versions + .../versions/{version} (history + snapshot)

Auth: defense-in-depth modeled on recall.py (X-API-Key; 401/403; allowed_scopes check).

**Tests**: `tests/test_documents_crud.py` (21 passed). Regression: `199 passed / 0 failed / 22 subtests`.
**quick_check**: service restarted, health 200, embedding_ready true.
**Migration**: none new (uses L0 tables). No ALTER on facts/FTS.
**Rollback L1**: `git revert <sha>`; the router is additive, reverting removes the endpoints.

(Test-only fix note: helper `create_doc` in test_documents_crud.py was patched to accept `scope`/`category`/`tags` so the scope-filter test is correct. Runtime untouched.)

---
## L2 — Markdown-aware chunks + FTS search

**Added** `pluribus/document_chunks.py` (chunker + FTS sync) and wired into
`pluribus/documents.py` (chunk+index generation on create/update/delete). New
endpoints:
- GET /v1/documents/search?q=&scope=&category=&tag=&limit=&offset= — FTS over document chunks
- GET /v1/documents/{id}/chunks?version= — reverse chunk->document lookup

**Chunking**: `chunk_markdown()` splits on ATX headings; each heading+body = 1 chunk
(section=heading), preamble = empty-section chunk; fallback to paragraph chunks when
no heading; max_len=6000 guard; degenerate text still yields >=1 chunk.
`documents_fts` (FTS5) is maintained explicitly (latest version only) via
`rebuild_document_index()` on create/content-update/soft-delete.

**FTS5 note (important):** `bm25()`/`snippet()` are only valid at top row-level over
the FTS table — never inside MIN() or a derived table. The search paginates bare
document_id rows (GROUP BY) and ranks per-chunk with bm25 in a separate row-level
query; the best score per doc is computed in Python from the chunk scores.

**Tests**: `tests/test_documents_chunks.py` (16 passed: 4 unit chunker + 12 API).
Regression: `215 passed / 0 failed / 22 subtests`. quick_check: integrity ok, service
health 200 + embedding_ready true after restart. Migration: none (L0 tables only).
**Rollback L2**: `git revert <sha>`; reverting removes the endpoints and the chunker;
leftover document_chunks/documents_fts rows are harmless.
