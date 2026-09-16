"""Writing a novel out to a Google Doc — the request shapes, and nothing else.

Deliberately pure. Nothing here touches the network, holds credentials or knows what a
project is: it turns chapters into ``documents.batchUpdate`` request dictionaries and
predicts what the document will read back as. That is what lets the whole shape of the
feature be tested before a single API call is made.

An export is a readable copy and nothing ever points at it
-----------------------------------------------------------
This is the entire safety argument, and it is worth being explicit about because an
earlier design for this module was not safe. That design CONVERTED a novel: it built a
document of short placeholder tabs and then switched the novel's source over to it. It
was dropped, for two reasons found by measuring rather than reasoning.

First, it would have broken the per-paragraph rewrite panel permanently. Conversion
replaces ``source.json``, ``paragraph_source`` feeds that panel from the stored source
paragraphs, and ``get_chapters`` overwrites ``source.json`` wholesale on the next
refresh — so every already-translated chapter would have offered two source paragraphs
against an eighty-eight-paragraph translation, with no way back.

Second, the round trip was not actually clean. It was reported as 165 chapters of 165;
it is 164, because ``insertText`` starts a new paragraph at every newline and exactly one
stored paragraph in the library contains them. :func:`round_trip_losses` is that finding
turned into a check.

So this module only ever builds a document that nothing depends on. Nothing is flipped,
no ``source.json`` is rewritten, no hash is re-stamped, and a novel too large for one
document simply spans several — which is harmless precisely because nothing reads them
back. A novel's own source document could never be split that way: one project is one
document.
"""

from __future__ import annotations

from .docs_extract import Chapter

# Google's documented hard limit for one document.
DOC_CHAR_CAP = 1_020_000

# Characters of prose per batchUpdate call. Well inside any payload limit, and it keeps
# the call count low: 2 calls for a 5-chapter novel, 7 for a 100-chapter one.
INSERT_CHUNK_CHARS = 200_000


def tab_text(paragraphs: list[str]) -> str:
    """The text to insert for one tab.

    Joined with a blank line: a newline in ``insertText`` implicitly starts a paragraph
    and ``docs_extract._read_structural_elements`` drops whitespace-only ones, so this
    reads back as the same list while leaving the document legible to a person.
    """
    return "\n\n".join(paragraphs)


def reads_back_as(paragraphs: list[str]) -> list[str]:
    """What :func:`tab_text` will come back as when the document is read again.

    The whole subtlety of this module in one function. ``insertText`` does not insert a
    paragraph — it inserts text, and starts a new paragraph at EVERY newline. So a stored
    paragraph that itself contains a line break arrives as two, and the chapter comes back
    longer than it went in.

    Modelling a stored paragraph as a Doc paragraph is what made the original round-trip
    measurement wrong by one chapter.
    """
    return [p for p in tab_text(paragraphs).split("\n") if p.strip()]


def round_trip_losses(chapters: list[Chapter]) -> list[dict]:
    """The chapters whose text would not come back exactly as it went in.

    Reported rather than refused. For an export it is cosmetic — a paragraph that already
    contains a line break gains a paragraph mark, in a document nothing reads back — so
    refusing the whole novel over it would be obstruction rather than safety. It is named
    because a surprise is worse than a known cost, and because this is the same check that
    would have to REFUSE if anything ever pointed a novel at a generated document again.
    """
    out = []
    for chapter in chapters:
        paragraphs = [p for p in chapter.paragraphs if p.strip()]
        back = reads_back_as(paragraphs)
        if back != paragraphs:
            out.append({
                "index": chapter.index,
                "title": chapter.title,
                "paragraphs": len(paragraphs),
                "reads_back_as": len(back),
                "why": "a paragraph contains a line break, which Google Docs stores as a "
                       "paragraph of its own",
            })
    return out


def document_body(chapters: list[Chapter]) -> list[dict]:
    """What each tab should contain: ``[{title, paragraphs, chars}]`` in reading order."""
    out = []
    for chapter in chapters:
        # A whitespace-only paragraph is dropped when a document is read back, so it is
        # dropped here too rather than silently disappearing later.
        paragraphs = [p for p in chapter.paragraphs if p.strip()]
        out.append({"title": chapter.title, "paragraphs": paragraphs,
                    "chars": len(tab_text(paragraphs))})
    return out


