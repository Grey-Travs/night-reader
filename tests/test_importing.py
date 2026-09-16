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
from fastapi.testclient import TestClient  # noqa: E402

import server.importing as importing  # noqa: E402
import server.posting as posting  # noqa: E402
import server.projects as pj  # noqa: E402
import server.series as series_mod  # noqa: E402
from translation_bot.chapter_numbers import infer_mapping  # noqa: E402
from translation_bot.config import Config  # noqa: E402
from translation_bot.state import DONE_STATUSES, State  # noqa: E402


# The posting side reads the site's limits and naming out of a TOML file, so anything
# that reaches plan_run needs one on disk. Same shape as tests/test_posting.py uses.
ADAPTER = """
[site]
name = "meiko"
label = "meiko.studio"
max_chars = 100000
title_template = "Chapter {n}"
side_title_template = "Side Stories {n}"
"""


@pytest.fixture(autouse=True)
def library(monkeypatch, tmp_path):
    """A projects/ of its own, so a test never writes into the real library."""
    root = tmp_path / "projects"
    root.mkdir()
    (tmp_path / "adapters").mkdir()
    (tmp_path / "adapters" / "meiko.toml").write_text(ADAPTER, encoding="utf-8")
    monkeypatch.setattr(pj, "PROJECTS_DIR", root)
    series_mod._invalidate_index()
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


# ---- pricing read off the site's own records ------------------------------


def test_a_free_chapter_carrying_coins_does_not_set_the_price():
    # The trap, measured on a real series: paid 0 alongside coins 1. Taking the modal
    # non-zero coin value without checking `paid` would price the novel at 1 coin.
    rows = [{"number": n, "paid": n > 47, "coins": 55 if n > 47 else 1}
            for n in range(1, 61)]
    assert importing.infer_pricing(rows) == {
        "free_through": 47, "coin_price": 55, "paid_chapters": 13}


def test_a_series_with_no_paid_chapter_is_stated_free_not_left_at_zero():
    # Zero means "nobody has said", which the posting guard refuses to act on. A novel
    # that really is free says so with a cutoff past its last chapter.
    rows = [{"number": n, "paid": False, "coins": 1} for n in range(1, 6)]
    out = importing.infer_pricing(rows)
    assert out["free_through"] == 5 and out["coin_price"] == 0


def test_one_oddly_priced_chapter_does_not_become_the_price():
    rows = [{"number": 1, "paid": True, "coins": 120},
            {"number": 2, "paid": True, "coins": 55},
            {"number": 3, "paid": True, "coins": 55}]
    assert importing.infer_pricing(rows)["coin_price"] == 55


def test_pricing_survives_chapters_with_no_number():
    rows = [{"number": None, "paid": True, "coins": 55},
            {"number": 1, "paid": False, "coins": 1}]
    assert importing.infer_pricing(rows)["free_through"] == 1


# ---- the series an imported novel gets ------------------------------------


def test_an_imported_novel_gets_its_own_series(library):
    out = importing.import_novel("Novel", _records(4, paid_from=3),
                                 series_url="https://meiko.studio/page/x/series/y/")
    series = series_mod.get_series(out["sid"])
    assert [m["project_id"] for m in series["members"]] == [out["pid"]]
    target = series["publish_targets"][0]
    assert target["series_url"] == "https://meiko.studio/page/x/series/y/"
    assert target["site"] == "meiko" and target["side_paid"] is True
    # Chapters 3 and 4 are paid at 55, so the cutoff is 2.
    assert target["free_through"] == 2 and target["coin_price"] == 55


def test_the_numbering_is_resolved_and_saved(library):
    out = importing.import_novel("Novel", _records(5))
    mapping = series_mod.load_mapping(out["sid"])
    rows = mapping["members"][out["pid"]]["rows"]
    assert [r["global"] for r in rows] == [1, 2, 3, 4, 5]
    assert out["numbered"] == 5


