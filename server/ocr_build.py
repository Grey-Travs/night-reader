"""Turn transcribed pages into chapters.

This is where the seam decisions finally matter. A page break inside a sentence must
join with no blank line, because a blank line is a paragraph break — and the
translator preserves paragraph structure faithfully, so a false break here survives
all the way into the English.

Output is ordinary :class:`~translation_bot.docs_extract.Chapter` objects, which the
caller writes to ``source.json``. From that point an image novel is indistinguishable
from a Google-Doc or pasted-text one, and the whole translation pipeline runs
unchanged.
"""

from __future__ import annotations

from translation_bot.docs_extract import Chapter
from translation_bot.ocr_join import join_text, looks_like_heading
from translation_bot.text_source import make_chapter, split_text_into_chapters

from .pages import APPROVED_STATUSES, STATUS_SKIPPED

BUILD_MODES = ("batch", "heading", "separator", "single")


def usable_pages(doc: dict, *, include: str = "approved") -> list[dict]:
    """Pages that should contribute text, in display order.

    ``include`` is ``"approved"`` (only pages the reader has accepted or edited) or
    ``"all"``. Skipped pages — covers, blanks, duplicates — never contribute.
    """
    out = []
    for page in doc.get("pages", []):
        if page.get("status") == STATUS_SKIPPED:
            continue
        if not (page.get("text") or "").strip():
            continue
        if include == "approved" and page.get("status") not in APPROVED_STATUSES:
            continue
        out.append(page)
    return out


def assemble_pages(pages: list[dict], *, chapter_separator: str = ""
                   ) -> tuple[str, list[str]]:
    """Concatenate page texts honouring each page's ``join_prev``.

    Returns ``(text, warnings)``. A ``gap`` join is joined as a paragraph but reported
    — that is the only place a page you never photographed can surface.
    """
    parts: list[str] = []
    warnings: list[str] = []
    out = ""

    for page in pages:
        text = (page.get("text") or "").strip()
        if not text:
            continue
        if not out:
            out = text
            continue

        kind = page.get("join_prev") or "paragraph"
        glue = page.get("join_glue") or "space"

        if kind == "gap":
            label = page.get("name") or f"page {page.get('seq')}"
            warnings.append(
                f"A page may be missing just before {label} — the text does not "
                f"follow on. Check whether a page went unphotographed."
            )
            kind = "paragraph"

        if kind == "chapter":
            piece = text
            heading = (page.get("heading") or "").strip()
            # The heading is normally already the page's first line. Restore it when
            # it isn't, or the split below has nothing to break on.
            if heading and not looks_like_heading(text):
                piece = f"{heading}\n\n{text}"
            joiner = f"\n\n{chapter_separator}\n\n" if chapter_separator else "\n\n"
            out = out + joiner + piece
        else:
            out = join_text(out, text, kind, glue)

    parts.append(out)
    return "\n".join(p for p in parts if p), warnings


def _batch_runs(pages: list[dict]) -> list[list[dict]]:
    """Group pages into contiguous runs sharing a batch id.

    Contiguous rather than by-id on purpose: if the reader has dragged pages around,
    the arrangement they made is the truth, not the order things were uploaded in.
    """
    runs: list[list[dict]] = []
    current_batch = object()
    for page in pages:
        batch = page.get("batch")
        if not runs or batch != current_batch:
            runs.append([])
            current_batch = batch
        runs[-1].append(page)
    return runs


def _batch_title(run: list[dict], batches: dict[str, str], index: int) -> str:
    label = (batches.get(run[0].get("batch", ""), "") or "").strip()
    if label:
        return label
    heading = (run[0].get("heading") or "").strip()
    if heading:
        return heading
    return f"Chapter {index}"


def build_chapters(doc: dict, *, mode: str = "batch", separator: str = "---",
                   include: str = "approved", start_index: int = 1
                   ) -> tuple[list[Chapter], list[str], dict]:
    """Build chapters from a page manifest.

    ``mode``:
    - ``batch``     one upload batch becomes one chapter. Used when pages were
                    photographed a chapter at a time. Does NOT round-trip through the
                    text splitter: a batch may legitimately contain no heading and no
                    separator, and would otherwise collapse into a single chapter.
    - ``heading``   assemble everything, then split on detected chapter headings.
    - ``separator`` same, splitting on a marker line.
    - ``single``    the whole novel is one chapter.

    Returns ``(chapters, warnings, page_map)`` where ``page_map`` maps a chapter index
    to the page ids that produced it.
    """
    if mode not in BUILD_MODES:
        raise ValueError(f"unknown build mode: {mode}")

    pages = usable_pages(doc, include=include)
    if not pages:
        return [], [], {}

    batches = {b.get("id", ""): b.get("label", "") for b in doc.get("batches", [])}

    if mode == "batch":
        chapters: list[Chapter] = []
        warnings: list[str] = []
        page_map: dict[str, list[str]] = {}
        for position, run in enumerate(_batch_runs(pages)):
            index = start_index + len(chapters)
            body, run_warnings = assemble_pages(run)
            warnings.extend(run_warnings)
            # A run's FIRST page has no predecessor inside the run, so assemble_pages
            # never looks at its join — but a gap landing exactly on a chapter
            # boundary is still a page that was never photographed, and is the case a
            # reader is least likely to notice by eye.
            if position and run[0].get("join_prev") == "gap":
                label = run[0].get("name") or f"page {run[0].get('seq')}"
                warnings.append(
                    f"A page may be missing just before {label}, where this chapter "
                    f"starts. Check whether a page went unphotographed."
                )
            chapter = make_chapter(index, _batch_title(run, batches, index), body)
            if chapter is None:
                continue
            chapters.append(chapter)
            page_map[str(index)] = [p.get("id") for p in run]
        return chapters, warnings, page_map

    text, warnings = assemble_pages(
        pages, chapter_separator=(separator if mode == "separator" else ""))
    chapters = split_text_into_chapters(text, mode, separator)
    # Re-index onto the caller's starting point (an append leaves earlier chapters,
    # and their translations, untouched).
    for offset, chapter in enumerate(chapters):
        chapter.index = start_index + offset
    # A whole-novel split can't be attributed to individual pages, so the map only
    # records which pages went in at all.
    page_map = {"*": [p.get("id") for p in pages]}
    return chapters, warnings, page_map
