"""Taking one photo into a novel: the step every way in shares.

The desktop's drop, the phone, a scanner PDF's pages and the Telegram bot all end here,
so a photo is checked, recognised if it is already in the novel, and placed by one set of
rules, with one set of messages.

Nothing here imports app.py. The Telegram poller and the PDF reader run on threads of
their own and call this directly; the web layer turns a refusal into an HTTP answer.

Every function here takes the manifest lock, so call them from a worker thread, never
from the event loop: the lock is a thread lock, and waiting on it there stalls every
request the app is serving.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import pages as pages_mod


class IntakeError(pages_mod.BoxError):
    """A photo that has to be refused, with the HTTP status to refuse it with."""


@dataclass
class Arrival:
    """What happened to one photo. ``doc`` is the manifest as it was saved."""

    page: dict
    doc: dict
    duplicate: bool = False
    # The photo arrived in a finished hands-free chapter, which now waits for Done again.
    reopened: bool = False
    # Retake in place: the page this photo replaced, and that page's file (gone now).
    replaced: str | None = None
    unlinked: list[str] = field(default_factory=list)


def check_image(data: bytes) -> str:
    """The file's real type, decided by its bytes, or a refusal saying why not."""
    if not data:
        raise IntakeError(400, "No image was uploaded.")
    ext = pages_mod.sniff_image(data[:32])
    if ext is None:
        raise IntakeError(400, pages_mod.unsupported_reason(data[:32]))
    return ext


def _write(pid: str, page: dict, data: bytes) -> None:
    target = pages_mod.pages_dir(pid) / page["file"]
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    except OSError as exc:
        # Raised inside the lock, so the save is skipped and the manifest never names a
        # file that isn't there.
        raise IntakeError(500, f"Couldn't save that image: {exc}")


def add_photo(pid: str, data: bytes, *, batch: str = "", label: str = "", name: str = "",
              origin: str | None = None, hands_free: bool = False) -> Arrival:
    """Add one photo at the end of box ``batch``, or of a new box at the end of the novel.

    A photo already in the novel is not added again: re-dropping a folder is a no-op, not
    a doubled novel. A new box is made with ``origin`` and ``hands_free`` when given; an
    origin the novel already has is that box (see pages.create_box).

    A photo arriving in a finished hands-free chapter opens it again, so it waits for Done
    rather than building without the pages that may follow.
    """
    ext = check_image(data)
    digest = pages_mod.sha256_of(data)
    with pages_mod.mutate_pages(pid) as doc:
        existing = pages_mod.find_by_hash(doc, digest)
        if existing is not None:
            arrival = Arrival(page=dict(existing), doc=doc, duplicate=True)
        else:
            # Read when called, not imported: a test lowers it.
            limit = pages_mod.MAX_PAGES_PER_PROJECT
            if len(doc.get("pages", [])) >= limit:
                raise IntakeError(400, f"This novel already has {limit} pages.")
            into = batch or pages_mod.create_box(doc, label, origin=origin,
                                                 hands_free=hands_free)["id"]
            page = pages_mod.add_page(doc, ext=ext, data_len=len(data), digest=digest,
                                      name=name, batch=into)
            box = pages_mod.find_box(doc, into)
            reopened = bool(box and pages_mod.is_hands_free(box) and box.get("captured_at"))
            if reopened:
                pages_mod.reopen_box(box)
            _write(pid, page, data)
            arrival = Arrival(page=dict(page), doc=doc, reopened=reopened)
    return arrival


@dataclass
class Bundle:
    """What happened to several photos taken in at once: a PDF's pages."""

    doc: dict
    batch: str | None
    added: list[dict] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)
    reopened: bool = False


class _Nothing(Exception):
    """Every photo was in the novel already: nothing to save."""


