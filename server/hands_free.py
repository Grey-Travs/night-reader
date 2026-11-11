"""Hands-free: what to do next for the chapters sent from the phone, decided without I/O.

The phone, a share or Telegram fill a chapter box and say when it's finished. From then
on nobody presses a button: each photo is read as it lands, a page Claude is unsure of
is double-checked, a finished chapter whose pages are all clean builds, and a built
chapter is translated. Anything that needs a person holds that chapter, and the phone is
told once.

Pure, like ocr_build: it reads a manifest and answers. app.py does the reading, the
queueing and the telling.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field

from translation_bot.ocr_join import propose_join

from . import pages as pages_mod

NOT_FROM_PHONE = ("Wasn't sent from the phone, so it builds when you press Build on the "
                  "computer.")


def paused(doc: dict) -> bool:
    """Stop was pressed for this novel's hands-free work, and Resume hasn't been."""
    return bool(doc.get("hands_free_paused_at"))


def any_boxes(doc: dict) -> bool:
    return any(pages_mod.is_hands_free(box) for box in doc.get("batches", []))


def worth_building(doc: dict) -> bool:
    """A cheap first check: is there a finished phone chapter that isn't built as it is
    now? Building takes the manifest lock, so a look skips it when nothing could build."""
    for box, pages in pages_mod.box_pages(doc):
        if not pages or not pages_mod.is_hands_free(box) or pages_mod.is_capturing(box):
            continue
        if not box.get("chapter") or pages_mod.box_signature(box, pages) != box.get("built_sig"):
            return True
    return False


def _position(pages: list[dict], page: dict) -> int:
    return next(i for i, p in enumerate(pages, start=1) if p["id"] == page["id"])


def photo_names(positions: list[int]) -> str:
    """"photo 4", "photos 4 and 9", "photos 4, 9 and 12": by place in the chapter, which
    is what the phone shows. A phone's file names mean nothing to the person reading."""
    positions = sorted(dict.fromkeys(positions))
    if len(positions) == 1:
        return f"photo {positions[0]}"
    listed = [str(n) for n in positions]
    return "photos " + ", ".join(listed[:-1]) + " and " + listed[-1]


# ---- holding a chapter back ----------------------------------------------------------------

def opens_chapter_mid_box(pages: list[dict]) -> dict | None:
    """A photo after the first that opens with a chapter heading: most likely "Chapter 13
    →" was never tapped, and built as one, two chapters would be translated as one.

    A join the reader set wins, either way: "keep it in this chapter" is a join set to
    paragraph, and a chapter join they chose is their own decision.
    """
    live = pages_mod.live_pages(pages)
    for prev, page in zip(live, live[1:]):
        if page.get("join_prev_source") == "user":
            continue
        kind = page.get("join_prev") or ""
        if not kind and pages_mod.page_has_text(prev) and pages_mod.page_has_text(page):
            kind = propose_join(prev.get("text") or "", page.get("text") or "", prev, page).kind
        if kind == "chapter":
            return page
    return None


def auto_hold(box: dict, pages: list[dict], number: int, *,
              has_english: Callable[[int], bool], translating: set[int]) -> str | None:
    """Why the automatic build must leave this box alone, or None. A manual Build never
    asks: these are reasons for a person to look first, not reasons the box can't build.

    Asked of every unbuilt box, and of built boxes that changed since they were built.
    """
    if not pages_mod.is_hands_free(box):
        return NOT_FROM_PHONE
    built = box.get("chapter")
    if built:
        if built in translating:
            return ("Changed while it was being translated. It waits for that to finish, "
                    "then needs the computer.")
        if has_english(built):
            return ("Changed after it was translated. Rebuild it on the computer, then "
                    "translate it again.")
    mid = opens_chapter_mid_box(pages)
    if mid is not None:
        n = _position(pages, mid)
        return (f"Photo {n} opens with a chapter heading. Split the chapter there, or keep "
                f"it as one chapter.")
    expect = box.get("expect_number")
    if expect and number != expect:
        return (f"Sent as chapter {expect}, but it would build as chapter {number}. Check "
                f"the chapter order on the computer.")
    return None


# ---- the work to queue -------------------------------------------------------------------------

@dataclass
class Work:
    reads: list[dict] = field(default_factory=list)
    retries: list[dict] = field(default_factory=list)
    verifies: list[dict] = field(default_factory=list)


