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
