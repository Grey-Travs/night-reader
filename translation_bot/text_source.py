"""Build chapters from pasted/raw text instead of a Google Doc.

Lets a novel come from the clipboard or a ``.txt`` file (no Google account or
tab-per-chapter Doc required). The same :class:`~translation_bot.docs_extract.Chapter`
objects are produced, so the rest of the engine (classify / translate / validate /
read) works unchanged.

Splitting modes:
- ``separator``: a chapter break is any line equal to a marker (default ``---``);
  also accepts ``===``/``***``/``###`` runs out of the box.
- ``heading``:   a chapter break is a line that looks like a chapter heading
  (``Chapter 3``, ``3화``, ``제3화``, ``프롤로그`` …); that line becomes the title.
- ``single``:    the whole text is one chapter.
"""

from __future__ import annotations

import re

from .docs_extract import Chapter, _strip_invisibles

# A line that introduces a chapter (English or Korean web-novel conventions).
_HEADING_RE = re.compile(
    r"^\s*(?:"
    r"(?:chapter|ch\.?|episode|ep\.?)\s*\d+"          # Chapter 3 / Ch. 3 / Episode 3
    r"|제?\s*\d+\s*[화장권부]"                          # 제3화 / 3화 / 3장 / 3권
    r"|\d+\s*화"                                        # 3화
    r"|프롤로그|에필로그|서장|종장"                      # prologue/epilogue/etc.
    r")\s*[:.\-–]?\s*.*$",
    re.IGNORECASE,
)

# A separator line: a run of the same marker char, or a user-chosen marker.
_DEFAULT_SEP_RE = re.compile(r"^\s*(?:-{3,}|={3,}|\*{3,}|#{3,}|_{3,})\s*$")


def _paragraphs(block: str) -> list[str]:
    """Split a chapter block into paragraphs on blank lines, dropping empties."""
    block = _strip_invisibles(block)
    paras = [p.strip() for p in re.split(r"\n\s*\n", block)]
    return [p for p in paras if p]


def _first_line_title(block: str, fallback: str) -> tuple[str, str]:
    """If the block's first line is a short heading-like line, use it as the title
    and drop it from the body. Returns (title, body)."""
    lines = block.lstrip("\n").split("\n", 1)
    head = lines[0].strip()
    if head and len(head) <= 80 and (_HEADING_RE.match(head) or len(head) <= 40):
        body = lines[1] if len(lines) > 1 else ""
        return head, body
    return fallback, block


def _make(index: int, title: str, body: str) -> Chapter | None:
    paras = _paragraphs(body)
    if not paras:
        return None
    return Chapter(index=index, title=title.strip() or f"Chapter {index}", paragraphs=paras)


def split_text_into_chapters(
    text: str, mode: str = "separator", separator: str = "---"
) -> list[Chapter]:
    """Parse raw text into ordered chapters. Indices are 1-based, in document order."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return []

    if mode == "single":
        ch = _make(1, "Chapter 1", text)
        return [ch] if ch else []

    if mode == "heading":
        chapters: list[Chapter] = []
        buf: list[str] = []
        title: str | None = None

        def flush() -> None:
            nonlocal buf, title
            if title is None and not any(s.strip() for s in buf):
                buf = []
                return
            ch = _make(len(chapters) + 1, title or f"Chapter {len(chapters) + 1}", "\n".join(buf))
            if ch:
                chapters.append(ch)
            buf = []

        for line in text.split("\n"):
            if _HEADING_RE.match(line) and line.strip():
                flush()
                title = line.strip()
            else:
                buf.append(line)
        flush()
        if chapters:
            return _reindex(chapters)
        # No headings found — fall back to one chapter rather than returning nothing.
        ch = _make(1, "Chapter 1", text)
        return [ch] if ch else []

    # mode == "separator" (default)
    marker = (separator or "").strip()
    custom_re = re.compile(r"^\s*" + re.escape(marker) + r"\s*$") if marker else None

    def is_sep(line: str) -> bool:
        if custom_re and custom_re.match(line):
            return True
        return bool(_DEFAULT_SEP_RE.match(line))

    blocks: list[str] = []
    cur: list[str] = []
    for line in text.split("\n"):
        if is_sep(line):
            blocks.append("\n".join(cur))
            cur = []
        else:
            cur.append(line)
    blocks.append("\n".join(cur))

    chapters = []
    for block in blocks:
        if not block.strip():
            continue
        title, body = _first_line_title(block, f"Chapter {len(chapters) + 1}")
        ch = _make(len(chapters) + 1, title, body)
        if ch:
            chapters.append(ch)
    if not chapters:  # no separators present -> single chapter
        ch = _make(1, "Chapter 1", text)
        return [ch] if ch else []
    return _reindex(chapters)


def _reindex(chapters: list[Chapter]) -> list[Chapter]:
    for i, ch in enumerate(chapters, start=1):
        ch.index = i
    return chapters


def chapters_to_records(chapters: list[Chapter]) -> list[dict]:
    """Serialize chapters for on-disk storage (source.json)."""
    return [{"title": c.title, "paragraphs": c.paragraphs} for c in chapters]


def records_to_chapters(records: list[dict]) -> list[Chapter]:
    """Rebuild chapters from stored records, in order."""
    out: list[Chapter] = []
    for i, rec in enumerate(records or [], start=1):
        out.append(Chapter(index=i, title=str(rec.get("title") or f"Chapter {i}"),
                           paragraphs=list(rec.get("paragraphs") or [])))
    return out