def test_every_chapter_is_recorded_as_already_posted(library):
    out = importing.import_novel("Novel", _records(4))
    entries = posting.load_ledger(out["sid"], "t1")["entries"]
    assert len(entries) == 4
    assert {e["status"] for e in entries} == {"posted"}
    assert sorted(e["title"] for e in entries) == [f"Chapter {i}" for i in range(1, 5)]
    assert all(e["project_id"] == out["pid"] for e in entries)
    assert [e["global"] for e in entries] == [1, 2, 3, 4]


def test_the_site_snapshot_is_written(library):
    out = importing.import_novel("Novel", _records(3, paid_from=2))
    snapshot = posting.load_site(out["sid"], "t1")
    assert len(snapshot["titles"]) == 3
    assert snapshot["observed_coins"] == 55


def test_the_posting_page_immediately_knows_it_is_all_published(library):
    # The whole point of writing the ledger and the snapshot. Without them the Posting
    # page would offer to publish a novel that is already published, chapter for chapter -
    # which on a coin platform means refunds and reader complaints.
    out = importing.import_novel("Novel", _records(6, paid_from=4),
                                 series_url="https://meiko.studio/page/x/series/y/")
    plan = posting.plan_run(out["sid"], "t1")
    assert plan["ready"] == 0, "nothing should be offered for posting"
    assert plan["blocked"] == 6
    for item in plan["items"]:
        assert any("already" in b for b in item["blockers"]), item


# ---- continuing an imported novel -----------------------------------------
#
# An imported novel has no Google Doc, so its next chapters arrive in a new one that is
# added to the series the importer gave it. What must not break is the ledger: the
# imported chapters are already live on the site, and a run that offered them again would
# re-post the entire back catalogue.

KOREAN = "그는 천천히 고개를 돌려 나를 바라보았다. " * 12


def _continuation(name, numbers):
    """A Korean continuation document, of the kind pasted into a new Doc by hand."""
    from translation_bot.docs_extract import Chapter
    chapters = [
        Chapter(index=i, title=f"Tab {i}", paragraphs=[
            f"ridibooks.com/books/{2000 + i}/view\n\n노벨 제목 {n}화", KOREAN])
        for i, n in enumerate(numbers, 1)
    ]
    return pj.create_text_project(name, chapters)["id"]


def _reresolve(sid):
    series = series_mod.get_series(sid)
    chapters = {pid: pj.load_text_chapters(pid)
                for pid in series_mod.member_ids(series)}
    return series_mod.save_mapping(sid, series_mod.resolve_mapping(series, chapters))


def test_the_imported_chapters_stay_published_after_a_document_is_added(library):
    out = importing.import_novel("Novel", _records(6, paid_from=4),
                                 series_url="https://meiko.studio/page/x/series/y/")
    series_mod.add_member(out["sid"], _continuation("Novel 2", [7, 8]))
    _reresolve(out["sid"])
    plan = posting.plan_run(out["sid"], "t1")
    published = [i for i in plan["items"] if i["global"] in range(1, 7)]
    assert len(published) == 6
    for item in published:
        assert any("already" in b for b in item["blockers"]), item


def test_the_added_document_continues_the_numbering(library):
    out = importing.import_novel("Novel", _records(6, paid_from=4),
                                 series_url="https://meiko.studio/page/x/series/y/")
    pid = _continuation("Novel 2", [7, 8])
    series_mod.add_member(out["sid"], pid)
    _reresolve(out["sid"])
    plan = posting.plan_run(out["sid"], "t1")
    fresh = [i for i in plan["items"] if i["global"] in (7, 8)]
    assert len(fresh) == 2
    # New chapters, so nothing about them is "already" anything. They are held only
    # because they have not been translated yet, which is the ordinary state of a
    # chapter that was pasted in five minutes ago.
    for item in fresh:
        assert not any("already" in b for b in item["blockers"]), item


