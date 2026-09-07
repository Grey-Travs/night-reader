"""The one paragraph separator, shared by every splitter in the app.

A paragraph boundary is a blank line. Thirteen places in Python each re-declared
``\\n\\s*\\n`` for that, and ``web/src/blocks.js`` declared it a fourteenth time — and
that last one is where it stops being a harmless duplication, because **``\\s`` is not
the same character set in Python and in JavaScript**:

- Python's ``\\s`` matches ``\\x1c``–``\\x1f`` (the file/group/record/unit separators)
  and ``\\x85`` (NEL); JavaScript's does not.
- JavaScript's ``\\s`` matches ``\\ufeff`` (a byte-order mark); Python's does not.

So on a chapter carrying any of those six characters on an otherwise blank line, the
reader and the server disagreed about where the paragraphs were. The server addresses
a paragraph by the ordinal the browser computed, so the addresses no longer lined up.
The ``expected_text`` proof means that fails safe — a rewrite is refused with a 409
rather than landing on the wrong paragraph — but the feature simply stops working on
that chapter, with nothing explaining why. A BOM is the realistic one: it is
invisible, and it survives a copy-paste out of a downloaded file.

The fix is a class both languages agree on, defined once here and mirrored in
``blocks.js``. It is the UNION rather than the intersection: a line carrying only an
invisible control character looks blank to the reader, so it should be blank to both
splitters. Each side therefore adds exactly what its own ``\\s`` lacks —
``\\ufeff`` here, ``\\x1c``–``\\x1f`` and ``\\x85`` there.

``tests/test_paragraph_split_parity.py`` runs both implementations over the same
fixtures and fails if they ever disagree again.
"""

from __future__ import annotations

import re

# What Python's \s lacks. (Everything JS's \s lacks is already in Python's.)
_EXTRA = "﻿"

#: Blank-line paragraph separator. Use this everywhere; never re-declare it.
SEP = rf"\n[\s{_EXTRA}]*\n"

SEP_RE = re.compile(SEP)

#: The same boundary, collapsing a run of blank lines — for normalising a reply down
#: to a single paragraph.
SEP_RUN_RE = re.compile(rf"\n[\s{_EXTRA}]*\n+")

# Trimming has to match too, and for the same reason. A block's SPAN is what makes a
# splice lossless, so the leading/trailing whitespace both sides skip must be the same
# set — and it is not: Python's ``str.strip()`` does not strip a BOM, while
# JavaScript's ``String.prototype.trim()`` does. A chapter beginning with a BOM
# therefore had every span off by one on one side only.
_LEAD_RE = re.compile(rf"\A[\s{_EXTRA}]+")
_TRAIL_RE = re.compile(rf"[\s{_EXTRA}]+\Z")


def lstrip_ws(s: str) -> str:
    return _LEAD_RE.sub("", s or "")


def rstrip_ws(s: str) -> str:
    return _TRAIL_RE.sub("", s or "")


def strip_ws(s: str) -> str:
    """``str.strip()`` widened to the same character set ``blocks.js`` trims."""
    return rstrip_ws(lstrip_ws(s))


def split_paragraphs(text: str) -> list[str]:
    """Non-blank paragraphs, each stripped. The common case at most call sites."""
    return [p for p in (strip_ws(b) for b in SEP_RE.split(text or "")) if p]
