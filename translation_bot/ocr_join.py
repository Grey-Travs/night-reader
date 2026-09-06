"""Decide how one scanned page's text joins onto the next.

Photographing a print book, a sentence routinely continues across the page break.
Splitting each page independently would drop a false paragraph break at every one of
those seams — and the translator faithfully preserves paragraph structure, so those
breaks survive into the English and the whole novel reads wrong.

This module makes the decision deterministically wherever the text itself settles it,
so a model call is only needed for genuinely ambiguous seams. It is pure: no I/O, no
model, no config. :func:`propose_join` never returns ``"gap"`` — a missing page can
only be spotted by reading both sides, which is the model's job.

The join is stored on the *following* page (``join_prev``), and applied at build time
rather than by rewriting page text, so re-reading one page never invalidates the
decisions around it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .text_source import _HEADING_RE

# Below this, the seam is worth spending a (batched, text-only) model call on.
LOW_CONFIDENCE = 0.6

# Sentence-final punctuation, including the fullwidth forms print Korean uses.
_TERMINATORS = ".!?…。！？"

# Closing marks that may trail a terminator: 그는 말했다." / 「…이다.」
_CLOSERS = "\"'”’」』）)]】》〉"

# Quote marks that open dialogue in Korean prose. An unclosed one at a page end is a
# strong signal the speech runs on — this catches a lot of real page breaks.
_QUOTE_PAIRS = (("「", "」"), ("『", "』"), ("“", "”"), ("‘", "’"), ("《", "》"), ("〈", "〉"))

_OPENERS = "\"'“‘「『（([【《〈"

_HANGUL_RE = re.compile(r"[가-힣]")


@dataclass(frozen=True)
class Join:
    """How a page joins to the one before it.

    ``kind``:  ``sentence`` (glue with no paragraph break) | ``paragraph`` | ``chapter``
    ``glue``:  ``none`` | ``space`` — only meaningful when ``kind == "sentence"``
    """

    kind: str
    glue: str
    confidence: float
    reason: str

    @property
    def needs_model(self) -> bool:
        return self.confidence < LOW_CONFIDENCE


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _last_line(text: str) -> str:
    for line in reversed((text or "").splitlines()):
        if line.strip():
            return line.strip()
    return ""


def ends_sentence(text: str) -> bool:
    """True when the text's last real character closes a sentence."""
    s = (text or "").rstrip()
    while s and s[-1] in _CLOSERS:
        s = s[:-1].rstrip()
    return bool(s) and s[-1] in _TERMINATORS


def has_unclosed_quote(text: str) -> bool:
    """True when a quotation opened in this text is still open at its end."""
    for opener, closer in _QUOTE_PAIRS:
        if text.count(opener) > text.count(closer):
            return True
    # Straight quotes are unpaired, so parity is the only available signal.
    return text.count('"') % 2 == 1


def looks_like_heading(text: str) -> bool:
    """True when the first non-blank line reads as an in-story chapter heading."""
    head = _first_line(text)
    # A heading is a short standalone line. The regex alone matches a paragraph that
    # merely opens with "3화" and keeps going, which is not a heading.
    return bool(head) and len(head) <= 80 and bool(_HEADING_RE.match(head))


def _glue_for(prev_text: str, prev_meta: dict) -> str:
    """Whether a sentence-join needs a space between the two halves.

    Korean typesetting breaks lines between any two characters, so a page can end in
    the middle of a word. The model reports that from the image (``ends_mid_word``),
    which is far more reliable than guessing from the text alone.
    """
    if prev_meta.get("ends_mid_word"):
        return "none"
    if prev_meta.get("ends_mid_word") is False:
        return "space"
    # Unknown: a trailing hyphen means the word was split; otherwise assume a space,
    # since a spurious join inside a word is harder to read than an extra space.
    return "none" if _last_line(prev_text).endswith(("-", "‐", "‑")) else "space"


def propose_join(prev_text: str, next_text: str,
                 prev_meta: dict | None = None,
                 next_meta: dict | None = None) -> Join:
    """Decide how ``next_text`` continues from ``prev_text``.

    ``prev_meta``/``next_meta`` are page records (or any dict); missing keys are fine,
    the rules simply fall back to what the text alone shows.
    """
    prev_meta = prev_meta or {}
    next_meta = next_meta or {}
    prev_text = prev_text or ""
    next_text = next_text or ""

    # A page with no text (cover, blank, illustration) tells us nothing about the seam.
    if not prev_text.strip() or not next_text.strip():
        return Join("paragraph", "space", 0.3, "a page on this seam has no text")

    # 1. A new chapter starts here. Checked first: a heading ends whatever came before,
    #    however that text happened to end.
    if next_meta.get("heading") or looks_like_heading(next_text):
        return Join("chapter", "space", 0.95, "the next page opens with a chapter heading")

    ends_mid = prev_meta.get("ends_mid_sentence")
    starts_mid = next_meta.get("starts_mid_sentence")

    # 2. Both sides agree the sentence runs on — the strongest signal available.
    if ends_mid and starts_mid:
        return Join("sentence", _glue_for(prev_text, prev_meta), 0.95,
                    "the sentence is unfinished at the page break")

    # 3. Dialogue opened and never closed. A page that ends inside speech continues it.
    if has_unclosed_quote(prev_text) and _first_line(next_text)[:1] not in tuple(_OPENERS):
        return Join("sentence", _glue_for(prev_text, prev_meta), 0.85,
                    "a quotation is still open at the end of the previous page")

    # 4. The previous page closes a sentence and the next opens cleanly.
    if ends_sentence(prev_text) and starts_mid is not True:
        opener = _first_line(next_text)[:1]
        if opener in tuple(_OPENERS) or _HANGUL_RE.match(opener) or opener.isupper():
            return Join("paragraph", "space", 0.8,
                        "the previous page ends a sentence and the next starts one")
        return Join("paragraph", "space", 0.65,
                    "the previous page ends a sentence")

    # 5. No terminator at the page end: the sentence almost certainly runs on, even
    #    though the model did not say so explicitly.
    if not ends_sentence(prev_text):
        if _first_line(next_text)[:1] in tuple(_OPENERS):
            # A new quotation cannot continue an open one, so the previous page most
            # likely lost its closing mark to OCR — but the quote could equally be the
            # object of the running sentence ("그가 조용히" / "「가자」고 말했다").
            # Genuinely ambiguous: let the model read both sides.
            return Join("paragraph", "space", 0.45,
                        "the next page opens a new quotation")
        return Join("sentence", _glue_for(prev_text, prev_meta), 0.7,
                    "the previous page does not end a sentence")

    # 6. Genuinely ambiguous — hand it to the model.
    return Join("paragraph", "space", 0.4, "unclear how these pages join")


def join_text(prev: str, nxt: str, kind: str, glue: str = "space") -> str:
    """Concatenate two page texts according to a join decision.

    This is the operation the false-paragraph-break problem comes down to: a
    ``sentence`` join must not introduce a blank line.
    """
    prev = (prev or "").rstrip()
    nxt = (nxt or "").lstrip()
    if not prev:
        return nxt
    if not nxt:
        return prev
    if kind == "sentence":
        return prev + ("" if glue == "none" else " ") + nxt
    return prev + "\n\n" + nxt
