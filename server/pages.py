"""Scanned-page storage for image-sourced novels.

An image novel keeps its uploads in ``projects/<pid>/pages/`` and one manifest,
``projects/<pid>/pages.json``, holding the pages, each page's OCR state, and the chapter
boxes the pages are sorted into. Building chapters turns that manifest into the ordinary
``source.json``, after which the novel is indistinguishable from any other and the whole
translation pipeline runs unchanged.

Why a separate file rather than ``state.json``: that one is per-*chapter* translation
state, overlaid one chapter at a time by the worker. Page records have a different key
space and lifecycle, and folding them in would break that merge.

Chapter boxes: every entry of ``batches`` is one chapter, and their order is the reading
order. ``pages`` stays a single list, but is always grouped by box, in box order, so
everything that walks the pages in order — the OCR queue, stitching, building — walks
them in story order. :func:`normalize` is what keeps that true; it runs on every load
and every save.

Ordering note: a page's filename carries its ``seq`` (a monotonic counter), never its
position. Moving a page rewrites the manifest only — it never renames a file, which would
break image caching in the browser and race an in-flight OCR call holding a path.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from translation_bot.atomic import atomic_write_text, quarantine_unreadable
from translation_bot.docs_extract import _strip_invisibles

from .locks import file_lock
from .projects import PROJECTS_DIR

PAGES_FILENAME = "pages.json"
PAGES_DIRNAME = "pages"

# 1 grouped chapters by contiguous runs of an upload batch; 2 has chapter boxes.
MANIFEST_VERSION = 2

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

# Claude is reading the page right now, or is about to.
_BUSY_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)
# The page has been read, whatever anyone thinks of the result yet.
_READ_STATUSES = (STATUS_OK, STATUS_EDITED, STATUS_NEEDS_CHECK)

JOIN_KINDS = ("sentence", "paragraph", "chapter", "gap")

# Page and box ids alike: eight lowercase hex characters, only ever minted here.
_ID_RE = re.compile(r"^[a-f0-9]{8}$")
_UNSAFE_LABEL_RE = re.compile(r"[^\w .\-()\[\]]", re.UNICODE)

BOX_GONE = "That chapter box isn't in this novel any more."
_BOX_LOCKED = ("Those photos were built into chapters before chapter boxes existed, so "
               "they can't be moved or added to. Start over rebuilds the novel from "
               "chapter boxes.")


class BoxError(Exception):
    """A change to the chapter boxes that has to be refused.

    Carries the HTTP status the API answers with, so the refusal is decided here, next
    to the rule it enforces, without this module importing the web layer.
    """

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


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
    if not _ID_RE.match(page_id or ""):
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
    return {"version": MANIFEST_VERSION, "rev": 0, "next_seq": 1, "pages": [],
            "batches": [], "build": None, "totals": {"cost_usd": 0.0}}


def load_pages(pid: str) -> dict:
    """Read the manifest. A missing or corrupt file reads as empty, never raises —
    one bad file must not take down the novel (same rule as State/Glossary/project).

    A version-1 manifest is migrated and every manifest is normalized on the way in,
    but nothing is written: the next save persists it. Both steps are deterministic,
    so every read before that save agrees on every box id.
    """
    path = pages_file(pid)
    if not path.exists():
        return new_doc()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        # Degrading to empty keeps one bad file from taking down the novel — but the
        # caller then mutates this and saves it back, which would turn a MOMENTARY
        # read failure (an antivirus holding the file, a sync client mid-write) into
        # the permanent loss of every page transcribed from a photo. Keep the bytes.
        quarantine_unreadable(path)
        return new_doc()
    if not isinstance(doc, dict):
        return new_doc()
    doc.setdefault("version", 1)
    doc.setdefault("rev", 0)
    doc.setdefault("pages", [])
    doc.setdefault("batches", [])
    doc.setdefault("build", None)
    doc.setdefault("totals", {"cost_usd": 0.0})
    if not isinstance(doc["pages"], list):
        doc["pages"] = []
    if not isinstance(doc["batches"], list):
        doc["batches"] = []
    if not isinstance(doc["build"], dict):
        doc["build"] = None
    if _int(doc.get("version")) < MANIFEST_VERSION:
        migrate_v1(doc)
    normalize(doc)
    doc.setdefault("next_seq", max((int(p.get("seq") or 0) for p in doc["pages"]),
                                   default=0) + 1)
    return doc


def save_pages(pid: str, doc: dict) -> None:
    """Normalize and write the manifest.

    ``rev`` counts saves, so a client holding two answers can tell which is newer: a
    poll that left before a move landed must not put the old order back on screen.
    """
    normalize(doc)
    doc["rev"] = _int(doc.get("rev")) + 1
    atomic_write_text(pages_file(pid),
                      json.dumps(doc, ensure_ascii=False, indent=2))


@contextmanager
def mutate_pages(pid: str):
    """Load → mutate → save the manifest with no other thread interleaving.

    Yields a FRESHLY LOADED manifest: anything read before the lock was taken is
    already stale. Keep the body short — an OCR worker may be waiting on it. An
    exception inside the body skips the save, so a refused change is never written.
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


