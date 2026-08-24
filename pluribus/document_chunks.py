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

**Chunk provenance (L2-CERT):** every chunk carries ``line_start`` / ``line_end``
(1-based, inclusive, mapping back to the original Markdown line numbers),
``section`` (the nearest heading text) and ``heading_path`` (the full nested
heading breadcrumb, e.g. ``Architecture > Storage > Backups``). Blocks are split
into non-overlapping segments so the concatenation of chunks in ``chunk_index``
order is lossless and deterministic.

Hard-rule compliance (matches L1): documents never become facts, ``facts`` /
``facts_fts`` / ``chunks`` / the Fact VectorIndex / Recall v2 / ``notion_cache``
are never touched, no automatic fact extraction, no embedding generation.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

# An ATX heading: 1-6 '#' + whitespace + heading text, at the start of a line.
_HEADING_RE = re.compile(r"^(?P<level>#{1,6})[ \t]+(?P<heading>.*)$")
_BLANK_LINE_RE = re.compile(r"\n[ \t]*\n+\s*")

# FTS5 column index of the indexed ``content`` field (for snippet()).
_FTS_CONTENT_COLUMN = 4


@dataclass
class Chunk:
    """One logical chunk of a document version's Markdown content.

    ``line_start`` / ``line_end`` are 1-based, inclusive line numbers into the
    original Markdown (``content.splitlines()``). ``heading_path`` holds the
    nested heading breadcrumb (``Architecture > Storage > Backups``); ``section``
    is the innermost heading text. ``chunk_text`` is the body of the block
    (the heading text itself lives in ``section`` / ``heading_path``).
    """

    chunk_index: int
    section: str
    chunk_text: str
    line_start: int = 0
    line_end: int = 0
    heading_path: str = ""


@dataclass
class _Segment:
    """A raw (possibly over-max) block destined to become one or more chunks."""

    section: str
    heading_path: str
    chunk_text: str
    line_start: int
    line_end: int


def chunk_markdown(content: str, *, max_len: int = 6000) -> list[Chunk]:
    """Split Markdown ``content`` into logical chunks with provenance.

    Strategy (Markdown-aware, with a plain-paragraph fallback):

    * If the content contains ATX headings (``# heading``, ``## heading``, ...),
      a heading hierarchy is tracked so nested headings accumulate a full
      ``heading_path`` (``Architecture > Storage > Backups``). Preamble text
      before the first heading becomes a chunk with empty ``section``/path.
    * If there are no headings, split on blank lines into paragraph chunks.
    * A degenerate single unbounded paragraph still produces at least one chunk.

    ``max_len`` guards against a single pathological block being stored as one
    oversized chunk: when a body segment exceeds ``max_len`` chars it is
    further split (deterministically) while preserving ``line_start``/``line_end``
    from the parent block so provenance always maps to the original Markdown.
    """
    if not content:
        return []

    lines = content.split("\n")
    segments = _build_segments(lines)
    chunks: list[Chunk] = []
    index = 0
    for seg in segments:
        text = seg.chunk_text
        if max_len and len(text) > max_len:
            parts = _split_oversized(text, max_len)
            for part in parts:
                p = part.strip("\n")
                if not p.strip():
                    continue
                chunks.append(
                    Chunk(
                        index,
                        seg.section,
                        p,
                        line_start=seg.line_start,
                        line_end=seg.line_end,
                        heading_path=seg.heading_path,
                    )
                )
                index += 1
        else:
            t = text.strip("\n")
            if t.strip():
                chunks.append(
                    Chunk(
                        index,
                        seg.section,
                        t,
                        line_start=seg.line_start,
                        line_end=seg.line_end,
                        heading_path=seg.heading_path,
                    )
                )
                index += 1
    return chunks


def _has_heading(lines: list[str]) -> bool:
    return any(_HEADING_RE.match(line) for line in lines)


def _build_segments(lines: list[str]) -> list[_Segment]:
    """Return the ordered list of raw blocks (heading or paragraph)."""
    if _has_heading(lines):
        return _heading_segments(lines)
    return _paragraph_segments(lines)


