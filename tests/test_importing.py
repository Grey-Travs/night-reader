"""Tests for importing a novel that is already published.

This is the piece that writes into the library, so what it has to get right is the on-disk
contract the rest of the app reads. Four things in particular, each of which silently
breaks something if it is wrong:

* **`source_type` must be `"text"`** — the one discriminator `get_chapters` reads. Any other
  value falls through to the Google-Doc branch and the app tries to fetch a document that
  does not exist.
* **`source.json` must be dense and positional.** No record for a chapter means no row in
  the chapter list and a 404 in the reader; the text branch never falls back to rebuilding
  from `state.json`.
* **`source_hash` must match the paragraphs actually written.** If it disagrees,
  `source_changed` flips the row to `pending` and the reader claims it was translated from
  different Korean.
* **The chapter's own title must be its first source paragraph**, because that is the shape
  `strip_source_header` reads a chapter number out of. Without it every row resolves at low
  confidence and the posting side blocks the whole novel as unconfirmed.

And one property that is worth more than all of them: an imported chapter is English, the
translate queue only accepts Korean, so nothing can re-translate or re-bill it. That is
asserted directly rather than trusted.

Runs under pytest (``pytest tests/``) and standalone (``python tests/test_importing.py``).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import server.importing as importing  # noqa: E402
import server.projects as pj  # noqa: E402
from translation_bot.chapter_numbers import infer_mapping  # noqa: E402
from translation_bot.config import Config  # noqa: E402
from translation_bot.state import DONE_STATUSES, State  # noqa: E402


@pytest.fixture(autouse=True)
def library(monkeypatch, tmp_path):
    """A projects/ of its own, so a test never writes into the real library."""
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(pj, "PROJECTS_DIR", root)
    return root


# Long enough to be prose. This is load-bearing, not padding: the resolver classes any
# chapter under 200 non-whitespace characters with no Hangul as an "extra" and strips its
# global number, and imported English is 0% Hangul by definition. A short fixture made
# every numbering assertion below pass for the wrong reason.
BODY = ("She lifted her brows at the sudden movement, as if he had been burned by it. "
        "A hint of expectation shone there, and then it was gone again. " * 4)


def _records(n=3, *, newest_first=True, paid_from=None):
    """What the extension hands over: the site's own chapter records, newest first."""
    rows = []
    for i in range(1, n + 1):
        paid = paid_from is not None and i >= paid_from
        rows.append({
            "title": f"Chapter {i}",
            "html": f"<p>Opening of chapter {i}.</p><p>{BODY}</p>",
            "remote_id": f"id-{i}", "remote_uid": f"uid-{i}",
            "paid": paid, "coins": 55 if paid else 1, "state": "public",
        })
    return list(reversed(rows)) if newest_first else rows


# ---- putting the site's list into reading order ---------------------------


def test_the_sites_newest_first_order_is_corrected():
    ordered, how = importing.order_chapters(_records(5))
    assert [r["title"] for r in ordered] == [f"Chapter {i}" for i in range(1, 6)]
    assert "by the number in each title" in how


def test_an_already_ascending_list_is_left_alone():
    ordered, _ = importing.order_chapters(_records(4, newest_first=False))
    assert [r["title"] for r in ordered] == [f"Chapter {i}" for i in range(1, 5)]


def test_a_list_with_side_stories_is_reversed_and_says_so():
    # Side stories state no number, so they cannot be sorted - the site's own ordering is
    # the only evidence there is, and the report has to admit that.
    rows = [{"title": "Side Story 2", "html": "<p>b</p>"},
            {"title": "Side Story 1", "html": "<p>a</p>"},
            {"title": "Chapter 2", "html": "<p>two</p>"},
            {"title": "Chapter 1", "html": "<p>one</p>"}]
    ordered, how = importing.order_chapters(rows)
    assert [r["title"] for r in ordered] == [
        "Chapter 1", "Chapter 2", "Side Story 1", "Side Story 2"]
    assert "reversed the site" in how and "check anything without one" in how


def test_an_order_that_cannot_be_read_is_kept_and_flagged():
    rows = [{"title": "Prologue", "html": "<p>a</p>"},
            {"title": "Interlude", "html": "<p>b</p>"}]
    ordered, how = importing.order_chapters(rows)
    assert [r["title"] for r in ordered] == ["Prologue", "Interlude"]
    assert "kept the site" in how and "check the order" in how


