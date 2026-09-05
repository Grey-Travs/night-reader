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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