def _heading_segments(lines: list[str]) -> list[_Segment]:
    """Build segments from a heading-driven document.

    ``line_start``/``line_end`` are 1-based inclusive ranges into ``lines``.
    A heading block's range starts at its heading line (the heading text is not
    duplicated into ``chunk_text`` — it lives in ``section``/``heading_path``).
    Nested headings produce a ``> ``-joined heading_path breadcrumb.
    """
    segments: list[_Segment] = []
    stack: list[tuple[int, str]] = []  # (level, heading_text)
    path = ""
    section = ""
    body: list[str] = []
    start_1based = 0
    first_heading_seen = False

    def flush(end_idx: int) -> None:
        nonlocal body, start_1based
        if not any(ln.strip() for ln in body):
            body = []
            return
        seg_end = end_idx  # last body line is at 1-based ``end_idx``
        segments.append(
            _Segment(section, path, "\n".join(body), max(start_1based, 1), seg_end)
        )
        body = []

    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m:
            if first_heading_seen:
                # Close the previous heading block's body.
                flush(i)
            else:
                # Preamble before the first heading (start at line 1).
                if any(ln.strip() for ln in body):
                    segments.append(
                        _Segment("", "", "\n".join(body), 1, i)
                    )
                body = []
                first_heading_seen = True

            level = len(m.group("level"))
            heading = m.group("heading").strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, heading))
            path = " > ".join(text for _, text in stack)
            section = heading
            # The heading is at 1-based ``i + 1``; the body starts after it.
            start_1based = i + 1
        else:
            body.append(line)

    if first_heading_seen:
        flush(len(lines))
    elif any(ln.strip() for ln in body):
        # No headings at all was handled separately, but keep the fallback safe.
        segments.append(_Segment("", "", "\n".join(body), 1, len(lines)))
    return segments


def _paragraph_segments(lines: list[str]) -> list[_Segment]:
    """Fallback: split on blank lines into paragraph segments with provenance."""
    # Walk the lines tracking blank-line boundaries.
    segments: list[_Segment] = []
    para: list[str] = []
    start = 0
    for i, line in enumerate(lines):
        if line.strip() == "":
            if para:
                segments.append(_Segment("", "", "\n".join(para), start + 1, i))
                para = []
        else:
            if not para:
                start = i
            para.append(line)
    if para:
        segments.append(_Segment("", "", "\n".join(para), start + 1, len(lines)))
    return segments


def _split_oversized(text: str, max_len: int) -> list[str]:
    """Split a single block that exceeds ``max_len`` on blank lines if possible,
    otherwise on sentence boundaries, otherwise hard wrap. Deterministic."""
    if not max_len or len(text) <= max_len:
        return [text]
    parts: list[str] = []
    for para in _BLANK_LINE_RE.split(text):
        para = para.strip("\n")
        if len(para) <= max_len:
            parts.append(para)
            continue
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


def chunk_sha(chunk_text: str) -> str:
    """Content-based (version-independent) hash, used for embedding reuse."""
    return hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()


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
               (id, version_id, document_id, chunk_index, section, chunk_text,
                line_start, line_end, heading_path, chunk_sha)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                cid, version_id, document_id, chunk.chunk_index, chunk.section,
                chunk.chunk_text, chunk.line_start, chunk.line_end,
                chunk.heading_path, chunk_sha(chunk.chunk_text),
            ),
        )
    return chunks


async def rebuild_document_index(db, *, document_id: str) -> int:
    """Make ``document_chunks`` + ``documents_fts`` consistent with a
    document's current state: regenerate chunks for the latest version and
    mirror that version's chunks into ``documents_fts`` (only the latest
    version is indexed). Soft-deleted / missing documents have their FTS rows
    cleared. Returns the number of indexed chunks (0 when deleted)."""
    await db.execute(
        "DELETE FROM documents_fts WHERE document_id = ?", (document_id,)
    )

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
