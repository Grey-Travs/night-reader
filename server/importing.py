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

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from translation_bot.atomic import atomic_write_text, quarantine_unreadable
from translation_bot.chapter_numbers import IN_SEQUENCE, classify_kind, parse_tab_title
from translation_bot.config import Config
from translation_bot.docs_extract import Chapter
from translation_bot.glossary import (glossary_lock, load_pending,
                                      save_pending)
from translation_bot.htmlmd import html_to_markdown, round_trips
from translation_bot.pipeline import write_chapter_file
from translation_bot.state import STATUS_VALIDATED, State
from translation_bot.textsplit import split_paragraphs

from . import posting
from . import projects as pj
from . import series as series_mod
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

    # Pricing, numbering and the posting record all live on a series, so the novel gets
    # one. Without this the Posting page would offer to publish a hundred chapters that
    # are already live, which is the single most expensive mistake available here.
    linked = link_into_series(pid, name, rows, series_url=series_url, site=site)
    # The established spellings, for approval. Never locked automatically - see the note
    # above queue_names for what this does and does not buy.
    names = queue_names(pid, rows)

    return {
        "project": project,
        "pid": pid,
        "sid": linked["sid"],
        "pricing": linked["pricing"],
        "numbered": linked["numbered"],
        "names_queued": names,
        "imported": total,
        "order": prepared["order"],
        "losses": prepared["losses"],
        "unfaithful": prepared["unfaithful"],
        "empty": prepared["empty"],
        "demoted": prepared["demoted"],
        "unnumbered": [r["title"] for r in rows if r["number"] is None],
    }


# ---------------------------------------------------------------------------
# Making an imported novel immediately useful
# ---------------------------------------------------------------------------
#
# A project on its own is only half of it. The pricing, the ledger and the chapter
# numbering all live on a SERIES, so an imported novel gets one with itself as the only
# member -- which is what makes the Posting page tell the truth about it from the moment it
# arrives, instead of offering to re-post a hundred chapters that are already live.
#
# Everything here is derived from what the site itself said, so none of it is a guess.


def infer_pricing(rows: list[dict]) -> dict:
    """What the site charges for this novel, read off its own chapter records.

    ``coins`` alone is not the answer: a FREE chapter still carries a coin value (paid 0
    alongside coins 1, observed on a real series), so the price has to come from the paid
    chapters only. Modal rather than maximum, because a series can carry a handful of oddly
    priced chapters and the price that matters is the one nearly every paid chapter has.

    ``free_through`` is the highest numbered chapter the site gives away. A novel with no
    paid chapter at all is free throughout, and says so with a cutoff past its last chapter
    rather than with a zero -- zero means "nobody has said", which is what the posting
    guard refuses to act on.
    """
    paid = [r for r in rows if r.get("paid")]
    free_numbers = [r["number"] for r in rows
                    if not r.get("paid") and r.get("number") is not None]
    numbers = [r["number"] for r in rows if r.get("number") is not None]

    counts: dict[int, int] = {}
    for row in paid:
        coins = int(row.get("coins") or 0)
        if coins > 0:
            counts[coins] = counts.get(coins, 0) + 1
    # Ties break towards the lower price: charging less than the site does is the smaller
    # mistake, and this is only ever a starting point a person can change.
    coin_price = min(counts, key=lambda c: (-counts[c], c)) if counts else 0

    if not paid:
        # Stated as free all the way, not left at zero.
        free_through = max(numbers) if numbers else 0
    else:
        free_through = max(free_numbers) if free_numbers else 0
    return {"free_through": free_through, "coin_price": coin_price,
            "paid_chapters": len(paid)}


