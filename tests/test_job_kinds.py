"""Tests for the job queue carrying a task KIND.

AI resolve and the pronoun fix used to be blocking HTTP calls that created no job and
published no events, so neither was visible in the Activity view. They now ride the
same per-novel worker as translation. What has to hold:

* the queue round-trips (index, force, kind) rather than (index, force);
* `queue_state()` reports the kind, since both Activity views and /api/queue are built
  from it and would otherwise only ever be able to say "Translating";
* the one-worker-per-novel invariant is untouched, so state.json writes stay serialized.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_job_kinds.py``).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server.app as A  # noqa: E402


def _job():
    return A.Job("test-job", "p")


# ---- the queue carries what to DO, not just which chapter ------------------

def test_enqueue_round_trips_the_kind():
    job = _job()
    job.enqueue([(1, True, A.TASK_RESOLVE)])
    assert list(job.pending) == [(1, True, A.TASK_RESOLVE)]


def test_all_three_kinds_can_share_one_queue():
    job = _job()
    job.enqueue([(1, False, A.TASK_TRANSLATE),
                 (2, True, A.TASK_RESOLVE),
                 (3, True, A.TASK_PRONOUNS)])
    assert [k for _i, _f, k in job.pending] == [
        A.TASK_TRANSLATE, A.TASK_RESOLVE, A.TASK_PRONOUNS]


def test_dedup_is_still_by_chapter_index():
    """One worker per novel means one operation per chapter at a time — queuing a
    repair for a chapter already waiting to be translated must not double it up."""
    job = _job()
    assert job.enqueue([(1, False, A.TASK_TRANSLATE)]) == [1]
    assert job.enqueue([(1, True, A.TASK_PRONOUNS)]) == [], "same chapter queued twice"
    assert len(job.pending) == 1


# ---- queue_state is what both Activity views render ------------------------

def test_queue_state_reports_the_running_kind():
    job = _job()
    job.current = 3
    job.kind = A.TASK_PRONOUNS
    assert job.queue_state()["kind"] == A.TASK_PRONOUNS


def test_queue_state_reports_the_queued_kind_before_anything_starts():
    """The enqueue response and the Activity bar both read this. Reporting the default
    here labelled a just-queued pronoun fix as "Translating" until its start event
    arrived."""
    job = _job()
    job.enqueue([(3, True, A.TASK_PRONOUNS)])
    assert job.queue_state()["kind"] == A.TASK_PRONOUNS


def test_queue_state_defaults_to_translate():
    assert _job().queue_state()["kind"] == A.TASK_TRANSLATE


def test_queue_state_lists_pending_indices_not_triples():
    job = _job()
    job.enqueue([(4, True, A.TASK_PRONOUNS), (7, True, A.TASK_RESOLVE)])
    assert job.queue_state()["pending"] == [4, 7]


def test_every_kind_has_a_label_for_the_ui():
    for kind in A.TASK_KINDS:
        assert A.TASK_LABEL.get(kind), f"{kind} would render as a blank in Activity"


# ---- a plain translation is unaffected -------------------------------------

def test_translate_items_still_default_to_the_translate_kind():
    job = _job()
    job.enqueue([(1, False, A.TASK_TRANSLATE)])
    idx, force, kind = job.pending[0]
    assert (idx, force, kind) == (1, False, "translate")


def test_publish_stamps_queue_state_onto_non_terminal_events():
    job = _job()
    job.kind = A.TASK_RESOLVE
    job.enqueue([(2, True, A.TASK_RESOLVE)])
    job.publish({"type": "start", "index": 2})
    assert job.history[-1]["kind"] == A.TASK_RESOLVE


# ---- pages and chapters share the worker, not the number space --------------
# Scanned-page work rides the same Job so Stop, the live console, the Activity view
# and the rate-limit auto-resume all apply to it unchanged. But a page item's index
# is a PAGE sequence number: once a scanned novel has been built, page 5 and
# chapter 5 both exist and are different things.

def test_a_page_and_a_chapter_with_the_same_number_do_not_collide():
    """Deduping on the bare index made queueing a page silently drop a chapter."""
    job = _job()
    assert job.enqueue([(5, False, A.TASK_TRANSLATE)]) == [5]
    assert job.enqueue([(5, False, A.TASK_OCR)]) == [5], \
        "page 5 is not chapter 5 — queueing it must not be swallowed as a duplicate"
    assert len(job.pending) == 2


def test_page_work_still_dedups_against_itself():
    job = _job()
    assert job.enqueue([(5, False, A.TASK_OCR)]) == [5]
    assert job.enqueue([(5, False, A.TASK_OCR_VERIFY)]) == [], \
        "one operation per page at a time, same as chapters"
    assert len(job.pending) == 1


def test_queue_state_still_reports_plain_indices_for_pages():
    """The Activity views and /api/queue read this; namespacing is internal."""
    job = _job()
    job.enqueue([(3, False, A.TASK_OCR), (4, False, A.TASK_TRANSLATE)])
    assert job.queue_state()["pending"] == [3, 4]


def test_page_kinds_are_registered_as_page_kinds():
    for kind in (A.TASK_OCR, A.TASK_OCR_VERIFY):
        assert kind in A.PAGE_TASK_KINDS, f"{kind} would be looked up in the chapter map"
    for kind in (A.TASK_TRANSLATE, A.TASK_RESOLVE, A.TASK_PRONOUNS):
        assert kind not in A.PAGE_TASK_KINDS


def test_queue_keys_are_namespaced_by_kind():
    assert A._queue_key(5, A.TASK_TRANSLATE) != A._queue_key(5, A.TASK_OCR)
    assert A._queue_key(5, A.TASK_TRANSLATE) == A._queue_key(5, A.TASK_RESOLVE), \
        "every chapter operation shares one key so they cannot run at once"


# ---- the browser must know about every kind the server can run --------------
# There used to be four independent copies of TASK_LABEL in web/src, each with a
# comment claiming to mirror app.py, and three had never learned about scanned-page
# work. A running OCR job therefore displayed as "Translating chapter 7" in Activity,
# Project Activity and the Review inbox - where 7 was a PAGE number. They are one
# module now (web/src/tasks.js); this fails if a kind is added without it.

def _tasks_js() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), "web", "src", "tasks.js")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_the_ui_has_a_label_for_every_server_task_kind():
    source = _tasks_js()
    for kind in A.TASK_KINDS:
        assert f"'{kind}'" in source or f"{kind}:" in source, (
            f"web/src/tasks.js has no label for {kind!r} — it would render as "
            f"'Translating' and name a page as though it were a chapter")


def test_the_ui_agrees_on_which_kinds_are_page_work():
    source = _tasks_js()
    marker = source.split("PAGE_TASK_KINDS", 1)[1].split("]", 1)[0]
    for kind in A.PAGE_TASK_KINDS:
        assert kind in marker, f"tasks.js does not treat {kind!r} as page work"
    for kind in (A.TASK_TRANSLATE, A.TASK_RESOLVE, A.TASK_PRONOUNS):
        assert kind not in marker, f"tasks.js wrongly treats {kind!r} as page work"


def test_the_terminal_console_labels_page_work_too():
    import server.console as C

    for kind in A.PAGE_TASK_KINDS:
        assert kind in C._TASK_NOTE, (
            f"console._TASK_NOTE has no note for {kind!r} — an OCR run prints as an "
            f"unlabelled chapter line")


def test_the_terminal_console_styles_page_statuses():
    import server.console as C
    import server.pages as P

    # Page work publishes the same "chapter" event type, so an unstyled status falls
    # back to a grey dot and the terminal never says whether a page read well.
    for status in (P.STATUS_OK, P.STATUS_NEEDS_CHECK, P.STATUS_EDITED, P.STATUS_SKIPPED):
        assert status in C._STATUS_STYLE, f"console._STATUS_STYLE has no entry for {status!r}"


# ---- the queue is read from request THREADS while the worker mutates it -----
# /api/queue polls every few seconds and Apply checks whether a chapter is busy, both
# on threadpool threads, while the worker pops and re-queues on the event loop.
# Iterating a deque that changes size raises RuntimeError, which surfaced as
# intermittent 500s that blanked the dashboard. Every access goes through helpers now.

def test_snapshot_is_a_copy_not_a_live_view():
    job = _job()
    job.enqueue([(1, False, A.TASK_TRANSLATE), (2, False, A.TASK_TRANSLATE)])
    snap = job.snapshot_pending()
    job.take_next()
    assert len(snap) == 2, "a snapshot must not change under the caller's feet"


def test_queue_state_survives_the_queue_emptying_underneath_it():
    job = _job()
    job.enqueue([(i, False, A.TASK_TRANSLATE) for i in range(50)])
    for _ in range(50):
        job.take_next()
    assert job.queue_state()["pending"] == []


def test_take_next_returns_none_when_empty_instead_of_raising():
    assert _job().take_next() is None


def test_take_next_is_fifo_and_put_back_goes_to_the_head():
    job = _job()
    job.enqueue([(1, False, A.TASK_TRANSLATE), (2, False, A.TASK_TRANSLATE)])
    first = job.take_next()
    assert first[0] == 1
    job.put_back(first)
    assert job.take_next()[0] == 1, "an interrupted item must be retried first"


def test_drain_empties_and_returns_everything_waiting():
    job = _job()
    job.enqueue([(1, False, A.TASK_TRANSLATE), (7, False, A.TASK_OCR)])
    dropped = job.drain()
    assert [i for i, _f, _k in dropped] == [1, 7]
    assert job.queue_state()["pending"] == []


def test_concurrent_readers_never_see_a_torn_queue():
    """The actual failure: RuntimeError: deque mutated during iteration."""
    import threading

    job = _job()
    job.enqueue([(i, False, A.TASK_TRANSLATE) for i in range(200)])
    errors: list[BaseException] = []
    stop = threading.Event()

    def drain():
        try:
            while not stop.is_set() and job.take_next() is not None:
                pass
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def poll():
        try:
            while not stop.is_set():
                job.queue_state()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=drain), threading.Thread(target=poll)]
    for t in threads:
        t.start()
    threads[0].join(timeout=10)
    stop.set()
    for t in threads:
        t.join(timeout=10)
    assert not errors, f"reading the queue while it drained raised: {errors!r}"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