def test_a_duplicate_number_falls_back_rather_than_sorting_blind():
    rows = [{"title": "Chapter 1", "html": "<p>a</p>"},
            {"title": "Chapter 1", "html": "<p>b</p>"}]
    _, how = importing.order_chapters(rows)
    assert "kept the site" in how


# ---- looking before writing ----------------------------------------------


def test_prepare_writes_nothing(library):
    importing.prepare(_records(3))
    assert list(library.iterdir()) == []


def test_a_chapter_with_no_prose_is_named_not_skipped_silently():
    rows = _records(2) + [{"title": "Chapter 3", "html": "<p>   </p>"}]
    out = importing.prepare(rows)
    assert out["empty"] == ["Chapter 3"]
    assert out["count"] == 2


def test_what_could_not_be_represented_is_counted():
    rows = [{"title": "Chapter 1",
             "html": "<p>a <u>b</u></p><img src='x'>"}]
    out = importing.prepare(rows)
    assert out["losses"] == {"an image": 1, "underline": 1}


def test_a_chapter_markdown_would_read_back_differently_is_flagged():
    # The text is intact; how Markdown reads it is not. There is no escape hatch, so the
    # only honest behaviour is to name it.
    rows = [{"title": "Chapter 1", "html": "<p>&gt;looks like a quote</p>"}]
    out = importing.prepare(rows)
    assert out["unfaithful"] == ["Chapter 1"]
    assert out["chapters"][0]["markdown"] == ">looks like a quote"


# ---- the on-disk contract -------------------------------------------------


def test_an_imported_novel_is_an_ordinary_text_project(library):
    out = importing.import_novel("Raised Too Precious", _records(3),
                                 series_uid="6417662e4db",
                                 series_url="https://meiko.studio/page/x/series/y/")
    project = pj.get_project(out["pid"])
    # "text" specifically: any other value falls through to the Google-Doc branch.
    assert project["source_type"] == "text"
    assert project["source_doc_id"] == ""
    assert project["chapter_count"] == 3
    assert project["name"] == "Raised Too Precious"
    came_from = project[importing.PROVENANCE]
    assert came_from["series_uid"] == "6417662e4db"
    assert came_from["site"] == "meiko" and came_from["chapters"] == 3
    assert came_from["at"]


def test_the_source_is_dense_positional_and_titled(library):
    out = importing.import_novel("Novel", _records(3))
    records = json.loads((library / out["pid"] / "source.json").read_text(encoding="utf-8"))
    assert len(records) == 3
    assert [r["title"] for r in records] == ["Chapter 1", "Chapter 2", "Chapter 3"]
    # The title is the first paragraph, which is the shape strip_source_header reads a
    # chapter number out of. Without it the numbering resolves at low confidence.
    for i, record in enumerate(records, start=1):
        assert record["paragraphs"][0] == f"Chapter {i}"
        assert len(record["paragraphs"]) == 3


def test_every_chapter_is_on_disk_and_marked_finished(library):
    out = importing.import_novel("Novel", _records(4))
    cfg = pj.project_config(Config(), pj.get_project(out["pid"]))
    state = State.load(cfg.paths.state_file)
    for index in range(1, 5):
        path = cfg.paths.output_dir / f"chapter-{index:02d}.md"
        assert path.exists(), path
        assert "Opening of chapter" in path.read_text(encoding="utf-8")
        record = state.get(index)
        assert record["status"] in DONE_STATUSES
        assert record["title"] == f"Chapter {index}"
        assert record["failures"] == []
        assert record["imported"] is True


def test_the_source_hash_matches_what_was_written(library):
    # If it disagrees, source_changed flips the row to pending and the reader claims the
    # chapter was translated from different Korean.
    out = importing.import_novel("Novel", _records(3))
    project = pj.get_project(out["pid"])
    cfg = pj.project_config(Config(), project)
    state = State.load(cfg.paths.state_file)
    for chapter in pj.load_text_chapters(out["pid"]):
        assert state.get(chapter.index)["source_hash"] == chapter.metrics.content_hash