def link_into_series(pid: str, name: str, rows: list[dict], *, series_url: str = "",
                     site: str = "meiko") -> dict:
    """Give an imported novel its series, pricing, numbering and posting record.

    Five things, in the order they depend on each other:

    1. a series with this project as its only member, because pricing, numbering and the
       ledger are all series-level;
    2. a publishing target carrying the site URL and the pricing read off the chapters;
    3. the resolved numbering, saved -- the posting side names a chapter from the mapping,
       so without this nothing can post;
    4. a ledger entry per chapter marking it posted, which is what stops the Posting page
       offering to publish a novel that is already published;
    5. the site snapshot, for the same reason from the other direction.
    """
    series = series_mod.create_series(name, [pid])
    sid = series["id"]
    pricing = infer_pricing(rows)
    series["publish_targets"] = [{
        "id": "t1",
        "site": site,
        "series_url": series_url,
        "free_through": pricing["free_through"],
        "coin_price": pricing["coin_price"],
        # Side stories carry no chapter number for the cutoff to compare, and the site
        # shows side content as the earliest paid entry, so paid unless said otherwise.
        "side_paid": True,
    }]
    series_mod.write_series(series)

    chapters = pj.load_text_chapters(pid)
    mapping = series_mod.resolve_mapping(series, {pid: chapters})
    series_mod.save_mapping(sid, mapping)
    globals_by_index = {
        row.get("index"): row.get("global")
        for row in ((mapping.get("members") or {}).get(pid) or {}).get("rows") or []
    }

    for row in rows:
        posting.record(sid, "t1", {
            "title": row["title"],
            "project_id": pid,
            "index": row["index"],
            "global": globals_by_index.get(row["index"]),
            "status": "posted",
            "remote_url": series_url,
            "remote_id": row.get("remote_id") or "",
            "price_coins": int(row.get("coins") or 0) if row.get("paid") else 0,
            "error": "",
            "at": _now(),
        })

    prices = [int(r.get("coins") or 0) for r in rows
              if r.get("paid") and int(r.get("coins") or 0) > 0]
    try:
        posting.write_site(sid, "t1", [r["title"] for r in rows], prices)
    except ValueError:
        # write_site refuses an empty list on purpose; an import always has titles, so this
        # can only mean something stranger, and it is not worth failing the import over.
        pass

    return {"sid": sid, "pricing": pricing,
            "numbered": sum(1 for v in globals_by_index.values() if v is not None)}

# ---------------------------------------------------------------------------
# The names the published chapters already use
# ---------------------------------------------------------------------------
#
# What this does and does NOT do, because the difference matters.
#
# The glossary's main job is locking a translation: see 네파티아, write Nephatia. That needs
# both halves of the pair, and an imported novel has only the English — the site never had
# the Korean. `Glossary` keys on the Korean, and `relevant_to` scans entries against the
# chapter being translated, so an English-only entry is never handed to the translator for
# a Korean chapter. Mining these does not make future chapters spell names consistently on
# its own.
#
# What it does give is the established spellings, in one list, ready to approve — and once
# approved they are what `consistency_scan` compares later chapters against, which is how
# drift gets caught. And approving one is the natural moment to add its Korean, which turns
# it into a real lock.
#
# So these go into the PENDING queue and never into the locked glossary. Nothing is
# committed without a person saying so, and the filters below are deliberately strict: a
# hundred junk candidates would bury the approval screen and the feature would be worse
# than nothing.

# Capitalised word, or a run of up to three - "Arkin", "Arkin Seilir", "The Silver Tower".
_NAME_RUN = re.compile(r"[A-Z][a-z’'\-]+(?:\s+[A-Z][a-z’'\-]+){0,2}")

# Sentence boundary, including the closing quote marks these translations use.
_SENTENCE = re.compile(r"[.!?…]+[\s”’\"')\]]*")

# Markdown markers, so emphasis does not split a name in half.
_MARKUP = re.compile(r"[*#>`_]+")

