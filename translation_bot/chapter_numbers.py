"""Work out each chapter's real number, so several documents can read as one novel.

Google Docs caps a document at ~100 tabs, so a long novel is split across documents and
chapter 101 restarts at tab 1. Reading and posting both need the GLOBAL number, and the
obvious sources for it turn out to be traps:

* **Tab titles are useless.** Every tab in this library is titled "Tab N", and that N is
  *creation order* -- not position, not the chapter number. Code that reads it as either
  is confidently wrong, so ``parse_tab_title`` returns None for that shape on purpose.
* **Position arithmetic is fragile.** It breaks on duplicated tabs, deleted chapters,
  leading author notes, and trailing side stories -- all of which occur here.

What *does* work is already in the text: the source export puts the chapter number in a
header line at the top, and ``sanitize.strip_source_header`` has always parsed it (the
app renders it as ``chapter_row()["number"]``). Measured across the real library, it is
present on 30-100% of tabs per document and agrees exactly where present:

    This Priest 2   100/100 tabs, offset +100   ->  101-200
    This Priest 3    24/24  tabs, offset +200   ->  201-224
    Becoming Top 2   96/96  tabs, offset +100   ->  101-196

But it cannot be trusted blindly either, which is what the run-splitting below is for:

* Some documents' headers **restart at 1** even though they hold chapters 101+
  ("Everyone is Suspiciously Targeting Me 2" reads 1-18 with a modal offset of -1, while
  the truth is +100). A caller that knows where the document starts passes ``start_hint``
  and it wins over the measured offset.
* Some documents **restart mid-way into side stories** ("I Possessed a Character 2" runs
  101-146 and then drops to 1-7). Those trailing tabs are extras, not chapters 1-7.
* Some tabs are **byte-identical duplicates** that were both translated and billed
  ("Crown Prince 2" tabs 42/43 are both chapter 142).

Everything here is pure: no I/O, no network, no imports from ``server``. That is what
lets it be characterized against the cached ``source.json`` of all 52 real projects.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from translation_bot.docs_extract import hangul_fraction
from translation_bot.sanitize import SEP_RE, strip_source_header

# --- kinds -----------------------------------------------------------------
# A row that is not part of the reading sequence gets global=None. It keeps its index,
# its translation and its state -- it is only left out of the chapter numbering.
KIND_CHAPTER = "chapter"
KIND_PROLOGUE = "prologue"
KIND_EPILOGUE = "epilogue"
KIND_SIDE = "side"
KIND_EXTRA = "extra"
KIND_DUPLICATE = "duplicate"

IN_SEQUENCE = {KIND_CHAPTER, KIND_PROLOGUE, KIND_EPILOGUE}

# --- where a number came from, so the review table can show its trustworthiness ---
SRC_HEADER = "header"  # parsed out of the chapter's own export header
SRC_INFERRED = "inferred"  # filled from its run's offset; no header on this tab
SRC_DUPE = "auto-dupe"  # identical text to an earlier tab
SRC_NONE = "none"

CONF_HIGH = "high"
CONF_INFERRED = "inferred"
CONF_LOW = "low"  # never auto-applied; forced into human review

# "제3화" / "3화" / "3장" -- a capturing form of the heading pattern in text_source.
_KO_NUM_RE = re.compile(r"제?\s*(\d{1,4})\s*[화장권부]")
_EN_NUM_RE = re.compile(r"(?:chapter|ch\.?|episode|ep\.?)\s*(\d{1,4})\b", re.IGNORECASE)

# Words that mark a tab as something other than a numbered chapter. "Epilouge" is not a
# typo here -- one real document spells it that way, so matching is loose on purpose.
_KIND_WORDS = (
    ("프롤로그", KIND_PROLOGUE),
    ("서장", KIND_PROLOGUE),
    ("prologue", KIND_PROLOGUE),
    ("에필로그", KIND_EPILOGUE),
    ("종장", KIND_EPILOGUE),
    ("epilog", KIND_EPILOGUE),
    ("epilou", KIND_EPILOGUE),
    ("외전", KIND_SIDE),
    ("번외", KIND_SIDE),
    ("side story", KIND_SIDE),
)

# A generic tab name carries no information. Treated as an explicit no-signal sentinel
# rather than as the number it appears to contain.
_GENERIC_TAB_RE = re.compile(r"^\s*tab\s*\d+\s*$", re.IGNORECASE)

# A tab this short with almost no Korean is front/back matter, not a chapter. Both
# conditions are required, and the result is only ever LOW confidence: an already-English
# chapter is a real chapter (the engine has a whole "english-source" status for it) and
# must not be silently dropped from the numbering.
_EXTRA_MAX_CHARS = 200
_EXTRA_MAX_HANGUL = 0.05

# How far BELOW the expected start a document's own numbering may sit before it is read as
# having restarted. A sequel that repeats the previous document's last chapter is off by
# one and genuinely states a global number; one whose headers begin at 1 while the series
# expects 101 has restarted and needs correcting. Numbering that runs AHEAD of the expected
# start is always taken at face value -- that is a real gap, and forcing it back onto a
# contiguous count would erase it.
_RESTART_TOLERANCE = 1


@dataclass
class Row:
    """One tab's resolved place in the novel.

    ``index`` is the existing per-document 1-based chapter index and never changes: it is
    the contract with ``state.json`` keys, ``chapters/chapter-NNN.md``, ``previous/``,
    ``audit/`` and ``variants/``. ``global_number`` is a new, separate, display-and-
    ordering-only value. Nothing that consumes ``index`` may ever be handed a
    ``global_number``.
    """

    index: int
    global_number: int | None = None
    kind: str = KIND_CHAPTER
    source: str = SRC_NONE
    confidence: str = CONF_LOW
    label: str = ""
    duplicate_of: int | None = None
    header_number: int | None = None

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "global": self.global_number,
            "kind": self.kind,
            "source": self.source,
            "confidence": self.confidence,
            "label": self.label,
            "duplicate_of": self.duplicate_of,
            "header_number": self.header_number,
        }


# A header or title line is short; a paragraph of prose is not. Blocks longer than this
# are never scanned for a chapter number.
_HEAD_MAX_CHARS = 120


def _first_blocks(text: str, count: int = 2) -> str:
    """The opening header/title block(s) only.

    The fallback number patterns are deliberately not run over a whole chapter: "3화"
    appears in dialogue often enough that scanning prose would invent numbers. Length is
    the discriminator rather than position alone, because a chapter whose export header
    lacks the usual URL line would otherwise put its first body paragraph in range.
    """
    blocks = SEP_RE.split((text or "").strip())[:count]
    return "\n\n".join(b for b in blocks if len(b.strip()) <= _HEAD_MAX_CHARS)


def parse_chapter_number(text: str) -> int | None:
    """The chapter number the text states about itself, or None.

    Prefers ``sanitize.strip_source_header``, which is the source that actually works on
    this library, and falls back to a Korean/English heading in the opening block.
    """
    _, number = strip_source_header(text or "")
    if number is not None:
        try:
            return int(number)
        except (TypeError, ValueError):
            pass
    head = _first_blocks(text)
    for pattern in (_KO_NUM_RE, _EN_NUM_RE):
        m = pattern.search(head)
        if m:
            return int(m.group(1))
    return None


def parse_tab_title(title: str) -> int | None:
    """A number from the tab's own title, or None -- which is the usual answer.

    "Tab 41" is creation order, not position and not a chapter number, so it is rejected
    outright. Without this the matcher is worse than having no tab titles at all.
    """
    t = (title or "").strip()
    if not t or _GENERIC_TAB_RE.match(t):
        return None
    for pattern in (_KO_NUM_RE, _EN_NUM_RE):
        m = pattern.search(t)
        if m:
            return int(m.group(1))
    m = re.match(r"^(\d{1,4})\s*\.?$", t)
    return int(m.group(1)) if m else None


def classify_kind(title: str, text: str) -> tuple[str, str]:
    """``(kind, label)`` from a tab's title and text, defaulting to a plain chapter."""
    haystack = f"{title or ''}\n{_first_blocks(text, 1)}".casefold()
    for word, kind in _KIND_WORDS:
        if word in haystack:
            return kind, (title or "").strip()
    stripped, _ = strip_source_header(text or "")
    body = re.sub(r"\s", "", stripped)
    if len(body) <= _EXTRA_MAX_CHARS and hangul_fraction(stripped) <= _EXTRA_MAX_HANGUL:
        return KIND_EXTRA, (title or "").strip()
    return KIND_CHAPTER, ""


