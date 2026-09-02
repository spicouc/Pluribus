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
    """A raw (possibly over-max) block destined to become one or more chunks.

    ``lines`` is the list of body lines that produced ``chunk_text`` via
    ``"\n".join(lines)``; ``line_start`` is the 1-based line number of
    ``lines[0]`` in the source document so the splitter can compute per-part
    line ranges when it has to sub-split an oversized block.
    """

    section: str
    heading_path: str
    chunk_text: str
    line_start: int
    line_end: int
    lines: list[str] = field(default_factory=list)


@dataclass
class _SplitPart:
    """One sub-chunk produced by ``_split_oversized`` with its own line range.

    ``line_start`` / ``line_end`` are 1-based, inclusive, and map to the
    original Markdown source — not to the parent segment's range. This is the
    fix for BUG A: every sub-chunk carries its real provenance, so a chunk
    whose ``chunk_text`` only covers a subset of the parent block's lines no
    longer falsely claims the whole parent range.
    """

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
    further split (deterministically) into sub-chunks. **Each sub-chunk
    receives its own ``line_start`` / ``line_end`` derived from the lines of
    text that actually produced it** — sub-chunks never inherit the parent
    segment's range. The split order is blank-line → sentence-boundary →
    character-level hard wrap, with hard wrap as the last-resort ceiling that
    guarantees ``0 < len(chunk_text) <= max_len`` for every emitted chunk.
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
            parts = _split_oversized(seg.lines, seg.line_start, max_len)
            for part in parts:
                p = part.chunk_text.strip("\n")
                if not p.strip():
                    continue
                chunks.append(
                    Chunk(
                        index,
                        seg.section,
                        p,
                        line_start=part.line_start,
                        line_end=part.line_end,
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

    ``line_start``/``line_end`` are 1-based inclusive ranges into ``lines``
    covering the **body** block of each section (the heading itself is not
    duplicated into ``chunk_text`` — it lives in ``section``/``heading_path``).
    The range therefore points at the lines of the source that actually
    produced ``chunk_text``: ``line_start`` is the first body line (the line
    right after the heading) and ``line_end`` is the last body line
    (inclusive). Nested headings produce a ``> ``-joined ``heading_path``.
    """
    segments: list[_Segment] = []
    stack: list[tuple[int, str]] = []  # (level, heading_text)
    path = ""
    section = ""
    body: list[str] = []
    body_start_1based = 0  # 1-based source line of body[0]; 0 = no body yet
    body_end_1based = 0    # 1-based source line of body[-1]
    first_heading_seen = False

    def flush() -> None:
        nonlocal body, body_start_1based, body_end_1based
        if not any(ln.strip() for ln in body):
            body = []
            return
        segments.append(
            _Segment(
                section, path, "\n".join(body),
                body_start_1based, body_end_1based, list(body),
            )
        )
        body = []

    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m:
            if first_heading_seen:
                # Close the previous heading block's body.
                flush()
            else:
                # Preamble before the first heading (start at line 1).
                if body and any(ln.strip() for ln in body):
                    segments.append(
                        _Segment(
                            "", "", "\n".join(body),
                            body_start_1based, body_end_1based, list(body),
                        )
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
            # The heading is at 1-based ``i + 1``; the body starts on the next
            # line (``i + 2``). body_start_1based is set when the first body
            # line is observed (below), so sub-chunks that come from a
            # still-empty body stay out of the segment stream.
            body_start_1based = 0
            body_end_1based = 0
        else:
            line_1based = i + 1
            if body_start_1based == 0:
                body_start_1based = line_1based
            body_end_1based = line_1based
            body.append(line)

    if first_heading_seen:
        flush()
    elif body and any(ln.strip() for ln in body):
        # No headings at all was handled separately, but keep the fallback safe.
        segments.append(
            _Segment(
                "", "", "\n".join(body),
                body_start_1based, body_end_1based, list(body),
            )
        )
    return segments


def _paragraph_segments(lines: list[str]) -> list[_Segment]:
    """Fallback: split on blank lines into paragraph segments with provenance.

    The per-paragraph ``lines`` list is retained so ``_split_oversized`` can
    compute exact ``line_start`` / ``line_end`` for any sub-chunk it produces.
    """
    # Walk the lines tracking blank-line boundaries.
    segments: list[_Segment] = []
    para: list[str] = []
    start = 0
    for i, line in enumerate(lines):
        if line.strip() == "":
            if para:
                segments.append(_Segment("", "", "\n".join(para), start + 1, i, list(para)))
                para = []
        else:
            if not para:
                start = i
            para.append(line)
    if para:
        segments.append(_Segment("", "", "\n".join(para), start + 1, len(lines), list(para)))
    return segments


def _split_oversized(
    lines: list[str],
    line_offset: int,
    max_len: int,
) -> list[_SplitPart]:
    """Split a block of lines (a single oversized segment) into sub-chunks.

    This is the central fix for BUG A and BUG B:

    * **BUG A** — every returned :class:`_SplitPart` carries its own
      ``line_start`` / ``line_end`` derived from the lines of text that
      actually produced it. Sub-chunks do **not** inherit the parent
      segment's range. ``line_offset`` is the 1-based line number of
      ``lines[0]`` in the source document.
    * **BUG B** — when neither blank-line nor sentence-boundary splitting
      produces a part whose length fits ``max_len``, a character-level
      hard-wrap guarantees the invariant
      ``0 < len(part.chunk_text) <= max_len`` for every emitted part.

    Split order (greedy, applied to the joined text of each paragraph):

    1. **Blank-line split** — paragraphs separated by one or more blank
       lines are split apart. Each paragraph is emitted if it fits, else
       it is recursively processed by step 2.
    2. **Sentence-boundary split** — a paragraph that is still > ``max_len``
       is split on sentence terminators (``.``, ``!``, ``?``) followed by
       whitespace. Each sentence group that fits ``max_len`` is emitted;
       remaining long sentences are processed by step 3.
    3. **Hard wrap** — the last-resort floor: any remaining chunk whose
       ``chunk_text`` still exceeds ``max_len`` is sliced into pieces of
       at most ``max_len`` characters. Each piece carries the same line
       range as the line it was sliced from (a single very long line can
       legitimately produce several pieces that all share its 1-based
       index — the text on that line *is* the source of those pieces).
    """
    if not max_len or not lines:
        return []
    full_text = "\n".join(lines)
    if len(full_text) <= max_len:
        return [
            _SplitPart(
                full_text,
                line_offset,
                line_offset + len(lines) - 1,
            )
        ]
    return _split_lines_by_blank(lines, line_offset, max_len)


def _split_lines_by_blank(
    lines: list[str],
    line_offset: int,
    max_len: int,
) -> list[_SplitPart]:
    """Step 1: split ``lines`` into blank-line-separated paragraph groups."""
    parts: list[_SplitPart] = []
    cur: list[str] = []
    cur_start_idx = -1  # -1 sentinel: no paragraph in progress
    for i, line in enumerate(lines):
        if line.strip() == "":
            if cur_start_idx >= 0:
                parts.extend(
                    _split_paragraph(cur, line_offset + cur_start_idx, max_len)
                )
                cur = []
                cur_start_idx = -1
        else:
            if cur_start_idx < 0:
                cur_start_idx = i
            cur.append(line)
    if cur and cur_start_idx >= 0:
        parts.extend(
            _split_paragraph(cur, line_offset + cur_start_idx, max_len)
        )
    return parts


def _split_paragraph(
    para_lines: list[str],
    para_line_start: int,
    max_len: int,
) -> list[_SplitPart]:
    """Step 2 (and 3): split a single paragraph group.

    ``para_line_start`` is the 1-based line number of ``para_lines[0]`` in
    the source document. Sentences that stay within the same line are
    emitted together with that line's index; sentence splits that cross
    line boundaries are not attempted (the existing v1 strategy did not do
    it either and a sentence can only live on a single line in this
    grammar).
    """
    para_text = "\n".join(para_lines)
    if len(para_text) <= max_len:
        return [
            _SplitPart(
                para_text,
                para_line_start,
                para_line_start + len(para_lines) - 1,
            )
        ]

    parts: list[_SplitPart] = []
    # Strategy 2: sentence split. We split each *line* individually into
    # sentences (a sentence never spans lines in the v1 grammar) and
    # pack consecutive sentences into a part until adding the next would
    # exceed max_len. When a single sentence is itself > max_len, we
    # fall through to the per-line hard-wrap in step 3.
    sentence_buf: list[str] = []
    sentence_buf_lines: list[int] = []  # 1-based line indices contributing
    sentence_buf_len = 0

    def flush_sentence_buf() -> None:
        nonlocal sentence_buf, sentence_buf_lines, sentence_buf_len
        if not sentence_buf:
            return
        joined = " ".join(sentence_buf)
        if len(joined) <= max_len:
            parts.append(
                _SplitPart(
                    joined,
                    sentence_buf_lines[0],
                    sentence_buf_lines[-1],
                )
            )
        else:
            # Step 3: a single sentence (or accumulated group) > max_len.
            # Walk the contributing lines and hard-wrap each line that
            # individually exceeds max_len.
            for line_text, line_idx in zip(sentence_buf, sentence_buf_lines):
                if len(line_text) <= max_len:
                    parts.append(_SplitPart(line_text, line_idx, line_idx))
                else:
                    parts.extend(_hard_wrap_line(line_text, line_idx, max_len))
        sentence_buf = []
        sentence_buf_lines = []
        sentence_buf_len = 0

    for line_offset_in_para, line_text in enumerate(para_lines):
        line_idx_1based = para_line_start + line_offset_in_para
        # Split the line on sentence boundaries. ``re.split`` with a
        # capturing group returns the delimiters interleaved with pieces.
        pieces = re.split(r"(?<=[.!?]) +", line_text)
        if not pieces:
            continue
        for piece in pieces:
            if not piece:
                continue
            if len(piece) > max_len:
                # Flush whatever fits, then hard-wrap this oversized piece.
                flush_sentence_buf()
                parts.extend(_hard_wrap_line(piece, line_idx_1based, max_len))
                continue
            # Tentative join: would adding this piece overflow?
            tentative_len = sentence_buf_len + (1 if sentence_buf else 0) + len(piece)
            if tentative_len > max_len and sentence_buf:
                flush_sentence_buf()
            sentence_buf.append(piece)
            sentence_buf_lines.append(line_idx_1based)
            sentence_buf_len = (sentence_buf_len + (1 if len(sentence_buf) > 1 else 0)
                                + len(piece))
    flush_sentence_buf()
    return parts


def _hard_wrap_line(
    line_text: str,
    line_idx_1based: int,
    max_len: int,
) -> list[_SplitPart]:
    """Step 3: slice ``line_text`` into pieces of at most ``max_len`` chars.

    Every piece maps to the same source line — that line *is* the source
    of every piece. This is the only place where the splitter crosses a
    mid-character boundary; it is reached exclusively when neither the
    blank-line nor the sentence-boundary strategies can bring a part
    under ``max_len`` (e.g. a 15 000-char token with no whitespace or
    punctuation, or a 15 000-char single line inside a code fence).
    """
    if max_len <= 0:
        return [_SplitPart(line_text, line_idx_1based, line_idx_1based)]
    if len(line_text) <= max_len:
        return [_SplitPart(line_text, line_idx_1based, line_idx_1based)]
    parts: list[_SplitPart] = []
    for start in range(0, len(line_text), max_len):
        piece = line_text[start:start + max_len]
        parts.append(_SplitPart(piece, line_idx_1based, line_idx_1based))
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
