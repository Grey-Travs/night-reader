"""Scanned-page storage for image-sourced novels.

An image novel keeps its uploads in ``projects/<pid>/pages/`` and one manifest,
``projects/<pid>/pages.json``, holding the ordered page list and each page's OCR
state. Building chapters turns that manifest into the ordinary ``source.json``, after
which the novel is indistinguishable from any other and the whole translation
pipeline runs unchanged.

Why a separate file rather than ``state.json``: that one is per-*chapter* translation
state, overlaid one chapter at a time by the worker. Page records have a different key
space and lifecycle, and folding them in would break that merge.

Ordering note: a page's filename carries its ``seq`` (a monotonic counter), never its
position. Reordering rewrites the manifest only — it never renames a file, which would
break image caching in the browser and race an in-flight OCR call holding a path.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from translation_bot.atomic import atomic_write_text

from .locks import file_lock
from .projects import PROJECTS_DIR

PAGES_FILENAME = "pages.json"
PAGES_DIRNAME = "pages"

# A phone photo of a book page is ~2-8 MB; 25 MB leaves room for a high-res scan
# without letting one request balloon the process.
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_PAGES_PER_PROJECT = 2000

STATUS_NEW = "new"
STATUS_QUEUED = "queued"
STATUS_RUNNING = "ocr-running"
STATUS_OK = "ok"
STATUS_NEEDS_CHECK = "needs-check"
STATUS_EDITED = "edited"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

STATUSES = (STATUS_NEW, STATUS_QUEUED, STATUS_RUNNING, STATUS_OK,
            STATUS_NEEDS_CHECK, STATUS_EDITED, STATUS_SKIPPED, STATUS_FAILED)

# Statuses whose text is ready to go into a chapter.
APPROVED_STATUSES = (STATUS_OK, STATUS_EDITED)

JOIN_KINDS = ("sentence", "paragraph", "chapter", "gap")

_PAGE_ID_RE = re.compile(r"^[a-f0-9]{8}$")
_UNSAFE_LABEL_RE = re.compile(r"[^\w .\-()\[\]]", re.UNICODE)


# ---- image sniffing ----------------------------------------------------------
# The Content-Type header is advisory — the bytes decide. An extension is only ever
# derived from what the file actually is, never from the client's filename.

def sniff_image(head: bytes) -> str | None:
    """Return the file extension for a supported image, or None."""
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def unsupported_reason(head: bytes) -> str:
    """A human explanation for a rejected upload."""
    # ISO base-media container: HEIC/HEIF (the iPhone default) and friends.
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
            return (
                "That looks like an iPhone HEIC photo, which browsers can't display. "
                "On your iPhone: Settings → Camera → Formats → Most Compatible, then "
                "re-take or re-export the photos as JPEG."
            )
        return "That looks like a video, not a page image. Upload a photo or scan."
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "GIF isn't supported. Save the page as JPEG or PNG."
    if head.startswith(b"%PDF"):
        return (
            "That's a PDF, not an image. Export its pages as JPEG or PNG images "
            "and upload those."
        )
    return "That file isn't a JPEG, PNG, or WebP image."


# ---- paths -------------------------------------------------------------------

def project_dir(pid: str) -> Path:
    return PROJECTS_DIR / pid


def pages_dir(pid: str) -> Path:
    return PROJECTS_DIR / pid / PAGES_DIRNAME


def pages_file(pid: str) -> Path:
    return PROJECTS_DIR / pid / PAGES_FILENAME


def resolve_page_file(pid: str, page_id: str, doc: dict | None = None) -> Path | None:
    """The image file for a page, or None.

    The path is resolved through the manifest only — a URL segment never becomes a
    path component. The containment check is belt-and-braces against a manifest that
    was hand-edited or restored from a tampered bundle.
    """
    if not _PAGE_ID_RE.match(page_id or ""):
        return None
    doc = doc if doc is not None else load_pages(pid)
    page = find_page(doc, page_id)
    if not page or not page.get("file"):
        return None
    folder = pages_dir(pid).resolve()
    try:
        target = (folder / str(page["file"])).resolve()
    except OSError:
        return None
    if target.parent != folder or not target.is_file():
        return None
    return target


# ---- manifest ----------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_doc() -> dict:
    return {"version": 1, "next_seq": 1, "pages": [], "batches": [],
            "build": None, "totals": {"cost_usd": 0.0}}


def load_pages(pid: str) -> dict:
    """Read the manifest. A missing or corrupt file reads as empty, never raises —
    one bad file must not take down the novel (same rule as State/Glossary/project)."""
    path = pages_file(pid)
    if not path.exists():
        return new_doc()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return new_doc()
    if not isinstance(doc, dict):
        return new_doc()
    doc.setdefault("version", 1)
    doc.setdefault("pages", [])
    doc.setdefault("batches", [])
    doc.setdefault("build", None)
    doc.setdefault("totals", {"cost_usd": 0.0})
    if not isinstance(doc["pages"], list):
        doc["pages"] = []
    doc.setdefault("next_seq", max((int(p.get("seq") or 0) for p in doc["pages"]),
                                   default=0) + 1)
    return doc


def save_pages(pid: str, doc: dict) -> None:
    atomic_write_text(pages_file(pid),
                      json.dumps(doc, ensure_ascii=False, indent=2))


@contextmanager
def mutate_pages(pid: str):
    """Load → mutate → save the manifest with no other thread interleaving.

    Yields a FRESHLY LOADED manifest: anything read before the lock was taken is
    already stale. Keep the body short — an OCR worker may be waiting on it.
    """
    with file_lock(pages_file(pid)):
        doc = load_pages(pid)
        yield doc
        save_pages(pid, doc)


# ---- records -----------------------------------------------------------------

def find_page(doc: dict, page_id: str) -> dict | None:
    for page in doc.get("pages", []):
        if page.get("id") == page_id:
            return page
    return None


def page_index(doc: dict, page_id: str) -> int:
    for i, page in enumerate(doc.get("pages", [])):
        if page.get("id") == page_id:
            return i
    return -1


def safe_label(name: str, limit: int = 120) -> str:
    """A client filename reduced to a display label. Never used as a path."""
    cleaned = _UNSAFE_LABEL_RE.sub("", (name or "").strip())
    return cleaned[:limit]


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def find_by_hash(doc: dict, digest: str) -> dict | None:
    for page in doc.get("pages", []):
        if page.get("sha256") == digest:
            return page
    return None


def new_batch(doc: dict, label: str = "") -> str:
    batch_id = uuid.uuid4().hex[:8]
    doc.setdefault("batches", []).append(
        {"id": batch_id, "label": safe_label(label), "at": now_iso(), "count": 0})
    return batch_id


def _touch_batch(doc: dict, batch_id: str) -> None:
    for batch in doc.get("batches", []):
        if batch.get("id") == batch_id:
            batch["count"] = sum(1 for p in doc.get("pages", [])
                                 if p.get("batch") == batch_id)
            return


def add_page(doc: dict, *, ext: str, data_len: int, digest: str,
             batch: str, name: str = "") -> dict:
    """Append a page record and return it. The caller writes the file itself, using
    the returned ``file`` name."""
    seq = int(doc.get("next_seq") or 1)
    doc["next_seq"] = seq + 1
    page = {
        "id": uuid.uuid4().hex[:8],
        "seq": seq,
        "file": f"page-{seq:04d}.{ext}",
        "name": safe_label(name),
        "bytes": data_len,
        "sha256": digest,
        "batch": batch,
        "added_at": now_iso(),
        "status": STATUS_NEW,
        "text": "",
        "raw_text": "",
        "confidence": "",
        "notes": [],
        "heading": None,
        "starts_mid_sentence": False,
        "ends_mid_sentence": False,
        "ends_mid_word": False,
        "join_prev": "",
        "join_prev_source": "",
        "join_glue": "space",
        "join_reason": "",
        "hangul_fraction": 0.0,
        "chars": 0,
        "ocr": None,
        "verify": None,
        "error": None,
    }
    doc.setdefault("pages", []).append(page)
    _touch_batch(doc, batch)
    return page


def reorder(doc: dict, ids: list[str]) -> bool:
    """Reorder pages. ``ids`` must be a permutation of the existing ids — a partial
    list would silently drop pages, so it is refused."""
    pages = doc.get("pages", [])
    current = [p.get("id") for p in pages]
    if sorted(str(i) for i in ids) != sorted(str(c) for c in current):
        return False
    by_id = {p.get("id"): p for p in pages}
    doc["pages"] = [by_id[str(i)] for i in ids]
    return True


def delete_pages(doc: dict, ids: list[str]) -> list[dict]:
    """Remove pages from the manifest and return the removed records (so the caller
    can unlink their files)."""
    wanted = {str(i) for i in ids}
    removed = [p for p in doc.get("pages", []) if p.get("id") in wanted]
    doc["pages"] = [p for p in doc.get("pages", []) if p.get("id") not in wanted]
    for batch in doc.get("batches", []):
        _touch_batch(doc, batch.get("id", ""))
    return removed


def counts(doc: dict) -> dict:
    out = {status: 0 for status in STATUSES}
    for page in doc.get("pages", []):
        status = page.get("status")
        if status in out:
            out[status] += 1
    out["total"] = len(doc.get("pages", []))
    return out


def summary(doc: dict) -> dict:
    """Manifest without page text — the list endpoint's payload. 400 pages of Korean
    would be several MB on every tab switch."""
    slim = []
    for page in doc.get("pages", []):
        row = {k: v for k, v in page.items() if k not in ("text", "raw_text")}
        row["chars"] = len(page.get("text") or "")
        row["has_text"] = bool((page.get("text") or "").strip())
        slim.append(row)
    return {
        "pages": slim,
        "batches": doc.get("batches", []),
        "build": doc.get("build"),
        "counts": counts(doc),
        "totals": doc.get("totals", {}),
    }
