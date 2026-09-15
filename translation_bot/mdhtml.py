"""The one Markdown to HTML converter, shared by the EPUB writer and the posting payload.

The translator emits a deliberately small Markdown subset — paragraphs, headings,
emphasis, a ``***`` scene break, the occasional blockquote — so this handles exactly that
and nothing else. There is no Markdown dependency in this repo on purpose (see the EPUB
writer's docstring), and adding one to gain CommonMark fidelity the translator never
produces would not pay for itself.

``web/src/mdhtml.js`` is a deliberate mirror, because the reader's Copy button has to
produce the same HTML inside the browser. ``tests/test_mdhtml_parity.py`` runs both over
the same fixtures: when they drift, what the user sees when they copy a chapter stops
matching what actually gets published, which is the sort of difference nobody notices
until a reader points at it.

Two knobs, one per consumer:

* ``xhtml`` closes the void tags (``<br/>``, ``<hr/>``) for the EPUB, which must be valid
  XML. The web editors want the HTML spelling.
* ``strip_part_markers`` drops a block that is nothing but a number. Those are the source
  novel's own section numbering, and they are **invisible in the reader** — a lone "33."
  is an empty ordered-list item in Markdown and the reading CSS gives lists no styling —
  so they have never been seen. 44 of them exist across 7 of these novels, 21 at the very
  top of a chapter. Publishing them as a visible ``<p>33.</p>`` would put a number above
  the text that does not even match the chapter's own number.
"""

from __future__ import annotations

import html
import re

from .textsplit import SEP_RE, strip_ws

# A block that is nothing but a number: "29" / "29.". blocks.js::isPlainParagraph already
# excludes this shape as "a standalone part marker", so the codebase already agrees these
# are not prose.
_BARE_NUMBER_RE = re.compile(r"^\d{1,4}\.?$")

# A thematic break. Spelled as the JS side spells it, rather than the tighter
# "three or more of one character", so the two cannot disagree on "- - -".
_RULE_RE = re.compile(r"^(?:[-*_] *){3,}$")

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_QUOTE_PREFIX_RE = re.compile(r"^[ \t]{0,3}>[ \t]?", re.MULTILINE)


def _inline(text: str) -> str:
    """Escape, then apply the emphasis the translator actually emits.

    ``quote=False`` so that only ``&``, ``<`` and ``>`` are escaped, exactly as the JS
    side does. Escaping apostrophes as well would be harmless in a text node, but these
    chapters are full of them ("I'll", "don't") and every one would make the two outputs
    differ byte for byte — and the parity test is the only thing keeping them honest.
    """
    out = html.escape(text, quote=False)
    # Order matters: the three-star form has to match before the two-star form, or
    # "***word***" comes out as bold with stray stars around it.
    out = re.sub(r"\*\*\*(.+?)\*\*\*", r"<strong><em>\1</em></strong>", out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"\*(.+?)\*", r"<em>\1</em>", out)
    out = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<em>\1</em>", out)
    # Inline code is dropped to plain text rather than rendered: the translator never
    # emits it deliberately, so a stray backtick is punctuation, not markup.
    out = re.sub(r"`([^`]+)`", r"\1", out)
    return out


def markdown_to_html(md: str, *, xhtml: bool = False,
                     strip_part_markers: bool = False) -> str:
    """Render the translator's Markdown subset as a bare sequence of block elements.

    No wrapper, no classes, no inline styles — which is why the result pastes cleanly
    into anything, and why a ProseMirror/TipTap-style editor parses it with its default
    schema and no custom rules.
    """
    br = "<br/>" if xhtml else "<br>"
    rule = "<hr/>" if xhtml else "<hr>"
    parts: list[str] = []
    for raw in SEP_RE.split((md or "").replace("\r\n", "\n")):
        block = strip_ws(raw)
        if not block:
            continue
        if strip_part_markers and _BARE_NUMBER_RE.match(block):
            continue
        if _RULE_RE.match(block):
            parts.append(rule)
            continue
        heading = _HEADING_RE.match(block)
        if heading:
            level = min(len(heading.group(1)), 6)
            parts.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
            continue
        if block.startswith(">"):
            inner = br.join(
                _inline(line) for line in _QUOTE_PREFIX_RE.sub("", block).split("\n")
            )
            parts.append(f"<blockquote><p>{inner}</p></blockquote>")
            continue
        parts.append("<p>" + br.join(_inline(line) for line in block.split("\n")) + "</p>")
    return "\n".join(parts)


_LEADING_HEADING_RE = re.compile(r"^﻿?\s*#{1,6}[ \t]+[^\n]*(?:\n+|$)")


def strip_leading_heading(md: str) -> str:
    """Drop a leading ``# 12`` title block so only the prose remains.

    Only the very first block, and only when it is a heading — the reader's Copy button
    has always done this, and the posting payload wants the same thing, because the
    publishing site puts the chapter's name in its own field.
    """
    return _LEADING_HEADING_RE.sub("", md or "")