def _modal_offset(pairs: list[tuple[int, int]]) -> int | None:
    """The most common ``number - position`` over ``(position, number)`` pairs.

    Ties break toward the offset of the earliest candidate rather than by whatever order
    Counter happens to produce, so the result is deterministic.
    """
    if not pairs:
        return None
    offsets = [number - position for position, number in pairs]
    counts = Counter(offsets)
    best = max(counts.values())
    for offset in offsets:  # earliest-first wins a tie
        if counts[offset] == best:
            return offset
    return None


@dataclass
class _Run:
    """A stretch of tabs whose stated numbers increase monotonically."""

    start: int  # 1-based position of the run's first tab
    pairs: list[tuple[int, int]] = field(default_factory=list)
    length: int = 0


def _split_runs(candidates: list[int | None]) -> list[_Run]:
    """Split positions into monotonic runs, breaking where a stated number DROPS.

    A drop is the signal that the document restarted -- into side stories, or because the
    export numbered a sequel document from 1. Equal consecutive numbers are NOT a break:
    those are the duplicated tabs, handled separately by content hash.
    """
    runs: list[_Run] = []
    current = _Run(start=1)
    previous: int | None = None
    for position, number in enumerate(candidates, 1):
        if number is not None and previous is not None and number < previous:
            runs.append(current)
            current = _Run(start=position)
            previous = None
        current.length += 1
        if number is not None:
            current.pairs.append((position, number))
            previous = number
    runs.append(current)
    return runs