# Capitalised mid-sentence without being a name. Short on purpose: the frequency filter
# does most of the work, and a long list of guesses is its own kind of wrong.
_NOT_A_NAME = {
    "i", "i’m", "i’ll", "i’ve", "i’d", "im", "ill", "ive",
    "mr", "mrs", "ms", "dr", "sir", "madam", "the", "and", "but", "he", "she",
    "they", "it", "we", "you", "his", "her", "their", "there", "then", "that",
    "this", "what", "why", "how", "when", "who", "if", "so", "no", "yes", "oh",
    "ah", "well", "even", "just", "still", "after", "before",
}

# Both floors have to be cleared. A name said once is a passing mention; a name in one
# chapter only is probably not the cast.
_MIN_TIMES = 3
_MIN_CHAPTERS = 2
_MAX_CANDIDATES = 60


def mine_names(rows: list[dict]) -> list[dict]:
    """Candidate proper nouns from the imported English, commonest first.

    Only mid-sentence capitals count. A name at the start of a sentence is
    indistinguishable from an ordinary word that happens to be capitalised there, and any
    real name appears mid-sentence somewhere too — so throwing those away costs almost
    nothing and removes most of the noise at a stroke.
    """
    times: dict[str, int] = {}
    chapters: dict[str, set[int]] = {}
    first_seen: dict[str, int] = {}

    for row in rows:
        index = int(row.get("index") or 0)
        text = _MARKUP.sub(" ", row.get("markdown") or "")
        for sentence in _SENTENCE.split(text):
            for match in _NAME_RUN.finditer(sentence):
                # Sentence-initial, i.e. nothing but whitespace in front of it.
                if not sentence[:match.start()].strip():
                    continue
                name = " ".join(match.group(0).split())
                if name.casefold() in _NOT_A_NAME or len(name) < 3:
                    continue
                times[name] = times.get(name, 0) + 1
                chapters.setdefault(name, set()).add(index)
                first_seen.setdefault(name, index)

    out = []
    for name, count in times.items():
        seen_in = chapters[name]
        if count < _MIN_TIMES or len(seen_in) < _MIN_CHAPTERS:
            continue
        out.append({"english": name, "times": count, "chapters": len(seen_in),
                    "first_chapter": first_seen[name]})
    # Commonest first, so the approval screen starts with the cast rather than the extras.
    out.sort(key=lambda r: (-r["times"], r["english"]))
    return out[:_MAX_CANDIDATES]


def queue_names(pid: str, rows: list[dict]) -> int:
    """Put the mined names in the project's approval queue. Returns how many were added.

    The PENDING queue, never the locked glossary — nothing here is certain enough to lock,
    and a wrong locked name is worse than no name at all. Written with the same lock and
    the same helpers the translator's own queue uses, so the approval screen needs no
    special case.

    ``queue_new_terms`` cannot be reused: it drops any entry without a Korean term, which
    every one of these is by definition.
    """
    candidates = mine_names(rows)
    if not candidates:
        return 0
    project = pj.get_project(pid)
    if project is None:
        return 0
    cfg = pj.project_config(Config(), project)
    path = cfg.paths.glossary_pending
    with glossary_lock(path):
        pending = load_pending(path)
        known = {str(p.get("english") or "").casefold() for p in pending}
        added = 0
        for row in candidates:
            if row["english"].casefold() in known:
                continue
            pending.append({
                # No Korean, and that is the honest record of what is known: the site never
                # had it. Adding one on approval is what turns this into a real lock.
                "korean": "",
                "english": row["english"],
                "type": "name",
                "note": (f"read off the published chapters "
                         f"({row['times']}x across {row['chapters']} chapters)"),
                "pronoun": "",
                "register": "",
                "chapter": row["first_chapter"],
            })
            known.add(row["english"].casefold())
            added += 1
        save_pending(path, pending)
    return added

# ---------------------------------------------------------------------------
# What is on the site but not in the library
# ---------------------------------------------------------------------------
#
# The site answers /app/series?studio_uid=&userid= with every series in the studio, so the
# candidate list is the studio's own list minus whatever is already here. Python cannot
# fetch it -- it has no session on the site and no business having one -- so the extension
# pushes it and this keeps it.