def page_work(doc: dict, *, image_exists: Callable[[dict], bool]) -> Work:
    """The page work hands-free queues, in box order: only pages in hands-free boxes.

    - Every unread photo is read as soon as it is there, so a bad one is known about
      while the chapter can still be shot again.
    - A photo that couldn't be read is read once more, and only once.
    - A reading Claude was unsure of is double-checked, once per reading: a fresh read
      clears ``verify``, a finished double-check fills it in. So is a confident reading
      of a photo the quick look thought should be taken again.
    """
    work = Work()
    for box, pages in pages_mod.box_pages(doc):
        if not pages_mod.is_hands_free(box):
            continue
        for page in pages:
            status = page.get("status") or pages_mod.STATUS_NEW
            unchecked = page.get("verify") is None and pages_mod.page_has_text(page)
            if status == pages_mod.STATUS_NEW:
                if image_exists(page):
                    work.reads.append(page)
            elif status == pages_mod.STATUS_FAILED:
                if not page.get("auto_retried") and image_exists(page):
                    work.retries.append(page)
            elif status == pages_mod.STATUS_NEEDS_CHECK and unchecked:
                work.verifies.append(page)
            elif (status == pages_mod.STATUS_OK and unchecked
                  and (page.get("look") or {}).get("verdict") == "retake"):
                work.verifies.append(page)
    return work


def to_translate(doc: dict, *, chapters: dict[int, object], is_korean: Callable[[object], bool],
                 has_english: Callable[[int], bool], state_of: Callable[[int], str | None]
                 ) -> list[int]:
    """The built hands-free chapters to translate: finished on the phone, unchanged since
    they were built, Korean, with no English yet, and never tried before. One that failed,
    or has English of any kind, is left to the reader: trying again by itself, it could
    spend the plan on the same failure over and over."""
    out = []
    for box, pages in pages_mod.box_pages(doc):
        k = box.get("chapter")
        if (not k or not pages_mod.is_hands_free(box) or pages_mod.is_capturing(box)
                or pages_mod.box_signature(box, pages) != box.get("built_sig")):
            continue
        chapter = chapters.get(k)
        if chapter is None or not is_korean(chapter) or has_english(k):
            continue
        if state_of(k) not in (None, "", "pending"):
            continue
        out.append(k)
    return out


def still_translatable(doc: dict, k: int) -> bool:
    """Checked again just before chapter ``k``'s translation starts. A photo that arrived
    since it was queued opened the box again, or changed it, and translated now it would
    be half a chapter, or yesterday's text."""
    for box, pages in pages_mod.box_pages(doc):
        if box.get("chapter") == k:
            return (pages_mod.is_hands_free(box) and not pages_mod.is_capturing(box)
                    and pages_mod.box_signature(box, pages) == box.get("built_sig"))
    return False


# ---- what needs a person --------------------------------------------------------------------------

@dataclass
class Held:
    """One thing that needs the reader. ``key`` is what the phone was told about it, so
    the same problem is pushed once, and a new reading of a page that is still unsure is
    pushed again."""

    key: str
    box: str
    number: int | None
    kind: str
    message: str
    page_id: str | None = None
    photo: int | None = None


def _key_time(page: dict, field_name: str) -> str:
    return str((page.get(field_name) or {}).get("at") or "")


def page_problems(box: dict, pages: list[dict], number: int | None, *,
                  image_exists: Callable[[dict], bool]) -> list[Held]:
    """The photos of one hands-free box that hands-free has done all it can with."""
    out = []
    for n, page in enumerate(pages, start=1):
        status = page.get("status") or pages_mod.STATUS_NEW
        base = dict(box=box["id"], number=number, page_id=page["id"], photo=n)
        if status == pages_mod.STATUS_SKIPPED:
            continue
        if status == pages_mod.STATUS_NEEDS_CHECK and page.get("verify") is not None:
            out.append(Held(key=f"{page['id']}:to-check:{_key_time(page, 'verify')}",
                            kind="to-check", message=f"Photo {n} needs checking.", **base))
        elif status == pages_mod.STATUS_FAILED and page.get("auto_retried"):
            out.append(Held(key=f"{page['id']}:failed:{_key_time(page, 'ocr')}",
                            kind="failed", message=f"Photo {n} couldn't be read.", **base))
        elif status in (pages_mod.STATUS_OK, pages_mod.STATUS_EDITED,
                        pages_mod.STATUS_NEEDS_CHECK) and not pages_mod.page_has_text(page):
            out.append(Held(key=f"{page['id']}:no-text:{_key_time(page, 'ocr')}",
                            kind="no-text", message=f"Photo {n} has no text.", **base))
        elif status == pages_mod.STATUS_NEW and not image_exists(page):
            out.append(Held(key=f"{page['id']}:missing", kind="missing",
                            message=f"Photo {n}'s image is missing.", **base))
    return out


