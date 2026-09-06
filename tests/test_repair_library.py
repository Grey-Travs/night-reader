"""The one-time repair for damage already on disk.

Three fixed bugs left wreckage behind. The fixes stop it recurring; they cannot undo
it, because the app reads what is on disk rather than rewriting it.

The properties that matter more than the repairs themselves:

- **Nothing is ever deleted.** A superseded chapter is a real translation the user
  paid for — just not the current one. It moves into ``.superseded/``.
- **A lone stale-width file is never touched.** It IS the live copy of that chapter;
  ``_normalize_chapter_padding`` renames it on load. Moving it aside would make a
  translated chapter vanish, which is the exact failure this tool exists to repair.
- **Dry run changes nothing**, and reports the same list ``--apply`` would act on.
- **A page whose manifest was just written is left alone** — a job may be live, and
  resetting its page would race the worker.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_repair_library.py``).
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.repair_library import (  # noqa: E402
    canonical_width,
    repair_library,
    repair_pages,
)


# ---- fixtures ----------------------------------------------------------------

def _novel(tmp_path, pid="abc123abc123", *, chapter_count=100, **files):
    """Build a project folder. `files` maps subdir -> {filename: text}."""
    pdir = tmp_path / pid
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "project.json").write_text(
        json.dumps({"id": pid, "name": "Test Novel", "chapter_count": chapter_count}),
        encoding="utf-8")
    for sub, contents in files.items():
        d = pdir / sub
        d.mkdir(parents=True, exist_ok=True)
        for name, text in contents.items():
            (d / name).write_text(text, encoding="utf-8")
    return pdir


def _age(path, minutes):
    """Backdate a file so the tool sees it as old."""
    when = time.time() - minutes * 60
    os.utime(path, (when, when))


# ---- A. stale pad widths ------------------------------------------------------

def test_a_superseded_stale_width_file_is_moved_aside_not_deleted(tmp_path):
    # The real shape: a novel that crossed 99 chapters and was then re-translated, so
    # both generations sit in chapters/ forever and search returns each chapter twice.
    pdir = _novel(tmp_path, chapter_count=100, chapters={
        "chapter-07.md": "the June generation",
        "chapter-007.md": "the August generation",
    })
    _age(pdir / "chapters" / "chapter-07.md", 60 * 24 * 60)

    report = repair_library(tmp_path, apply=True)

    assert not (pdir / "chapters" / "chapter-07.md").exists()
    moved = pdir / "chapters" / ".superseded" / "chapter-07.md"
    assert moved.exists(), "a superseded translation must be kept, not deleted"
    assert moved.read_text(encoding="utf-8") == "the June generation"
    assert (pdir / "chapters" / "chapter-007.md").read_text(encoding="utf-8") == \
        "the August generation", "the live chapter must not move"
    assert report.by_kind() == {"padding": 1}


def test_a_lone_stale_width_file_is_left_alone(tmp_path):
    """The load-bearing safety property.

    With no canonical counterpart, chapter-07.md IS chapter 7 — the app renames it on
    the next load. Moving it aside would delete a translated chapter, which is the
    damage this tool exists to undo.
    """
    pdir = _novel(tmp_path, chapter_count=100, chapters={
        "chapter-07.md": "the only copy of chapter 7",
        "chapter-008.md": "chapter 8",
    })
    _age(pdir / "chapters" / "chapter-07.md", 60 * 24 * 60)

    report = repair_library(tmp_path, apply=True)

    assert (pdir / "chapters" / "chapter-07.md").exists(), \
        "a chapter with no canonical twin must never be moved"
    assert report.changes == []


def test_a_stale_width_file_that_is_newer_is_reported_not_moved(tmp_path):
    """If the stale-width copy is the newer one, this tool's model of the damage does
    not hold for that novel — moving it aside would hide the better translation."""
    pdir = _novel(tmp_path, chapter_count=100, chapters={
        "chapter-07.md": "written yesterday",
        "chapter-007.md": "written last year",
    })
    _age(pdir / "chapters" / "chapter-007.md", 60 * 24 * 365)

    report = repair_library(tmp_path, apply=True)

    assert (pdir / "chapters" / "chapter-07.md").exists()
    assert report.changes == []
    assert any("NEWER" in w for w in report.warnings), \
        "the human has to be told, not silently skipped"


def test_every_padded_directory_is_repaired(tmp_path):
    # previous/ and audit/ and variants/ are named from the same stem, so they strand
    # in exactly the same way — and audit/ holds the ONLY copy of a needs-review
    # chapter's translation.
    pdir = _novel(tmp_path, chapter_count=100,
                  chapters={"chapter-07.md": "a", "chapter-007.md": "A"},
                  previous={"chapter-07.md": "b", "chapter-007.md": "B"},
                  audit={"chapter-07.md": "c", "chapter-007.md": "C"},
                  variants={"chapter-07.json": "{}", "chapter-007.json": "{}"})
    for sub, name in (("chapters", "chapter-07.md"), ("previous", "chapter-07.md"),
                      ("audit", "chapter-07.md"), ("variants", "chapter-07.json")):
        _age(pdir / sub / name, 60 * 24 * 60)

    report = repair_library(tmp_path, apply=True)

    assert report.by_kind() == {"padding": 4}
    for sub, name in (("chapters", "chapter-07.md"), ("previous", "chapter-07.md"),
                      ("audit", "chapter-07.md"), ("variants", "chapter-07.json")):
        assert (pdir / sub / ".superseded" / name).exists(), f"{sub} was not repaired"


def test_a_dry_run_changes_nothing(tmp_path):
    pdir = _novel(tmp_path, chapter_count=100, chapters={
        "chapter-07.md": "old", "chapter-007.md": "new",
    })
    _age(pdir / "chapters" / "chapter-07.md", 60 * 24 * 60)

    report = repair_library(tmp_path, apply=False)

    assert (pdir / "chapters" / "chapter-07.md").exists(), "a dry run must not move files"
    assert not (pdir / "chapters" / ".superseded").exists()
    assert report.by_kind() == {"padding": 1}, "but it must report what it would do"


def test_repairing_twice_is_a_no_op(tmp_path):
    pdir = _novel(tmp_path, chapter_count=100, chapters={
        "chapter-07.md": "old", "chapter-007.md": "new",
    })
    _age(pdir / "chapters" / "chapter-07.md", 60 * 24 * 60)

    repair_library(tmp_path, apply=True)
    second = repair_library(tmp_path, apply=True)

    assert second.changes == [], "the repair must be safe to run again"
    assert second.warnings == []


def test_only_the_named_project_is_touched(tmp_path):
    a = _novel(tmp_path, "aaaaaaaaaaaa", chapter_count=100,
               chapters={"chapter-07.md": "x", "chapter-007.md": "y"})
    b = _novel(tmp_path, "bbbbbbbbbbbb", chapter_count=100,
               chapters={"chapter-07.md": "x", "chapter-007.md": "y"})
    _age(a / "chapters" / "chapter-07.md", 60 * 24 * 60)
    _age(b / "chapters" / "chapter-07.md", 60 * 24 * 60)

    repair_library(tmp_path, apply=True, only="aaaaaaaaaaaa")

    assert not (a / "chapters" / "chapter-07.md").exists()
    assert (b / "chapters" / "chapter-07.md").exists()


# ---- the pad width itself -----------------------------------------------------

def test_canonical_width_comes_from_the_chapter_count(tmp_path):
    assert canonical_width(_novel(tmp_path, chapter_count=100)) == 3
    assert canonical_width(_novel(tmp_path, "bbbbbbbbbbbb", chapter_count=57)) == 2


def test_canonical_width_is_floored_by_what_is_on_disk(tmp_path):
    """A project.json that lags the source doc must not shrink the width and make the
    tool call the CURRENT files stale — that would move the live novel aside."""
    pdir = _novel(tmp_path, chapter_count=9,
                  chapters={"chapter-100.md": "x", "chapter-007.md": "y"})
    assert canonical_width(pdir) == 3


def test_a_two_digit_novel_keeps_two_digit_names(tmp_path):
    # max(2, …): a 9-chapter novel is chapter-01.md, never chapter-1.md.
    pdir = _novel(tmp_path, chapter_count=9, chapters={"chapter-01.md": "x"})
    assert canonical_width(pdir) == 2
    assert repair_library(tmp_path, apply=True).changes == []


# ---- B. colliding variant ids -------------------------------------------------

def _variants_doc(*ids, current="v0", next_seq=None):
    group = {"id": "g1", "paragraph": 0, "original": "o", "current_id": current,
             "variants": [{"id": i, "text": f"text of {i}"} for i in ids]}
    if next_seq is not None:
        group["next_seq"] = next_seq
    return {"version": 1, "chapter": 1, "groups": [group]}


def test_a_duplicate_variant_id_is_renumbered(tmp_path):
    # What prune-then-add produced: v3 exists twice, and find_variant returns the
    # first — so picking "v3" could splice the wrong version into the chapter.
    doc = _variants_doc("v0", "v3", "v3")
    pdir = _novel(tmp_path, variants={"chapter-01.json": json.dumps(doc)})

    report = repair_library(tmp_path, apply=True)

    saved = json.loads((pdir / "variants" / "chapter-01.json").read_text(encoding="utf-8"))
    ids = [v["id"] for v in saved["groups"][0]["variants"]]
    assert len(set(ids)) == len(ids), "ids must be unique after the repair"
    assert report.by_kind()["variant-id"] == 1


def test_the_first_occurrence_keeps_its_id(tmp_path):
    """find_variant already resolves "v3" to the first one, so that is the text
    currently in the chapter. Renumbering it instead would move the reader's chapter
    text out from under current_id."""
    doc = _variants_doc("v0", "v3", "v3", current="v3")
    pdir = _novel(tmp_path, variants={"chapter-01.json": json.dumps(doc)})

    repair_library(tmp_path, apply=True)

    saved = json.loads((pdir / "variants" / "chapter-01.json").read_text(encoding="utf-8"))
    variants = saved["groups"][0]["variants"]
    assert variants[1]["id"] == "v3", "the resolved variant must keep its id"
    assert variants[1]["text"] == "text of v3"
    assert variants[2]["id"] != "v3", "only the shadowed duplicate is renumbered"


def test_next_seq_is_raised_above_every_id_in_use(tmp_path):
    # Otherwise the very next rewrite mints a colliding id all over again.
    doc = _variants_doc("v0", "v3", "v3")
    pdir = _novel(tmp_path, variants={"chapter-01.json": json.dumps(doc)})

    repair_library(tmp_path, apply=True)

    group = json.loads((pdir / "variants" / "chapter-01.json")
                       .read_text(encoding="utf-8"))["groups"][0]
    used = {int(v["id"][1:]) for v in group["variants"]}
    assert group["next_seq"] > max(used)


def test_a_legacy_group_with_unique_ids_only_gains_next_seq(tmp_path):
    doc = _variants_doc("v0", "v1", "v2")
    pdir = _novel(tmp_path, variants={"chapter-01.json": json.dumps(doc)})

    report = repair_library(tmp_path, apply=True)

    group = json.loads((pdir / "variants" / "chapter-01.json")
                       .read_text(encoding="utf-8"))["groups"][0]
    assert [v["id"] for v in group["variants"]] == ["v0", "v1", "v2"], \
        "nothing was wrong with these ids"
    assert group["next_seq"] == 3
    assert report.by_kind()["variant-id"] == 1


def test_a_healthy_variants_file_is_not_rewritten(tmp_path):
    doc = _variants_doc("v0", "v1", next_seq=2)
    pdir = _novel(tmp_path, variants={"chapter-01.json": json.dumps(doc)})
    before = (pdir / "variants" / "chapter-01.json").read_text(encoding="utf-8")

    assert repair_library(tmp_path, apply=True).changes == []
    assert (pdir / "variants" / "chapter-01.json").read_text(encoding="utf-8") == before


def test_an_unreadable_variants_file_is_reported_not_crashed_on(tmp_path):
    _novel(tmp_path, variants={"chapter-01.json": "{ this is not json"})
    report = repair_library(tmp_path, apply=True)
    assert report.changes == []
    assert any("unreadable" in w for w in report.warnings)


# ---- C. stranded pages --------------------------------------------------------

def _pages_doc(*statuses):
    return {"version": 1, "next_seq": len(statuses) + 1,
            "pages": [{"id": f"p{i}", "status": s} for i, s in enumerate(statuses)]}


def test_a_stranded_page_is_reset_to_new(tmp_path):
    pdir = _novel(tmp_path)
    f = pdir / "pages.json"
    f.write_text(json.dumps(_pages_doc("ocr-running", "queued", "ok")), encoding="utf-8")
    _age(f, 120)

    report = repair_library(tmp_path, apply=True)

    saved = json.loads(f.read_text(encoding="utf-8"))
    assert [p["status"] for p in saved["pages"]] == ["new", "new", "ok"], \
        "only the stranded pages move, and 'ok' is never touched"
    assert report.by_kind()["stuck-page"] == 2


def test_a_recently_written_manifest_is_left_alone(tmp_path):
    """A job may be live. Resetting its page would race the worker — and the page is
    not stranded, it is running."""
    pdir = _novel(tmp_path)
    f = pdir / "pages.json"
    f.write_text(json.dumps(_pages_doc("ocr-running")), encoding="utf-8")
    # mtime is now, so the manifest is fresh

    report = repair_library(tmp_path, apply=True)

    assert json.loads(f.read_text(encoding="utf-8"))["pages"][0]["status"] == "ocr-running"
    assert report.changes == []
    assert any("may be live" in w for w in report.warnings)


def test_the_staleness_threshold_is_configurable(tmp_path):
    pdir = _novel(tmp_path)
    f = pdir / "pages.json"
    f.write_text(json.dumps(_pages_doc("queued")), encoding="utf-8")
    _age(f, 10)

    assert repair_library(tmp_path, apply=True, stale_minutes=30).changes == []
    assert repair_library(tmp_path, apply=True, stale_minutes=5).changes != []


def test_a_project_with_no_pages_file_is_skipped(tmp_path):
    _novel(tmp_path)
    report = repair_library(tmp_path, apply=True)
    assert report.changes == [] and report.warnings == []
    assert report.projects_scanned == 1


def test_pages_repair_uses_the_clock_it_is_given(tmp_path):
    # Guards the age arithmetic itself, without waiting 30 minutes.
    from tools.repair_library import Report
    pdir = _novel(tmp_path)
    f = pdir / "pages.json"
    f.write_text(json.dumps(_pages_doc("queued")), encoding="utf-8")

    report = Report()
    repair_pages(pdir, report, apply=False, stale_minutes=30,
                 now=time.time() + 31 * 60)
    assert len(report.changes) == 1


# ---- the driver ---------------------------------------------------------------

def test_a_folder_with_no_project_json_is_not_a_novel(tmp_path):
    (tmp_path / "not-a-novel" / "chapters").mkdir(parents=True)
    (tmp_path / "not-a-novel" / "chapters" / "chapter-07.md").write_text("x")
    report = repair_library(tmp_path, apply=True)
    assert report.projects_scanned == 0
    assert (tmp_path / "not-a-novel" / "chapters" / "chapter-07.md").exists()


def test_a_missing_library_is_reported_not_raised(tmp_path):
    report = repair_library(tmp_path / "nope", apply=True)
    assert report.projects_scanned == 0
    assert report.warnings


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
