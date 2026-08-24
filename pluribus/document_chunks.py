"""Markdown-aware chunking + FTS sync for the document library (phase L2).

L2 adds two capabilities **on top of** the L1 document CRUD/versioning layer,
still purely within the document library:

1. **Markdown-aware chunking** of a document version's content. Each logical
   block (a heading plus its body, or a blank-line paragraph as fallback) is
   stored as a row in ``document_chunks`` (one row per chunk, tagged with the
   ``version_id`` and a per-version ``chunk_index``). The ``embedding_blob``
   column is intentionally left NULL — embeddings are a later phase (L3).

2. **FTS full-text search** over chunk content through the ``documents_fts``
   FTS5 virtual table. The L0 schema created ``documents_fts`` with ``content``
   as the only indexed column and the rest as UNINDEXED markers, but created
   **no** trigger to keep it in sync with ``document_chunks``. L2 therefore
   maintains it explicitly from the application layer whenever a document's
   chunk set changes (create / content update / soft-delete).

Hard-rule compliance (matches L1): documents never become facts, ``facts`` /
``facts_fts`` / ``chunks`` / the Fact VectorIndex / Recall v2 / ``notion_cache``
are never touched, no automatic fact extraction, no embedding generation.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# An ATX heading: 1-6 '#' + whitespace + heading text, at the start of a line.
_HEADING_RE = re.compile(r"^#{1,6}[ \t]+(?P<heading>.*)$")
_BLANK_LINE_RE = re.compile(r"\n[ \t]*\n+\s*")

# FTS5 column index of the indexed ``content`` field (for snippet()).
_FTS_CONTENT_COLUMN = 4


@dataclass
class Chunk:
    """One logical chunk of a document version's Markdown content."""

    chunk_index: int
    section: str
    chunk_text: str


def chunk_markdown(content: str, *, max_len: int = 6000) -> list[Chunk]:
    """Split Markdown ``content`` into logical chunks.

    Strategy (Markdown-aware, with a plain-paragraph fallback):

    * If the content contains ATX headings (``# heading``, ``## heading``, ...),
      each heading
      starts a new chunk that carries the heading text as its ``section`` and
      accumulates the following body until the next heading. Preamble text
      before the first heading becomes a chunk with an empty ``section``.
    * If there are no headings, split on blank lines into paragraph chunks.
    * A degenerate single unbounded paragraph still produces at least one chunk.

    ``max_len`` guards against a single pathological block being stored as one
    oversized chunk: when a body segment exceeds ``max_len`` chars it is
    further split on blank lines / sentence boundaries. For L2 this is rarely
    hit but keeps stored rows bounded.
    """
    if not content:
        return []

    lines = content.split("\n")
    return _chunk_by_heading(lines, max_len) if _has_heading(lines) else _chunk_by_paragraph(content, max_len)


def _has_heading(lines: list[str]) -> bool:
    return any(_HEADING_RE.match(line) for line in lines)


def _chunk_by_heading(lines: list[str], max_len: int) -> list[Chunk]:
    """Split content into heading+body chunks. Content before the first
    heading (the preamble) becomes a chunk with an empty ``section``."""
    segments: list[tuple[str, str]] = []
    current_section = ""
    current_lines: list[str] = []
    seen_heading = False

    for line in lines:
        m = _HEADING_RE.match(line)
        if m:
            if not seen_heading:
                # Everything we collected so far is preamble.
                seen_heading = True
                if any(ln.strip() for ln in current_lines):
                    segments.append(("", "\n".join(current_lines)))
                current_lines = []
            else:
                if current_lines or current_section:
                    segments.append((current_section, "\n".join(current_lines)))
                current_lines = []
            current_section = m.group("heading").strip()
        else:
            current_lines.append(line)
    # flush trailing body
    if current_lines or current_section:
        segments.append((current_section, "\n".join(current_lines)))

    chunks: list[Chunk] = []
    index = 0
    for section, chunk_text in segments:
        for part in _split_oversized(chunk_text, max_len):
            text = part.strip("\n")
            if not text.strip():
                continue
            chunks.append(Chunk(index, section, text))
            index += 1
    return chunks