def infer_mapping(chapters, *, start_hint: int | None = None) -> list[Row]:
    """Resolve every tab's global chapter number.

    ``chapters`` is a list of ``docs_extract.Chapter`` (anything with ``index``, ``title``,
    ``text`` and ``metrics``). ``start_hint`` is the global number the caller believes the
    document's first chapter has -- from the series manifest -- and it OVERRIDES the
    measured offset for the first run, because some documents' own headers restart at 1.

    The result is a suggestion. Rows at ``CONF_LOW`` must be confirmed by a human before
    anything downstream relies on them.
    """
    chapters = list(chapters or [])
    if not chapters:
        return []

    texts = [getattr(c, "text", "") or "" for c in chapters]
    titles = [getattr(c, "title", "") or "" for c in chapters]
    candidates = [parse_chapter_number(t) for t in texts]

    rows = [
        Row(index=getattr(c, "index", i), header_number=candidates[i - 1])
        for i, c in enumerate(chapters, 1)
    ]

    runs = _split_runs(candidates)
    first_offset: int | None = None

    for run_number, run in enumerate(runs):
        offset = _modal_offset(run.pairs)
        is_first = run_number == 0
        # How much every stated number in this run has to move to become global. Normally
        # zero: the export already states global numbers. It is non-zero only when the
        # caller's hint says this document starts elsewhere than its own headers claim.
        shift = 0

        if is_first:
            if start_hint is not None:
                if run.pairs:
                    first_position, first_number = run.pairs[0]
                    expected = start_hint + (first_position - run.start)
                    # Only numbering that runs backwards into the previous document's
                    # range is a restart the hint has to correct. Anything at or ahead of
                    # what the series expects is believed: see _RESTART_TOLERANCE.
                    if first_number < expected - _RESTART_TOLERANCE:
                        shift = expected - first_number
                    if offset is not None:
                        offset += shift
                else:
                    # No tab in this run states anything, so position is all there is.
                    offset = start_hint - run.start
            first_offset = offset
        elif offset is not None and first_offset is not None and offset != first_offset:
            # A later run on a different offset is a restart, not a continuation: side
            # stories or extras. Leave them out of the sequence and force review.
            for position in range(run.start, run.start + run.length):
                row = rows[position - 1]
                row.kind = KIND_SIDE
                row.global_number = None
                row.source = SRC_NONE
                row.confidence = CONF_LOW
                row.label = titles[position - 1].strip()
            continue

        for position in range(run.start, run.start + run.length):
            row = rows[position - 1]
            stated = candidates[position - 1]
            if stated is not None:
                # The stated number WINS. Overriding it with the run's offset would close
                # real gaps: one document genuinely has no chapter 65, and forcing its
                # tail back into a contiguous count renumbers every chapter after it.
                row.global_number = stated + shift
                row.source = SRC_HEADER
                row.confidence = CONF_HIGH
            elif offset is not None:
                # No header on this tab -- 30-85% coverage per document is normal. Absence
                # of signal, not absence of a chapter: fill it from its run's offset.
                row.global_number = position + offset
                row.source = SRC_INFERRED
                row.confidence = CONF_INFERRED
            else:
                # Nothing stated anywhere in the run and no hint: number by position, but
                # flag it so a human confirms rather than the app inventing a sequence.
                row.global_number = position
                row.source = SRC_NONE
                row.confidence = CONF_LOW

    _mark_kinds(rows, titles, texts)
    _mark_duplicates(rows, chapters)
    return rows


