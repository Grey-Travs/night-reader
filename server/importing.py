"""Turning already-published chapters into an ordinary Night Reader novel.

The problem this solves is tedium. About 50 novels are live on the site with no local
project, and picking one back up meant copying every posted chapter into a Google Doc by
hand — fine for ten chapters, miserable for a hundred and fifty, and the reason old novels
stay un-resumed.

The Doc was only ever the way text got IN. Night Reader's own storage is ``source.json``
plus ``chapters/*.md``, so importing writes those directly and the Doc step disappears
rather than being automated.

Nothing here touches a browser or a network. The extension reads the site and hands over a
list of plain records; every decision about what lands on disk is made here, where it can
be tested. That is the same division of labour the posting side uses, and for the same
reason.

What an imported novel is
-------------------------
An ordinary ``source_type: "text"`` project, which is the one discriminator the rest of the
app reads (``server/app.py`` in ``get_chapters``): it makes the source come from
``source.json`` and never from the network. Per chapter:

* a **positional** record in ``source.json`` whose FIRST paragraph is the chapter's own
  title, then its prose;
* ``chapters/chapter-NNN.md`` holding the prose as Markdown;
* a ``state.json`` record marked ``validated``, which is the only status the posting side
  treats as done.

The title-as-first-paragraph is not a quirk. ``sanitize.strip_source_header`` exists because
the Korean export puts a header block above the prose, and it reads the chapter number out
of exactly that shape — so writing it means ``parse_chapter_number`` states every number
with high confidence instead of the resolver guessing and the posting side blocking all of
it as unconfirmed.

Why an imported chapter can never be re-translated
--------------------------------------------------
``source.json`` holds the English, which sounds wrong and is the strongest safety property
available. ``classify()`` calls anything under ``min_hangul_fraction`` English, and the
translate queue only ever accepts Korean — so imported chapters are skipped by Translate-all
and by a forced sweep alike. They cannot be re-translated or re-billed by accident, and the
reader already hides the retranslate button for them.
"""

from __future__ import annotations

from datetime import datetime, timezone

from translation_bot.chapter_numbers import IN_SEQUENCE, classify_kind, parse_tab_title
from translation_bot.config import Config
from translation_bot.docs_extract import Chapter
from translation_bot.htmlmd import html_to_markdown, round_trips
from translation_bot.pipeline import write_chapter_file
from translation_bot.state import STATUS_VALIDATED, State
from translation_bot.textsplit import split_paragraphs

from . import projects as pj
from .locks import file_lock

# Where an imported novel came from, recorded on the project so the library can say so.
# A novel with no Google Doc, whose chapters cannot be translated and whose Refresh has
# nothing to fetch, reads as broken unless something explains why — so this is the label,
# not decoration.
PROVENANCE = "imported_from"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def order_chapters(records: list[dict]) -> tuple[list[dict], str]:
    """Put the site's chapter list into reading order, and say how that was decided.

    The site answers newest-first, so the order it gives is backwards. Where every title
    states a number that is the thing to trust; where they do not — side stories, an
    epilogue — the least-worst answer is to reverse what the site said and keep it, because
    the site's own ordering is the only evidence there is.

    Returns the ordered records and a one-line account of the method, which the import
    report shows. An ordering that was inferred rather than read should be visible.
    """
    rows = [dict(r, _at=i) for i, r in enumerate(records or [])]
    numbers = [parse_tab_title(str(r.get("title") or "")) for r in rows]
    stated = [n for n in numbers if n is not None]

    if stated and len(stated) == len(rows) and len(set(stated)) == len(rows):
        for row, n in zip(rows, numbers):
            row["_n"] = n
        rows.sort(key=lambda r: r["_n"])
        return [_strip_private(r) for r in rows], (
            f"by the number in each title ({rows[0]['_n']} to {rows[-1]['_n']})")

    # Not every title states a number. If the ones that do run downwards, the whole list is
    # newest-first and reversing it is right; anything else is left exactly as given rather
    # than half-sorted into an order nobody chose.
    descending = sum(1 for a, b in zip(stated, stated[1:]) if b < a)
    ascending = sum(1 for a, b in zip(stated, stated[1:]) if b > a)
    if descending > ascending:
        rows.reverse()
        return [_strip_private(r) for r in rows], (
            f"reversed the site's own order; {len(stated)} of {len(rows)} titles state a "
            f"number — check anything without one")
    return [_strip_private(r) for r in rows], (
        f"kept the site's own order; only {len(stated)} of {len(rows)} titles state a "
        f"number — check the order before posting")