def test_the_free_and_paid_split_matches_the_site(library):
    out = importing.import_novel("Novel", _records(10, paid_from=6))
    plan = posting.plan_run(out["sid"], "t1", use_site=False)
    by_number = {i["global"]: i for i in plan["items"]}
    assert by_number[5]["paid"] is False
    assert by_number[6]["paid"] is True and by_number[6]["coins"] == 55


# ---- the candidate list ---------------------------------------------------


def test_the_catalogue_round_trips(library):
    importing.write_catalogue([
        {"uid": "aaa", "name": "Second Novel", "slug": "second", "chapters": 10},
        {"uid": "bbb", "name": "First Novel", "slug": "first"},
    ], studio_uid="studio1")
    out = importing.candidates()
    # Sorted by name, so the list reads the way a person would scan it.
    assert [r["name"] for r in out["series"]] == ["First Novel", "Second Novel"]
    assert out["series"][0]["series_url"] == (
        "https://meiko.studio/page/studio1/series/bbb/")
    assert out["importable"] == 2
    assert out["fetched_at"]


def test_an_empty_catalogue_never_replaces_a_good_one(library):
    importing.write_catalogue([{"uid": "aaa", "name": "Novel"}], studio_uid="s")
    with pytest.raises(ValueError, match="Empty series list"):
        importing.write_catalogue([])
    assert len(importing.candidates()["series"]) == 1


def test_rows_without_a_uid_or_name_are_dropped(library):
    importing.write_catalogue([
        {"uid": "aaa", "name": "Real"},
        {"uid": "", "name": "No id"},
        {"uid": "ccc", "name": ""},
    ], studio_uid="s")
    assert [r["name"] for r in importing.candidates()["series"]] == ["Real"]


def test_a_novel_already_imported_is_marked_not_hidden(library):
    importing.write_catalogue([{"uid": "abc", "name": "Already Here"}], studio_uid="s")
    importing.import_novel("Already Here", _records(3), series_uid="abc")
    out = importing.candidates()
    # Marked rather than filtered: "why is that row greyed out" beats "why is that novel
    # missing from the list".
    assert len(out["series"]) == 1
    row = out["series"][0]
    assert row["in_library"] is True and "imported as" in row["why"]
    assert out["importable"] == 0


def test_a_novel_already_linked_to_a_series_is_marked_too(library):
    # The 22 already-linked series store the site id inside their publishing URL, so it is
    # on disk already and just has to be read back out of it.
    a = importing.import_novel("Existing", _records(2), series_uid="zzz")
    series = series_mod.get_series(a["sid"])
    series["publish_targets"][0]["series_url"] = (
        "https://meiko.studio/page/s/series/linked-uid/")
    series_mod.write_series(series)
    importing.write_catalogue([{"uid": "linked-uid", "name": "Some Novel"}],
                              studio_uid="s")
    row = importing.candidates()["series"][0]
    assert row["in_library"] is True and "already linked" in row["why"]


def test_an_unreadable_catalogue_is_kept_aside(library):
    importing.write_catalogue([{"uid": "aaa", "name": "Novel"}], studio_uid="s")
    path = importing.catalogue_path()
    path.write_text("{ not json", encoding="utf-8")
    assert importing.candidates()["series"] == []
    assert list(path.parent.glob("site_series.json.unreadable-*"))



# ---- the names the published chapters already use -------------------------
#
# Worth being precise about what this buys, because it is less than it looks. The glossary
# locks a translation by keying on the Korean, and an imported novel has only the English,
# so these entries are never handed to the translator. What they give is the established
# spellings in one approvable list, which is what consistency checks compare against.

CAST = ("Nephatia lifted her brows at the sudden movement. "
        "A hint of expectation shone in her eyes, but Arkin Seilir cut her off. "
        "Even now, Arkin was wary of the unfamiliar. "
        "The tower stood behind Nephatia, and Arkin looked away. ")


def _cast_rows(n=3):
    return [{"index": i, "markdown": CAST} for i in range(1, n + 1)]


