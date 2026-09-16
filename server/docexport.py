"""Writing a novel out to a Google Doc, one tab per chapter.

The one thing in this app that writes to Google, and it is deliberately the least
dangerous shape that could: it CREATES a document and then nothing ever points at it.
No project is flipped to it, no ``source.json`` is rewritten, no hash is re-stamped. The
worst outcome of a bug here is a document in the user's Drive that they delete.

That is not an accident of implementation, it is the whole design. An earlier version of
this feature converted an imported novel onto a generated document so it could be
continued, and conversion is destructive: ``get_chapters`` overwrites ``source.json``
wholesale on the next refresh, so the generated document becomes the only copy. It was
dropped in favour of adding a hand-made continuation document to the novel's series --
which needs no write permission at all -- leaving this as a plain export.

Two content modes, because they answer different questions:

* ``translation`` -- the finished English. What most people mean by "export to a Doc":
  something to read, share or edit outside the app.
* ``source`` -- the Korean (or, for an imported novel, the published English) exactly as
  the app holds it.

The Docs service is injected rather than built here, so every step up to the API boundary
is testable without credentials.
"""

from __future__ import annotations

from pathlib import Path

from translation_bot import docsmake
from translation_bot.chapter_numbers import infer_mapping
from translation_bot.config import Config
from translation_bot.docs_extract import Chapter
from translation_bot.google_auth import WRITE_SCOPES, require_scopes
from translation_bot.pipeline import chapter_filename

from . import projects as pj

DOC_URL = "https://docs.google.com/document/d/{}/edit"


def _translated(cfg, chapters: list[Chapter]) -> list[Chapter]:
    """The finished English, read by exact path per chapter.

    Never globbed. 28 projects on disk carry a stray ``export_artifacts_backup_*`` tree
    holding copies of ``chapters/``, so a recursive search returns two to four times the
    real chapter count -- which here would mean exporting the same chapter several times.
    """
    total = len(chapters)
    out = []
    for chapter in chapters:
        path = cfg.paths.output_dir / chapter_filename(chapter.index, total)
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        paragraphs = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n")]
        out.append(Chapter(index=chapter.index, title=chapter.title,
                           paragraphs=[p for p in paragraphs if p]))
    return out


def _titled(chapters: list[Chapter]) -> list[Chapter]:
    """Name each tab by the chapter number the app resolved, not by its tab title.

    Real tab titles in this library are all "Tab 1", "Tab 2" ... where the number is
    CREATION order rather than the chapter, so carrying them into an export would produce
    a document whose contents page means nothing.
    """
    rows = infer_mapping(chapters)
    by_index = {r.index: r for r in rows}
    out = []
    for chapter in chapters:
        row = by_index.get(chapter.index)
        number = getattr(row, "global_number", None) if row else None
        title = f"Chapter {number}" if number else (chapter.title or f"Tab {chapter.index}")
        out.append(Chapter(index=chapter.index, title=title,
                           paragraphs=list(chapter.paragraphs)))
    return out


def plan(pid: str, *, content: str = "translation") -> dict:
    """What an export would contain. Writes nothing and needs no credentials.

    Separate from :func:`export_novel` for the same reason the posting plan is separate
    from a posting run: the counts, the document split and the round-trip warnings are
    worth seeing before anything is created.
    """
    project = pj.get_project(pid)
    if not project:
        raise ValueError("That novel doesn't exist.")
    cfg = pj.project_config(Config(), project)
    chapters = _titled(pj.load_text_chapters(pid))
    if content == "translation":
        chapters = _translated(cfg, chapters)
    if not chapters:
        raise ValueError(
            "There is nothing to export — no chapter of this novel has been translated yet."
            if content == "translation" else "This novel has no chapters.")

    body = docsmake.document_body(chapters)
    parts = docsmake.split_for_export(body)
    return {
        "project_id": pid,
        "name": project.get("name") or "Untitled novel",
        "content": content,
        "chapters": len(body),
        "chars": sum(t["chars"] for t in body),
        "documents": len(parts),
        # Reported, not refused: nothing points at an export, so a paragraph that already
        # contains a line break costs a paragraph mark in a document nobody reads back.
        "losses": docsmake.round_trip_losses(chapters),
        "_parts": parts,
    }


def _doc_title(name: str, content: str, part: int, of: int) -> str:
    kind = "source" if content == "source" else "translated"
    suffix = f" ({part} of {of})" if of > 1 else ""
    return f"{name} — {kind}{suffix}"


def export_novel(pid: str, docs_service, *, content: str = "translation",
                 creds=None, verify: bool = True) -> dict:
    """Create the document(s) and fill them. Returns what was made.

    The write is necessarily two-phase: ``AddDocumentTabRequest`` carries only
    ``tabProperties`` and the new tab's id comes back in the reply, so text cannot be
    inserted in the same batch that creates the tab.
    """
    if creds is not None:
        # Before anything is created, so a half-built document is never left in the
        # user's Drive for them to find and clean up. Google would otherwise answer the
        # first write with a 403 that reads exactly like a sharing problem.
        require_scopes(creds, WRITE_SCOPES)

    out = plan(pid, content=content)
    parts = out.pop("_parts")
    documents = []

    for i, part in enumerate(parts, start=1):
        title = _doc_title(out["name"], content, i, len(parts))
        created = docs_service.documents().create(body={"title": title}).execute()
        doc_id = created.get("documentId")

        replies = docs_service.documents().batchUpdate(
            documentId=doc_id, body={"requests": docsmake.tab_requests(part)},
        ).execute().get("replies") or []
        tab_ids = docsmake.tab_ids_from_replies(replies)

        for batch in docsmake.insert_batches(part, tab_ids):
            docs_service.documents().batchUpdate(
                documentId=doc_id, body={"requests": batch}).execute()

        entry = {"id": doc_id, "title": title, "url": DOC_URL.format(doc_id),
                 "chapters": len(part)}
        if verify:
            entry["check"] = _verify(docs_service, doc_id, part)
        documents.append(entry)

    out["documents"] = documents
    return out


def _verify(docs_service, doc_id: str, part: list[dict]) -> dict:
    """Read the document back and say whether it matches.

    Report-only, and failure to verify is itself only reported: the document exists
    either way, and an export that cannot be re-read is still an export. Saying "it
    worked" without looking is the outcome worth avoiding.
    """
    from translation_bot.docs_extract import extract_chapters, fetch_document
    try:
        fetched = extract_chapters(fetch_document(docs_service, doc_id))
    except Exception as exc:  # noqa: BLE001 — verification must not fail the export
        return {"ok": False, "problems": [f"could not read the document back: {exc}"],
                "differed": []}
    return docsmake.check(part, fetched)


def can_write(token_file) -> bool:
    """Whether the saved token was granted the write scope."""
    from translation_bot.google_auth import _load_token, missing_scopes
    creds = _load_token(Path(token_file))
    return bool(creds) and not missing_scopes(creds, WRITE_SCOPES)
