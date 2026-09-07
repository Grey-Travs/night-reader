"""Address, align and replace a single paragraph of a finished translation.

A translated chapter is stored as one Markdown file, and that file stays the source
of truth — export, EPUB, search, scan/fix, the consistency scan, ``previous/`` and
whole-chapter manual edits all read it directly. So a paragraph is not given a stored
id (which all seven writers of that file would have to maintain, and silently corrupt
the moment one of them forgot). Instead it is addressed by ordinal, PROVED by its
exact text, and replaced by character span.

The load-bearing property is in :func:`splice_block`: one paragraph in, one paragraph
out. Because a replacement can never introduce or remove a blank line, a splice
cannot change the chapter's paragraph count — which is what keeps the validator's
paragraph check, every other paragraph's address, and the EPUB structure intact.

Pure: no I/O, no model, no config.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .glossary import GlossaryEntry
from .sanitize import (
    find_leaks,
    korean_fraction,
    remove_korean_echoes,
    strip_reasoning,
)
from .textsplit import SEP_RE, SEP_RUN_RE, lstrip_ws, rstrip_ws, strip_ws

# The paragraph separator used everywhere else in this app (validate._paragraphs,
# sanitize, epub, _paragraph_count, the reader's SourceProse) AND in web/src/blocks.js.
# Imported rather than re-declared: it had been written out inline in fourteen places,
# and the JavaScript one was not the same expression — `\s` differs between the two
# languages. See translation_bot/textsplit.
_SEP_RE = SEP_RE

_QUOTE_RE = re.compile(r"[\"“”「」『』]")
_WS_RE = re.compile(r"\s+")

# A reply that opens with the model talking about the task rather than doing it.
# strip_reasoning's own patterns need a translation-specific object and miss these.
_COMMENTARY_RE = re.compile(
    r"^\s*(?:here(?:'s| is)\b|sure[,!.]|certainly\b|of course\b|okay[,!.]|"
    r"i've\b|i have\b|revised\b|rewritten\b|version\s*\d)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Block:
    """One paragraph, with the EXACT span it occupies in the chapter text."""

    i: int
    start: int
    end: int
    text: str


@dataclass
class Alignment:
    """Which Korean paragraph an English one probably came from."""

    index: int          # best guess, or -1 when there is nothing to align to
    confidence: float   # 0..1
    method: str         # exact | dialogue | length | proportional | none
    span: int           # how many neighbours to show either side


@dataclass
class ParagraphCheck:
    ok: bool
    text: str
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duplicate: bool = False


# ---- splitting and splicing --------------------------------------------------

def _block(text: str, start: int, end: int, i: int) -> Block | None:
    # strip_ws, not str.strip: the span is what makes a splice lossless, so the
    # whitespace skipped here has to be exactly what blocks.js trims. str.strip()
    # does not remove a BOM and JavaScript's trim() does, which put every span in a
    # BOM-prefixed chapter off by one on the server side only.
    raw = text[start:end]
    stripped = strip_ws(raw)
    if not stripped:
        return None
    lead = len(raw) - len(lstrip_ws(raw))
    trail = len(raw) - len(rstrip_ws(raw))
    return Block(i=i, start=start + lead, end=end - trail, text=stripped)


def split_blocks(text: str) -> list[Block]:
    """Non-blank paragraphs with exact spans into ``text``.

    Spans make the splice lossless: ``text[:start] + new + text[end:]`` preserves
    every separator character byte-for-byte, including the trailing newline and any
    unusual spacing the file happens to carry.
    """
    text = text or ""
    blocks: list[Block] = []
    pos = 0
    for match in _SEP_RE.finditer(text):
        made = _block(text, pos, match.start(), len(blocks))
        if made:
            blocks.append(made)
        pos = match.end()
    made = _block(text, pos, len(text), len(blocks))
    if made:
        blocks.append(made)
    return blocks


def block_texts(text: str) -> list[str]:
    return [b.text for b in split_blocks(text)]


def normalize_paragraph(s: str) -> str:
    """Reduce a model's reply to exactly one paragraph's worth of text.

    Collapsing internal blank lines is what guarantees one-block-in/one-block-out;
    it is deliberately NOT a way of accepting a two-paragraph answer, because the
    caller rejects those separately (joining two paragraphs changes meaning).
    """
    s = (s or "").strip()
    s = re.sub(r"[ \t]+\n", "\n", s)     # trailing spaces before a newline
    s = SEP_RUN_RE.sub("\n", s)          # blank lines -> a single line break
    return s.strip()


def splice_block(text: str, k: int, replacement: str) -> str:
    """Replace paragraph ``k``, preserving the chapter's paragraph count.

    Raises ValueError if the replacement is not exactly one paragraph, or if the
    result would change how many paragraphs the chapter has.
    """
    blocks = split_blocks(text)
    if not 0 <= k < len(blocks):
        raise ValueError(f"there is no paragraph {k} in this chapter")

    # Checked BEFORE normalizing: collapsing blank lines is how exactly one paragraph
    # is guaranteed out, not a licence to quietly accept two in. Joining two
    # paragraphs would change the meaning.
    if len(split_blocks(replacement or "")) > 1:
        raise ValueError("a replacement must be exactly one paragraph")

    new = normalize_paragraph(replacement)
    if not new:
        raise ValueError("the replacement paragraph is empty")

    target = blocks[k]
    out = text[:target.start] + new + text[target.end:]
    if len(split_blocks(out)) != len(blocks):
        raise ValueError("that replacement would change the chapter's paragraph count")
    return out


def locate_block(blocks: list[Block], expected: str, k: int) -> int | None:
    """Resolve an address: the ordinal, proved by the text that was there.

    An exact match at ``k`` wins. Otherwise a UNIQUE match anywhere in the chapter is
    accepted, which rescues the case where an unrelated earlier edit shifted every
    ordinal. Zero or ambiguous matches return None — the caller answers 409 rather
    than replacing the wrong paragraph.
    """
    expected = (expected or "").strip()
    if not expected:
        return None
    if 0 <= k < len(blocks) and blocks[k].text == expected:
        return k
    matches = [b.i for b in blocks if b.text == expected]
    return matches[0] if len(matches) == 1 else None


# ---- aligning to the Korean --------------------------------------------------

def _nonspace(s: str) -> int:
    return len(re.sub(r"\s", "", s or ""))


def _dialogue_anchor(en_blocks: list[str], k: int, ko_paras: list[str],
                     fallback: int) -> int | None:
    """Match on how many quoted paragraphs have gone by.

    In these novels a line of dialogue is its own paragraph and almost never merges
    with its neighbours, which makes this by far the strongest cheap signal.
    """
    en_flags = [bool(_QUOTE_RE.search(b)) for b in en_blocks]
    ko_flags = [bool(_QUOTE_RE.search(p)) for p in ko_paras]
    if sum(en_flags) < 3 or sum(ko_flags) < 3:
        return None

    target = sum(en_flags[:k + 1])
    want_dialogue = en_flags[k]
    best, best_dist, running = None, None, 0
    for j, flag in enumerate(ko_flags):
        running += 1 if flag else 0
        if running == target and flag == want_dialogue:
            dist = abs(j - fallback)
            if best_dist is None or dist < best_dist:
                best, best_dist = j, dist
    return best


def _length_anchor(en_blocks: list[str], k: int, ko_paras: list[str]) -> int | None:
    """Match on how far through the chapter, by character count."""
    en_lens = [_nonspace(b) for b in en_blocks]
    ko_lens = [_nonspace(p) for p in ko_paras]
    total_en, total_ko = sum(en_lens), sum(ko_lens)
    if not total_en or not total_ko:
        return None
    target = sum(en_lens[:k + 1]) / total_en
    running = 0
    for j, length in enumerate(ko_lens):
        running += length
        if running / total_ko >= target:
            return j
    return len(ko_lens) - 1


def align_korean(en_blocks: list[str], k: int, ko_paras: list[str]) -> Alignment:
    """Best guess at which Korean paragraph English paragraph ``k`` came from.

    Never used as a precise answer: the caller passes a WINDOW around this index and
    lets the model do the final matching itself. A wrong point estimate produces a
    wrong translation; a window containing the right paragraph produces a right one.
    """
    n_en, n_ko = len(en_blocks), len(ko_paras)
    if not n_en or not n_ko or not 0 <= k < n_en:
        return Alignment(index=-1, confidence=0.0, method="none", span=0)

    # Equal counts is the common case — the validator bounds paragraph drift to
    # max(2, 5%), so most chapters come back one-for-one.
    if n_en == n_ko:
        return Alignment(index=k, confidence=1.0, method="exact", span=2)

    proportional = min(n_ko - 1, max(0, round(k * n_ko / max(1, n_en))))
    dialogue = _dialogue_anchor(en_blocks, k, ko_paras, proportional)
    by_length = _length_anchor(en_blocks, k, ko_paras)

    if dialogue is not None:
        index, method = dialogue, "dialogue"
    elif by_length is not None:
        index, method = by_length, "length"
    else:
        index, method = proportional, "proportional"

    # Disagreement between two independent estimates, and overall paragraph drift,
    # both eat into confidence.
    spread = abs((dialogue if dialogue is not None else index)
                 - (by_length if by_length is not None else index))
    drift = abs(n_en - n_ko) / max(1, n_ko)
    # The cap has to sit below the "offer retranslate at all" threshold (0.4), so a
    # chapter whose counts bear no relation to each other disables the button rather
    # than translating from a paragraph that isn't the right one.
    confidence = 0.95 - 0.08 * spread - min(0.6, drift)
    if method == "proportional":
        confidence = min(confidence, 0.4)
    confidence = max(0.0, min(1.0, confidence))

    return Alignment(index=index, confidence=round(confidence, 3), method=method,
                     span=(2 if confidence >= 0.8 else 4))


def korean_window(ko_paras: list[str], alignment: Alignment) -> tuple[list[str], int]:
    """The Korean paragraphs to show, and which one in that list is the target."""
    if alignment.index < 0 or not ko_paras:
        return [], 0
    lo = max(0, alignment.index - alignment.span)
    hi = min(len(ko_paras), alignment.index + alignment.span + 1)
    return ko_paras[lo:hi], alignment.index - lo


# ---- checking what came back -------------------------------------------------

def _structure_of(s: str) -> str:
    s = (s or "").lstrip()
    if s.startswith("#"):
        return "heading"
    if s.startswith(">"):
        return "quote"
    if s.startswith("```"):
        return "code"
    if re.match(r"^(?:[-*+]\s|\d+[.)]\s)", s):
        return "list"
    return "paragraph"


def _same(a: str, b: str) -> bool:
    return _WS_RE.sub(" ", (a or "").strip()) == _WS_RE.sub(" ", (b or "").strip())


def check_paragraph_result(before: str, after: str, *, mode: str = "rephrase",
                           source_ko: str | None = None,
                           glossary: list[GlossaryEntry] | None = None,
                           prior: tuple[str, ...] = ()) -> ParagraphCheck:
    """Sanitize and vet one rewritten paragraph.

    Changing the words IS the operation here, so unlike the pronoun fix there is no
    skeleton to compare against. The guard is structural and hygienic instead, and it
    gates STORAGE rather than a file write — a generated variant never touches the
    chapter until the reader picks it.
    """
    reasons: list[str] = []
    warnings: list[str] = []

    raw, _dropped = strip_reasoning(after or "")
    raw = raw.strip()
    if not raw:
        return ParagraphCheck(False, "", ["the model returned nothing usable"], [], False)

    # Counted BEFORE normalizing: normalize_paragraph collapses blank lines to
    # guarantee one paragraph out, which would otherwise silently accept two in.
    # Merging two paragraphs changes the meaning, so it is rejected, never joined.
    blocks = split_blocks(raw)
    if len(blocks) != 1:
        reasons.append(f"the model returned {len(blocks)} paragraphs, not one")

    # Measured BEFORE the echo-stripper runs, so leaked Korean is REPORTED rather
    # than quietly deleted. Relative, not absolute: a chat or SNS paragraph that
    # legitimately keeps ㅋㅋㅋ must survive being rephrased.
    if korean_fraction(raw) > max(0.02, korean_fraction(before) + 0.01):
        reasons.append("the reply left untranslated Korean in the text")

    text, _echoes = remove_korean_echoes(raw)
    text = normalize_paragraph(text)
    if not text:
        reasons.append("the model returned nothing usable")
        return ParagraphCheck(False, "", reasons, [], False)

    if find_leaks(text):
        reasons.append("the reply still has the model's own notes in it")
    if _COMMENTARY_RE.match(text):
        reasons.append("the reply starts with commentary instead of the paragraph")

    if _structure_of(text) != _structure_of(before):
        reasons.append("the reply changed the paragraph's formatting")

    low, high = (0.35, 3.0) if mode == "retranslate" else (0.4, 2.5)
    baseline = _nonspace(before) or 1
    ratio = _nonspace(text) / baseline
    if not low <= ratio <= high:
        reasons.append(f"the reply is {ratio:.1f}× the length of the original")

    # A name that vanished is a WARNING, not a rejection: a good rephrase may
    # legitimately turn a name into a pronoun. But it is exactly what the reader
    # needs to know when choosing between versions.
    for entry in glossary or []:
        name = (entry.english or "").strip()
        if not name or entry.type not in ("name", "place"):
            continue
        if name in before and name not in text:
            warnings.append(f"“{name}” was in the original but not in this version")

    duplicate = _same(text, before) or any(_same(text, p) for p in prior)

    return ParagraphCheck(ok=not reasons, text=text, reasons=reasons,
                          warnings=warnings, duplicate=duplicate)