def needs_reader(doc: dict, waiting: list[dict], *, image_exists: Callable[[dict], bool],
                 refused: str = "") -> list[Held]:
    """Everything about the hands-free chapters that is waiting for a person.

    ``waiting`` is a build's list of the boxes it couldn't build, worked out with
    hands-free's holds, so a box's own reason comes first. Page problems are listed for
    every hands-free box, finished or not: a bad photo is worth knowing about while the
    chapter can still be shot again. A chapter's own reasons only once it is finished.
    """
    numbers = pages_mod.box_numbers(doc)
    boxes = {box["id"]: (box, pages) for box, pages in pages_mod.box_pages(doc)}
    by_number = {w["number"]: w for w in waiting}
    out: list[Held] = []
    for box, pages in boxes.values():
        if pages_mod.is_hands_free(box):
            out.extend(page_problems(box, pages, numbers.get(box["id"]),
                                     image_exists=image_exists))
    first_captured = None
    for w in waiting:
        box, pages = boxes.get(w["batch"], (None, None))
        if box is None or not pages_mod.is_hands_free(box) or pages_mod.is_capturing(box):
            continue
        first_captured = first_captured or box
        number = w["number"]
        if w.get("held"):
            reason = w["reasons"][0]
            mid = opens_chapter_mid_box(pages)
            n = _position(pages, mid) if mid is not None else None
            if n is not None and reason.startswith(f"Photo {n} opens with a chapter heading"):
                # On a photo, so the phone can offer Split here or Keep together on it.
                out.append(Held(key=f"box:{box['id']}:heading:{mid['id']}", box=box["id"],
                                number=number, kind="heading", message=reason,
                                page_id=mid["id"], photo=n))
                continue
            tag = hashlib.sha1(reason.encode("utf-8")).hexdigest()[:8]
            out.append(Held(key=f"box:{box['id']}:hold:{tag}", box=box["id"],
                            number=number, kind="hold", message=reason))
        elif w.get("blocked_by") is not None:
            ahead = by_number.get(w["blocked_by"])
            ahead_box = boxes.get(ahead["batch"], (None, None))[0] if ahead else None
            if ahead_box is None:
                continue
            if pages_mod.is_capturing(ahead_box):
                message = (f"Chapter {number} is waiting for chapter {w['blocked_by']}, "
                           f"which was never finished. Tap Done on it, or delete it.")
            elif not pages_mod.is_hands_free(ahead_box):
                message = (f"Chapter {number} is waiting for chapter {w['blocked_by']}, "
                           f"which was added on the computer. Build it there.")
            else:
                continue  # that chapter's own problems are reported for it
            out.append(Held(key=f"box:{box['id']}:after:{ahead_box['id']}", box=box["id"],
                            number=number, kind="after", message=message))
        elif not pages:
            out.append(Held(key=f"box:{box['id']}:empty", box=box["id"], number=number,
                            kind="empty", message=f"Chapter {number} has no photos."))
    if refused and first_captured is not None:
        tag = hashlib.sha1(refused.encode("utf-8")).hexdigest()[:8]
        out.append(Held(key=f"box:{first_captured['id']}:refused:{tag}",
                        box=first_captured["id"], number=numbers.get(first_captured["id"]),
                        kind="refused", message=refused))
    return out


def untold(doc: dict, held: list[Held]) -> list[Held]:
    """The problems the phone hasn't been told about yet."""
    told = {box["id"]: set(box.get("notified") or []) for box in doc.get("batches", [])}
    return [h for h in held if h.key not in told.get(h.box, set())]


def mark_told(doc: dict, held: list[Held]) -> None:
    """Remember what was pushed, on its box, so a restart doesn't push it again."""
    for box in doc.get("batches", []):
        keys = [h.key for h in held if h.box == box["id"]]
        if keys and pages_mod.is_hands_free(box):
            box["notified"] = list(dict.fromkeys([*(box.get("notified") or []), *keys]))


# ---- what the phone is told -------------------------------------------------------------------------

def _chapters_title(numbers: list[int], verb_one: str, verb_many: str) -> str:
    numbers = sorted(dict.fromkeys(n for n in numbers if n))
    if len(numbers) == 1:
        return f"Chapter {numbers[0]} {verb_one}"
    if not numbers:
        return f"A chapter {verb_one}"
    return f"{len(numbers)} chapters {verb_many}"


