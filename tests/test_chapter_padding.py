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


def test_missing_project_dir_does_not_raise(tmp_path, monkeypatch):
    import server.projects as pj

    monkeypatch.setattr(pj, "PROJECTS_DIR", tmp_path)
    _normalize_chapter_padding("no-such-novel", 100)


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