CATALOGUE_NAME = "site_series.json"


def catalogue_path() -> Path:
    # A function rather than a constant so the test suite's redirected PROJECTS_DIR is
    # honoured, the same reason series_root() and adapters_dir() are functions.
    return pj.PROJECTS_DIR.parent / CATALOGUE_NAME


def load_catalogue() -> dict:
    path = catalogue_path()
    if not path.exists():
        return {"fetched_at": None, "studio_uid": "", "series": []}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        # Keep the bytes rather than overwrite them, as every other reader here does.
        quarantine_unreadable(path)
        return {"fetched_at": None, "studio_uid": "", "series": []}
    if not isinstance(doc, dict):
        return {"fetched_at": None, "studio_uid": "", "series": []}
    rows = doc.get("series")
    return {"fetched_at": doc.get("fetched_at"),
            "studio_uid": str(doc.get("studio_uid") or ""),
            "series": [r for r in rows if isinstance(r, dict)] if isinstance(rows, list)
            else []}


def write_catalogue(series: list[dict], *, studio_uid: str = "") -> dict:
    """Record the studio's own series list, pushed here by the extension.

    An EMPTY list is refused rather than stored. The one way to get an empty answer is to
    have asked before the page was ready, and storing that would empty a good list and
    leave the Import page saying there is nothing to import.
    """
    rows = []
    for row in series or []:
        uid = str((row or {}).get("uid") or "").strip()
        name = str((row or {}).get("name") or (row or {}).get("displayName") or "").strip()
        if not uid or not name:
            continue
        rows.append({
            "uid": uid,
            "name": name,
            "slug": str(row.get("slug") or "").strip(),
            "chapters": int(row.get("chapters") or 0),
            "views": int(row.get("views") or 0),
        })
    if not rows:
        raise ValueError("Empty series list - refusing to overwrite the last one.")
    rows.sort(key=lambda r: r["name"].casefold())
    doc = {"fetched_at": _now(), "studio_uid": studio_uid, "series": rows}
    path = catalogue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(doc, ensure_ascii=False, indent=2))
    return doc


def _linked_series_uids() -> dict[str, str]:
    """Site series ids that an existing series already publishes to, and its name.

    The 22 linked series each store a publishing URL of the form
    .../page/<studio>/series/<series uid>/, so the id is already on disk -- it just has to
    be read out of the URL. Without this the Import page would offer to import novels that
    are in the library already, which is the commonest way to end up with two copies.
    """
    out: dict[str, str] = {}
    for series in series_mod.list_series():
        for target in series.get("publish_targets") or []:
            url = str(target.get("series_url") or "")
            match = re.search(r"/series/([^/?#]+)", url)
            if match:
                out[match.group(1)] = series.get("name") or ""
    return out


def candidates() -> dict:
    """The studio's series, each marked with whether it is already here and how.

    Reported rather than filtered. A list that silently omits rows cannot be checked, and
    "why is that novel missing" is a worse question than "why is that row greyed out".
    """
    doc = load_catalogue()
    linked = _linked_series_uids()
    studio = doc["studio_uid"]
    rows = []
    for row in doc["series"]:
        imported = existing_import(row["uid"])
        already = None
        if imported is not None:
            already = f"imported as {imported.get('name')!r}"
        elif row["uid"] in linked:
            already = f"already linked to {linked[row['uid']]!r}"
        rows.append({
            **row,
            "in_library": already is not None,
            "why": already or "",
            "series_url": (f"https://meiko.studio/page/{studio}/series/{row['uid']}/"
                           if studio else ""),
        })
    return {
        "fetched_at": doc["fetched_at"],
        "studio_uid": studio,
        "series": rows,
        "importable": sum(1 for r in rows if not r["in_library"]),
    }
