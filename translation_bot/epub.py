"""Minimal, dependency-free EPUB 3 writer for finished translations.

A web novel is most enjoyable on a real e-reader, so we emit a valid EPUB with a
table of contents and one navigable section per chapter — built with nothing but
the standard library (``zipfile``). The chapter bodies are light Markdown
(paragraphs, headings, emphasis, rules), which we convert to safe XHTML.
"""

from __future__ import annotations

import html
import re
import uuid
import zipfile
from pathlib import Path

_CSS = """\
body { font-family: Georgia, 'Times New Roman', serif; line-height: 1.6; margin: 5%; }
h1, h2 { font-weight: 600; line-height: 1.25; }
p { margin: 0 0 1em; text-indent: 0; }
em { font-style: italic; }
strong { font-weight: 700; }
hr { border: none; border-top: 1px solid #999; margin: 2em 0; }
blockquote { border-left: 2px solid #999; padding-left: 1em; color: #555; margin: 0 0 1em; }
"""


def _inline(text: str) -> str:
    """Escape, then apply **bold** and *italic* (after escaping, so user text is safe)."""
    out = html.escape(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", out)
    return out


def _markdown_to_xhtml(md: str) -> str:
    """Convert the small Markdown subset the translator emits into XHTML body."""
    blocks = re.split(r"\n\s*\n", (md or "").strip())
    parts: list[str] = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        if re.fullmatch(r"(?:-{3,}|\*{3,}|_{3,})", block):
            parts.append("<hr/>")
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", block)
        if m:
            level = min(len(m.group(1)), 6)
            parts.append(f"<h{level}>{_inline(m.group(2).strip())}</h{level}>")
            continue
        if block.startswith(">"):
            inner = "<br/>".join(_inline(re.sub(r"^>\s?", "", ln)) for ln in block.split("\n"))
            parts.append(f"<blockquote><p>{inner}</p></blockquote>")
            continue
        # Ordinary paragraph; preserve hard line breaks within it.
        parts.append("<p>" + "<br/>".join(_inline(ln) for ln in block.split("\n")) + "</p>")
    return "\n".join(parts)


def _chapter_doc(title: str, body_md: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">\n'
        f"<head><meta charset=\"utf-8\"/><title>{html.escape(title)}</title>"
        '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
        f"<body>\n<h1>{html.escape(title)}</h1>\n{_markdown_to_xhtml(body_md)}\n</body>\n</html>\n"
    )


def build_epub(title: str, author: str, chapters: list[tuple[str, str]], out_path: Path) -> Path:
    """Write an EPUB to ``out_path``. ``chapters`` is an ordered list of (title, markdown)."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    book_id = f"urn:uuid:{uuid.uuid4()}"
    files = [(f"chap{i:04d}.xhtml", t or f"Chapter {i}", body) for i, (t, body) in enumerate(chapters, 1)]

    manifest = "\n".join(
        f'    <item id="chap{i}" href="{fn}" media-type="application/xhtml+xml"/>'
        for i, (fn, _t, _b) in enumerate(files, 1)
    )
    spine = "\n".join(f'    <itemref idref="chap{i}"/>' for i, _ in enumerate(files, 1))
    nav_items = "\n".join(
        f'      <li><a href="{fn}">{html.escape(t)}</a></li>' for fn, t, _b in files
    )
    ncx_points = "\n".join(
        f'    <navPoint id="np{i}" playOrder="{i}"><navLabel><text>{html.escape(t)}</text>'
        f'</navLabel><content src="{fn}"/></navPoint>'
        for i, (fn, t, _b) in enumerate(files, 1)
    )

    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'    <dc:identifier id="bookid">{book_id}</dc:identifier>\n'
        f"    <dc:title>{html.escape(title)}</dc:title>\n"
        f"    <dc:creator>{html.escape(author or 'Night Reader')}</dc:creator>\n"
        '    <dc:language>en</dc:language>\n'
        '    <meta property="dcterms:modified">2024-01-01T00:00:00Z</meta>\n'
        '  </metadata>\n'
        '  <manifest>\n'
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>\n'
        '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n'
        '    <item id="css" href="style.css" media-type="text/css"/>\n'
        f"{manifest}\n"
        '  </manifest>\n'
        '  <spine toc="ncx">\n'
        f"{spine}\n"
        '  </spine>\n'
        '</package>\n'
    )
    nav = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">\n'
        '<head><meta charset="utf-8"/><title>Contents</title></head>\n'
        '<body>\n  <nav epub:type="toc" id="toc"><h1>Contents</h1>\n    <ol>\n'
        f"{nav_items}\n    </ol>\n  </nav>\n</body>\n</html>\n"
    )
    ncx = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        f'  <head><meta name="dtb:uid" content="{book_id}"/></head>\n'
        f"  <docTitle><text>{html.escape(title)}</text></docTitle>\n"
        '  <navMap>\n'
        f"{ncx_points}\n"
        '  </navMap>\n'
        '</ncx>\n'
    )
    container = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        '  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>\n'
        '</container>\n'
    )

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        # The mimetype entry must be first and stored uncompressed (EPUB OCF rule).
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/nav.xhtml", nav)
        z.writestr("OEBPS/toc.ncx", ncx)
        z.writestr("OEBPS/style.css", _CSS)
        for fn, t, body in files:
            z.writestr(f"OEBPS/{fn}", _chapter_doc(t, body))
    return out_path
