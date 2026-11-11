"""A scanner app's PDF, split into page images that go in like photos.

Microsoft Lens, the Google Drive scanner and their kind save each page as one full-page
JPEG inside the PDF. That JPEG is copied out byte for byte: nothing is lost, and the same
PDF sent twice gives the same bytes, so its pages are recognised as already in. Any other
page — text, drawings, several images — is drawn instead, as an 8-bit grey PNG 2200
pixels on its long edge, by a small writer here, so no imaging library is needed.

PDFium isn't safe to use from several threads, so the splitting runs in a child process,
which also means a PDF that hangs it can be given up on, and one that crashes it can't
take the app down:

    python -m server.pdf_pages <pdf> <folder for the pages> <most pages>

It prints one line of JSON: {"pages": ["p0001.jpg", ...]} or {"error": ..., ...}.
"""

from __future__ import annotations

import json
import struct
import subprocess
import sys
import zlib
from pathlib import Path

LONG_EDGE = 2200          # pixels, for a page that has to be drawn
COVERS = 0.9              # a "full-page" image covers at least this much of the page
ROOT = Path(__file__).resolve().parent.parent


class PdfError(Exception):
    """A PDF that can't be split, with what to tell the person."""

    def __init__(self, kind: str, message: str, status: int = 400):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status


def png_grey(width: int, height: int, pixels: bytes, stride: int) -> bytes:
    """An 8-bit greyscale PNG of ``pixels`` (one byte a pixel, rows ``stride`` apart)."""
    rows = b"".join(b"\x00" + pixels[y * stride:y * stride + width] for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6))
            + chunk(b"IEND", b""))


def _whole_page_jpeg(pdfium, raw, page) -> bytes | None:
    """The page's one JPEG, if the page is a scan: a single image, upright, covering the
    page. Text alongside it is fine — a scanner's invisible search layer."""
    if page.get_rotation() % 360:
        return None
    images = [obj for obj in page.get_objects(max_depth=2) if obj.type == raw.FPDF_PAGEOBJ_IMAGE]
    if len(images) != 1:
        return None
    image = images[0]
    if image.get_filters() != ["DCTDecode"]:
        return None
    a, b, c, d, _e, _f = image.get_matrix().get()
    if b or c or a <= 0 or d <= 0:      # turned or mirrored: draw it instead
        return None
    left, bottom, right, top = image.get_bounds()
    width, height = page.get_size()
    if width <= 0 or height <= 0 or (right - left) * (top - bottom) < COVERS * width * height:
        return None
    data = bytes(image.get_data(decode_simple=False))
    return data if data[:2] == b"\xff\xd8" else None


def _drawn(pdfium, page) -> bytes:
    width, height = page.get_size()
    scale = LONG_EDGE / max(width, height, 1)
    bitmap = page.render(scale=scale, grayscale=True, may_draw_forms=False)
    if bitmap.n_channels != 1:
        raise PdfError("damaged", "That PDF's pages couldn't be drawn.")
    return png_grey(bitmap.width, bitmap.height, bytes(bitmap.buffer), bitmap.stride)


def split(pdf: Path, out: Path, most: int) -> list[str]:
    """Write each page of ``pdf`` into ``out``, in order, and return the file names."""
    try:
        import pypdfium2 as pdfium
        import pypdfium2.raw as raw
    except ImportError:
        raise PdfError("missing", "Reading PDFs needs one more part. Run setup.bat on the "
                                  "computer, then try again.", status=503)
    try:
        doc = pdfium.PdfDocument(str(pdf))
    except pdfium.PdfiumError as exc:
        if "password" in str(exc).lower():
            raise PdfError("password", "That PDF is locked with a password. Save it again "
                                       "without one, then send it.")
        raise PdfError("damaged", "That PDF couldn't be opened. It may be damaged, or not "
                                  "finished saving.")
    count = len(doc)
    if count == 0:
        raise PdfError("empty", "That PDF has no pages.")
    if count > most:
        raise PdfError("too-many", f"That PDF has {count} pages. At most {most} go in at "
                                   f"once: send it in parts.")
    names = []
    for index in range(count):
        page = doc[index]
        jpeg = _whole_page_jpeg(pdfium, raw, page)
        name = f"p{index + 1:04d}.{'jpg' if jpeg else 'png'}"
        (out / name).write_bytes(jpeg if jpeg else _drawn(pdfium, page))
        names.append(name)
        page.close()
    doc.close()
    return names


def split_in_child(pdf: Path, out: Path, most: int, *, timeout: float = 180.0) -> list[Path]:
    """Split ``pdf`` in a child process. Returns the page files, in order, or raises
    :class:`PdfError` saying why not."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        done = subprocess.run(
            [sys.executable, "-m", "server.pdf_pages", str(pdf), str(out), str(most)],
            cwd=str(ROOT), capture_output=True, text=True, timeout=timeout, creationflags=flags)
    except subprocess.TimeoutExpired:
        raise PdfError("slow", "That PDF took too long to open. Try sending it in parts.")
    try:
        answer = json.loads((done.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise PdfError("damaged", "That PDF couldn't be opened. It may be damaged.")
    if "error" in answer:
        raise PdfError(answer["error"], answer.get("message") or "That PDF couldn't be used.",
                       status=int(answer.get("status") or 400))
    return [out / name for name in answer.get("pages") or []]


def main(argv: list[str]) -> int:
    pdf, out, most = Path(argv[0]), Path(argv[1]), int(argv[2])
    try:
        answer = {"pages": split(pdf, out, most)}
    except PdfError as exc:
        answer = {"error": exc.kind, "message": exc.message, "status": exc.status}
    except Exception:  # noqa: BLE001 — anything PDFium chokes on is a damaged PDF to us
        answer = {"error": "damaged", "message": "That PDF couldn't be opened. It may be damaged.",
                  "status": 400}
    print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
