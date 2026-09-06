"""Regression tests for chapter-file zero-padding (server/app.py).

The pad width of ``chapter-NN.md`` comes from the total chapter count, so a
source doc that grows past a digit boundary (96 tabs -> 100) changes the name
the app looks for. Without re-padding, every already-translated chapter goes
invisible and the reader shows the Korean source with "Not translated yet".

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_chapter_padding.py``).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.app import _normalize_chapter_padding  # noqa: E402
from translation_bot.pipeline import chapter_filename  # noqa: E402


def _make(tmp_path, monkeypatch, names, sub="chapters"):
    """Build a fake project dir and point the app's PROJECTS_DIR at it."""
    import server.projects as pj

    monkeypatch.setattr(pj, "PROJECTS_DIR", tmp_path)
    d = tmp_path / "novel" / sub
    d.mkdir(parents=True)
    for n in names:
        (d / n).write_text(n, encoding="utf-8")
    return d


# ---- the filename contract --------------------------------------------------

def test_pad_width_follows_total():
    assert chapter_filename(1, 96) == "chapter-01.md"
    assert chapter_filename(1, 100) == "chapter-001.md"
    assert chapter_filename(7, 9) == "chapter-07.md"      # floor of 2


# ---- re-padding -------------------------------------------------------------

def test_widens_names_when_doc_grows(tmp_path, monkeypatch):
    d = _make(tmp_path, monkeypatch, ["chapter-01.md", "chapter-96.md"])
    _normalize_chapter_padding("novel", 100)
    assert (d / "chapter-001.md").exists()
    assert (d / "chapter-096.md").exists()
    assert not (d / "chapter-01.md").exists()


def test_content_is_preserved(tmp_path, monkeypatch):
    d = _make(tmp_path, monkeypatch, ["chapter-04.md"])
    _normalize_chapter_padding("novel", 100)
    assert (d / "chapter-004.md").read_text(encoding="utf-8") == "chapter-04.md"


def test_already_correct_is_untouched(tmp_path, monkeypatch):
    d = _make(tmp_path, monkeypatch, ["chapter-001.md"])
    _normalize_chapter_padding("novel", 100)
    assert (d / "chapter-001.md").exists()
    assert len(list(d.glob("*.md"))) == 1


def test_is_idempotent(tmp_path, monkeypatch):
    d = _make(tmp_path, monkeypatch, ["chapter-01.md"])
    _normalize_chapter_padding("novel", 100)
    _normalize_chapter_padding("novel", 100)
    assert [f.name for f in d.glob("*.md")] == ["chapter-001.md"]


def test_never_overwrites_a_newer_translation(tmp_path, monkeypatch):
    # Both names present: the 3-digit file is the live one and must survive.
    d = _make(tmp_path, monkeypatch, ["chapter-01.md", "chapter-001.md"])
    _normalize_chapter_padding("novel", 100)
    assert (d / "chapter-001.md").read_text(encoding="utf-8") == "chapter-001.md"
    assert (d / "chapter-01.md").exists()  # left alone rather than destroyed


def test_previous_and_audit_are_repadded_too(tmp_path, monkeypatch):
    import server.projects as pj

    monkeypatch.setattr(pj, "PROJECTS_DIR", tmp_path)
    for sub in ("chapters", "previous", "audit"):
        d = tmp_path / "novel" / sub
        d.mkdir(parents=True)
        (d / "chapter-05.md").write_text("x", encoding="utf-8")
    _normalize_chapter_padding("novel", 100)
    for sub in ("chapters", "previous", "audit"):
        assert (tmp_path / "novel" / sub / "chapter-005.md").exists()


def test_ignores_unrelated_and_bad_names(tmp_path, monkeypatch):
    d = _make(tmp_path, monkeypatch, ["chapter-01.md", "chapter-draft.md",
                                      "notes.md"])
    _normalize_chapter_padding("novel", 100)
    assert (d / "chapter-001.md").exists()
    assert (d / "chapter-draft.md").exists()
    assert (d / "notes.md").exists()


def test_zero_total_is_a_no_op(tmp_path, monkeypatch):
    d = _make(tmp_path, monkeypatch, ["chapter-01.md"])
    _normalize_chapter_padding("novel", 0)
    assert (d / "chapter-01.md").exists()


# ---- a stale count must never create a SECOND file for one chapter ----------
# The worker captures a chapter count at job start and keeps it for its whole run,
# while a Refresh press or a newly added tab re-pads every file underneath it. Writing
# at the stale width then produced two files for one chapter: the reader saw one, the
# other was invisible, previous/ captured neither, and the padding normalizer refused
# to reconcile them because its destination was taken. A paid-for translation, marked
# validated in state.json, that could be neither read nor recovered.

def test_writing_with_a_stale_count_reuses_the_existing_file(tmp_path):
    from translation_bot.pipeline import write_chapter_file

    out = tmp_path / "chapters"
    # Novel had 100 chapters: 3-digit names on disk.
    write_chapter_file(out, 7, 100, "first translation")
    assert (out / "chapter-007.md").exists()

    # A worker holding the OLD count of 99 finishes the same chapter.
    write_chapter_file(out, 7, 99, "second translation")

    files = sorted(p.name for p in out.glob("chapter-*.md"))
    assert files == ["chapter-007.md"], f"a stale count created a duplicate: {files}"
    assert "second translation" in (out / "chapter-007.md").read_text(encoding="utf-8")


