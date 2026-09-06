"""Read Korean novel pages out of photographs, using the same Claude subscription.

The Claude Agent SDK has no image content block: the only way to hand the agent a
picture is a path it opens with the ``Read`` tool. So every call here opts into that
one tool via :meth:`Translator._call`, scoped with ``cwd``/``add_dirs`` to the folder
the image lives in. Nothing else is unblocked, and the opt-in does not persist to the
next call — :mod:`tests.test_ocr_options` guards that.

Kept out of :mod:`translation_bot.translator` deliberately: that module's
``_BLOCKED_TOOLS`` is a safety invariant, and mixing an image-reading path into the
translation domain invites someone to widen it later.

Three calls live here:
- :func:`transcribe_page`   one photo  -> Korean text + how it joins to its neighbours
- :func:`verify_page`       one photo  -> discrepancies against an existing transcription
- :func:`stitch_boundaries` many seams -> how each pair of pages joins (text-only, batched)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .prompts import (
    OCR_STITCH_PROMPT,
    OCR_VERIFY_PROMPT,
    PAGE_META_DELIMITER,
    build_ocr_prompt,
)
from .sanitize import strip_reasoning
from .translator import StreamHooks, Translator

# The Read is a turn of its own, so a single-turn cap would abort the run before the
# model ever answers. The existing aux checks use 8 for the same reason.
_OCR_MAX_TURNS = 6

_VALID_CONFIDENCE = ("high", "medium", "low")
_VALID_JOINS = ("sentence", "paragraph", "chapter", "gap")
_VALID_ISSUE_KINDS = ("missing", "extra", "wrong", "leak")

# How much of each page to show the stitcher. Enough to judge a seam, small enough
# that ~25 boundaries stay a couple of KB.
_SEAM_CHARS = 200


@dataclass
class PageText:
    """One transcribed page."""

    text: str = ""
    confidence: str = "low"
    heading: str | None = None
    starts_mid_sentence: bool = False
    ends_mid_sentence: bool = False
    ends_mid_word: bool = False
    notes: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0


@dataclass
class VerifyIssue:
    kind: str
    where: str
    page_says: str
    suggest: str


@dataclass
class VerifyResult:
    verdict: str = "ok"  # ok | discrepancies
    issues: list[VerifyIssue] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0


@dataclass
class JoinDecision:
    i: int
    join: str
    glue: str = "space"
    note: str = ""


# ---- tolerant parsing --------------------------------------------------------
# Same discipline as translator.parse_response: a malformed reply degrades to a
# usable result rather than raising. A page we transcribed but can't classify is
# still worth keeping; the UI flags it for a human.

def _json_slice(text: str, open_ch: str, close_ch: str):
    start, end = text.find(open_ch), text.rfind(close_ch)
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return bool(value)


def _as_notes(value) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def split_page_meta(raw: str) -> tuple[str, dict | None]:
    """Split a transcription response into (page text, metadata dict or None)."""
    if PAGE_META_DELIMITER in raw:
        prose, _, tail = raw.partition(PAGE_META_DELIMITER)
        return prose.strip(), _json_slice(tail, "{", "}")
    # No delimiter: the model may still have emitted a trailing object. Only treat a
    # trailing brace-block as metadata, never text that merely contains braces.
    stripped = raw.rstrip()
    if stripped.endswith("}"):
        start = stripped.rfind("\n{")
        if start != -1:
            meta = _json_slice(stripped[start:], "{", "}")
            if meta is not None:
                return stripped[:start].strip(), meta
    return raw.strip(), None


def parse_page_response(raw: str) -> PageText:
    """Turn a raw transcription reply into a :class:`PageText`."""
    prose, meta = split_page_meta(raw)
    # A "let me look at the image..." preamble must never reach the stored source.
    prose, _dropped = strip_reasoning(prose)
    page = PageText(text=prose.strip())

    if meta is None:
        page.confidence = "low"
        page.notes = ["The page was read, but its quality could not be assessed."]
        return page

    confidence = str(meta.get("confidence", "")).strip().lower()
    page.confidence = confidence if confidence in _VALID_CONFIDENCE else "low"

    heading = meta.get("heading")
    page.heading = str(heading).strip() or None if heading else None

    page.starts_mid_sentence = _as_bool(meta.get("starts_mid_sentence"))
    page.ends_mid_sentence = _as_bool(meta.get("ends_mid_sentence"))
    page.ends_mid_word = _as_bool(meta.get("ends_mid_word"))
    page.notes = _as_notes(meta.get("notes"))

    # An empty transcription is a real outcome (cover, blank page, illustration), but
    # it should never look confident.
    if not page.text:
        page.confidence = "low"
        if not page.notes:
            page.notes = ["No story text was found on this page."]
    return page


def parse_verify_response(raw: str, transcription: str) -> VerifyResult:
    """Turn a raw proof-reading reply into a :class:`VerifyResult`."""
    rows = _json_slice(raw, "[", "]")
    if not isinstance(rows, list):
        return VerifyResult(verdict="ok")

    issues: list[VerifyIssue] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("kind", "")).strip().lower()
        if kind not in _VALID_ISSUE_KINDS:
            kind = "wrong"
        where = str(row.get("where") or "")
        suggest = str(row.get("suggest") or "")
        # A `where` that isn't in the transcription verbatim can't be applied safely.
        # Keep the issue (the human still wants to see it) but blank the anchor so the
        # UI disables Apply rather than fuzzy-matching into the user's source text.
        if where and where not in transcription:
            where = ""
        if not where and not suggest and not str(row.get("page_says") or ""):
            continue
        issues.append(VerifyIssue(kind=kind, where=where,
                                  page_says=str(row.get("page_says") or ""),
                                  suggest=suggest))
    return VerifyResult(verdict="discrepancies" if issues else "ok", issues=issues)


def parse_stitch_response(raw: str, count: int) -> list[JoinDecision]:
    """Turn a raw seam-adjudication reply into decisions, indexed 1..count."""
    rows = _json_slice(raw, "[", "]")
    if not isinstance(rows, list):
        return []
    out: list[JoinDecision] = []
    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            i = int(row.get("i"))
        except (TypeError, ValueError):
            continue
        if not (1 <= i <= count) or i in seen:
            continue
        join = str(row.get("join", "")).strip().lower()
        if join not in _VALID_JOINS:
            continue
        glue = str(row.get("glue", "")).strip().lower()
        seen.add(i)
        out.append(JoinDecision(i=i, join=join,
                                glue=glue if glue in ("none", "space") else "space",
                                note=str(row.get("note") or "")))
    return out


# ---- the calls ---------------------------------------------------------------

def _read_image_call(tr: Translator, image_path: Path, system_text: str,
                     user_text: str, hooks: StreamHooks | None = None):
    """One Read-enabled agent call, sandboxed to the image's own folder."""
    folder = image_path.parent
    return tr._call(system_text, user_text, max_turns=_OCR_MAX_TURNS, hooks=hooks,
                    tools=["Read"], cwd=folder, add_dirs=[folder])