def _mark_kinds(rows: list[Row], titles: list[str], texts: list[str]) -> None:
    """Apply prologue/epilogue/side/extra classification without losing a number."""
    for row, title, text in zip(rows, titles, texts):
        if row.kind != KIND_CHAPTER:
            continue
        kind, label = classify_kind(title, text)
        if kind == KIND_CHAPTER:
            continue
        row.kind = kind
        row.label = label
        if kind not in IN_SEQUENCE:
            # Out of the reading sequence, so it no longer consumes a chapter number --
            # but low confidence, because dropping a real chapter is the worse mistake.
            row.global_number = None
            row.confidence = CONF_LOW


def _mark_duplicates(rows: list[Row], chapters) -> None:
    """Flag a tab whose text is byte-identical to an earlier one.

    Both copies were translated and paid for, so this only removes the later one from the
    reading sequence. Its chapter file, state, ``previous/`` and ``variants/`` are keyed on
    its index and must all stay exactly as they are.
    """
    seen: dict[str, int] = {}
    for row, chapter in zip(rows, chapters):
        metrics = getattr(chapter, "metrics", None)
        digest = getattr(metrics, "content_hash", None)
        if not digest:
            continue
        if digest in seen:
            row.kind = KIND_DUPLICATE
            row.duplicate_of = seen[digest]
            row.global_number = None
            row.source = SRC_DUPE
            row.confidence = CONF_HIGH
        else:
            seen[digest] = row.index


def detect_gaps_duplicates(rows: list[Row]) -> dict:
    """Report missing and repeated global numbers across an already-resolved mapping.

    Both are real in this library and neither is automatically a bug: one document
    genuinely has no chapter 65. The caller persists which gaps are expected, so the
    overview does not carry a red flag the user can never clear.
    """
    numbers = [r.global_number for r in rows if r.global_number is not None]
    if not numbers:
        return {"gaps": [], "duplicates": [], "first": None, "last": None}
    counts = Counter(numbers)
    return {
        "gaps": sorted(set(range(min(numbers), max(numbers) + 1)) - set(numbers)),
        "duplicates": sorted(n for n, c in counts.items() if c > 1),
        "first": min(numbers),
        "last": max(numbers),
    }