def split_for_export(body: list[dict], *, cap: int | None = None) -> list[list[dict]]:
    """Break an export across as many documents as it needs.

    Measured on the real library: English runs about 1.60x the characters of the Korean it
    came from, so one document holds roughly 109 English chapters against 175 Korean ones,
    and a finished 100-chapter novel is already over the cap at ~1,146,200 characters.
    Splitting is therefore the normal case for a long novel, not an edge case.

    A single chapter larger than the cap still gets its own document rather than being
    dropped. It would be refused by Google, which is the right place for that to fail —
    silently omitting a chapter from an export is the one outcome nobody could detect.

    ``cap`` is resolved at call time rather than bound as a default argument, so the limit
    can be lowered in a test. Bound as a default it read the constant once at import and
    a test could never reach the splitting path at all without building a megabyte of
    fixture prose.
    """
    cap = DOC_CHAR_CAP if cap is None else cap
    parts: list[list[dict]] = [[]]
    used = 0
    for tab in body:
        if parts[-1] and used + tab["chars"] > cap:
            parts.append([])
            used = 0
        parts[-1].append(tab)
        used += tab["chars"]
    return parts


def tab_requests(body: list[dict]) -> list[dict]:
    """One ``addDocumentTab`` per chapter, in order.

    ``parentTabId`` is deliberately never set. ``flatten_child_tabs`` defaults to true, so
    a nested tab would be merged into its parent on read and its title discarded — the
    document would come back with fewer chapters than were written.
    """
    return [
        {"addDocumentTab": {"tabProperties": {"title": tab["title"], "index": i}}}
        for i, tab in enumerate(body)
    ]


def insert_batches(body: list[dict], tab_ids: list[str],
                   *, chunk_chars: int = INSERT_CHUNK_CHARS) -> list[list[dict]]:
    """The ``insertText`` requests, grouped into batchUpdate-sized calls.

    Each tab is filled by a single insert at index 1, which is where a tab's body begins.
    Inserts into different tabs do not shift each other's indices, so the grouping is free
    to be about payload size and nothing else.
    """
    if len(tab_ids) != len(body):
        raise ValueError(
            f"got {len(tab_ids)} tab ids for {len(body)} chapters — the document was not "
            f"built as expected, so nothing was filled")
    batches: list[list[dict]] = []
    current: list[dict] = []
    used = 0
    for tab, tab_id in zip(body, tab_ids):
        text = tab_text(tab["paragraphs"])
        if current and used + len(text) > chunk_chars:
            batches.append(current)
            current, used = [], 0
        current.append({"insertText": {
            "location": {"index": 1, "tabId": tab_id},
            "text": text,
        }})
        used += len(text)
    if current:
        batches.append(current)
    return batches


def tab_ids_from_replies(replies: list[dict]) -> list[str]:
    """Pull the new tabs' ids out of a batchUpdate response, in request order.

    Two phases are unavoidable: ``AddDocumentTabRequest`` carries only ``tabProperties``
    and the new tab's id comes back in ``AddDocumentTabResponse``, so text cannot be
    inserted in the same batch that creates the tab.
    """
    ids = []
    for reply in replies or []:
        added = (reply or {}).get("addDocumentTab") or {}
        tab_id = (added.get("tabProperties") or {}).get("tabId")
        if tab_id:
            ids.append(tab_id)
    return ids


def check(written: list[dict], fetched: list[Chapter]) -> dict:
    """Compare what was written against what the document actually returned.

    Report-only, and that is the difference between this and the dropped conversion's
    version. Nothing points at an export, so a difference costs the reader a paragraph
    mark rather than un-validating a finished chapter — there is nothing to abort and
    nothing to re-stamp. It is still worth saying out loud, because "it exported fine"
    about a document with a missing chapter is exactly the kind of quiet wrong answer
    this whole feature has to avoid.
    """
    problems: list[str] = []
    if len(fetched) != len(written):
        problems.append(
            f"the document came back with {len(fetched)} chapters, not {len(written)}")
        return {"ok": False, "problems": problems, "differed": []}

    differed: list[str] = []
    for i, (want, got) in enumerate(zip(written, fetched), start=1):
        if want["title"] and got.title != want["title"]:
            # docs_extract substitutes "Chapter {i}" for a blank tab title, so this is
            # how a title that never took shows itself.
            problems.append(
                f"tab {i} came back titled {got.title!r} instead of {want['title']!r}")
        elif tab_text(want["paragraphs"]) != got.text:
            differed.append(got.title)
    return {"ok": not problems and not differed,
            "problems": problems, "differed": differed}