def test_the_superseded_text_still_reaches_previous(tmp_path):
    """The dual-file bug also skipped the previous/ snapshot, because the canonical
    name did not exist yet — so neither copy was recoverable.

    Note the file keeps the name it already had: renaming belongs to the padding
    normalizer, not to a writer that may be holding a stale count.
    """
    from translation_bot.pipeline import write_chapter_file

    out = tmp_path / "chapters"
    write_chapter_file(out, 7, 99, "older translation")     # width 2
    write_chapter_file(out, 7, 100, "newer translation")    # count grew to width 3

    files = sorted(p.name for p in out.glob("chapter-*.md"))
    assert len(files) == 1, f"one chapter must mean one file, got {files}"
    assert "newer translation" in (out / files[0]).read_text(encoding="utf-8")

    prev = tmp_path / "previous"
    kept = list(prev.glob("chapter-*.md")) if prev.is_dir() else []
    assert kept, "the superseded translation must be snapshotted, not lost"
    assert "older translation" in kept[0].read_text(encoding="utf-8")


def test_a_chapter_written_at_any_width_is_still_found(tmp_path):
    from translation_bot.pipeline import chapter_path, write_chapter_file

    out = tmp_path / "chapters"
    write_chapter_file(out, 7, 99, "translated at width two")

    # Every reader now passes the CURRENT count, which implies width 3.
    found = chapter_path(out, 7, 100)
    assert found.exists(), "a chapter written at another width must still resolve"
    assert "width two" in found.read_text(encoding="utf-8")


def test_the_canonical_name_wins_when_both_exist(tmp_path):
    """Existing damage: both widths on disk. Resolution must be deterministic and
    match what the app already shows, so a repair never swaps the visible text."""
    from translation_bot.pipeline import chapter_path

    out = tmp_path / "chapters"
    out.mkdir()
    (out / "chapter-07.md").write_text("older, hidden", encoding="utf-8")
    (out / "chapter-007.md").write_text("newer, visible", encoding="utf-8")

    assert chapter_path(out, 7, 100).name == "chapter-007.md"


def test_variants_history_is_repadded_too(tmp_path, monkeypatch):
    """Per-paragraph history is named from the same stem as the chapter file.

    It was left out of the re-pad, so a novel crossing 99 -> 100 chapters orphaned
    every rewrite the reader had kept: the app looked for chapter-007.json while the
    file on disk was still chapter-07.json, and the history silently vanished.
    """
    import server.projects as pj

    monkeypatch.setattr(pj, "PROJECTS_DIR", tmp_path)
    d = tmp_path / "novel" / "variants"
    d.mkdir(parents=True)
    (d / "chapter-07.json").write_text('{"version": 1, "groups": []}', encoding="utf-8")

    _normalize_chapter_padding("novel", 100)

    assert (d / "chapter-007.json").exists(), "paragraph history must follow the re-pad"
    assert not (d / "chapter-07.json").exists()


def test_repadding_leaves_unrelated_json_alone(tmp_path, monkeypatch):
    import server.projects as pj

    monkeypatch.setattr(pj, "PROJECTS_DIR", tmp_path)
    d = tmp_path / "novel" / "variants"
    d.mkdir(parents=True)
    (d / "notes.json").write_text("{}", encoding="utf-8")

    _normalize_chapter_padding("novel", 100)
    assert (d / "notes.json").exists()


# ---- the CALLER has to pass the right total ---------------------------------
# Every test above exercises _normalize_chapter_padding directly. The bug was one
# level up: get_chapters passed len(_chapter_cache[pid]) instead of _output_total.
# On the offline fallback the cache is rebuilt from state.json alone and can hold
# far fewer records than the novel really has, so the short count NARROWED the
# filenames the rest of the app then failed to find.

def test_offline_fallback_does_not_narrow_existing_chapter_files(tmp_path, monkeypatch):
    import json

    import server.app as A
    import server.projects as pj
    from translation_bot.config import Config

    monkeypatch.setattr(pj, "PROJECTS_DIR", tmp_path)
    pid = "abcdef012345"
    pdir = tmp_path / pid
    (pdir / "chapters").mkdir(parents=True)

    # A 150-chapter novel: files are 3-digit padded.
    (pdir / "project.json").write_text(
        json.dumps({"id": pid, "name": "Big novel", "chapter_count": 150}),
        encoding="utf-8")
    for i in (1, 2, 99):
        (pdir / "chapters" / f"chapter-{i:03d}.md").write_text(f"ch {i}", encoding="utf-8")

    # state.json knows about only a handful, which is what the offline rebuild sees.
    (pdir / "state.json").write_text(
        json.dumps({"chapters": {str(i): {"status": "validated", "title": f"Tab {i}"}
                                 for i in (1, 2, 99)}}),
        encoding="utf-8")

    # Force the offline branch deterministically — never touch the network or the
    # developer's real token.json.
    monkeypatch.setattr(A, "load_saved_credentials",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("offline")))
    A._chapter_cache.pop(pid, None)
    A._offline_projects.discard(pid)

    cfg = pj.project_config(Config(), pj.get_project(pid))
    try:
        A.get_chapters(pid, cfg)
    finally:
        A._chapter_cache.pop(pid, None)
        A._offline_projects.discard(pid)

    for i in (1, 2, 99):
        assert (pdir / "chapters" / f"chapter-{i:03d}.md").exists(), (
            f"chapter-{i:03d}.md was re-padded away; the app would report a finished "
            f"chapter as untranslated and re-bill the whole novel to redo it")
        assert not (pdir / "chapters" / f"chapter-{i:02d}.md").exists()


def test_missing_project_dir_does_not_raise(tmp_path, monkeypatch):
    import server.projects as pj

    monkeypatch.setattr(pj, "PROJECTS_DIR", tmp_path)
    _normalize_chapter_padding("no-such-novel", 100)


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