def transcribe_page(tr: Translator, image_path: Path, *,
                    hint: str | None = None,
                    hooks: StreamHooks | None = None) -> PageText:
    """Transcribe one page image into Korean text plus its seam metadata."""
    image_path = Path(image_path).resolve()
    user_text = (
        "Transcribe the Korean novel page in this image.\n\n"
        f"Absolute path: {image_path}\n\n"
        "Read that file, then output the page text followed by the "
        f"{PAGE_META_DELIMITER} block."
    )
    raw, usage, cost = _read_image_call(
        tr, image_path, build_ocr_prompt(hint=hint), user_text, hooks)
    page = parse_page_response(raw)
    page.usage = usage
    page.cost_usd = cost
    return page


def verify_page(tr: Translator, image_path: Path, text: str,
                hooks: StreamHooks | None = None) -> VerifyResult:
    """Proof-read an existing transcription against its photograph."""
    image_path = Path(image_path).resolve()
    user_text = (
        f"Absolute path of the page image: {image_path}\n\n"
        "Read that image, then compare it against this transcription:\n\n"
        "---\n"
        f"{text}\n"
        "---\n\n"
        "Report only real discrepancies, as the JSON array described above."
    )
    raw, usage, cost = _read_image_call(
        tr, image_path, OCR_VERIFY_PROMPT, user_text, hooks)
    result = parse_verify_response(raw, text)
    result.usage = usage
    result.cost_usd = cost
    return result


def format_seams(pairs: list[tuple[str, str]], *, seam_chars: int = _SEAM_CHARS) -> str:
    """Render page-end/page-start pairs as a numbered list for the stitcher."""
    blocks = []
    for i, (prev_text, next_text) in enumerate(pairs, start=1):
        tail = (prev_text or "").rstrip()[-seam_chars:]
        head = (next_text or "").lstrip()[:seam_chars]
        blocks.append(
            f"### Boundary {i}\n"
            f"END of the previous page:\n...{tail}\n\n"
            f"START of the next page:\n{head}...\n"
        )
    return "\n".join(blocks)


def stitch_boundaries(tr: Translator, pairs: list[tuple[str, str]],
                      hooks: StreamHooks | None = None
                      ) -> tuple[list[JoinDecision], dict, float]:
    """Adjudicate several page seams in one text-only call.

    Batched, unlike transcription: each seam is a couple of hundred characters and
    they are independent, so one call settles ~25 of them. Returns the decisions the
    model actually made — a seam it skipped simply keeps its deterministic guess.
    """
    if not pairs:
        return [], {}, 0.0
    user_text = (
        "Decide how each of these page boundaries joins.\n\n"
        + format_seams(pairs)
    )
    raw, usage, cost = tr._call(OCR_STITCH_PROMPT, user_text, max_turns=8, hooks=hooks)
    return parse_stitch_response(raw, len(pairs)), usage, cost