def _chunk_by_paragraph(content: str, max_len: int) -> list[Chunk]:
    """Fallback: split on blank lines into paragraph chunks."""
    paragraphs = [p.strip("\n").strip() for p in _BLANK_LINE_RE.split(content)]
    paragraphs = [p for p in paragraphs if p.strip()]
    if not paragraphs:
        return []
    chunks: list[Chunk] = []
    index = 0
    for para in paragraphs:
        for part in _split_oversized(para, max_len):
            if not part.strip():
                continue
            chunks.append(Chunk(index, "", part.strip()))
            index += 1
    return chunks


def _split_oversized(text: str, max_len: int) -> list[str]:
    """Split a single block that exceeds ``max_len`` on blank lines if possible,
    otherwise on sentence boundaries, otherwise hard wrap (rare in L2)."""
    if not max_len or len(text) <= max_len:
        return [text]
    parts: list[str] = []
    for para in _BLANK_LINE_RE.split(text):
        para = para.strip("\n")
        if len(para) <= max_len:
            parts.append(para)
            continue
        # fall back to sentence splits
        sentences = re.split(r"(?<=[.!?]) +", para)
        buf = ""
        for sentence in sentences:
            if buf and len(buf) + len(sentence) + 1 > max_len:
                parts.append(buf)
                buf = sentence
            else:
                buf = f"{buf} {sentence}".strip() if buf else sentence
        if buf:
            parts.append(buf)
    return parts


# ── Persistence helpers ────────────────────────────────────────────────

def _chunk_id(version_id: str, chunk_index: int, chunk_text: str) -> str:
    """Deterministic, tamper-evident chunk id (stable across versions of the
    same content, so FTS rows are idempotent)."""
    return hashlib.sha256(
        f"{version_id}:{chunk_index}:{chunk_text}".encode("utf-8")
    ).hexdigest()


async def store_version_chunks(db, *, version_id: str, document_id: str,
                               content: str) -> list[Chunk]:
    """Re-chunk ``content`` for a document version, replacing any previously
    stored chunks for that version. Returns the generated chunks."""
    await db.execute(
        "DELETE FROM document_chunks WHERE version_id = ?", (version_id,)
    )
    chunks = chunk_markdown(content)
    for chunk in chunks:
        cid = _chunk_id(version_id, chunk.chunk_index, chunk.chunk_text)
        await db.execute(
            """INSERT INTO document_chunks
               (id, version_id, document_id, chunk_index, section, chunk_text)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (cid, version_id, document_id, chunk.chunk_index, chunk.section, chunk.chunk_text),
        )
    return chunks


async def rebuild_document_index(db, *, document_id: str) -> int:
    """Make ``document_chunks`` + ``documents_fts`` consistent with a
    document's current state: regenerate chunks for the latest version and
    mirror that version's chunks into ``documents_fts`` (only the latest
    version is indexed). Soft-deleted / missing documents have their FTS rows
    cleared. Returns the number of indexed chunks (0 when deleted)."""
    # Remove any existing FTS rows for this document first.
    await db.execute(
        "DELETE FROM documents_fts WHERE document_id = ?", (document_id,)
    )

    # Load the document master + latest version directly on the caller's db.
    cur = await db.execute(
        """SELECT id, scope, deleted_at, current_version
           FROM documents WHERE id = ?""",
        (document_id,),
    )
    doc = await cur.fetchone()
    if doc is None or doc["deleted_at"] is not None:
        return 0

    latest_cur = await db.execute(
        """SELECT id, version, content FROM document_versions
           WHERE document_id = ? ORDER BY version DESC LIMIT 1""",
        (document_id,),
    )
    latest = await latest_cur.fetchone()
    if latest is None:
        return 0

    chunks = await store_version_chunks(
        db, version_id=latest["id"], document_id=document_id, content=latest["content"]
    )
    for chunk in chunks:
        cid = _chunk_id(latest["id"], chunk.chunk_index, chunk.chunk_text)
        await db.execute(
            """INSERT INTO documents_fts
               (chunk_id, document_id, version, scope, content)
               VALUES (?, ?, ?, ?, ?)""",
            (cid, document_id, latest["version"], doc["scope"], chunk.chunk_text),
        )
    return len(chunks)


def sanitize_fts_query(q: str) -> str:
    """Build a safe FTS5 phrase query from user input.

    FTS5 operators ( -, +, *, (, ), quotes ) are stripped so arbitrary user
    text cannot trigger query syntax errors, and the cleaned text is wrapped in
    a quoted phrase so multi-word input behaves like an exact-phrase match.
    """
    cleaned = re.sub(r'["\\()*+\-\^:!]', " ", q)
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return ""
    return f'"{cleaned}"'
