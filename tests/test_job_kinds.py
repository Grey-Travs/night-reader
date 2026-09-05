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


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
