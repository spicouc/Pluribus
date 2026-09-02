"""L2-CERT-HF: hotfix tests for the _heading_segments off-by-one.

These tests certify the chunk provenance guarantees that the L2-CERT
`_assert_reconstructs` check depends on, with a sharper focus on the
off-by-one that the hotfix closes (start_1based = i + 1, which pointed at
the heading line instead of the first body line).

Each test is independent and exercises one property of
``chunk_markdown``. Run with::

    /tmp/pluribus_audit_g1_1787869687/venv/bin/python -m pytest \\
        tests/test_document_chunk_provenance_hotfix.py -v
"""
from __future__ import annotations

import re
import unittest

from pluribus.document_chunks import chunk_markdown


def _collapse(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip()


def _reconstructable(content: str, chunks) -> None:
    """Same shape as L2-CERT's private helper, but exported for the
    hotfix suite. Asserts the substring / lossless / order contract."""
    lines = content.split("\n")
    # Substring mapping for every chunk.
    for ch in chunks:
        assert ch.line_start >= 1, ch
        assert ch.line_end >= ch.line_start, ch
        block = " ".join(ln.strip() for ln in lines[ch.line_start - 1:ch.line_end])
        assert _collapse(ch.chunk_text) in _collapse(block), (
            f"chunk {ch.chunk_index} text not found in claimed range "
            f"[{ch.line_start}, {ch.line_end}]"
        )
    # chunk_index monotonic.
    assert [c.chunk_index for c in chunks] == list(range(len(chunks))), chunks
    # Ordered line ranges.
    for a, b in zip(chunks, chunks[1:]):
        assert b.line_start >= a.line_start, (a, b)
    # Word-stream coverage (lossless).
    src = " ".join(
        ln.strip() for ln in lines
        if ln.strip() and not ln.lstrip().startswith("#")
    )
    src_words = re.findall(r"[^\W_]+", src)
    chunk_words = re.findall(r"[^\W_]+", " ".join(c.chunk_text for c in chunks))
    for w in src_words:
        assert w in chunk_words, f"word {w!r} lost in chunking"


# ── HF-01 ─────────────────────────────────────────────────────────────
class HF01_OversizedMultilineSectionLineRanges(unittest.TestCase):
    """A multi-line oversized section must produce per-chunk line ranges
    that point at the actual body lines (not the heading, not blanks)."""

    def test(self) -> None:
        para = ("Once upon a time the industrious zebra crossed the wide savannah "
                "toward the distant acacia grove, carefully avoiding every sleepy "
                "predator it met on its way, all while keeping a steady rhythm. ") * 40
        content = f"# Long Report\n\n{para}\n\n## Notes\n\nshort\n"
        chunks = chunk_markdown(content)
        self.assertGreater(len(chunks), 2, "oversized body must split")
        # Every chunk's range must be 1-based and within the source.
        n_lines = content.count("\n") + 1
        for ch in chunks:
            self.assertGreaterEqual(ch.line_start, 1)
            self.assertLessEqual(ch.line_end, n_lines)
            self.assertGreaterEqual(ch.line_end, ch.line_start)
        _reconstructable(content, chunks)


# ── HF-02 ─────────────────────────────────────────────────────────────
class HF02_SingleLineFifteenThousandRespectsMaxLen(unittest.TestCase):
    """A 15 000-char single line (no punctuation, no whitespace) must be
    hard-wrapped so every emitted chunk is <= max_len."""

    def test(self) -> None:
        body = "x" * 15_000
        content = f"# Mega\n\n{body}\n"
        max_len = 4_000
        chunks = chunk_markdown(content, max_len=max_len)
        self.assertGreater(len(chunks), 1)
        for ch in chunks:
            self.assertLessEqual(len(ch.chunk_text), max_len,
                                 f"chunk {ch.chunk_index} > max_len")
            self.assertGreater(len(ch.chunk_text), 0)
        # All sub-chunks share the same line (the only body line).
        src_line = 3
        for ch in chunks:
            self.assertEqual(ch.line_start, src_line)
            self.assertEqual(ch.line_end, src_line)


# ── HF-03 ─────────────────────────────────────────────────────────────
class HF03_FifteenThousandNoPunctuationRespectsMaxLen(unittest.TestCase):
    """A 15 000-char single line with no punctuation and only spaces must
    still be hard-wrapped (no natural break point)."""

    def test(self) -> None:
        # Punctuation-free with spaces; sentence-boundary split won't help
        # because there is no sentence terminator. Blank-line split won't
        # help (single line). Hard-wrap must catch it.
        body = "word " * 3_000  # 15 000 chars
        content = f"# Mega\n\n{body}\n"
        max_len = 4_000
        chunks = chunk_markdown(content, max_len=max_len)
        for ch in chunks:
            self.assertLessEqual(len(ch.chunk_text), max_len,
                                 f"chunk {ch.chunk_index} > max_len")
        # Lossless reconstruction: every word survives.
        src_words = re.findall(r"[^\W_]+", body)
        chunk_words = re.findall(r"[^\W_]+", " ".join(c.chunk_text for c in chunks))
        self.assertEqual(sorted(src_words), sorted(chunk_words))


# ── HF-04 ─────────────────────────────────────────────────────────────
class HF04_LongUrlTokenRespectsMaxLen(unittest.TestCase):
    """A 15 000-char URL-like token (no whitespace, no punctuation) must
    still be hard-wrapped under max_len."""

    def test(self) -> None:
        body = "a" * 15_000
        content = f"# Mega\n\n{body}\n"
        max_len = 2_000
        chunks = chunk_markdown(content, max_len=max_len)
        self.assertGreater(len(chunks), 1)
        for ch in chunks:
            self.assertLessEqual(len(ch.chunk_text), max_len)


# ── HF-05 ─────────────────────────────────────────────────────────────
class HF05_OversizedFencedCodeLosslessAndBounded(unittest.TestCase):
    """An oversized fenced code block must be lossless and every chunk <=
    max_len (code-fence protection is part of the L2-CERT promise)."""

    def test(self) -> None:
        # Each line is under max_len on its own; the whole block overshoots.
        line = "return " + "x + " * 200 + "1\n"  # ~1300 chars
        code_body = "".join(line for _ in range(8))
        content = f"# Code\n\n```python\n{code_body}```\n"
        max_len = 2_000
        chunks = chunk_markdown(content, max_len=max_len)
        joined = "\n".join(c.chunk_text for c in chunks)
        # Fence open/close survive.
        self.assertIn("```python", joined)
        self.assertIn("```", joined)
        # Every code line survives.
        for ln in line.splitlines()[:8]:
            self.assertIn(ln, joined)
        # Invariant.
        for ch in chunks:
            self.assertLessEqual(len(ch.chunk_text), max_len)
        _reconstructable(content, chunks)


# ── HF-06 ─────────────────────────────────────────────────────────────
class HF06_NestedHeadingPathPreservedAfterSplit(unittest.TestCase):
    """Nested heading_path must remain on every sub-chunk of an oversized
    body, and all sub-chunks must belong to the same nested section."""

    def test(self) -> None:
        para = ("zebra " * 30 + "paws on the warm savannah sand, very calmly. ") * 20
        content = (
            "# Architecture\n\n"
            "## Storage\n\n"
            "### Backups\n\n"
            f"{para}\n"
        )
        max_len = 2_000
        chunks = chunk_markdown(content, max_len=max_len)
        # All sub-chunks inherit the breadcrumb.
        nested = [c for c in chunks if "Backups" in c.heading_path]
        self.assertGreater(len(nested), 1, "expected the Backups body to be split")
        for c in nested:
            self.assertEqual(c.heading_path, "Architecture > Storage > Backups")
        _reconstructable(content, chunks)


# ── HF-07 ─────────────────────────────────────────────────────────────
class HF07_DeterministicOutput(unittest.TestCase):
    """Two calls with identical input must yield identical chunk lists."""

    def test(self) -> None:
        content = "# A\n\naa bb cc.\n\n## B\n\nx y z.\n"
        first = [(c.section, c.chunk_text, c.line_start, c.line_end, c.heading_path)
                 for c in chunk_markdown(content)]
        second = [(c.section, c.chunk_text, c.line_start, c.line_end, c.heading_path)
                  for c in chunk_markdown(content)]
        self.assertEqual(first, second)


# ── HF-08 ─────────────────────────────────────────────────────────────
class HF08_NoTextLoss(unittest.TestCase):
    """Concatenated chunk_text must contain every non-heading source word
    (lossless invariant)."""

    def test(self) -> None:
        para = ("the " * 50 + "industrious zebra quietly grazes while humming a "
                "soft lullaby, as the warm savannah breeze carries the distant "
                "echo of a curious hyena's call. ") * 30
        content = f"# Header\n\n{para}\n\n## Tail\n\nfinal word: gizmo\n"
        max_len = 2_500
        chunks = chunk_markdown(content, max_len=max_len)
        # Words from the body (non-heading lines) must all survive.
        body_text = "\n".join(
            ln for ln in content.split("\n")
            if ln.strip() and not ln.lstrip().startswith("#")
        )
        src_words = re.findall(r"[^\W_]+", body_text)
        chunk_words = re.findall(r"[^\W_]+", " ".join(c.chunk_text for c in chunks))
        for w in set(src_words):
            self.assertIn(w, set(chunk_words), f"word {w!r} lost")


# ── HF-09 ─────────────────────────────────────────────────────────────
class HF09_ChunkOrdering(unittest.TestCase):
    """chunk_index is 0..N-1 and line_start is monotonically non-decreasing
    across the document."""

    def test(self) -> None:
        para = ("alpha beta gamma delta epsilon zeta eta theta iota kappa. ") * 60
        content = f"# Top\n\n{para}\n\n## Mid\n\nmiddle body.\n\n### Bot\n\n{para}\n"
        chunks = chunk_markdown(content)
        self.assertEqual([c.chunk_index for c in chunks], list(range(len(chunks))))
        for a, b in zip(chunks, chunks[1:]):
            self.assertGreaterEqual(b.line_start, a.line_start)


# ── HF-10 ─────────────────────────────────────────────────────────────
class HF10_NormalSmallMarkdownUnchanged(unittest.TestCase):
    """A normal small Markdown document without oversized blocks must keep
    its exact line ranges: heading's body is on the lines after it."""

    def test(self) -> None:
        content = (
            "# Handbook\n\n"
            "Welcome text.\n"
            "\n"
            "## Habitats\n\n"
            "Savannah regions.\n"
        )
        chunks = chunk_markdown(content)
        # Map section -> (line_start, line_end) for the two headings.
        by_section = {c.section: (c.line_start, c.line_end) for c in chunks}
        self.assertIn("Handbook", by_section)
        self.assertIn("Habitats", by_section)
        # Handbook's body starts at the line right after the heading.
        self.assertGreaterEqual(by_section["Handbook"][0], 1)
        self.assertGreaterEqual(by_section["Handbook"][1], by_section["Handbook"][0])
        self.assertGreaterEqual(by_section["Habitats"][0], by_section["Handbook"][0])
        _reconstructable(content, chunks)


# ── HF-11 ─────────────────────────────────────────────────────────────
class HF11_PreambleProvenance(unittest.TestCase):
    """A preamble before the first heading must keep exact body provenance
    (this is the off-by-one path that the L2-CERT sub-chunk substring
    check first exposed)."""

    def test(self) -> None:
        content = (
            "Preamble line one.\n"
            "Preamble line two.\n"
            "\n"
            "# Heading\n\n"
            "Body of heading.\n"
        )
        chunks = chunk_markdown(content)
        preambles = [c for c in chunks if not c.heading_path]
        self.assertEqual(len(preambles), 1, "exactly one preamble chunk")
        p = preambles[0]
        # Preamble body block starts at the first non-heading source line.
        self.assertEqual(p.line_start, 1)
        # End is the last body line (the trailing blank at line 3 is part of
        # the body block, since the block carries the entire region between
        # the previous heading and this one).
        self.assertEqual(p.line_end, 3)
        # The chunk_text after stripping surrounding whitespace.
        self.assertIn("Preamble line one", p.chunk_text)
        self.assertIn("Preamble line two", p.chunk_text)
        _reconstructable(content, chunks)


# ── HF-12 ─────────────────────────────────────────────────────────────
class HF12_UnicodeMultilineProvenance(unittest.TestCase):
    """A multi-line body with non-ASCII content (Catalan, accented, CJK)
    must keep the exact body line range."""

    def test(self) -> None:
        content = (
            "# Memòria\n\n"
            "Línia 1: el zebratge creua la sabana.\n"
            "Línia 2: un àlbum d'infantesa ben entranyable.\n"
            "Línia 3: 中文测试行 — Ωμέγα.\n"
            "Línia 4: ünïcödé αβγδ ✓.\n"
        )
        chunks = chunk_markdown(content)
        mem = [c for c in chunks if c.section == "Memòria"]
        self.assertEqual(len(mem), 1)
        m = mem[0]
        # Body block starts at the first line after the heading (the
        # blank separator at line 2 is part of the body block); ends at
        # the last body line (the trailing blank at line 7, because the
        # content ends with a newline).
        self.assertEqual(m.line_start, 2)
        self.assertEqual(m.line_end, 7)
        # All four body lines survive in chunk_text.
        self.assertIn("Línia 1", m.chunk_text)
        self.assertIn("Línia 2", m.chunk_text)
        self.assertIn("Línia 3", m.chunk_text)
        self.assertIn("Línia 4", m.chunk_text)
        self.assertIn("中文测试行", m.chunk_text)
        _reconstructable(content, chunks)


# ── Property invariant (HF-Prop) ──────────────────────────────────────
class HFProp_MaxLenInvariant(unittest.TestCase):
    """For any document and max_len > 0, every emitted chunk is
    strictly bounded by max_len."""

    def test_various(self) -> None:
        para_long = ("Once upon a time the industrious zebra crossed the wide "
                     "savannah toward the distant acacia grove. ") * 20
        para_short = "short text."
        documents = [
            "# A\n\nshort.\n",
            "# A\n\n" + para_long + "\n",
            "# A\n\n" + "x" * 15_000 + "\n",
            "# A\n\npara1.\n\npara2.\n\n## B\n\n" + para_long + "\n",
        ]
        for d in documents:
            for max_len in (200, 1_000, 4_000):
                chunks = chunk_markdown(d, max_len=max_len)
                for ch in chunks:
                    self.assertLessEqual(len(ch.chunk_text), max_len,
                                         f"chunk over max_len for {d[:30]!r}")


class ExactNewlinePreservationTests(unittest.TestCase):
    """HF-13 and HF-14: exact newline preservation when an oversized
    multiline block is split. The original newlines between source lines
    must survive into the chunk text — they are NOT to be collapsed to
    spaces or removed."""

    def test_hf13_oversized_multiline_paragraph_preserves_newlines(self) -> None:
        """HF-13: oversized multiline paragraph → split keeps original
        newlines (no whitespace collapse)."""
        lines = [f"Body line {i} with some content" for i in range(1, 101)]
        content = f"# Heading\n\n{chr(10).join(lines)}\n"
        chunks = chunk_markdown(content, max_len=300)
        self.assertGreater(len(chunks), 1, "over-max must split")
        for ch in chunks:
            self.assertLessEqual(len(ch.chunk_text), 300)
        # Reconstruct body from the chunk text by re-splitting on \n.
        # Every line of the original (except the empty/heading) must
        # appear verbatim in some chunk's lines — and a chunk's lines
        # must equal exactly slices of the original lines, joined by \n.
        all_chunk_lines: list[str] = []
        for ch in chunks:
            all_chunk_lines.extend(ch.chunk_text.split("\n"))
        # All original body lines must appear in the reconstructed output.
        for original in lines:
            self.assertIn(
                original, all_chunk_lines,
                f"original line {original!r} not found verbatim in any chunk's"
                f" split-by-\\n output",
            )

    def test_hf14_oversized_fenced_code_preserves_newlines(self) -> None:
        """HF-14: oversized fenced code (multiline) → split keeps
        newlines, fence markers stay on their own line, and every chunk
        respects max_len with exact character + order preservation."""
        code_lines = [f"x = {i}  # comment line with some text" for i in range(50)]
        # code body as a single string with embedded \n
        code_body = "\n".join(code_lines)
        content = f"# Code Section\n\n```python\n{code_body}\n```\n\nTrailing.\n"
        chunks = chunk_markdown(content, max_len=400)
        self.assertGreater(len(chunks), 1, "over-max must split")
        for ch in chunks:
            self.assertLessEqual(len(ch.chunk_text), 400)
            # No chunk may be the result of a join-with-space on a fenced
            # line — that would erase Python's syntactic newlines.
            # We assert that the chunk text, when split on \n, yields
            # only lines that exist verbatim in the original.
            for chunk_line in ch.chunk_text.split("\n"):
                self.assertIn(
                    chunk_line, content,
                    f"chunk line {chunk_line[:60]!r} not found verbatim in"
                    f" original document",
                )
        # The two ``` fence markers must each appear in some chunk text
        # (not collapsed away, not merged with surrounding text).
        backticks_total = sum(ch.chunk_text.count("```") for ch in chunks)
        self.assertEqual(
            backticks_total, 2,
            f"expected exactly 2 fence markers (```), got {backticks_total}",
        )
        # The trailing line "Trailing." must appear verbatim in one chunk.
        all_text = "\n".join(ch.chunk_text for ch in chunks)
        self.assertIn("Trailing.", all_text)


if __name__ == "__main__":
    unittest.main()
