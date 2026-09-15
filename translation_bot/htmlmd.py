"""HTML back to Markdown — the inverse of :mod:`translation_bot.mdhtml`.

Why this exists
---------------
Novels that are already published on the site but absent from Night Reader have to be got
into the library somehow, and until now that meant copying every posted chapter into a
Google Doc by hand. The site hands back each chapter as HTML; Night Reader stores Markdown.
This is the one place that gap is closed.

Deliberately narrow, and deliberately paranoid
----------------------------------------------
``mdhtml.markdown_to_html`` emits exactly eight tags and **no attributes at all**: ``p``,
``h1``-``h6``, ``hr``, ``br``, ``blockquote``, ``strong``, ``em``. A real novel measured off
the site came back as ``p``, ``em``, ``hr`` and a bare ``span`` — so for chapters this app
posted, the job is nearly an identity. But the chapters actually worth importing were typed
into the site's own rich-text editor, which can emit lists, links, underline, colour spans
and non-breaking spaces, and none of those have a Markdown spelling here.

So the rule is **never corrupt prose**. Losing bold is a nuisance; mangling a sentence is
not recoverable. Anything unrepresentable keeps its text, drops its markup, and is *named*
in the returned list of losses, so an import can report "3 chapters contained images"
rather than quietly dropping them.

Checking its own work
---------------------
``round_trips`` re-renders the Markdown through ``markdown_to_html`` and compares. That is
a genuine per-chapter proof of faithfulness using the converter that already exists, and it
catches the one class of damage that is otherwise invisible: prose containing characters
Markdown reads as markup. ``<p>*not emphasis*</p>`` converts to ``*not emphasis*``, which
renders back as ``<em>``. There is no escape hatch — ``markdown_to_html`` has no ``\\*``
support — so the honest answer is to detect it and say so.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from .docs_extract import _strip_invisibles
from .mdhtml import markdown_to_html
from .textsplit import strip_ws

# Tags that end the current block and begin a new one.
_BLOCKS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote",
           "section", "article"}

# Inline markup with an exact Markdown spelling here.
_EMPHASIS = {"strong": "**", "b": "**", "em": "*", "i": "*"}

# Carries no meaning worth keeping: unwrap silently, keep the text. A bare attribute-less
# `span` is what the site's editor leaves behind and means nothing at all; `font` is the
# same idea from an older editor.
_UNWRAP_QUIETLY = {"span", "font", "a"} | {"tbody", "thead", "tr", "ul", "ol"}

# Unwrapped too, but worth reporting: the text survives and the formatting does not.
_UNWRAP_LOUDLY = {
    "u": "underline", "s": "strikethrough", "del": "strikethrough",
    "ins": "inserted text", "mark": "highlighting", "code": "code formatting",
    "sub": "subscript", "sup": "superscript", "small": "small text",
}

# Dropped whole, content and all. None of these is prose.
_DISCARD = {"script", "style", "head", "title", "noscript"}

# Reported by name when seen, because losing one silently would be losing content.
_NAMED_LOSS = {"img": "an image", "table": "a table", "iframe": "an embed",
               "video": "a video", "audio": "audio", "svg": "a drawing"}

_WS_RUN = re.compile(r"[^\S\n]+")


class _Reader(HTMLParser):
    """Accumulates Markdown blocks from a stream of HTML tokens.

    Tolerant by construction: unknown tags fall through to "keep the text, drop the tag",
    and unclosed tags simply never close. The input is somebody else's editor output, so
    refusing to parse it is not an option.
    """

    def __init__(self) -> None:
        # convert_charrefs handles the full HTML5 entity table for us, so `&amp;` and
        # `&hellip;` arrive as characters and never need a second unescape pass.
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.losses: set[str] = set()
        self._buf: list[str] = []
        self._open: list[str] = []
        self._quote = 0
        self._heading = 0
        self._list_item = False
        self._discard = 0

    # -- block assembly ----------------------------------------------------

    def _flush(self) -> None:
        # Close anything still open. An editor that emitted an <em> and never
        # closed it would otherwise leave a lone marker in the prose, which is
        # the one thing this module promises not to do.
        for marker in reversed(self._open):
            self._buf.append(marker)
        self._open = []
        raw = "".join(self._buf)
        self._buf = []
        # Collapse horizontal whitespace runs but keep the newlines that <br> put in:
        # inside a block a newline is meaningful to Night Reader's Markdown, and becomes a
        # <br> again on the way out.
        lines = [_WS_RUN.sub(" ", line).strip() for line in raw.split("\n")]
        text = "\n".join(lines).strip("\n")
        if not text.strip():
            return
        if self._heading:
            text = f"{'#' * min(self._heading, 6)} {text.replace(chr(10), ' ')}"
        elif self._list_item:
            # There is no list branch in markdown_to_html, so this renders as an ordinary
            # paragraph beginning with a dash. That keeps what the author typed visible
            # rather than inventing a structure this app cannot render.
            text = f"- {text}"
        if self._quote:
            text = "\n".join(f"> {line}" for line in text.split("\n"))
        self.blocks.append(text)

    # -- tokens ------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs) -> None:
        if self._discard:
            return
        if tag in _DISCARD:
            self._discard += 1
            return
        if tag in _NAMED_LOSS:
            self.losses.add(_NAMED_LOSS[tag])
            return
        if tag == "br":
            self._buf.append("\n")
            return
        if tag == "hr":
            self._flush()
            self.blocks.append("---")
            return
        if tag in _EMPHASIS:
            self._buf.append(_EMPHASIS[tag])
            self._open.append(_EMPHASIS[tag])
            return
        if tag in _UNWRAP_LOUDLY:
            self.losses.add(_UNWRAP_LOUDLY[tag])
            return
        if tag in _UNWRAP_QUIETLY:
            return
        if tag in _BLOCKS:
            self._flush()
            if tag == "blockquote":
                self._quote += 1
            elif tag.startswith("h") and tag[1:].isdigit():
                self._heading = int(tag[1:])
            elif tag == "li":
                self._list_item = True

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in _DISCARD:
            self._discard = max(0, self._discard - 1)
            return
        if self._discard:
            return
        if tag in _EMPHASIS:
            marker = _EMPHASIS[tag]
            # A stray close with nothing open is malformed markup; adding a
            # marker for it would invent emphasis that was never there.
            if marker in self._open:
                self._open.remove(marker)
                self._buf.append(marker)
            return
        if tag in _BLOCKS:
            self._flush()
            if tag == "blockquote":
                self._quote = max(0, self._quote - 1)
            elif tag.startswith("h") and tag[1:].isdigit():
                self._heading = 0
            elif tag == "li":
                self._list_item = False

    def handle_data(self, data: str) -> None:
        if self._discard:
            return
        self._buf.append(data)

    def close(self) -> None:  # noqa: D102 - inherited
        super().close()
        # Text after the last closing tag, or a document with no tags at all.
        self._flush()


def html_to_markdown(html: str) -> tuple[str, list[str]]:
    """Convert one chapter of HTML to Night Reader's Markdown.

    Returns the Markdown and a sorted list of anything that could not be represented, in
    plain words ("an image", "underline"), for an import report to pass on.
    """
    if not (html or "").strip():
        return "", []
    reader = _Reader()
    # A non-breaking space is a space as far as prose is concerned, and leaving it in makes
    # paragraphs that look identical compare unequal. The zero-width family is stripped for
    # the reason docs_extract already strips it at extraction: it inflates counts and
    # confuses the model while carrying no meaning.
    reader.feed(_strip_invisibles((html or "").replace(" ", " ")))
    reader.close()
    blocks = [b for b in (strip_ws(b) for b in reader.blocks) if b]
    return "\n\n".join(blocks), sorted(reader.losses)


# Markup that carries no meaning for prose, and so must not count as a difference
# when comparing two renderings of the same chapter.
_MEANINGLESS = re.compile(
    r'</?(?:span|font|a|div|section|article)\b[^>]*>', re.IGNORECASE)


def _normalize(html: str) -> str:
    """Whitespace-insensitive, meaning-preserving form for comparing two renderings.

    Two differences have to be forgiven here or this cries wolf. The site stores its
    paragraphs run together with no newlines while ``markdown_to_html`` joins blocks
    with one; and an attribute-less ``<span>`` is deliberately unwrapped, which changes
    the HTML without touching a word of the prose. Neither is what this looks for -- it
    looks for text that Markdown has silently reinterpreted.
    """
    without = _MEANINGLESS.sub("", html or "")
    return _WS_RUN.sub(" ", without.replace(chr(10), "")).strip()


def round_trips(html: str) -> tuple[bool, str]:
    """Whether converting this HTML and rendering it back reproduces the same HTML.

    The per-chapter proof of faithfulness, and the only way to catch prose that Markdown
    reads as markup — an asterisk in the text, a line starting with a ``#`` or a ``>``.
    Returns the verdict and the re-rendered HTML, so a caller can show the difference.
    """
    markdown, _ = html_to_markdown(html)
    again = markdown_to_html(markdown)
    return _normalize(again) == _normalize(html), again
