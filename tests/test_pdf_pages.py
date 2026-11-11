"""Scanner PDFs split into page images: a scan's own JPEG is copied out untouched, any
other page is drawn as a grey PNG, and a PDF that can't be used says why.

PDFs are made here with pypdfium2's own writer.

Runs under pytest (``pytest tests/``).
"""

import io
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

pdfium = pytest.importorskip("pypdfium2")

from server import pdf_pages  # noqa: E402

# A 1 x 1 baseline JPEG. A comment segment after the start makes each copy distinct.
_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b"
    "0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e3334"
    "32ffc0000b080001000101011100ffc4001f0000010501010101010100000000000000000102030405060708"
    "090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114"
    "328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a43444546474849"
    "4a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4"
    "a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2"
    "f3f4f5f6f7f8f9faffda0008010100003f00fbd3ffd9")


def jpeg(note: str) -> bytes:
    text = note.encode()
    return _JPEG[:2] + b"\xff\xfe" + struct.pack(">H", len(text) + 2) + text + _JPEG[2:]


def make_pdf(pages) -> bytes:
    """``pages``: each a JPEG to fill the page with, ("small", jpeg) for one that doesn't
    cover it, ("turned", jpeg) for a page with /Rotate 90, ("mirrored", jpeg) for a full
    page drawn back to front, or None for an empty page."""
    pdf = pdfium.PdfDocument.new()
    for spec in pages:
        page = pdf.new_page(595, 842)
        kind, data = ("full", spec) if isinstance(spec, bytes) else (spec or ("empty", None))
        if data is not None:
            image = pdfium.PdfImage.new(pdf)
            image.load_jpeg(io.BytesIO(data), pages=[page])
            if kind == "small":
                image.set_matrix(pdfium.PdfMatrix().scale(200, 200).translate(50, 50))
            elif kind == "mirrored":
                image.set_matrix(pdfium.PdfMatrix(-595, 0, 0, 842, 595, 0))
            else:
                image.set_matrix(pdfium.PdfMatrix().scale(595, 842))
            page.insert_obj(image)
            page.gen_content()
        if kind == "turned":
            page.set_rotation(90)
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


def png_size(data: bytes) -> tuple[int, int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height, depth, colour = struct.unpack(">IIBB", data[16:26])
    assert depth == 8
    return width, height, colour


@pytest.fixture
def folder(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    return tmp_path, out


def _split(folder, data, most=150):
    root, out = folder
    pdf = root / "in.pdf"
    pdf.write_bytes(data)
    return [out / name for name in pdf_pages.split(pdf, out, most)]


def test_a_scanned_page_is_copied_out_untouched(folder):
    a, b = jpeg("page one"), jpeg("page two")
    files = _split(folder, make_pdf([a, b]))
    assert [f.name for f in files] == ["p0001.jpg", "p0002.jpg"]
    assert [f.read_bytes() for f in files] == [a, b]


def test_any_other_page_is_drawn_grey_at_the_size_claude_reads(folder):
    files = _split(folder, make_pdf([None, ("small", jpeg("x")), ("turned", jpeg("y"))]))
    assert [f.suffix for f in files] == [".png", ".png", ".png"]
    width, height, colour = png_size(files[0].read_bytes())
    assert (max(width, height), colour) == (2200, 0), "grey, 2200 on the long edge"
    w2, h2, _ = png_size(files[2].read_bytes())
    assert w2 > h2, "a turned page is drawn turned"


def test_a_scan_drawn_back_to_front_is_drawn_the_right_way_round(folder):
    # Its JPEG alone would come out mirrored: only drawing it gives the page as it shows.
    files = _split(folder, make_pdf([("mirrored", jpeg("m"))]))
    assert [f.suffix for f in files] == [".png"]


def test_the_same_pdf_gives_the_same_bytes(folder):
    data = make_pdf([None, jpeg("a")])
    first = [f.read_bytes() for f in _split(folder, data)]
    for f in folder[1].iterdir():
        f.unlink()
    assert [f.read_bytes() for f in _split(folder, data)] == first, \
        "so pages sent twice are recognised as already in"


def test_a_damaged_pdf_says_so(folder):
    with pytest.raises(pdf_pages.PdfError) as refusal:
        _split(folder, b"%PDF-1.7\nnot really a pdf")
    assert refusal.value.kind == "damaged"


def test_too_many_pages_says_how_many(folder):
    with pytest.raises(pdf_pages.PdfError) as refusal:
        _split(folder, make_pdf([None, None, None]), most=2)
    assert refusal.value.kind == "too-many"
    assert "3 pages" in refusal.value.message


def test_a_locked_pdf_says_so(folder, monkeypatch):
    def locked(*_a, **_k):
        raise pdfium.PdfiumError("Failed to load document (PDFium: Incorrect password error).")

    monkeypatch.setattr(pdfium, "PdfDocument", locked)
    with pytest.raises(pdf_pages.PdfError) as refusal:
        _split(folder, b"%PDF-1.7\n")
    assert refusal.value.kind == "password"


def test_the_grey_writer_makes_a_real_png():
    pixels = bytes(range(6)) + b"\xff\xff" + bytes(range(6)) + b"\xee\xee"   # stride 8
    data = pdf_pages.png_grey(6, 2, pixels, 8)
    assert png_size(data) == (6, 2, 0)
    idat = data[data.index(b"IDAT") + 4:data.index(b"IEND") - 8]
    assert zlib.decompress(idat) == b"\x00" + bytes(range(6)) + b"\x00" + bytes(range(6))


def test_the_child_process_splits_and_reports(folder):
    root, out = folder
    pdf = root / "in.pdf"
    pdf.write_bytes(make_pdf([jpeg("one"), None]))
    files = pdf_pages.split_in_child(pdf, out, 150)
    assert [f.name for f in files] == ["p0001.jpg", "p0002.png"]
    bad = root / "bad.pdf"
    bad.write_bytes(b"%PDF-1.7 broken")
    with pytest.raises(pdf_pages.PdfError) as refusal:
        pdf_pages.split_in_child(bad, out, 150)
    assert refusal.value.kind == "damaged"