def test_the_cast_is_found():
    names = [c["english"] for c in importing.mine_names(_cast_rows())]
    assert "Nephatia" in names and "Arkin" in names


def test_a_sentence_initial_word_is_not_mistaken_for_a_name():
    # "Even now, ..." and "A hint of ..." both open sentences. Any real name appears
    # mid-sentence somewhere too, so discarding these costs almost nothing.
    names = [c["english"] for c in importing.mine_names(_cast_rows())]
    assert "Even" not in names and "The" not in names and "Hint" not in names


def test_a_name_said_once_is_not_a_candidate():
    rows = [{"index": 1, "markdown": "He met Gaspard once, and never again."},
            {"index": 2, "markdown": "Nothing more of him was heard at all."}]
    assert importing.mine_names(rows) == []


def test_a_name_in_only_one_chapter_is_not_a_candidate():
    # Three mentions, but all in one chapter - probably not the cast.
    one = "Arkin spoke to Arkin about Arkin again and again and again."
    assert importing.mine_names([{"index": 1, "markdown": one}]) == []


def test_the_commonest_names_come_first():
    rows = importing.mine_names(_cast_rows())
    assert rows == sorted(rows, key=lambda r: (-r["times"], r["english"]))


def test_markdown_emphasis_does_not_split_a_name():
    # Three chapters, because two mentions would fail the frequency floor rather than the
    # thing being tested - which is the mistake the first version of this made.
    rows = [{"index": i, "markdown": "She saw *Arkin Seilir* there again, and again."}
            for i in (1, 2, 3)]
    names = [c["english"] for c in importing.mine_names(rows)]
    assert "Arkin Seilir" in names


def test_the_queue_is_capped():
    # An unfiltered pass would bury the approval screen, at which point the feature is
    # worse than not having it.
    many = " ".join(f"and Person{n} spoke to Person{n} and Person{n} replied."
                    for n in range(200))
    rows = [{"index": i, "markdown": many} for i in (1, 2)]
    assert len(importing.mine_names(rows)) <= 60


def test_the_names_land_in_the_pending_queue_not_the_glossary(library):
    from translation_bot.glossary import load_pending
    rows = [{"title": f"Chapter {i}", "html": f"<p>{CAST}</p>"} for i in (1, 2, 3)]
    out = importing.import_novel("Cast", rows)
    assert out["names_queued"] > 0
    cfg = pj.project_config(Config(), pj.get_project(out["pid"]))
    pending = load_pending(cfg.paths.glossary_pending)
    assert len(pending) == out["names_queued"]
    # No Korean, which is the honest record: the site never had it. Adding one on
    # approval is what would turn this into a real translation lock.
    assert all(p["korean"] == "" for p in pending)
    assert all(p["type"] == "name" for p in pending)
    assert all("published chapters" in p["note"] for p in pending)
    # And nothing was locked - the glossary itself is untouched.
    assert not cfg.paths.glossary_json.exists() or not load_pending(
        cfg.paths.glossary_json)


def test_a_novel_with_no_repeated_names_queues_nothing(library):
    out = importing.import_novel("Plain", _records(3))
    assert out["names_queued"] == 0

# ---- over HTTP, which is how the extension speaks to it -------------------


@pytest.fixture
def client(monkeypatch, library):
    import server.app as A
    monkeypatch.setattr(A, "load_global_config", lambda: Config())
    A._chapter_cache.clear()
    # client= matters: the default TestClient host is the literal string "testclient",
    # which is not a loopback address, so without it every request meets the remote-access
    # gate and comes back 403.
    return TestClient(A.app, base_url="http://localhost", client=("127.0.0.1", 50000))


def _body(n=3, **kw):
    return {"name": "Novel", "series_uid": "abc", "chapters": _records(n), **kw}


