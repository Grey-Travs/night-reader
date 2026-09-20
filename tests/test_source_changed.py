"""A translation whose Korean has moved on must stop claiming to be finished.

``state.json`` fingerprints the source each translation was made from, and
``State.is_done`` already compares it — which is why a plain Translate re-queues such
a chapter. But nothing READ that fingerprint, so the chapter went on reporting
``validated`` with its old English file sitting right there.

Two paths reach that state, both on scanned books:

- rebuilding after deleting or reordering pages, which renumbers what each index holds,
- correcting a page that was already built into a chapter.

Either replaces the Korean under a chapter while the English stays exactly where it
was. The reader then showed one chapter's English beside another's Korean, presented
as finished; exports shipped it; and the user had no reason to press the one button
that would have fixed it.

The translation is never deleted or hidden — it is real work the user paid for. It is
reported as stale, and the reader says so.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_source_changed.py``).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server.app as A  # noqa: E402
import server.pages as P  # noqa: E402
import server.projects as pj  # noqa: E402
from translation_bot import state as state_mod  # noqa: E402
from translation_bot.config import Config  # noqa: E402
from translation_bot.state import State  # noqa: E402

KOREAN = "그는 천천히 문을 열었다."
ENGLISH = "He opened the door slowly.\n"


@pytest.fixture
def client(monkeypatch, tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    for module in (pj, P):
        monkeypatch.setattr(module, "PROJECTS_DIR", root)
    monkeypatch.setattr(A, "load_global_config", lambda: Config())
    A._chapter_cache.clear()
    A._offline_projects.clear()
    return TestClient(A.app, base_url="http://localhost",
                      client=("127.0.0.1", 50000))


def _novel(client, text=KOREAN):
    pid = client.post("/api/projects/text", json={
        "name": "Test novel", "text": text, "split_mode": "single"}).json()["id"]
    return pid, pj.project_config(Config(), pj.get_project(pid))


def _translate(cfg, *, source_hash, status=state_mod.STATUS_VALIDATED):
    """Put a finished translation on disk, fingerprinted against `source_hash`."""
    cfg.paths.output_dir.mkdir(parents=True, exist_ok=True)
    (cfg.paths.output_dir / "chapter-01.md").write_text(ENGLISH, encoding="utf-8")
    state = State.load(cfg.paths.state_file)
    state.update(1, status=status, title="Chapter 1", source_hash=source_hash)
    state.save(cfg.paths.state_file)


def _real_hash(pid, cfg):
    A._chapter_cache.pop(pid, None)
    return A.get_chapters(pid, cfg)[0].metrics.content_hash


def _row(client, pid):
    A._chapter_cache.pop(pid, None)
    return client.get(f"/api/projects/{pid}/chapters").json()["chapters"][0]


def _detail(client, pid):
    A._chapter_cache.pop(pid, None)
    return client.get(f"/api/projects/{pid}/chapters/1").json()


# ---- the matching case must stay untouched -----------------------------------

def test_a_translation_matching_its_source_is_still_done(client):
    pid, cfg = _novel(client)
    _translate(cfg, source_hash=_real_hash(pid, cfg))

    row = _row(client, pid)
    assert row["status"] == "validated"
    assert row["source_changed"] is False
    assert _detail(client, pid)["source_changed"] is False


def test_an_untranslated_chapter_is_never_called_stale(client):
    """No stored hash means nothing to differ from — a chapter that was simply never
    translated must not be reported as having changed."""
    pid, _cfg = _novel(client)
    row = _row(client, pid)
    assert row["source_changed"] is False
    assert row["status"] == "pending"


def test_a_chapter_that_was_never_finished_is_not_called_stale(client):
    """Only a DONE status carries the promise this guard protects. A failed attempt
    already reads as unfinished."""
    pid, cfg = _novel(client)
    _translate(cfg, source_hash="a-hash-from-somewhere-else",
               status=state_mod.STATUS_FAILED)
    assert _row(client, pid)["source_changed"] is False


# ---- the mismatching case ----------------------------------------------------

def test_a_changed_source_stops_the_chapter_reading_as_finished(client):
    pid, cfg = _novel(client)
    _translate(cfg, source_hash="the-hash-of-some-OTHER-korean")

    row = _row(client, pid)
    assert row["source_changed"] is True
    assert row["status"] == "pending", \
        "the list must agree with the queue, which already re-queues this chapter"


def test_the_reader_is_told_rather_than_shown_a_silent_mismatch(client):
    pid, cfg = _novel(client)
    _translate(cfg, source_hash="the-hash-of-some-OTHER-korean")

    detail = _detail(client, pid)
    assert detail["source_changed"] is True
    assert detail["translation"].strip() == ENGLISH.strip(), \
        "the translation is real work — flag it, never hide or delete it"


def test_a_stale_chapter_is_what_translate_would_queue(client):
    """The guard exists to make the UI agree with is_done, so the two must not be able
    to drift apart."""
    pid, cfg = _novel(client)
    _translate(cfg, source_hash="the-hash-of-some-OTHER-korean")
    A._chapter_cache.pop(pid, None)

    items = A._resolve_items(pid, cfg, A.TranslateRequest())
    assert sorted(i for i, _f, _k in items) == [1]
    assert _row(client, pid)["status"] == "pending"


def test_re_translating_clears_it(client):
    """Saving a translation records the CURRENT source hash, so the flag lifts by
    itself — there is no separate thing to remember to reset."""
    pid, cfg = _novel(client)
    _translate(cfg, source_hash="stale")
    assert _row(client, pid)["source_changed"] is True

    _translate(cfg, source_hash=_real_hash(pid, cfg))
    assert _row(client, pid)["source_changed"] is False
    assert _row(client, pid)["status"] == "validated"


def test_the_helper_itself(client):
    """Directly, so the rule is pinned independently of the two endpoints."""
    pid, cfg = _novel(client)
    A._chapter_cache.pop(pid, None)
    ch = A.get_chapters(pid, cfg)[0]
    real = ch.metrics.content_hash

    done = state_mod.STATUS_VALIDATED
    assert A.source_changed(ch, {"status": done, "source_hash": "other"}) is True
    assert A.source_changed(ch, {"status": done, "source_hash": real}) is False
    assert A.source_changed(ch, {"status": done, "source_hash": ""}) is False
    assert A.source_changed(ch, {"status": done}) is False
    assert A.source_changed(ch, {}) is False
    assert A.source_changed(ch, {"status": "pending", "source_hash": "other"}) is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


# ---- telling drift from an edit -------------------------------------------

# Staleness alone cannot say WHY a chapter no longer matches its source, and the two
# answers are opposites: an edited chapter should be re-translated, while a whole novel
# sitting a few tabs off its own text must NOT be -- that is paid work, intact, merely
# filed under the wrong number.
#
# It happened: 10 tabs added to the front of a 56-tab document left all 56 validated
# chapters pointing at the wrong text. Every one read as stale and the page offered to
# re-translate the book. Run against that project's backup, this reports exactly
# {"count": 56, "shift": 10}; against the repaired copy, and against four healthy
# novels, it reports nothing.

from types import SimpleNamespace  # noqa: E402

from server.app import DRIFT_MIN, index_drift  # noqa: E402


def _tabs(hashes):
    """Chapter-likes at indices 1..N holding these source hashes, in order."""
    return [SimpleNamespace(index=i, metrics=SimpleNamespace(content_hash=h))
            for i, h in enumerate(hashes, 1)]


def _state(recorded):
    """``{index: source_hash}`` as state.json holds it, all validated."""
    return SimpleNamespace(chapters={
        str(i): {"source_hash": h, "status": "validated"} for i, h in recorded.items()})


def _rows(tabs, state):
    return [{"source_changed":
             (state.chapters.get(str(c.index)) or {}).get("source_hash")
             not in (None, c.metrics.content_hash)}
            for c in tabs]


def _call(tab_hashes, recorded):
    tabs, st = _tabs(tab_hashes), _state(recorded)
    return index_drift(tabs, st, _rows(tabs, st))


def test_a_block_inserted_at_the_front_is_named_with_its_shift():
    # Tabs x,y were added above a,b,c,d. The four translations still say a,b,c,d.
    got = _call(["x", "y", "a", "b", "c", "d"], {1: "a", 2: "b", 3: "c", 4: "d"})
    assert got == {"count": 4, "shift": 2}


def test_a_healthy_novel_is_silent():
    assert _call(["a", "b", "c", "d"], {1: "a", 2: "b", 3: "c", 4: "d"}) is None


def test_genuinely_edited_chapters_are_not_drift():
    # Every source changed and none of the old text is anywhere in the document. That is
    # editing, and re-translating IS the right answer -- so this must stay quiet.
    assert _call(["A", "B", "C", "D"], {1: "a", 2: "b", 3: "c", 4: "d"}) is None


def test_a_couple_of_moved_chapters_are_not_a_pattern():
    # Below the threshold: two is a coincidence, and a banner on a coincidence is noise.
    assert DRIFT_MIN == 3
    assert _call(["x", "a", "b"], {1: "a", 2: "b"}) is None


def test_a_source_on_two_tabs_is_never_placed():
    # Duplicated text: "where does this belong" has two answers, and the repair refuses
    # that one rather than guessing. Sized so the answer proves it: b and c are genuinely
    # moved, and if the ambiguous a were placed too the count would reach three and this
    # would report drift. It stays silent, so a was not counted.
    assert _call(["a", "a", "b", "c"], {1: "a", 2: "b", 3: "c"}) is None


def test_a_block_inserted_in_the_middle_shifts_only_the_tail():
    # Still one offset, because everything after the insertion moves together -- the
    # chapters above it do not move at all and are not counted.
    got = _call(["a", "b", "z", "c", "d", "e"], {1: "a", 2: "b", 3: "c", 4: "d", 5: "e"})
    assert got == {"count": 3, "shift": 1}


def test_a_reordering_reports_no_single_number():
    # Two chapters swapped AND the rest pushed down: the offsets disagree, so there is no
    # one number to name. It is still drift, and the repair still handles it.
    got = _call(["b", "a", "x", "c"], {1: "a", 2: "b", 3: "c"})
    assert got == {"count": 3, "shift": None}


def test_two_translations_landing_on_one_tab_are_refused():
    # Two records share a source hash, so both want the same index. The repair empties its
    # whole move set on that and does nothing, and this has to refuse on the same terms.
    assert _call(["x", "y", "a", "b", "c"],
                 {1: "a", 2: "a", 3: "b", 4: "c"}) is None


def test_an_untranslated_chapter_contributes_nothing():
    # No recorded hash, so nothing to place. It cannot read as stale and cannot drift.
    assert _call(["x", "y", "a", "b", "c"], {1: "a", 2: "b", 3: "c"}) == {
        "count": 3, "shift": 2}


def test_it_reports_nothing_when_no_chapter_reads_as_stale():
    # The gate is stale ROWS: that is what inflates the count and makes the page offer to
    # re-translate. With nothing stale the function returns before building anything.
    tabs = _tabs(["a", "b", "c"])
    st = _state({1: "a", 2: "b", 3: "c"})
    assert index_drift(tabs, st, [{"source_changed": False}] * 3) is None