def _strip_private(row: dict) -> dict:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def prepare(records: list[dict]) -> dict:
    """Convert and inspect a novel's chapters without writing anything.

    The gate in front of the write, and the thing the import report is built from: what
    would land, what could not be represented, and which chapters say something Markdown
    would read back differently.
    """
    ordered, how = order_chapters(records)
    chapters: list[dict] = []
    losses: dict[str, int] = {}
    unfaithful: list[str] = []
    empty: list[str] = []
    demoted: list[dict] = []

    for position, row in enumerate(ordered, start=1):
        title = str(row.get("title") or "").strip() or f"Chapter {position}"
        html = str(row.get("html") or "")
        markdown, lost = html_to_markdown(html)
        for item in lost:
            losses[item] = losses.get(item, 0) + 1
        if not markdown.strip():
            # A chapter with no prose is not importable, and skipping it silently would
            # leave a hole in the numbering that nothing explains.
            empty.append(title)
            continue
        if html and not round_trips(html)[0]:
            # The text is intact; what is at risk is how Markdown reads it back. Named
            # rather than corrected, because there is no escape hatch to correct it with.
            unfaithful.append(title)
        # Ask the resolver itself what it will make of this chapter, rather than
        # reimplementing its rules. A row it does not put IN_SEQUENCE loses its global
        # number, drops out of the reading order, and is blocked from posting -- and the
        # commonest cause is length: anything under 200 non-whitespace characters with no
        # Hangul is classed as an extra, which imported English always is. Short author
        # notes and teaser chapters are exactly that shape, so this has to be said out
        # loud rather than discovered later as a hole in the numbering.
        paragraphs = [title, *split_paragraphs(markdown)]
        kind, _label = classify_kind(title, "\n\n".join(paragraphs))
        if kind not in IN_SEQUENCE:
            demoted.append({"title": title, "kind": kind,
                            "chars": len(markdown.replace(" ", ""))})
        chapters.append({
            "index": len(chapters) + 1,
            "title": title,
            "markdown": markdown,
            "kind": kind,
            # The site's own identifiers, kept so the posting ledger can address a chapter
            # that this app did not create.
            "remote_id": str(row.get("remote_id") or ""),
            "remote_uid": str(row.get("remote_uid") or ""),
            "paid": bool(row.get("paid")),
            "coins": int(row.get("coins") or 0),
            "state": str(row.get("state") or ""),
            "number": parse_tab_title(title),
        })

    return {
        "chapters": chapters,
        "order": how,
        "losses": dict(sorted(losses.items())),
        "unfaithful": unfaithful,
        "empty": empty,
        "demoted": demoted,
        "count": len(chapters),
    }


def existing_import(series_uid: str) -> dict | None:
    """The project a given site series was already imported into, if any.

    Importing twice must not make a second copy of a novel. Matched on the site's own
    series id rather than on the name, because a name can be edited here afterwards and
    the id cannot.
    """
    if not (series_uid or "").strip():
        return None
    for project in pj.list_projects():
        came_from = project.get(PROVENANCE) or {}
        if str(came_from.get("series_uid") or "") == series_uid:
            return project
    return None


def import_novel(name: str, records: list[dict], *, series_uid: str = "",
                 series_url: str = "", site: str = "meiko") -> dict:
    """Create a novel from already-published chapters.

    Writes a complete project: ``project.json``, ``source.json``, one Markdown file per
    chapter and a ``state.json`` marking each one finished. From that point the rest of the
    app cannot tell it apart from a novel that came from a Google Doc, except that it knows
    where it came from and that its chapters are already in English.
    """
    already = existing_import(series_uid)
    if already is not None:
        raise ValueError(
            f"That novel is already in the library as {already.get('name')!r}. "
            f"Delete it first if you want to import it again.")

    prepared = prepare(records)
    rows = prepared["chapters"]
    if not rows:
        raise ValueError("None of those chapters had any text to import.")

    # The source is the title block plus the prose, which is the shape the Korean export
    # has and therefore the shape strip_source_header already reads a chapter number out of.
    chapters = [
        Chapter(index=row["index"], title=row["title"],
                paragraphs=[row["title"], *split_paragraphs(row["markdown"])])
        for row in rows
    ]
    project = pj.create_text_project(name, chapters)
    pid = project["id"]
    project[PROVENANCE] = {
        "site": site,
        "series_uid": series_uid,
        "series_url": series_url,
        "at": _now(),
        "chapters": len(rows),
    }
    pj._write_project(project)

    cfg = pj.project_config(Config(), project)
    total = len(rows)
    # The same shared lock registry every other state.json writer uses, so two modules
    # can never hold two different locks for one path. `mutate_state` itself lives in
    # server/app.py, which imports this module -- taking the lock directly is the way to
    # get its guarantee without the cycle.
    with file_lock(cfg.paths.state_file):
        state = State.load(cfg.paths.state_file)
        for row, chapter in zip(rows, chapters):
            write_chapter_file(cfg.paths.output_dir, row["index"], total,
                               row["markdown"], snapshot=False)
            state.update(
                row["index"],
                status=STATUS_VALIDATED,
                title=row["title"],
                # Must equal the hash of the paragraphs written into source.json, or
                # source_changed flips the row to pending and the reader warns that it was
                # translated from different Korean.
                source_hash=chapter.metrics.content_hash,
                source_chars=chapter.metrics.char_count,
                failures=[],
                imported=True,
            )
        state.save(cfg.paths.state_file)

    return {
        "project": project,
        "pid": pid,
        "imported": total,
        "order": prepared["order"],
        "losses": prepared["losses"],
        "unfaithful": prepared["unfaithful"],
        "empty": prepared["empty"],
        "demoted": prepared["demoted"],
        "unnumbered": [r["title"] for r in rows if r["number"] is None],
    }