def test_the_preview_writes_nothing(client, library):
    r = client.post("/api/import/preview", json=_body(3))
    assert r.status_code == 200
    out = r.json()
    assert out["count"] == 3
    assert out["chars"] > 0
    # The markdown itself is deliberately not returned - a whole novel is megabytes and
    # the page only needs the counts and the warnings.
    assert "markdown" not in out["chapters"][0]
    assert list(library.iterdir()) == []


def test_the_preview_says_if_it_is_already_here(client):
    client.post("/api/import/site", json=_body(2))
    out = client.post("/api/import/preview", json=_body(2)).json()
    assert out["already_imported"] == "Novel"


def test_importing_over_http_creates_the_novel(client):
    r = client.post("/api/import/site", json=_body(4, name="Raised Too Precious"))
    assert r.status_code == 200
    out = r.json()
    assert out["imported"] == 4 and out["name"] == "Raised Too Precious"
    assert out["pricing"]["coin_price"] == 0
    # And it shows up in the library like any other novel.
    names = [p["name"] for p in client.get("/api/projects").json()["projects"]]
    assert "Raised Too Precious" in names


def test_the_library_card_can_tell_it_was_imported(client):
    client.post("/api/import/site", json=_body(2))
    project = client.get("/api/projects").json()["projects"][0]
    # project_summary spreads the whole project, so the provenance arrives with no
    # backend change - that is what the card reads to show its label.
    assert project["imported_from"]["site"] == "meiko"
    assert project["source_type"] == "text"


def test_an_import_with_no_name_is_refused(client):
    assert client.post("/api/import/site", json=_body(2, name="  ")).status_code == 400


def test_an_import_with_no_chapters_is_refused(client):
    assert client.post("/api/import/site",
                       json={"name": "X", "chapters": []}).status_code == 400


def test_a_second_import_of_the_same_novel_is_refused(client):
    assert client.post("/api/import/site", json=_body(2)).status_code == 200
    again = client.post("/api/import/site", json=_body(2))
    assert again.status_code == 400
    assert "already in the library" in again.json()["detail"]["title"]


def test_an_unknown_field_is_refused_rather_than_ignored(client):
    # extra="forbid", so a renamed field fails loudly instead of silently importing
    # something different from what was asked for.
    r = client.post("/api/import/site", json={**_body(2), "seriesUid": "camel"})
    assert r.status_code == 422


def test_the_catalogue_and_candidates_round_trip(client):
    r = client.post("/api/import/catalogue", json={
        "studio_uid": "studio1",
        "series": [{"uid": "aaa", "name": "One"}, {"uid": "bbb", "name": "Two"}],
    })
    assert r.status_code == 200 and r.json()["importable"] == 2
    out = client.get("/api/import/candidates").json()
    assert [s["name"] for s in out["series"]] == ["One", "Two"]


def test_an_empty_catalogue_is_refused_over_http(client):
    client.post("/api/import/catalogue", json={"studio_uid": "s",
                                               "series": [{"uid": "a", "name": "One"}]})
    assert client.post("/api/import/catalogue",
                       json={"studio_uid": "s", "series": []}).status_code == 400
    assert len(client.get("/api/import/candidates").json()["series"]) == 1


def test_an_imported_novel_reads_end_to_end(client):
    # The chapter list and the reader both work, which is the thing a sparse source.json
    # would silently break.
    out = client.post("/api/import/site", json=_body(3)).json()
    pid = out["pid"]
    rows = client.get(f"/api/projects/{pid}/chapters").json()
    assert rows["total"] == 3
    assert [c["number"] for c in rows["chapters"]] == ["1", "2", "3"]
    assert all(c["status"] == "validated" for c in rows["chapters"])
    assert all(c["has_output"] for c in rows["chapters"])
    assert not any(c["source_changed"] for c in rows["chapters"])
    # The reader opens it, with the English as both the source and the translation.
    one = client.get(f"/api/projects/{pid}/chapters/1").json()
    assert "Opening of chapter 1." in one["translation"]
    assert one["language"] == "english"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