def notice_needs_you(novel: str, held: list[Held]) -> tuple[str, str]:
    """One push for everything new in one look."""
    title = _chapters_title([h.number for h in held], "needs you", "need you")
    lines = []
    by_chapter: dict[int | None, list[Held]] = {}
    for h in held:
        by_chapter.setdefault(h.number, []).append(h)
    groups = (("to-check", "need checking", "needs checking"),
              ("failed", "couldn't be read", "couldn't be read"),
              ("no-text", "have no text", "has no text"),
              ("missing", "are missing their image", "is missing its image"))
    grouped = {kind for kind, _many, _one in groups}
    for number, items in by_chapter.items():
        parts = []
        for kind, many, one in groups:
            photos = [h.photo for h in items if h.kind == kind and h.photo]
            if photos:
                parts.append(f"{photo_names(photos)} {many if len(photos) > 1 else one}")
        said = "; ".join(parts)
        others = [h.message for h in items if h.kind not in grouped]
        head = f"Chapter {number}" if number else "A chapter"
        if said:
            lines.append(f"{head}: {said}.")
        lines.extend(others)
    body = f"{novel}: " + " ".join(lines)
    return title, body + " Fix it in Night Reader and the chapter carries on by itself."


def notice_translated(novel: str, number: int, chapter_title: str, flagged: int = 0
                      ) -> tuple[str, str]:
    name = chapter_title or f"chapter {number}"
    if flagged:
        return (f"Chapter {number} is translated",
                f"{novel}: {name} is ready to read. Its check flagged {flagged} "
                f"thing{'s' if flagged != 1 else ''} to look at.")
    return f"Chapter {number} is translated", f"{novel}: {name} is ready to read."


def notice_failed(novel: str, number: int, error: str) -> tuple[str, str]:
    first = (error or "something went wrong").strip().splitlines()[0][:200]
    return (f"Chapter {number} couldn't be translated",
            f"{novel}: {first}. Open Night Reader to try again.")


def notice_waiting(novel: str, resume_at: str) -> tuple[str, str]:
    when = f" at about {resume_at}" if resume_at else " when it refreshes"
    return ("Waiting for your Claude plan",
            f"{novel}: the plan's limit was reached. Night Reader carries on by itself{when}.")


def notice_gave_up(novel: str, retry_at: str) -> tuple[str, str]:
    when = f"at about {retry_at}" if retry_at else "later"
    return ("Stopped waiting for your Claude plan",
            f"{novel}: the plan's limit kept coming back. Night Reader tries again {when}, "
            f"or press Resume.")


def notice_back(novel: str, reads: int, chapters: int) -> tuple[str, str]:
    parts = []
    if reads:
        parts.append(f"{reads} photo{'s' if reads != 1 else ''} to read")
    if chapters:
        parts.append(f"{chapters} chapter{'s' if chapters != 1 else ''} to translate")
    doing = " and ".join(parts) or "where it left off"
    return "Night Reader is back", f"{novel}: carrying on with {doing}."


# ---- where each photo stands, for the camera's badges ---------------------------------------------------

_ISSUE_TEXT = {
    "missing": "Some text on the photo was missed",
    "extra": "Text was read that isn't on the photo",
    "wrong": "Some words were read wrong",
    "leak": "A header or page number got into the text",
}


def _found(verify: dict) -> str:
    """What the double-check found, in a line the phone can show."""
    if verify.get("verdict") == "unclear":
        return "The double-check gave no clear answer."
    issues = verify.get("issues") or []
    if not issues:
        return "Claude's double-check found a problem."
    first = issues[0] if isinstance(issues[0], dict) else {}
    said = " ".join(str(first.get("page_says") or "").split())[:60]
    what = _ISSUE_TEXT.get(first.get("kind"), "Claude's double-check found a problem")
    more = f" (and {len(issues) - 1} more)" if len(issues) > 1 else ""
    return f"{what}{': ' + said if said else ''}{more}."


