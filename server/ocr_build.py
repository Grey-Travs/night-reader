"""Turn transcribed pages into chapters.

This is where the seam decisions finally matter. A page break inside a sentence must
join with no blank line, because a blank line is a paragraph break — and the
translator preserves paragraph structure faithfully, so a false break here survives
all the way into the English.

Each chapter box builds exactly one chapter, in box order. Box N is chapter N, so a box
that isn't ready holds back every box after it rather than letting them take its number,
and a built chapter is rebuilt in place, from its box as it is now, when its photos
change.

Output is ordinary :class:`~translation_bot.docs_extract.Chapter` objects, which the
caller writes to ``source.json``. From that point an image novel is indistinguishable
from a Google-Doc or pasted-text one, and the whole translation pipeline runs
unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from translation_bot.docs_extract import Chapter
from translation_bot.ocr_join import join_text, looks_like_heading, propose_join
from translation_bot.text_source import make_chapter

from . import pages as pages_mod

_UNSURE_REASON = ("Not sure how these pages join, so it was built as a new paragraph. "
                  "'Work out the joins' can check it.")


def assemble_pages(pages: list[dict]) -> tuple[str, list[str]]:
    """Concatenate page texts honouring each page's ``join_prev``.

    Returns ``(text, warnings)``. A ``gap`` join is joined as a paragraph but reported
    — that is the only place a page you never photographed can surface. A ``chapter``
    join is reported too: a box is one chapter, so a heading inside it is either the
    next chapter's first photo in the wrong box, or a line that only looks like one.
    """
    warnings: list[str] = []
    out = ""

    for page in pages:
        text = (page.get("text") or "").strip()
        if not text:
            continue
        if not out:
            out = text
            continue

        kind = page.get("join_prev") or "paragraph"
        glue = page.get("join_glue") or "space"
        label = page.get("name") or f"page {page.get('seq')}"

        if kind == "gap":
            warnings.append(
                f"A page may be missing just before {label} — the text does not "
                f"follow on. Check whether a page went unphotographed."
            )
            kind = "paragraph"

        if kind == "chapter":
            piece = text
            heading = (page.get("heading") or "").strip()
            # The heading is normally already the page's first line. Restore it when
            # it isn't, or it is lost from the text altogether.
            if heading and not looks_like_heading(text):
                piece = f"{heading}\n\n{text}"
            warnings.append(
                f"{label} opens with a chapter heading in the middle of this box, so it "
                f"was joined as a new paragraph. If it starts the next chapter, move it "
                f"and the photos after it into their own box."
            )
            out = out + "\n\n" + piece
        else:
            out = join_text(out, text, kind, glue)

    return out, warnings


def fill_seams(live: list[dict]) -> list[dict]:
    """Decide every seam nobody has worked out yet, writing the decision onto the page.

    Left undecided, a seam builds as a paragraph break, which is wrong at nearly every
    page boundary of a printed novel — so a build never leaves one to chance. The free
    rules settle what they can. Where they are unsure the seam becomes a paragraph
    break, the safe guess (a wrong sentence join glues two lines together mid-word),
    marked so that 'Work out the joins' still looks at it.

    Writing the decisions back is what keeps a fresh build reading "built": the box's
    signature covers its joins, and it is taken after this runs.

    Returns the pages whose seam the rules were unsure about.
    """
    unsure = []
    for prev, page in zip(live, live[1:]):
        if page.get("join_prev"):
            continue
        join = propose_join(prev.get("text") or "", page.get("text") or "", prev, page)
        if join.needs_model:
            pages_mod.set_seam(page, kind="paragraph", glue="space", source="auto",
                               reason=_UNSURE_REASON, prev=prev["id"])
            unsure.append(page)
        else:
            pages_mod.set_seam(page, kind=join.kind, glue=join.glue, source="auto",
                               reason=join.reason, prev=prev["id"])
    return unsure


def box_chapter(box: dict, live: list[dict], number: int) -> tuple[Chapter, list[str]]:
    """One box's chapter, and its warnings, each saying which chapter it is about."""
    text, warnings = assemble_pages(live)
    chapter = make_chapter(number, pages_mod.box_title(box, live, number), text)
    return chapter, [f"Chapter {number}: {warning}" for warning in warnings]


@dataclass
class BoxBuild:
    """What a build does. ``chapters`` is the whole chapter list to write; ``waiting``
    lists the boxes it couldn't build, and why."""

    chapters: list[Chapter]
    page_map: dict[str, list[str]]
    added: list[int] = field(default_factory=list)
    refreshed: list[int] = field(default_factory=list)
    updated: list[int] = field(default_factory=list)
    waiting: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unworked_seams: int = 0