def test_the_chapter_file_holds_the_prose_without_the_title(library):
    out = importing.import_novel("Novel", _records(1))
    cfg = pj.project_config(Config(), pj.get_project(out["pid"]))
    text = (cfg.paths.output_dir / "chapter-01.md").read_text(encoding="utf-8")
    # The site puts the chapter's name in its own field, so the prose carries no heading.
    assert not text.lstrip().startswith("#")
    assert text.startswith("Opening of chapter 1.")


# ---- what the resolver will quietly refuse to number ----------------------


def test_a_short_chapter_is_reported_rather_than_losing_its_number_silently():
    # The trap this feature has to warn about. The resolver classes anything under 200
    # non-whitespace characters with no Hangul as an "extra", which strips its global
    # number, drops it out of the reading order and blocks it from posting. Imported
    # English is 0% Hangul by definition, so every short author note hits this.
    #
    # The number itself parses perfectly -- it is the classification that removes it, which
    # is precisely why this had to be surfaced rather than inferred from a missing number.
    rows = _records(1) + [{"title": "Chapter 2", "html": "<p>Too short.</p>"}]
    out = importing.prepare(rows)
    assert out["count"] == 2, "it is still imported"
    assert [d["title"] for d in out["demoted"]] == ["Chapter 2"]
    assert out["demoted"][0]["kind"] == "extra"


def test_a_side_story_is_reported_too():
    rows = [{"title": "Side Story 1", "html": f"<p>{BODY}</p>"}]
    out = importing.prepare(rows)
    assert out["demoted"][0]["kind"] == "side"


def test_a_normal_chapter_is_not_reported(library):
    out = importing.import_novel("Novel", _records(4))
    assert out["demoted"] == []


# ---- the safety property that matters most --------------------------------


def test_an_imported_chapter_can_never_be_re_translated(library):
    # source.json holds English, classify() calls anything under min_hangul_fraction
    # English, and the translate queue only ever accepts Korean. So this is not a policy
    # anyone has to remember - it falls out of the data.
    from server.app import classify
    out = importing.import_novel("Novel", _records(3))
    cfg = Config()
    for chapter in pj.load_text_chapters(out["pid"]):
        assert classify(chapter, cfg) == "english"


def test_every_chapter_number_resolves_at_high_confidence(library):
    # The whole point of writing the title as the first paragraph. Get this wrong and
    # plan_run blocks the entire novel as "chapter number not confirmed".
    out = importing.import_novel("Novel", _records(6))
    # infer_mapping returns Row dataclasses, and the field is `global_number` because
    # `global` is a Python keyword. series.py serialises it to "global" on the way to disk.
    rows = infer_mapping(pj.load_text_chapters(out["pid"]))
    assert [r.global_number for r in rows] == [1, 2, 3, 4, 5, 6]
    assert {r.confidence for r in rows} == {"high"}
    # Read from the header block, not guessed from position - that is the distinction
    # plan_run blocks on.
    assert {r.source for r in rows} == {"header"}


# ---- importing the same novel twice --------------------------------------


def test_the_same_novel_is_not_imported_twice(library):
    importing.import_novel("Novel", _records(3), series_uid="abc123")
    with pytest.raises(ValueError, match="already in the library"):
        importing.import_novel("Novel", _records(3), series_uid="abc123")
    assert len(pj.list_projects()) == 1


def test_a_different_novel_is_not_mistaken_for_it(library):
    importing.import_novel("One", _records(2), series_uid="aaa")
    importing.import_novel("Two", _records(2), series_uid="bbb")
    assert len(pj.list_projects()) == 2


def test_matching_is_on_the_sites_id_not_the_name(library):
    # A name can be edited here afterwards; the site's id cannot.
    out = importing.import_novel("Original name", _records(2), series_uid="aaa")
    pj.update_project(out["pid"], name="Renamed entirely")
    assert importing.existing_import("aaa") is not None


def test_a_novel_with_nothing_importable_is_refused(library):
    with pytest.raises(ValueError, match="had any text"):
        importing.import_novel("Empty", [{"title": "Chapter 1", "html": ""}])
    assert pj.list_projects() == []


def test_the_report_says_what_needs_checking(library):
    rows = _records(2) + [{"title": "Side Story 1", "html": "<p>extra</p>"}]
    out = importing.import_novel("Novel", rows)
    assert out["imported"] == 3
    assert out["unnumbered"] == ["Side Story 1"]
    assert "reversed the site" in out["order"] or "kept the site" in out["order"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
