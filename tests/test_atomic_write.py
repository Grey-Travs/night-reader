"""Tests for the atomic write behind state.json / glossary.json / project.json.

The server writes a project's state.json from several threads of ONE process: the
translation worker (via run_in_threadpool) and every request thread that edits, accepts,
cleans or re-checks a chapter. The write is supposed to be atomic — serialize to a temp
file, then os.replace — so a concurrent reader only ever sees the old file or the new one.

That guarantee was defeated by the temp file's NAME. It was ``state.json.<pid>.tmp``: one
name per process, shared by every thread in it. Two saves at once picked the same path, so
thread A was still writing the temp file when thread B tried to os.replace it, and Windows
refuses to move a file another handle has open:

    PermissionError: [WinError 32] The process cannot access the file because it is
    being used by another process: 'state.json.12500.tmp' -> 'state.json'

The temp file is also the only copy of the new content at that instant, so the same
interleaving could truncate it instead of crashing.

Naming the temp file uniquely per CALL fixes both. These tests hammer the real write path
from many threads at once; they fail against the shared-name version.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_atomic_write.py``).
"""

import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import server.app as A  # noqa: E402
from translation_bot.atomic import atomic_write_text  # noqa: E402
from translation_bot.state import State  # noqa: E402

THREADS = 8
ROUNDS = 25


def _run_concurrently(fn, threads: int = THREADS) -> list[BaseException]:
    """Run fn(i) on `threads` threads released together, collecting what they raise.

    The barrier matters: without it the threads trickle in and the writes serialize by
    accident, so the race the test exists for never happens.
    """
    barrier = threading.Barrier(threads)
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        barrier.wait()
        try:
            fn(i)
        except BaseException as exc:  # noqa: BLE001 - the assertion is "nothing raised"
            with lock:
                errors.append(exc)

    pool = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    return errors


def test_concurrent_saves_never_collide(tmp_path):
    """The reported crash: N threads saving one state.json at the same moment.

    Against the shared temp name this raises WinError 32 within a round or two.
    """
    path = tmp_path / "state.json"
    State().save(path)

    for _ in range(ROUNDS):
        def save(i: int) -> None:
            state = State.load(path)
            state.update(i, status="translated")
            state.save(path)

        errors = _run_concurrently(save)
        assert not errors, f"a concurrent save raised: {errors[0]!r}"

        # Whatever the interleaving, the file left behind must be parseable JSON —
        # never the half-written temp content.
        json.loads(path.read_text(encoding="utf-8"))


def test_no_temp_files_are_left_behind(tmp_path):
    """A finished write leaves the directory clean — no orphaned .tmp litter."""
    path = tmp_path / "state.json"

    def save(i: int) -> None:
        State.load(path).save(path)

    for _ in range(ROUNDS):
        assert not _run_concurrently(save)

    assert list(tmp_path.glob("*.tmp")) == []


def test_a_failed_replace_cleans_up_its_temp_file(tmp_path, monkeypatch):
    """A write that fails must propagate AND not leave a temp file behind.

    The crash in the log left an orphaned state.json.12500.tmp in the project folder;
    .gitignore does not cover *.tmp, so they would otherwise accumulate in view.
    """
    path = tmp_path / "state.json"

    def boom(src, dst):
        raise PermissionError(32, "in use")

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(PermissionError):
        atomic_write_text(path, "never lands")

    assert list(tmp_path.glob("*.tmp")) == []
    assert not path.exists()  # the target was never touched


def test_replace_retries_a_transient_windows_lock(tmp_path, monkeypatch):
    """Unique temp names remove the self-inflicted collision, but the DESTINATION can
    still be held for a moment by a reader or an AV scanner. That must be ridden out,
    not surfaced as a crash."""
    path = tmp_path / "state.json"
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(32, "in use")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky)

    atomic_write_text(path, "landed")

    assert calls["n"] == 3
    assert path.read_text(encoding="utf-8") == "landed"
    assert list(tmp_path.glob("*.tmp")) == []