def build_boxes(doc: dict, existing: list[Chapter], *, include: str = "approved",
                append: bool = True) -> BoxBuild:
    """Work out the chapters from a normalized manifest's boxes.

    Pure: no I/O. It writes what it decides into ``doc`` — the seams it fills, each
    built box's chapter and signature — and the caller decides whether that is saved.

    1. A built box that changed is rebuilt in place, from its photos as they are now.
       It waits instead if a photo isn't ready, never building without it.
    2. Unbuilt boxes are added in box order. The first one that isn't ready stops the
       rest, so box N is always chapter N.
    3. A photo never feeds two chapters: a box waits while one of its photos is still
       part of another chapter's text that this build can't rewrite.

    ``append=False`` starts over: every box is numbered again from 1.
    """
    if append:
        # Numbered entries only: a version-1 pile build's "*" names no chapter.
        page_map = {key: list(ids) for key, ids in
                    ((doc.get("build") or {}).get("page_map") or {}).items()
                    if str(key).isdigit() and isinstance(ids, list)}
        chapters = list(existing)
    else:
        # An empty box would stop the build at itself and drop every chapter after it,
        # so starting over removes empty boxes — built ones too, since every chapter is
        # being rebuilt anyway. That is also the only way to retire a built chapter.
        groups = dict((box["id"], pages) for box, pages in pages_mod.box_pages(doc))
        doc["batches"] = [box for box in doc["batches"] if groups[box["id"]]]
        for box in doc["batches"]:
            box["chapter"] = None
            box["built_sig"] = None
            box.pop("legacy", None)
        page_map = {}
        chapters = []

    groups = dict((box["id"], pages) for box, pages in pages_mod.box_pages(doc))
    result = BoxBuild(chapters=chapters, page_map=page_map)
    blockers = {box["id"]: pages_mod.box_blockers(box, groups[box["id"]], include=include)
                for box in doc["batches"]}

    def live(box: dict) -> list[dict]:
        return pages_mod.live_pages(groups[box["id"]])

    def wait(box: dict, number: int, reasons: list[str], blocked_by: int | None = None):
        result.waiting.append({
            "batch": box["id"], "number": number,
            "title": pages_mod.box_title(box, live(box), number),
            "built": bool(box.get("chapter")), "reasons": reasons, "blocked_by": blocked_by,
        })

    def owners() -> dict[str, int]:
        found: dict[str, int] = {}
        for key, ids in page_map.items():
            for page_id in ids:
                found.setdefault(page_id, int(key))
        return found

    def still_elsewhere(box: dict, owned: dict[str, int], free: set[int]) -> str | None:
        """Why the box must wait for another chapter to let go of a photo, or None."""
        for page in live(box):
            k = owned.get(page["id"])
            if k is None or k == box.get("chapter") or k in free:
                continue
            who = pages_mod.page_names([page], groups[box["id"]])
            held = next((w for w in result.waiting if w["number"] == k and w["built"]), None)
            if held is None:
                return (f"{who} is still part of chapter {k}, and no box builds chapter "
                        f"{k} any more. Use Start over to rebuild the chapters.")
            return (f"{who} is still part of chapter {k}, which has to be updated first. "
                    f"Chapter {k}: {held['reasons'][0]}")
        return None

    def write(box: dict, number: int) -> Chapter:
        pages = groups[box["id"]]
        unsure = fill_seams(live(box))
        chapter, warnings = box_chapter(box, live(box), number)
        if unsure:
            # One warning per chapter, however many seams: a list of thirty identical
            # lines buries every other warning the build has.
            who = pages_mod.page_names(unsure, pages)
            how = ("joins the photo before it, so it starts" if len(unsure) == 1 else
                   "join the photos before them, so each starts")
            warnings.append(f"Chapter {number}: not sure how {who} {how} a new paragraph. "
                            f"Press Work out the joins, then build again, to check.")
        result.warnings.extend(warnings)
        result.unworked_seams += len(unsure)
        page_map[str(number)] = [page["id"] for page in live(box)]
        box["chapter"] = number
        box["built_sig"] = pages_mod.box_signature(box, pages)
        return chapter

    # 1. Built boxes whose photos changed since they were built.
    rewrite = []
    for box in doc["batches"]:
        k = box.get("chapter")
        if not k or pages_mod.box_signature(box, groups[box["id"]]) == box.get("built_sig"):
            continue
        if k > len(chapters):
            wait(box, k, ["This chapter is missing from the built chapter list. Use "
                          "Start over to rebuild the chapters."])
        elif blockers[box["id"]]:
            wait(box, k, [b["message"] for b in blockers[box["id"]]])
        else:
            rewrite.append(box)

    # One photo, one chapter. A box that waits can't release its photos, so whatever
    # was waiting on it waits too — repeated until nothing more changes.
    owned = owners()
    held_back = True
    while held_back:
        held_back = False
        free = {box["chapter"] for box in rewrite}
        for box in list(rewrite):
            reason = still_elsewhere(box, owned, free)
            if reason:
                rewrite.remove(box)
                wait(box, box["chapter"], [reason])
                held_back = True
                break

    for box in rewrite:
        k = box["chapter"]
        chapter = write(box, k)
        (result.refreshed if chapter.paragraphs != chapters[k - 1].paragraphs
         else result.updated).append(k)
        chapters[k - 1] = chapter

    # 2. Unbuilt boxes, in box order, until one isn't ready.
    owned = owners()
    stop = None
    for box in doc["batches"]:
        if pages_mod.is_built(box):
            continue
        number = len(chapters) + 1 + sum(1 for w in result.waiting if not w["built"])
        if stop is not None:
            wait(box, number, [f"Comes after chapter {stop}, which isn't ready yet."],
                 blocked_by=stop)
            continue
        reasons = [b["message"] for b in blockers[box["id"]]]
        if not reasons:
            reason = still_elsewhere(box, owned, set())
            reasons = [reason] if reason else []
        if reasons:
            stop = number
            wait(box, number, reasons)
            continue
        chapters.append(write(box, number))
        result.added.append(number)

    result.page_map = {key: ids for key, ids in page_map.items()
                       if 1 <= int(key) <= len(chapters)}
    result.waiting.sort(key=lambda w: w["number"])
    return result