def photo_rows(doc: dict, box_id: str, *, looks_on: bool = True) -> list[dict]:
    """Where each photo of one chapter stands, for the camera's badges while it is
    being shot: read, being double-checked because Claude isn't sure, what the check
    found, or couldn't be read — and what the quick look made of it."""
    box = pages_mod.find_box(doc, box_id)
    if box is None:
        return []
    from_phone = str(box.get("origin") or "").startswith("phone:")
    rows = []
    pages = [p for p in doc.get("pages", []) if p.get("batch") == box_id]
    for n, page in enumerate(pages, start=1):
        status = page.get("status") or pages_mod.STATUS_NEW
        look = page.get("look") or None
        prior = page.get("prior_status")
        note = ""
        if status == pages_mod.STATUS_FAILED:
            step = "failed" if page.get("auto_retried") else "reading"
            if step == "failed":
                note = "Claude couldn't read this photo."
        elif status == pages_mod.STATUS_NEEDS_CHECK:
            if page.get("verify") is None:
                step = "double-checking"
            else:
                step = "needs-you"
                note = _found(page["verify"])
        elif status in (pages_mod.STATUS_QUEUED, pages_mod.STATUS_RUNNING):
            doubted = (look or {}).get("verdict") == "retake"
            step = ("double-checking" if prior == pages_mod.STATUS_NEEDS_CHECK
                    or (prior == pages_mod.STATUS_OK and doubted) else "reading")
        elif status == pages_mod.STATUS_NEW:
            step = "reading"
        elif status == pages_mod.STATUS_SKIPPED:
            step = "skipped"
        else:
            step = "read"
            if not pages_mod.page_has_text(page):
                step, note = "needs-you", "No text was read from this photo."
        rows.append({
            "id": page["id"], "n": n, "step": step, "note": note,
            "look": ({"verdict": look.get("verdict"), "note": look.get("note") or "",
                      "why": list(look.get("why") or [])} if look else None),
            "looking": bool(from_phone and looks_on and look is None
                            and status != pages_mod.STATUS_SKIPPED),
        })
    return rows


# ---- where each chapter stands, for the phone ---------------------------------------------------------

STEP_TEXT = {
    "capturing": "Still being photographed.",
    "reading": "Reading the photos.",
    "checking": "Claude isn't sure of a page, so it is double-checking.",
    "needs-you": "",
    "waiting-turn": "",
    "building": "Building the chapter.",
    "translating": "Translating.",
    "translated": "Translated.",
    "failed": "Couldn't be translated. Open Night Reader on the computer to try again.",
}


def status_rows(doc: dict, waiting: list[dict], held: list[Held], *,
                translating: set[int], queued: set[int],
                state_of: Callable[[int], str | None],
                has_english: Callable[[int], bool]) -> list[dict]:
    """Where each hands-free chapter stands, newest last, as the phone shows it."""
    numbers = pages_mod.box_numbers(doc)
    waits = {w["batch"]: w for w in waiting}
    rows = []
    for box, pages in pages_mod.box_pages(doc):
        if not pages_mod.is_hands_free(box):
            continue
        number = numbers.get(box["id"])
        statuses = [p.get("status") or pages_mod.STATUS_NEW for p in pages]
        counts = {
            "read": sum(s in pages_mod.APPROVED_STATUSES for s in statuses),
            "reading": sum(s in (pages_mod.STATUS_QUEUED, pages_mod.STATUS_RUNNING)
                           for s in statuses),
            "unread": sum(s == pages_mod.STATUS_NEW for s in statuses),
            "to_check": sum(s == pages_mod.STATUS_NEEDS_CHECK for s in statuses),
            "failed": sum(s == pages_mod.STATUS_FAILED for s in statuses),
            "skipped": sum(s == pages_mod.STATUS_SKIPPED for s in statuses),
        }
        mine = [h for h in held if h.box == box["id"]]
        k = box.get("chapter")
        changed = bool(k) and pages_mod.box_signature(box, pages) != box.get("built_sig")
        w = waits.get(box["id"])
        message = ""
        if k and not changed and (k in translating or k in queued):
            step = "translating"
        elif k and not changed and state_of(k) == "failed":
            step = "failed"
        elif k and not changed and has_english(k):
            step = "translated"
        elif mine:
            step = "needs-you"
            message = " ".join(h.message for h in mine)
        elif pages_mod.is_capturing(box):
            step = "capturing"
        elif counts["unread"] or counts["reading"]:
            step = "reading"
        elif counts["to_check"]:
            step = "checking"
        elif w and w.get("blocked_by") is not None:
            step = "waiting-turn"
            message = f"Waits for chapter {w['blocked_by']} to be built first."
        elif w:
            step = "needs-you"
            message = w["reasons"][0]
        else:
            step = "building"
        rows.append({
            "batch": box["id"], "number": number, "chapter": k,
            "title": pages_mod.box_title(box, pages_mod.live_pages(pages), number),
            "origin": box.get("origin") or "", "captured": not pages_mod.is_capturing(box),
            "photos": len(pages), "pages": counts, "step": step,
            "message": message or STEP_TEXT.get(step, ""),
            "needs_you": [{"page_id": h.page_id, "photo": h.photo, "kind": h.kind,
                           "message": h.message} for h in mine],
        })
    return rows
