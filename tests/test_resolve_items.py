"""Tests for translation-queue resolution — specifically the whole-novel re-translate.

The bug: ``_resolve_items`` applied its already-done filter regardless of ``force``, so a
request of ``{"force": true}`` with no explicit indices (i.e. "re-translate this whole
novel", the Library action) silently resolved to an EMPTY list whenever every chapter was
already validated. The request returned 200 and queued nothing, so the feature looked like
it worked while doing nothing at all.

Also covers what force must NOT do: already-English chapters and empty tabs stay skipped,
because forcing a translation of those is meaningless.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from types import SimpleNamespace  # noqa: E402

import server.app as A  # noqa: E402
from translation_bot.docs_extract import Chapter  # noqa: E402
from translation_bot.state import State  # noqa: E402


def _chapters():
    """Three Korean chapters, one already-English, one empty tab."""
    return [
        Chapter(index=1, title="1화", paragraphs=["그는 문을 열었다."]),
        Chapter(index=2, title="2화", paragraphs=["안에는 아무도 없었다."]),
        Chapter(index=3, title="3화", paragraphs=["그녀가 웃었다."]),
        Chapter(index=4, title="Prologue", paragraphs=["He opened the door."]),
        Chapter(index=5, title="blank", paragraphs=[]),
    ]


def _setup(monkeypatch, tmp_path, *, done_indices=()):
    chapters = _chapters()
    state = State()
    for i in done_indices:
        ch = next(c for c in chapters if c.index == i)
        state.update(i, status="validated", source_hash=ch.metrics.content_hash)
    state_file = tmp_path / "state.json"
    state.save(state_file)

    cfg = SimpleNamespace(
        paths=SimpleNamespace(state_file=state_file),
        translation=SimpleNamespace(min_hangul_fraction=0.15, skip_non_korean=True),
    )
    monkeypatch.setattr(A, "get_chapters", lambda pid, c, refresh=False: chapters)
    return cfg


def _indices(items):
    return sorted(i for i, _force, _kind in items)


def test_force_requeues_every_korean_chapter_even_when_all_are_done(monkeypatch, tmp_path):
    """The reported bug: force + no indices must NOT be filtered by is_done."""
    cfg = _setup(monkeypatch, tmp_path, done_indices=(1, 2, 3))
    items = A._resolve_items("p", cfg, A.TranslateRequest(force=True))
    assert _indices(items) == [1, 2, 3], "a forced whole-novel run queued nothing"
    assert all(force is True for _i, force, _kind in items)
    assert all(kind == A.TASK_TRANSLATE for _i, _f, kind in items)


def test_force_still_skips_english_and_empty_tabs(monkeypatch, tmp_path):
    cfg = _setup(monkeypatch, tmp_path, done_indices=(1, 2, 3))
    got = _indices(A._resolve_items("p", cfg, A.TranslateRequest(force=True)))
    assert 4 not in got, "an already-English chapter must not be force-translated"
    assert 5 not in got, "an empty tab has nothing to translate"


def test_without_force_finished_chapters_are_still_skipped(monkeypatch, tmp_path):
    """The resumability guarantee this filter exists for must be untouched."""
    cfg = _setup(monkeypatch, tmp_path, done_indices=(1, 2))
    items = A._resolve_items("p", cfg, A.TranslateRequest(force=False))
    assert _indices(items) == [3]
    assert all(force is False for _i, force, _kind in items)


def test_without_force_a_fresh_novel_queues_all_korean(monkeypatch, tmp_path):
    cfg = _setup(monkeypatch, tmp_path)
    assert _indices(A._resolve_items("p", cfg, A.TranslateRequest(force=False))) == [1, 2, 3]


def test_explicit_indices_bypass_classification_entirely(monkeypatch, tmp_path):
    """An explicit list is the user pointing at specific chapters, so it's honoured as
    given — that's what the per-row Re-translate button and the Review inbox rely on."""
    cfg = _setup(monkeypatch, tmp_path, done_indices=(1, 2, 3))
    items = A._resolve_items("p", cfg, A.TranslateRequest(indices=[2, 4], force=True))
    assert _indices(items) == [2, 4]


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