def safe_label(name: str, limit: int = 120) -> str:
    """A client filename reduced to a display label. Never used as a path."""
    cleaned = _UNSAFE_LABEL_RE.sub("", (name or "").strip())
    return cleaned[:limit]


def clean_title(text: str, limit: int = 120) -> str:
    """A chapter box's title as the reader typed it, made safe for a one-line title.

    Not :func:`safe_label`: that is a filename sanitiser, and it strips the ``: ! ? "``
    a chapter title is made of — "제3화: 문이 열렸다!" would build as "제3화 문이 열렸다".
    Only what can't belong in a single line goes: a line break or tab becomes a space,
    and other invisible control and format characters are dropped.
    """
    kept = []
    for ch in str(text or ""):
        category = unicodedata.category(ch)
        if category == "Cc":
            kept.append(" ")
        elif not category.startswith("C"):
            kept.append(ch)
    return " ".join("".join(kept).split())[:limit].rstrip()


def page_has_text(page: dict) -> bool:
    """Whether a page carries any text a reader could see.

    Zero-width watermark characters alone don't count. They are the same characters
    chapter building strips, so a page of nothing else would build to an empty chapter.
    """
    return bool(_strip_invisibles(page.get("text") or "").strip())


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def find_by_hash(doc: dict, digest: str) -> dict | None:
    for page in doc.get("pages", []):
        if page.get("sha256") == digest:
            return page
    return None


def _int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _fresh_id(taken) -> str:
    """A new random id that isn't in use yet. Eight hex characters collide about once
    in two thousand novels of 2000 pages — rare, but a collision between two boxes
    would merge two chapters."""
    while True:
        candidate = uuid.uuid4().hex[:8]
        if candidate not in taken:
            return candidate


def _derive_id(seed: str, taken) -> str:
    """A box id derived from ``seed`` rather than drawn at random, for the boxes a READ
    has to create.

    normalize and migrate_v1 run on every load until the next save persists their
    result, so a random id would differ between two loads of the same file — and a
    client holding the first would address a box the second no longer has.
    """
    attempt = 0
    while True:
        digest = hashlib.sha256(f"{seed}#{attempt}".encode("utf-8")).hexdigest()
        for start in range(0, len(digest), 8):
            candidate = digest[start:start + 8]
            if candidate not in taken:
                return candidate
        attempt += 1


# ---- chapter boxes -----------------------------------------------------------

def is_box_id(value: str) -> bool:
    return bool(_ID_RE.match(value or ""))


def find_box(doc: dict, box_id: str) -> dict | None:
    for box in doc.get("batches", []):
        if box.get("id") == box_id:
            return box
    return None


def is_built(box: dict) -> bool:
    """A numbered chapter, or a legacy pile. Either way its chapters exist in
    source.json under fixed numbers, so the box keeps its place: nothing moves it, and
    nothing goes in before it."""
    return bool(box.get("chapter") or box.get("legacy"))


def live_pages(pages: list[dict]) -> list[dict]:
    """The pages that make up the chapter. A skipped page — a cover, a blank, a
    duplicate — is still in its box, but is not part of the story."""
    return [p for p in pages if p.get("status") != STATUS_SKIPPED]


def box_pages(doc: dict) -> list[tuple[dict, list[dict]]]:
    """Each box with its pages, both in order."""
    grouped: dict[str, list[dict]] = {box["id"]: [] for box in doc.get("batches", [])}
    for page in doc.get("pages", []):
        grouped.setdefault(page.get("batch"), []).append(page)
    return [(box, grouped[box["id"]]) for box in doc.get("batches", [])]