def test_concurrent_mutations_do_not_lose_each_other(tmp_path):
    """The quieter half of the bug: load -> mutate -> save with no mutual exclusion is a
    read-modify-write race, so the last writer wins and the others' chapters vanish.

    Through mutate_state every thread's chapter must survive — the worker finishing a
    chapter can't be erased by the user accepting a different one at the same moment.
    """
    path = tmp_path / "state.json"
    State().save(path)

    def mutate(i: int) -> None:
        with A.mutate_state(path) as state:
            state.update(i, status="validated")

    errors = _run_concurrently(mutate)
    assert not errors, f"a concurrent mutation raised: {errors[0]!r}"

    final = State.load(path)
    missing = [i for i in range(THREADS) if final.get(i) is None]
    assert not missing, f"lost updates for chapters {missing}"


# ---- an unreadable file must never be silently replaced ---------------------
# state.json, glossary.json and pages.json are all read with "a corrupt file must not
# take down the library" fallbacks that return EMPTY. The caller then mutates that
# empty value and saves it back — so a momentary read failure (an antivirus or a sync
# client holding the file for an instant, which is ordinary on Windows) permanently
# destroyed every chapter's progress / every locked term / every transcribed page.
# The bytes are copied aside first now.

def test_a_corrupt_file_is_preserved_before_it_is_treated_as_empty(tmp_path):
    from translation_bot.atomic import quarantine_unreadable

    path = tmp_path / "state.json"
    path.write_text('{"chapters": {"1": {"status": "vali', encoding="utf-8")

    kept = quarantine_unreadable(path)
    assert kept is not None and kept.exists()
    assert "status" in kept.read_text(encoding="utf-8"), "the real bytes must survive"


def test_repeated_reads_do_not_pile_up_copies(tmp_path):
    from translation_bot.atomic import quarantine_unreadable

    path = tmp_path / "glossary.json"
    path.write_text("[{broken", encoding="utf-8")
    for _ in range(5):
        quarantine_unreadable(path)
    assert len(list(tmp_path.glob("glossary.json.unreadable-*"))) == 1


def test_a_different_corruption_is_kept_separately(tmp_path):
    from translation_bot.atomic import quarantine_unreadable

    path = tmp_path / "state.json"
    path.write_text("first damage", encoding="utf-8")
    quarantine_unreadable(path)
    path.write_text("second, different damage", encoding="utf-8")
    quarantine_unreadable(path)
    assert len(list(tmp_path.glob("state.json.unreadable-*"))) == 2


def test_an_absent_or_empty_file_is_not_quarantined(tmp_path):
    from translation_bot.atomic import quarantine_unreadable

    assert quarantine_unreadable(tmp_path / "nope.json") is None
    empty = tmp_path / "empty.json"
    empty.write_text("", encoding="utf-8")
    assert quarantine_unreadable(empty) is None
    assert not list(tmp_path.glob("*.unreadable-*"))


def test_state_load_preserves_a_corrupt_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"chapters": {"7": {"status": "validated", "cost_usd": 1.2',
                    encoding="utf-8")

    loaded = State.load(path)
    assert loaded.chapters == {}, "loading still degrades so the library stays usable"

    kept = list(tmp_path.glob("state.json.unreadable-*"))
    assert kept, "the real progress must be recoverable, not overwritten by the next save"
    assert "validated" in kept[0].read_text(encoding="utf-8")


def test_glossary_load_preserves_a_corrupt_file(tmp_path):
    from translation_bot.glossary import Glossary

    path = tmp_path / "glossary.json"
    path.write_text('[{"korean": "유나", "english": "Yuna"', encoding="utf-8")

    assert Glossary.load(path).entries() == []
    kept = list(tmp_path.glob("glossary.json.unreadable-*"))
    assert kept and "Yuna" in kept[0].read_text(encoding="utf-8")


def test_pages_load_preserves_a_corrupt_manifest(tmp_path, monkeypatch):
    import server.pages as P

    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path)
    pid = "abcdef012345"
    (tmp_path / pid).mkdir()
    P.pages_file(pid).write_text('{"pages": [{"text": "그는 문을 열었다."', encoding="utf-8")

    assert P.load_pages(pid)["pages"] == []
    kept = list((tmp_path / pid).glob("pages.json.unreadable-*"))
    assert kept and "그는" in kept[0].read_text(encoding="utf-8"), \
        "every page transcribed from a photo must be recoverable"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