def add_photos(pid: str, files: list[tuple[str, bytes]], *, batch: str = "", label: str = "",
               origin: str | None = None, hands_free: bool = False,
               captured: bool = False) -> Bundle:
    """Add several photos in one save, in order, at the end of box ``batch`` — or of a new
    box at the end of the novel. A photo already in the novel, or twice in ``files``, is
    left out.

    ``captured`` finishes a new hands-free box in the same save: a PDF sent with no
    chapter open is a whole chapter. Into an existing box, the arrival rule applies as for
    one photo: a finished hands-free chapter opens again, and its sender finishes it.
    """
    checked = []
    for name, data in files:
        checked.append((name, data, check_image(data), pages_mod.sha256_of(data)))
    added: list[dict] = []
    duplicates: list[dict] = []
    try:
        with pages_mod.mutate_pages(pid) as doc:
            seen: set[str] = set()
            fresh = []
            for name, data, ext, digest in checked:
                existing = pages_mod.find_by_hash(doc, digest)
                if existing is not None or digest in seen:
                    duplicates.append(dict(existing) if existing else {"name": name})
                    continue
                seen.add(digest)
                fresh.append((name, data, ext, digest))
            if not fresh:
                raise _Nothing()
            limit = pages_mod.MAX_PAGES_PER_PROJECT
            if len(doc.get("pages", [])) + len(fresh) > limit:
                raise IntakeError(400, f"That would take this novel past {limit} pages.")
            into = batch or pages_mod.create_box(doc, label, origin=origin,
                                                 hands_free=hands_free)["id"]
            box = pages_mod.find_box(doc, into)
            reopened = bool(batch and box and pages_mod.is_hands_free(box)
                            and box.get("captured_at"))
            for name, data, ext, digest in fresh:
                page = pages_mod.add_page(doc, ext=ext, data_len=len(data), digest=digest,
                                          name=name, batch=into)
                _write(pid, page, data)
                added.append(dict(page))
            if reopened:
                pages_mod.reopen_box(box)
            if captured and not batch:
                pages_mod.set_captured(doc, into)
    except _Nothing:
        return Bundle(doc=pages_mod.load_pages(pid), batch=None, duplicates=duplicates)
    return Bundle(doc=doc, batch=into, added=added, duplicates=duplicates, reopened=reopened)


def replace_page(pid: str, page_id: str, data: bytes, *, name: str = "") -> Arrival:
    """Retake in place: a new photo for page ``page_id``, in the same place in its box.

    The new photo is a new page, with its own id and file, because a page's image is
    served as never changing: the phone and the desktop would go on showing the old
    photo under the old id. The old page and its file go in the same save. A read still
    running for the old page finds it gone, and its result is dropped.

    It is not an arrival: a finished chapter stays finished, since nothing was added.
    The same bytes again change nothing. A photo that is another page already is refused,
    rather than one page of the story appearing twice.
    """
    ext = check_image(data)
    digest = pages_mod.sha256_of(data)
    with pages_mod.mutate_pages(pid) as doc:
        old = pages_mod.find_page(doc, page_id)
        if old is None:
            raise IntakeError(404, "That page isn't in this novel.")
        box = pages_mod.find_box(doc, old.get("batch"))
        if box is None or box.get("legacy"):
            raise IntakeError(409, pages_mod.BOX_LOCKED)
        if old.get("sha256") == digest:
            return Arrival(page=dict(old), doc=doc, duplicate=True)
        other = pages_mod.find_by_hash(doc, digest)
        if other is not None:
            group = [p for p in doc["pages"] if p.get("batch") == other.get("batch")]
            n = next(i for i, p in enumerate(group, start=1) if p["id"] == other["id"])
            number = pages_mod.box_numbers(doc).get(other.get("batch"))
            where = f"photo {n} of chapter {number}" if number else f"photo {n}"
            raise IntakeError(409, f"That photo is already in this novel, as {where}.")
        new = pages_mod.add_page(doc, ext=ext, data_len=len(data), digest=digest,
                                 name=name or old.get("name") or "", batch=old["batch"])
        # add_page put it at the end of the box; it takes the old page's place instead.
        doc["pages"].remove(new)
        doc["pages"][doc["pages"].index(old)] = new
        pages_mod.normalize(doc)
        _write(pid, new, data)
        arrival = Arrival(page=dict(new), doc=doc, replaced=old["id"],
                          unlinked=[str(old.get("file") or "")])
    folder = pages_mod.pages_dir(pid)
    for file in arrival.unlinked:
        if file:
            try:
                (folder / file).unlink(missing_ok=True)
            except OSError:
                pass  # the manifest no longer names it; a stray file is harmless
    return arrival