def chapter_count(doc: dict) -> int:
    """How many chapters are built: what the last build wrote, or more if a box claims a
    higher number. Unbuilt boxes are numbered on from here."""
    built = _int((doc.get("build") or {}).get("chapters"))
    return max([built] + [box["chapter"] for box in doc.get("batches", [])
                          if box.get("chapter")])


def legacy_chapters(doc: dict) -> list[int]:
    """The chapters a legacy box was built into: every built chapter no numbered box
    claims. A version-1 pile build never recorded which page landed in which chapter,
    so this is all there is to go on."""
    claimed = {box.get("chapter") for box in doc.get("batches", [])}
    return [n for n in range(1, chapter_count(doc) + 1) if n not in claimed]


def box_numbers(doc: dict) -> dict[str, int | None]:
    """The chapter each box is, or will be: a built box keeps its own; the unbuilt ones
    follow the last built chapter in box order. A legacy box stands for several
    chapters, so it has no single number."""
    next_number = chapter_count(doc)
    numbers: dict[str, int | None] = {}
    for box in doc.get("batches", []):
        if box.get("legacy"):
            numbers[box["id"]] = None
        elif box.get("chapter"):
            numbers[box["id"]] = box["chapter"]
        else:
            next_number += 1
            numbers[box["id"]] = next_number
    return numbers


def box_title(box: dict, live: list[dict], number: int | None) -> str:
    """What the chapter is called: the box's own title, else the heading Claude read off
    its first photo, else "Chapter N"."""
    label = (box.get("label") or "").strip()
    if label:
        return label
    heading = (live[0].get("heading") or "").strip() if live else ""
    if heading:
        return heading
    return f"Chapter {number}" if number else ""


