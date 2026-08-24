"""Markdown document library CRUD + versioning (phase L1).

L1 builds a Markdown document CRUD + versioning layer **on top of** the L0
schema tables (``documents``, ``document_versions``, ...). It is purely
additive and scope-safe:

- Documents are first-class records stored in ``documents``, never as facts.
- Versioned content snapshots live in ``document_versions``; a new version is
  an immutable row whose ``content_hash`` makes it tamper-evident.
- No writes to ``facts``, ``facts_fts``, ``chunks``, the Fact VectorIndex,
  Recall v2 or ``notion_cache``. No automatic fact extraction.
- Authorization mirrors the rest of the service: the ``X-API-Key`` middleware
  already authenticates the agent into ``request.state.agent``; each handler
  re-checks permission + scope in a defense-in-depth style (like recall.py).

Later phases (L2+) add chunking/search/vector provenance on top; L1 does NOT
populate ``document_chunks``/``documents_fts`` nor generate embeddings.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from pluribus.audit import log_audit
from pluribus.db import get_db
from pluribus.validation import (
    validate_category,
    validate_content,
    validate_metadata,
    validate_scope,
)

router = APIRouter(prefix="/v1/documents", tags=["documents"])


# ── Response/request models ────────────────────────────────────────────
class DocumentVersionSummary(BaseModel):
    id: str
    version: int
    title: str
    change_note: str = ""
    author_agent_id: Optional[str] = None
    content_hash: str
    created_at: str


class DocumentVersion(BaseModel):
    id: str
    document_id: str
    version: int
    title: str
    content: str
    change_note: str = ""
    author_agent_id: Optional[str] = None
    content_hash: str
    created_at: str


class DocumentRead(BaseModel):
    id: str
    title: str
    scope: str
    category: str = ""
    tags: list[str] = Field(default_factory=list)
    description: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    current_version: int = 1
    created_at: str
    updated_at: str
    deleted_at: Optional[str] = None
    content: Optional[str] = None


class DocumentList(BaseModel):
    items: list[DocumentRead]
    total: int
    scope: str
    filters: dict[str, Any] = Field(default_factory=dict)


class DocumentVersionsResult(BaseModel):
    document_id: str
    total: int
    versions: list[DocumentVersionSummary]


class DocumentCreateRequest(BaseModel):
    title: str
    content: str
    scope: str = "shared"
    category: str = ""
    tags: list[str] = Field(default_factory=list)
    description: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    _title = field_validator("title")(lambda value: _validate_title(value))
    _content = field_validator("content")(validate_content)
    _scope = field_validator("scope")(validate_scope)

    @field_validator("category")
    @classmethod
    def _validate_optional_category(cls, value: str) -> str:
        return validate_category(value)

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, value: list[str]) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("tags ha de ser una llista")
        if len(value) > 32:
            raise ValueError("tags no pot tenir més de 32 elements")
        tags: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("cada tag ha de ser text")
            tag = item.strip()
            if not 1 <= len(tag) <= 64:
                raise ValueError("cada tag ha de tenir entre 1 i 64 caràcters")
            if tag not in tags:
                tags.append(tag)
        return tags

    _metadata = field_validator("metadata")(validate_metadata)


class DocumentUpdateRequest(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[list[str]] = None
    description: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    change_note: str = ""

    @field_validator("title")
    @classmethod
    def _validate_optional_title(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else _validate_title(value)

    @field_validator("content")
    @classmethod
    def _validate_optional_content(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else validate_content(value)

    @field_validator("category")
    @classmethod
    def _validate_optional_category(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else validate_category(value)

    @field_validator("tags")
    @classmethod
    def _validate_optional_tags(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is None:
            return None
        if not isinstance(value, list):
            raise ValueError("tags ha de ser una llista")
        if len(value) > 32:
            raise ValueError("tags no pot tenir més de 32 elements")
        tags: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("cada tag ha de ser text")
            tag = item.strip()
            if not 1 <= len(tag) <= 64:
                raise ValueError("cada tag ha de tenir entre 1 i 64 caràcters")
            if tag not in tags:
                tags.append(tag)
        return tags

    @field_validator("metadata")
    @classmethod
    def _validate_optional_metadata(
        cls, value: Optional[dict[str, Any]]
    ) -> Optional[dict[str, Any]]:
        return None if value is None else validate_metadata(value)


class DeleteResult(BaseModel):
    document_id: str
    message: str = "Document eliminat correctament (soft delete)"


def _validate_title(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("title ha de ser text")
    value = value.strip()
    if not 1 <= len(value) <= 512:
        raise ValueError("title ha de tenir entre 1 i 512 caràcters")
    return value


# ── Authorization helpers (defense-in-depth, mirrors recall.py) ────────
def _permissions(agent: dict[str, Any]) -> dict[str, Any]:
    value = agent.get("permissions", {}) or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            value = {}
    return value if isinstance(value, dict) else {}


def _allowed_scopes(agent: dict[str, Any]) -> list[str]:
    value = agent.get("allowed_scopes", []) or []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            value = []
    if not isinstance(value, list):
        return []
    scopes: list[str] = []
    for item in value:
        try:
            normalized = validate_scope(item)
        except (TypeError, ValueError):
            continue
        if normalized not in scopes:
            scopes.append(normalized)
    return scopes


def _require(agent: dict[str, Any], permission: str, scope: str) -> None:
    """Enforce authentication + permission + scope inside a document handler."""
    if not agent or not agent.get("id"):
        raise HTTPException(status_code=401, detail="Autenticació requerida")
    perms = _permissions(agent)
    is_admin = bool(perms.get("admin", False))
    if is_admin:
        return
    if not perms.get(permission, False):
        raise HTTPException(
            status_code=403,
            detail=f"L'agent no té permís '{permission}'",
        )
    if scope not in _allowed_scopes(agent):
        raise HTTPException(
            status_code=403,
            detail=f"Àmbit '{scope}' no permès per a aquest agent",
        )


async def _load_document(
    db, document_id: str, *, require_scope: bool = True
) -> Optional[dict[str, Any]]:
    cursor = await db.execute(
        """SELECT id, title, scope, category, tags, description, metadata,
                  current_version, created_at, updated_at, deleted_at
           FROM documents WHERE id = ?""",
        (document_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


def _doc_read(doc: dict[str, Any], content: Optional[str] = None) -> DocumentRead:
    try:
        tags = json.loads(doc["tags"]) if isinstance(doc["tags"], str) else doc["tags"]
        if not isinstance(tags, list):
            tags = []
    except (json.JSONDecodeError, TypeError):
        tags = []
    try:
        metadata = json.loads(doc["metadata"]) if isinstance(doc["metadata"], str) else doc["metadata"]
        if not isinstance(metadata, dict):
            metadata = {}
    except (json.JSONDecodeError, TypeError):
        metadata = {}
    return DocumentRead(
        id=doc["id"],
        title=doc["title"],
        scope=doc["scope"],
        category=doc.get("category") or "",
        tags=tags,
        description=doc.get("description") or "",
        metadata=metadata,
        current_version=doc["current_version"],
        created_at=doc["created_at"],
        updated_at=doc["updated_at"],
        deleted_at=doc.get("deleted_at"),
        content=content,
    )


async def _latest_version(db, document_id: str) -> Optional[dict[str, Any]]:
    cursor = await db.execute(
        """SELECT id, document_id, version, title, content, content_hash,
                  change_note, author_agent_id, created_at
           FROM document_versions WHERE document_id = ?
           ORDER BY version DESC LIMIT 1""",
        (document_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ── CRUD endpoints ─────────────────────────────────────────────────────
@router.post("", status_code=201, response_model=DocumentRead)
async def create_document(request: Request, body: DocumentCreateRequest) -> DocumentRead:
    agent: dict[str, Any] = getattr(request.state, "agent", None) or {}
    _require(agent, "write", body.scope)

    content_hash = _content_hash(body.content)
    async with get_db() as db:
        cursor = await db.execute(
            """INSERT INTO documents
               (title, scope, category, tags, description, metadata, current_version)
               VALUES (?, ?, ?, ?, ?, ?, 1)""",
            (
                body.title,
                body.scope,
                body.category,
                json.dumps(body.tags, ensure_ascii=False),
                body.description,
                json.dumps(body.metadata, ensure_ascii=False),
            ),
        )
        rowid = cursor.lastrowid
        await db.commit()
        cursor2 = await db.execute(
            "SELECT id FROM documents WHERE rowid = ?", (rowid,)
        )
        row = await cursor2.fetchone()
        document_id = row["id"] if row else ""

        await db.execute(
            """INSERT INTO document_versions
               (document_id, version, title, content, content_hash,
                change_note, author_agent_id)
               VALUES (?, 1, ?, ?, ?, '', ?)""",
            (document_id, body.title, body.content, content_hash, agent["id"]),
        )
        await log_audit(
            db,
            agent["id"],
            "CREATE",
            "document",
            resource_id=document_id,
            payload=json.dumps(
                {"scope": body.scope, "title": body.title, "content_length": len(body.content)}
            ),
        )
        await db.commit()

        doc = await _load_document(db, document_id)
    return _doc_read(doc, content=body.content)


@router.get("/lookup", response_model=DocumentRead)
async def get_document_by_title(
    request: Request,
    title: str = Query(..., min_length=1, max_length=512),
    scope: str = Query("shared"),
) -> DocumentRead:
    """Lookup a document by (title [+ scope]): the L1 slug-equivalent.

    The L0 ``documents`` table has no ``slug`` column, so L1 resolves a
    document by an exact (case-insensitive) title within a scope. A dedicated
    slug field can be introduced in a later phase if the operator needs
    stable URL-style identifiers.
    """
    agent: dict[str, Any] = getattr(request.state, "agent", None) or {}
    normalized_scope = validate_scope(scope)
    _require(agent, "read", normalized_scope)

    async with get_db() as db:
        cursor = await db.execute(
            """SELECT id, title, scope, category, tags, description, metadata,
                      current_version, created_at, updated_at, deleted_at
               FROM documents
               WHERE deleted_at IS NULL
                 AND lower(title) = lower(?)
                 AND scope = ?
               ORDER BY updated_at DESC LIMIT 1""",
            (title.strip(), normalized_scope),
        )
        row = await cursor.fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"No s'ha trobat cap document '{title}' a l'àmbit '{normalized_scope}'",
            )
        doc = dict(row)
        latest = await _latest_version(db, doc["id"])
    return _doc_read(doc, content=latest["content"] if latest else None)


@router.get("", response_model=DocumentList)
async def list_documents(
    request: Request,
    scope: str = Query("shared"),
    category: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
    q: Optional[str] = Query(None, max_length=256),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> DocumentList:
    """List/search documents by scope, category, tag and title/description text.

    L1 searches the master record (title/description) plus scope/category/tag
    filters. Content-level FTS search over ``documents_fts`` is a later phase.
    """
    agent: dict[str, Any] = getattr(request.state, "agent", None) or {}
    normalized_scope = validate_scope(scope)
    _require(agent, "read", normalized_scope)

    where = ["deleted_at IS NULL", "scope = ?"]
    params: list[Any] = [normalized_scope]
    if category:
        normalized_category = validate_category(category)
        where.append("category = ?")
        params.append(normalized_category)
    if q and q.strip():
        where.append("(lower(title) LIKE ? OR lower(description) LIKE ?)")
        like = f"%{q.strip().lower()}%"
        params.append(like)
        params.append(like)

    where_sql = " AND ".join(where)

    async with get_db() as db:
        cursor = await db.execute(
            f"SELECT COUNT(*) AS total FROM documents WHERE {where_sql}", params
        )
        total = (await cursor.fetchone())["total"]
        cursor = await db.execute(
            f"""SELECT id, title, scope, category, tags, description, metadata,
                       current_version, created_at, updated_at, deleted_at
                FROM documents WHERE {where_sql}
                ORDER BY updated_at DESC LIMIT ? OFFSET ?""",
            [*params, limit, offset],
        )
        rows = await cursor.fetchall()

    items: list[DocumentRead] = []
    if tag and tag.strip():
        needle = tag.strip().lower()
        for row in rows:
            doc = dict(row)
            try:
                tags = json.loads(doc["tags"]) if isinstance(doc["tags"], str) else doc["tags"]
            except (json.JSONDecodeError, TypeError):
                tags = []
            if isinstance(tags, list) and any(str(t).lower() == needle for t in tags):
                items.append(_doc_read(doc))
    else:
        for row in rows:
            items.append(_doc_read(dict(row)))

    return DocumentList(
        items=items,
        total=total,
        scope=normalized_scope,
        filters={
            "category": category,
            "tag": tag,
            "q": q,
        },
    )


@router.get("/{document_id}", response_model=DocumentRead)
async def get_document(request: Request, document_id: str) -> DocumentRead:
    agent: dict[str, Any] = getattr(request.state, "agent", None) or {}
    if not agent or not agent.get("id"):
        raise HTTPException(status_code=401, detail="Autenticació requerida")

    async with get_db() as db:
        doc = await _load_document(db, document_id)
        if doc is None or doc["deleted_at"] is not None:
            raise HTTPException(status_code=404, detail="Document no trobat")
        _require(agent, "read", doc["scope"])
        latest = await _latest_version(db, document_id)
    return _doc_read(doc, content=latest["content"] if latest else None)


@router.get("/{document_id}/versions", response_model=DocumentVersionsResult)
async def list_versions(request: Request, document_id: str) -> DocumentVersionsResult:
    agent: dict[str, Any] = getattr(request.state, "agent", None) or {}
    async with get_db() as db:
        doc = await _load_document(db, document_id)
        if doc is None or doc["deleted_at"] is not None:
            raise HTTPException(status_code=404, detail="Document no trobat")
        _require(agent, "read", doc["scope"])

        cursor = await db.execute(
            """SELECT id, version, title, change_note, author_agent_id,
                      content_hash, created_at
               FROM document_versions
               WHERE document_id = ?
               ORDER BY version DESC""",
            (document_id,),
        )
        rows = await cursor.fetchall()

    versions = [
        DocumentVersionSummary(**dict(row))
        for row in rows
    ]
    return DocumentVersionsResult(
        document_id=document_id,
        total=len(versions),
        versions=versions,
    )


@router.get("/{document_id}/versions/{version}", response_model=DocumentVersion)
async def get_version(request: Request, document_id: str, version: int) -> DocumentVersion:
    agent: dict[str, Any] = getattr(request.state, "agent", None) or {}
    async with get_db() as db:
        doc = await _load_document(db, document_id)
        if doc is None or doc["deleted_at"] is not None:
            raise HTTPException(status_code=404, detail="Document no trobat")
        _require(agent, "read", doc["scope"])

        cursor = await db.execute(
            """SELECT id, document_id, version, title, content, content_hash,
                      change_note, author_agent_id, created_at
               FROM document_versions
               WHERE document_id = ? AND version = ?""",
            (document_id, version),
        )
        row = await cursor.fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"No existeix la versió {version} del document",
            )
    return DocumentVersion(**dict(row))


@router.put("/{document_id}", response_model=DocumentRead)
async def update_document(
    request: Request, document_id: str, body: DocumentUpdateRequest
) -> DocumentRead:
    """Update a document, creating a new immutable version when content changes.

    A new ``document_versions`` row is written (with the next version number and
    a fresh ``content_hash``) and ``documents.current_version`` is bumped only
    when the content actually changed. Metadata-only edits update the master
    record without minting an empty version.
    """
    agent: dict[str, Any] = getattr(request.state, "agent", None) or {}
    if not agent or not agent.get("id"):
        raise HTTPException(status_code=401, detail="Autenticació requerida")

    async with get_db() as db:
        doc = await _load_document(db, document_id)
        if doc is None or doc["deleted_at"] is not None:
            raise HTTPException(status_code=404, detail="Document no trobat")
        _require(agent, "write", doc["scope"])

        latest = await _latest_version(db, document_id)

        set_clauses = ["updated_at = datetime('now')"]
        version_params: list[Any] = []

        new_title = body.title if body.title is not None else doc["title"]
        set_clauses.append("title = ?")
        version_params.append(new_title)

        if body.category is not None:
            set_clauses.append("category = ?")
            version_params.append(body.category)
        if body.tags is not None:
            set_clauses.append("tags = ?")
            version_params.append(json.dumps(body.tags, ensure_ascii=False))
        if body.description is not None:
            set_clauses.append("description = ?")
            version_params.append(body.description)
        if body.metadata is not None:
            set_clauses.append("metadata = ?")
            version_params.append(json.dumps(body.metadata, ensure_ascii=False))

        if set_clauses:
            sql = (
                "UPDATE documents SET "
                + ", ".join(set_clauses)
                + " WHERE id = ?"
            )
            version_params.append(document_id)
            await db.execute(sql, version_params)
            await db.commit()

        # Mint a new version only when the content (or its hash) changed.
        new_content = body.content
        if new_content is not None:
            new_hash = _content_hash(new_content)
            if latest is None or latest["content_hash"] != new_hash:
                new_version = (doc["current_version"] or 1) + 1
                await db.execute(
                    """INSERT INTO document_versions
                       (document_id, version, title, content, content_hash,
                        change_note, author_agent_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        document_id,
                        new_version,
                        new_title,
                        new_content,
                        new_hash,
                        body.change_note or "",
                        agent["id"],
                    ),
                )
                await db.execute(
                    "UPDATE documents SET current_version = ?, updated_at = datetime('now') "
                    "WHERE id = ?",
                    (new_version, document_id),
                )
            else:
                # Content unchanged: still refresh the latest version title if the
                # title changed, keeping the snapshot consistent with the record.
                if new_title != latest["title"]:
                    await db.execute(
                        "UPDATE document_versions SET title = ? WHERE id = ?",
                        (new_title, latest["id"]),
                    )

        await log_audit(
            db,
            agent["id"],
            "UPDATE",
            "document",
            resource_id=document_id,
            payload=json.dumps(
                {
                    "new_version": (doc["current_version"] or 1) + 1
                    if new_content is not None and (
                        latest is None or latest["content_hash"] != _content_hash(new_content)
                    )
                    else doc["current_version"],
                    "title": new_title,
                }
            ),
        )
        await db.commit()

        refreshed = await _load_document(db, document_id)
        new_latest = await _latest_version(db, document_id)
    return _doc_read(refreshed, content=new_latest["content"] if new_latest else None)


@router.delete("/{document_id}", response_model=DeleteResult)
async def delete_document(request: Request, document_id: str) -> DeleteResult:
    """Soft-delete a document by setting ``deleted_at`` (keeps history intact)."""
    agent: dict[str, Any] = getattr(request.state, "agent", None) or {}
    if not agent or not agent.get("id"):
        raise HTTPException(status_code=401, detail="Autenticació requerida")

    async with get_db() as db:
        doc = await _load_document(db, document_id)
        if doc is None or doc["deleted_at"] is not None:
            raise HTTPException(status_code=404, detail="Document no trobat")
        _require(agent, "delete", doc["scope"])

        await db.execute(
            "UPDATE documents SET deleted_at = datetime('now'), updated_at = datetime('now') "
            "WHERE id = ?",
            (document_id,),
        )
        await log_audit(
            db,
            agent["id"],
            "DELETE",
            "document",
            resource_id=document_id,
            payload=json.dumps({"soft": True}),
        )
        await db.commit()

    return DeleteResult(document_id=document_id)
