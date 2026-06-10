"""Extract paragraph-segmented Korean text from a Google Doc, one chapter per tab.

Uses ``documents.get(..., includeTabsContent=True)`` so ``document.tabs`` is
populated. Each *top-level* tab is one chapter, in order. Nested ``childTabs``
are flattened depth-first into their parent chapter (configurable).

Verified against the live Docs API reference: with ``includeTabsContent=true``,
content lives under ``tab.documentTab.body.content`` and nested tabs under
``tab.childTabs`` (e.g. ``document.tabs[2].childTabs[0].childTabs[1].documentTab.body``).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

# Quote characters that mark a dialogue line (Korean prose uses several).
_QUOTE_CHARS = "\"“”「」『』"
_QUOTE_RE = re.compile(f"[{re.escape(_QUOTE_CHARS)}]")

# Hangul syllables — used to tell a Korean source tab from an already-English one.
_HANGUL_RE = re.compile(r"[가-힣]")


def hangul_fraction(text: str) -> float:
    """Fraction of non-whitespace characters that are Hangul syllables."""
    stripped = re.sub(r"\s", "", text)
    if not stripped:
        return 0.0
    return len(_HANGUL_RE.findall(text)) / len(stripped)

# Zero-width / invisible formatting characters — often a copy-protection watermark
# pasted into web-novel text. They carry no meaning but inflate char/paragraph
# counts and confuse the model, so strip them at extraction.
_INVISIBLE_RE = re.compile(
    "[­͏؜ᅟᅠ឴឵᠎"
    "​‌‍‎‏"
    "‪‫‬‭‮"
    "⁠⁡⁢⁣⁤"
    "⁦⁧⁨⁩⁪⁫⁬⁭⁮⁯"
    "ㅤ﻿ﾠ]"
)


def _strip_invisibles(text: str) -> str:
    return _INVISIBLE_RE.sub("", text)


@dataclass
class ChapterMetrics:
    paragraph_count: int
    dialogue_count: int
    char_count: int
    content_hash: str


@dataclass
class Chapter:
    index: int  # 1-based, in tab order
    title: str
    paragraphs: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(self.paragraphs)

    @property
    def metrics(self) -> ChapterMetrics:
        text = self.text
        return ChapterMetrics(
            paragraph_count=len(self.paragraphs),
            dialogue_count=sum(1 for p in self.paragraphs if _QUOTE_RE.search(p)),
            char_count=len(re.sub(r"\s", "", text)),  # non-whitespace chars
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )


def fetch_document(docs_service, doc_id: str) -> dict:
    """Fetch the document with tab content included."""
    return (
        docs_service.documents()
        .get(documentId=doc_id, includeTabsContent=True)
        .execute()
    )


def _read_paragraph(paragraph: dict) -> str:
    """Concatenate the textRuns of a single paragraph into a plain string."""
    parts: list[str] = []
    for element in paragraph.get("elements", []):
        text_run = element.get("textRun")
        if text_run and "content" in text_run:
            parts.append(text_run["content"])
    return _strip_invisibles("".join(parts)).strip("\n")


def _read_structural_elements(elements: list[dict]) -> list[str]:
    """Walk body.content structural elements into a list of paragraph strings.

    Handles paragraphs and tables (cell contents are recursed into). Blank
    paragraphs are dropped so the blank-line separation between paragraphs stays
    meaningful.
    """
    paragraphs: list[str] = []
    for element in elements:
        if "paragraph" in element:
            text = _read_paragraph(element["paragraph"])
            if text.strip():
                paragraphs.append(text)
        elif "table" in element:
            for row in element["table"].get("tableRows", []):
                for cell in row.get("tableCells", []):
                    paragraphs.extend(_read_structural_elements(cell.get("content", [])))
        # tableOfContents / sectionBreak / etc. carry no prose — skip.
    return paragraphs


def _tab_body_paragraphs(tab: dict) -> list[str]:
    body = tab.get("documentTab", {}).get("body", {})
    return _read_structural_elements(body.get("content", []))


def _tab_title(tab: dict, fallback_index: int) -> str:
    title = tab.get("tabProperties", {}).get("title", "").strip()
    return title or f"Chapter {fallback_index}"


def _collect_child_paragraphs(tab: dict) -> list[str]:
    """Depth-first flatten of a tab's own body plus all descendant child tabs."""
    paragraphs = list(_tab_body_paragraphs(tab))
    for child in tab.get("childTabs", []):
        paragraphs.extend(_collect_child_paragraphs(child))
    return paragraphs


def extract_chapters(document: dict, *, flatten_child_tabs: bool = True) -> list[Chapter]:
    """Turn a fetched Document into ordered chapters (one per top-level tab)."""
    tabs = document.get("tabs", [])
    if not tabs:
        raise ValueError(
            "Document has no tabs. Was it fetched with includeTabsContent=True, "
            "and does the doc actually use tabs (one chapter per tab)?"
        )

    chapters: list[Chapter] = []
    for i, tab in enumerate(tabs, start=1):
        if flatten_child_tabs:
            paragraphs = _collect_child_paragraphs(tab)
        else:
            paragraphs = _tab_body_paragraphs(tab)
        chapters.append(Chapter(index=i, title=_tab_title(tab, i), paragraphs=paragraphs))
    return chapters