def box_signature(box: dict, pages: list[dict]) -> str:
    """A fingerprint of everything in a box that reaches its built chapter: the title,
    and each live page's id, text, join and heading, in order.

    Compared with the ``built_sig`` stored at build time, it is what says a built chapter
    has changed since — a correction, a reorder, a photo moved in or out, a skip, a join
    or a new title. Status, notes and the reason behind a join are left out: none of
    them change a word of the chapter, so a page marked "Looks good" after the build
    does not ask for one.
    """
    rows = [[page["id"], (page.get("text") or "").strip(),
             "" if i == 0 else (page.get("join_prev") or ""),
             "" if i == 0 else (page.get("join_glue") or "space"),
             (page.get("heading") or "").strip()]
            for i, page in enumerate(live_pages(pages))]
    blob = json.dumps([(box.get("label") or "").strip(), rows],
                      ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def page_names(group: list[dict], among: list[dict]) -> str:
    """Name some of a box's photos for a message: by file name, else by the number the
    photo carries in its box. Three at most, then a count."""
    place = {page["id"]: i for i, page in enumerate(among, start=1)}
    names = [page.get("name") or f"Photo {place.get(page['id'], '?')}" for page in group]
    if len(names) > 3:
        names = names[:2] + [f"{len(names) - 2} more"]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _blocker(kind: str, group: list[dict], message: str) -> dict:
    return {"kind": kind, "count": len(group), "page_ids": [p["id"] for p in group],
            "message": message}


def box_blockers(box: dict, pages: list[dict], *, include: str = "approved") -> list[dict]:
    """What stops this box being built, most fundamental first.

    Each names its photos and says what to do about them: a box that merely "isn't
    ready" leaves the reader opening every photo to find the one that matters. A photo
    that isn't ready holds its whole box back rather than being left out of it — left
    out, it used to come back later as a chapter of its own at the end of the novel.

    ``include="all"`` lets photos still marked to check through. A photo that hasn't
    been read, or has no text, never builds.
    """
    built = bool(box.get("chapter"))
    if not pages:
        return [_blocker("empty", [], (
            "This chapter has no photos left. Built chapters keep their place, so add "
            "its photos back, or use Start over." if built else
            "No photos yet. Add this chapter's photos, or delete the box."))]
    live = live_pages(pages)
    if not live:
        return [_blocker("all-skipped", pages, (
            "Every photo in this chapter is marked Skip. Un-skip the ones that belong "
            "in it, or use Start over." if built else
            "Every photo in this box is marked Skip. Un-skip the ones that belong in "
            "the chapter, or delete the box."))]

    def status(page: dict) -> str:
        return page.get("status") or STATUS_NEW

    groups = (
        ("reading", [p for p in live if status(p) in _BUSY_STATUSES],
         "{who} is still being read. Wait for Claude to finish.",
         "{who} are still being read. Wait for Claude to finish."),
        ("unread", [p for p in live if status(p) not in
                    _BUSY_STATUSES + _READ_STATUSES + (STATUS_FAILED,)],
         "{who} hasn't been read yet. Read it, or type its text.",
         "{who} haven't been read yet. Read them, or type their text."),
        ("failed", [p for p in live if status(p) == STATUS_FAILED],
         "{who} couldn't be read. Read it again, type its text, or Skip it.",
         "{who} couldn't be read. Read them again, type their text, or Skip them."),
        ("no-text", [p for p in live if status(p) in _READ_STATUSES
                     and not page_has_text(p)],
         "{who} has no text. If it's a picture or a blank page, mark it Skip.",
         "{who} have no text. If they're pictures or blank pages, mark them Skip."),
        ("to-check", [p for p in live if include == "approved"
                      and status(p) == STATUS_NEEDS_CHECK and page_has_text(p)],
         "{who} needs checking. Check its text and press Looks good, or include "
         "photos marked to check when you build.",
         "{who} need checking. Check their text and press Looks good, or include "
         "photos marked to check when you build."),
    )
    out = []
    for kind, group, one, many in groups:
        if group:
            message = (one if len(group) == 1 else many).format(who=page_names(group, pages))
            out.append(_blocker(kind, group, message))
    return out


def box_state(box: dict, pages: list[dict], blockers: list[dict]) -> str:
    """One word for the box's pill. A built box only ever reads ``changed`` when its
    chapter would come out different; an unbuilt one reads the most fundamental thing
    holding it back."""
    if box.get("legacy"):
        return "legacy"
    if box.get("chapter"):
        if box_signature(box, pages) == box.get("built_sig"):
            return "built"
        return "changed-waiting" if blockers else "changed"
    kinds = {b["kind"] for b in blockers}
    if "empty" in kinds:
        return "empty"
    if "all-skipped" in kinds:
        return "skipped"
    if "reading" in kinds:
        return "reading"
    if kinds & {"unread", "failed"}:
        return "unread"
    return "to-check" if blockers else "ready"


def box_rows(doc: dict) -> list[dict]:
    """The boxes as the Pages tab shows them, in reading order."""
    numbers = box_numbers(doc)
    rows = []
    for box, pages in box_pages(doc):
        number = numbers[box["id"]]
        blockers = box_blockers(box, pages)
        row = {"id": box["id"], "label": box.get("label") or "",
               "title": box_title(box, live_pages(pages), number),
               "at": box.get("at") or "", "count": len(pages),
               "chapter": box.get("chapter"), "number": number,
               "state": box_state(box, pages, blockers), "blockers": blockers}
        if box.get("legacy"):
            row["legacy"] = True
            covered = legacy_chapters(doc)
            if covered:
                row["chapters"] = [covered[0], covered[-1]]
        rows.append(row)
    return rows


# ---- seams -------------------------------------------------------------------
# A seam is how a page joins the one before it, stored on the later page. It is only
# meaningful against that exact neighbour, so the page also records which one it was
# decided against (``join_prev_id``), and normalize clears any seam whose neighbour has
# since changed.

def seam_neighbours(doc: dict) -> dict[str, str | None]:
    """For every page that isn't skipped: the page its seam joins onto — the previous
    page in its box that isn't skipped — or None for the first page of a box."""
    out: dict[str, str | None] = {}
    last: dict[str, str] = {}
    for page in doc.get("pages", []):
        if page.get("status") == STATUS_SKIPPED:
            continue
        out[page["id"]] = last.get(page.get("batch"))
        last[page.get("batch")] = page["id"]
    return out


def clear_seam(page: dict) -> None:
    page["join_prev"] = ""
    page["join_prev_source"] = ""
    page["join_reason"] = ""
    page["join_glue"] = "space"


def set_seam(page: dict, *, kind: str, glue: str, source: str, reason: str,
             prev: str | None) -> None:
    page["join_prev"] = kind
    page["join_glue"] = glue
    page["join_prev_source"] = source
    page["join_reason"] = reason
    page["join_prev_id"] = prev


# ---- keeping the manifest straight -------------------------------------------

def normalize(doc: dict) -> dict:
    """Restore the manifest's invariants in place, and return it.

    Pure and idempotent — no clock, no uuid — because it runs on every load as well as
    every save, and two loads of one file must agree. Afterwards:

    - every page sits in a box, and the pages are grouped by box, in box order;
    - built boxes come first, in chapter order, then the unbuilt ones as arranged;
    - every box's ``count`` is right;
    - every seam was decided against the page now before it, or it is cleared.

    The seam rule is the one place a seam is reset. A move, a delete, a skip and an
    un-skip all change some page's neighbour, and a join decided against the old
    neighbour describes a page break that no longer exists: a "sentence continues, no
    space" join left on it glues two unrelated pages together mid-word. Checking the
    neighbour here rather than in each of those paths means a path added later can't
    forget to. Seams the reader set by hand are cleared too, for the same reason.
    """
    pages = [p for p in doc.get("pages") or []
             if isinstance(p, dict) and isinstance(p.get("id"), str)]
    boxes: list[dict] = []
    seen: set[str] = set()
    for box in doc.get("batches") or []:
        if isinstance(box, dict) and isinstance(box.get("id"), str) and box["id"] not in seen:
            seen.add(box["id"])
            boxes.append(box)

    taken = set(seen)
    for box in boxes:
        # A box id travels in URLs, so one that can't (a hand edit) is re-minted, and
        # its pages follow it.
        if not _ID_RE.match(box["id"]):
            old = box["id"]
            box["id"] = _derive_id(f"box|{old}", taken)
            taken.add(box["id"])
            for page in pages:
                if page.get("batch") == old:
                    page["batch"] = box["id"]

    claimed: set[int] = set()
    for box in boxes:
        if not isinstance(box.get("label"), str):
            box["label"] = ""
        if not isinstance(box.get("at"), str):
            box["at"] = ""
        if box.get("legacy"):
            box["legacy"] = True
        else:
            box.pop("legacy", None)
        chapter = box.get("chapter")
        if box.get("legacy") or not _positive_int(chapter) or chapter in claimed:
            box["chapter"] = None
        else:
            claimed.add(chapter)
        if box["chapter"] is None or not isinstance(box.get("built_sig"), str):
            box["built_sig"] = None

    # A page whose box is gone is adopted rather than dropped — losing a transcribed
    # page to a bookkeeping slip is the one thing this file must never do.
    known = {box["id"] for box in boxes}
    adopted: dict[str, dict] = {}
    for page in pages:
        batch = page.get("batch")
        if isinstance(batch, str) and batch in known:
            continue
        key = batch if isinstance(batch, str) else ""
        box = adopted.get(key)
        if box is None:
            box_id = (key if _ID_RE.match(key) and key not in taken
                      else _derive_id(f"orphan|{page['id']}", taken))
            taken.add(box_id)
            box = {"id": box_id, "label": "", "at": str(page.get("added_at") or ""),
                   "count": 0, "chapter": None, "built_sig": None}
            adopted[key] = box
            boxes.append(box)
        page["batch"] = box["id"]

    sizes = Counter(page["batch"] for page in pages)
    boxes = [box for box in boxes if sizes[box["id"]] or not box.get("legacy")]

    doc["batches"] = boxes
    legacy_at = (legacy_chapters(doc) or [chapter_count(doc) + 1])[0]
    fixed = sorted((box for box in boxes if is_built(box)),
                   key=lambda box: box["chapter"] or legacy_at)
    boxes = fixed + [box for box in boxes if not is_built(box)]
    position = {box["id"]: i for i, box in enumerate(boxes)}
    pages.sort(key=lambda page: position[page["batch"]])
    for box in boxes:
        box["count"] = sizes[box["id"]]

    last: dict[str, str] = {}
    for page in pages:
        if page.get("status") == STATUS_SKIPPED:
            page.setdefault("join_prev_id", None)
            continue
        prev = last.get(page["batch"])
        if page.get("join_prev_id") != prev or (prev is None and page.get("join_prev")):
            clear_seam(page)
        page["join_prev_id"] = prev
        last[page["batch"]] = page["id"]

    doc["pages"] = pages
    doc["batches"] = boxes
    return doc


def migrate_v1(doc: dict) -> dict:
    """Turn a version-1 manifest into chapter boxes, in place, and return it.

    Version 1 grouped chapters by contiguous RUNS of an upload batch in page order, and
    its pile modes (heading, separator, single) built the whole novel from one heap with
    no record of which page landed in which chapter. So:

    - each run becomes a box, in order of first appearance; an empty batch makes none;
    - pages a batch build put into chapter K go into one box with ``chapter=K``, and a
      page that missed that build (it was still to check) rejoins it — version 1 would
      have made it a chapter of its own at the end of the novel;
    - pages a pile build used go into one locked legacy box, since nothing says which
      chapter each is in.

    Deterministic like normalize, because it runs on every load until a save persists
    it. A migrated built box has no ``built_sig``, so it reads "changed" once and the
    next build checks it against its chapter.
    """
    pages = [p for p in doc.get("pages") or []
             if isinstance(p, dict) and isinstance(p.get("id"), str)]
    old_boxes = {box["id"]: box for box in doc.get("batches") or []
                 if isinstance(box, dict) and isinstance(box.get("id"), str)}
    build = doc.get("build") if isinstance(doc.get("build"), dict) else {}
    page_map = build.get("page_map") if isinstance(build.get("page_map"), dict) else {}

    def ids(value) -> list[str]:
        return [i for i in value if isinstance(i, str)] if isinstance(value, list) else []

    chapter_of: dict[str, int] = {}
    for key in sorted((k for k in page_map if str(k).isdigit() and int(k) > 0), key=int):
        for page_id in ids(page_map[key]):
            chapter_of.setdefault(page_id, int(key))
    legacy_ids: set[str] = set()
    if "*" in page_map:
        legacy_ids = (set(ids(build.get("used_page_ids"))) | set(ids(page_map["*"]))) \
            - set(chapter_of)

    # Version 1 stitched across batches, so each seam was decided against the previous
    # page of the whole novel. normalize then clears those whose neighbour changes.
    prev_id = None
    for page in pages:
        if page.get("status") != STATUS_SKIPPED:
            page["join_prev_id"] = prev_id
            prev_id = page["id"]

    taken: set[str] = set()
    boxes: list[dict] = []

    def make_box(batch, first: dict, *, legacy: bool = False) -> dict:
        old = old_boxes.get(batch) if isinstance(batch, str) else None
        if legacy:
            box_id = _derive_id(f"legacy|{first['id']}", taken)
        elif isinstance(batch, str) and _ID_RE.match(batch) and batch not in taken:
            box_id = batch
        else:
            box_id = _derive_id(f"{batch}|{first['id']}", taken)
        taken.add(box_id)
        box = {"id": box_id, "label": "" if legacy else clean_title((old or {}).get("label")),
               "at": str((old or {}).get("at") or first.get("added_at") or ""),
               "count": 0, "chapter": None, "built_sig": None}
        if legacy:
            box["legacy"] = True
        boxes.append(box)
        return box

    by_chapter: dict[int, dict] = {}
    legacy_box = None
    run_batch = object()
    run_box = None
    for page in pages:
        batch = page.get("batch")
        if batch != run_batch:
            run_batch, run_box = batch, None
        if page["id"] in chapter_of:
            k = chapter_of[page["id"]]
            box = by_chapter.get(k)
            if box is None:
                # The run's unbuilt pages so far were this chapter's opening photos.
                if run_box is not None and not is_built(run_box):
                    box = run_box
                else:
                    box = make_box(batch, page)
                box["chapter"] = k
                by_chapter[k] = box
            run_box = box
        elif page["id"] in legacy_ids:
            if legacy_box is None:
                legacy_box = make_box(batch, page, legacy=True)
            box = legacy_box
            run_box = None
        else:
            if run_box is None:
                run_box = make_box(batch, page)
            box = run_box
        page["batch"] = box["id"]

    # Version 1 built an undecided seam as a paragraph break. Deciding it now would let
    # the next build join it differently, rewriting — and asking to re-translate — a
    # chapter nobody touched.
    for page in pages:
        if ((page["id"] in chapter_of or page["id"] in legacy_ids)
                and page.get("status") != STATUS_SKIPPED
                and page.get("join_prev_id") and not page.get("join_prev")):
            set_seam(page, kind="paragraph", glue="space", source="migrated",
                     reason="built as a new paragraph before chapter boxes",
                     prev=page["join_prev_id"])

    doc["pages"] = pages
    doc["batches"] = boxes
    doc["version"] = MANIFEST_VERSION
    return normalize(doc)


# ---- changes -----------------------------------------------------------------
# Every change below ends normalized, and refuses with a BoxError BEFORE touching
# anything, so a refusal never leaves a half-made change behind.

def _moving(doc: dict, ids: list[str]) -> list[dict]:
    """The pages ``ids`` name, once each and in that order — or a refusal."""
    by_id = {page["id"]: page for page in doc.get("pages", [])}
    wanted = list(dict.fromkeys(ids))
    if any(page_id not in by_id for page_id in wanted):
        raise BoxError(404, "That photo isn't in this novel any more. Reload and try again.")
    locked = {box["id"] for box in doc.get("batches", []) if box.get("legacy")}
    if any(by_id[page_id].get("batch") in locked for page_id in wanted):
        raise BoxError(409, _BOX_LOCKED)
    return [by_id[page_id] for page_id in wanted]


def create_box(doc: dict, label: str = "", *, after: str | None = None,
               ids: list[str] | None = None) -> dict:
    """Add a chapter box — at the end, or straight after ``after`` — and move ``ids``
    into it, in that order, and return it.

    One step rather than create-then-move, so "Split chapter here" can't leave an
    empty box behind when the move is refused. ``after`` may be the last built box or
    any unbuilt one; anything earlier would put a new chapter between two built ones,
    and it is refused rather than nudged along, which would quietly carry the photos
    away from the chapter they were split off.
    """
    boxes = doc.setdefault("batches", [])
    at = len(boxes)
    if after:
        anchor = find_box(doc, after)
        if anchor is None:
            raise BoxError(404, BOX_GONE)
        at = boxes.index(anchor) + 1
        if at < sum(1 for box in boxes if is_built(box)):
            raise BoxError(409, "A new chapter can only go after the last built chapter: "
                                "built chapters keep their numbers. Start over to put one "
                                "between them.")
    moving = _moving(doc, ids or [])
    box = {"id": _fresh_id({b["id"] for b in boxes}), "label": clean_title(label),
           "at": now_iso(), "count": 0, "chapter": None, "built_sig": None}
    boxes.insert(at, box)
    if moving:
        move_pages(doc, [page["id"] for page in moving], box["id"])
    else:
        normalize(doc)
    return box


def rename_box(doc: dict, box_id: str, label: str) -> dict:
    """Retitle a box. On a built box this changes its signature, so the next build
    gives the chapter its new title."""
    box = find_box(doc, box_id)
    if box is None:
        raise BoxError(404, BOX_GONE)
    if box.get("legacy"):
        raise BoxError(409, _BOX_LOCKED)
    box["label"] = clean_title(label)
    return box


def reorder_boxes(doc: dict, ids: list[str]) -> bool:
    """Put the boxes in the order ``ids`` gives. Returns whether anything moved.

    ``ids`` must name every box exactly once — a partial list would lose the boxes it
    leaves out — and the built boxes at the front must stay where they are, since their
    chapter numbers are already fixed.
    """
    current = [box["id"] for box in doc.get("batches", [])]
    if sorted(ids) != sorted(current):
        raise BoxError(400, "That chapter order doesn't match this novel's chapters. "
                            "Reload and try again.")
    fixed = sum(1 for box in doc["batches"] if is_built(box))
    if list(ids[:fixed]) != current[:fixed]:
        raise BoxError(409, "Built chapters keep their place. Only the chapters after "
                            "them can move.")
    by_id = {box["id"]: box for box in doc["batches"]}
    doc["batches"] = [by_id[box_id] for box_id in ids]
    normalize(doc)
    return list(ids) != current


def delete_box(doc: dict, box_id: str, expect_page_ids: list[str] | None = None
               ) -> list[dict]:
    """Remove an unbuilt box and its pages, and return the removed pages (so the caller
    can unlink their files).

    ``expect_page_ids`` is the list of photos the reader was shown when they confirmed.
    A photo uploaded into the box after that would otherwise be deleted unseen.
    """
    box = find_box(doc, box_id)
    if box is None:
        raise BoxError(404, BOX_GONE)
    if is_built(box):
        raise BoxError(409, "That chapter is already built, so deleting it would renumber "
                            "every chapter after it. Move its photos out instead, or use "
                            "Start over.")
    removed = [page for page in doc.get("pages", []) if page.get("batch") == box_id]
    if expect_page_ids is not None and set(expect_page_ids) != {p["id"] for p in removed}:
        raise BoxError(409, "The photos in that chapter changed. Look again before "
                            "deleting it.")
    doc["batches"] = [b for b in doc["batches"] if b is not box]
    doc["pages"] = [page for page in doc["pages"] if page.get("batch") != box_id]
    normalize(doc)
    return removed


def add_page(doc: dict, *, ext: str, data_len: int, digest: str,
             batch: str, name: str = "") -> dict:
    """Add a page at the end of its box and return it. The caller writes the file
    itself, using the returned ``file`` name.

    An unknown box is refused rather than replaced by a new one: a drop whose box was
    deleted meanwhile would otherwise scatter the rest of its photos into a chapter of
    their own.
    """
    box = find_box(doc, batch)
    if box is None:
        raise BoxError(404, BOX_GONE)
    if box.get("legacy"):
        raise BoxError(409, _BOX_LOCKED)
    seq = int(doc.get("next_seq") or 1)
    doc["next_seq"] = seq + 1
    page = {
        "id": _fresh_id({p["id"] for p in doc.get("pages", [])}),
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
        "join_prev_id": None,
        "hangul_fraction": 0.0,
        "chars": 0,
        "ocr": None,
        "verify": None,
        "error": None,
    }
    doc.setdefault("pages", []).append(page)
    normalize(doc)
    return page


def move_pages(doc: dict, ids: list[str], box_id: str, before: str | None = None) -> bool:
    """Move pages, as one block in the order given, into ``box_id`` just before the page
    ``before`` — or at the end of the box. Returns whether anything moved.

    Any page named that isn't here refuses the whole move: moving the rest would put
    them somewhere the reader never asked for. Moving a page to where it already is
    changes nothing, so no seam is reset.
    """
    if not ids:
        raise BoxError(400, "Choose the photos to move.")
    target = find_box(doc, box_id)
    if target is None:
        raise BoxError(404, BOX_GONE)
    if target.get("legacy"):
        raise BoxError(409, _BOX_LOCKED)
    moving = _moving(doc, ids)
    pages = doc.get("pages", [])
    in_box = [page for page in pages if page.get("batch") == box_id]
    if before is not None and all(page["id"] != before for page in in_box):
        raise BoxError(400, "That photo isn't in that chapter any more. Reload and try again.")

    moving_ids = {page["id"] for page in moving}
    anchor = None
    if before is not None:
        # Dropped just before itself, or before another photo that is moving with it:
        # the block goes before the next photo that stays put.
        start = next(i for i, page in enumerate(in_box) if page["id"] == before)
        anchor = next((page["id"] for page in in_box[start:]
                       if page["id"] not in moving_ids), None)

    before_move = [(page["id"], page.get("batch")) for page in pages]
    rest = [page for page in pages if page["id"] not in moving_ids]
    for page in moving:
        page["batch"] = box_id
    if anchor is None:
        rest.extend(moving)  # normalize's stable sort carries them to the end of the box
    else:
        at = next(i for i, page in enumerate(rest) if page["id"] == anchor)
        rest[at:at] = moving
    doc["pages"] = rest
    normalize(doc)
    return [(page["id"], page["batch"]) for page in doc["pages"]] != before_move


def delete_pages(doc: dict, ids: list[str]) -> list[dict]:
    """Remove pages from the manifest and return the removed records (so the caller
    can unlink their files)."""
    wanted = {str(i) for i in ids}
    removed = [p for p in doc.get("pages", []) if p.get("id") in wanted]
    doc["pages"] = [p for p in doc.get("pages", []) if p.get("id") not in wanted]
    normalize(doc)
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
        row["has_text"] = page_has_text(page)
        slim.append(row)
    return {
        "pages": slim,
        "batches": box_rows(doc),
        "build": doc.get("build"),
        "counts": counts(doc),
        "totals": doc.get("totals", {}),
        "chapter_count": chapter_count(doc),
        "rev": doc.get("rev", 0),
    }
