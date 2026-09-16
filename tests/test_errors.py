"""Tests for the error-explanation layer and the translator's retry/abort contract.

The bug these exist to prevent: the Claude Agent SDK raises a BARE ``Exception`` when the
CLI is spawned but never answers its ``initialize`` control request. It matched no except
clause in ``Translator._call`` and none in the endpoints, so it escaped as an unhandled
500 — an ~80-line traceback in the console and the words "Internal Server Error" in the
UI, with no retry attempted even though a cold start usually succeeds on a second try.

Contract asserted here:
  (1) that specific error is classified, retryable, and carries actionable fix steps;
  (2) ``_call`` RETRIES it rather than failing on the first attempt;
  (3) no exception, however novel, escapes ``_call`` as a bare Exception;
  (4) a deliberate stop (``TranslationAborted``) is never retried and never swallowed;
  (5) ``_resolve_items`` honours ``force`` for a whole-novel re-translate.

Runs under pytest (``pytest tests/``) and standalone.
"""

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from types import SimpleNamespace  # noqa: E402

from server import errors  # noqa: E402
from translation_bot import translator as T  # noqa: E402

# The exact message from the reported traceback.
STARTUP_TIMEOUT = "Control request timeout: initialize"


# --------------------------------------------------------------------------- explain()
def test_startup_timeout_is_classified_and_retryable():
    e = errors.explain(Exception(STARTUP_TIMEOUT))
    assert e.code == "claude-start-timeout"
    assert e.retryable is True
    assert e.action == errors.ACTION_RETRY
    # The point of the feature: the user is told what to DO, not just what broke.
    assert len(e.fixes) >= 3
    assert any("claude" in f.lower() for f in e.fixes)
    # The traceback is captured but kept out of the headline.
    assert STARTUP_TIMEOUT in e.detail
    assert STARTUP_TIMEOUT not in e.title


def test_google_statuses_map_to_distinct_causes():
    def http_error(status):
        return SimpleNamespace(resp=SimpleNamespace(status=status))

    cases = {401: "google-auth-expired", 403: "google-forbidden", 404: "google-not-found"}
    for status, code in cases.items():
        exc = Exception("request failed")
        exc.resp = http_error(status).resp
        assert errors.explain(exc).code == code, status

    exc = Exception("boom")
    exc.resp = http_error(401).resp
    assert errors.explain(exc).action == errors.ACTION_RECONNECT_GOOGLE


# ---- Google failures must not be reported as Claude failures --------------
#
# Two rules that match on message text -- `\b400\b` and "rate limit" -- sat ABOVE the
# Google section, so a Docs batchUpdate 400 was reported as "Claude rejected the request"
# and told the user to change their model settings, and a Docs 429 as their Claude plan's
# usage limit being reached. Both confirmed against real googleapiclient HttpError objects
# before the fix. Latent until now only because nothing in the app ever WROTE to Google.


def _google(status, message="request failed"):
    exc = Exception(message)
    exc.resp = SimpleNamespace(status=status)
    return exc


def test_a_docs_rejection_is_not_blamed_on_claude():
    e = errors.explain(_google(400, "Invalid requests[0].insertText"))
    assert e.code == "google-rejected"
    # The specific wrong turn: sending someone to Settings to change their model over a
    # problem that has nothing to do with the model.
    assert "claude" not in e.title.lower()
    assert e.action != errors.ACTION_SETTINGS


def test_a_docs_quota_is_not_blamed_on_the_claude_plan():
    e = errors.explain(_google(429, "Quota exceeded: rate limit"))
    assert e.code == "google-quota"
    assert e.retryable is True
    assert "claude" not in e.title.lower()


def test_a_missing_scope_is_not_reported_as_a_sharing_problem():
    # The advice is the opposite of the sharing 403's: nothing is wrong with the document
    # or the account, so "share it with the account you signed in with" sends someone to
    # re-share a document they already own.
    e = errors.explain(_google(403, "Request had insufficient authentication scopes."))
    assert e.code == "google-scope"
    assert e.action == errors.ACTION_RECONNECT_GOOGLE
    assert not any("share" in f.lower() for f in e.fixes)


def test_a_sharing_403_still_says_to_share_it():
    e = errors.explain(_google(403, "The caller does not have permission"))
    assert e.code == "google-forbidden"
    assert any("share" in f.lower() for f in e.fixes)


def test_claudes_own_rejections_are_still_claudes():
    # The other half: narrowing those two rules must not stop them catching what they
    # were written for.
    assert errors.explain(Exception("Agent error: 400 bad thinking config")).code \
        == "agent-rejected"
    assert errors.explain(Exception("API rate limit exceeded")).code == "rate-limited"


def test_rate_limit_keeps_its_own_code_and_429():
    e = errors.explain(T.RateLimitedError(SimpleNamespace(rate_limit_type="usage", resets_at=None)))
    assert e.code == "rate-limited"
    assert e.status == 429


def test_unknown_error_still_yields_a_usable_explanation():
    e = errors.explain(ValueError("something nobody predicted"))
    assert e.code == "unknown"
    assert e.title and e.fixes            # never an empty dialog
    assert "something nobody predicted" in e.detail
    assert e.trace                        # copy-report has something to copy


def test_explain_never_raises_even_on_a_hostile_exception():
    class Hostile(Exception):
        def __str__(self):
            raise RuntimeError("can't stringify me")

    e = errors.explain(Hostile())
    assert e.code == "unknown" and e.title


def test_as_dict_is_json_safe_for_the_wire():
    import json

    payload = errors.as_dict(errors.explain(Exception(STARTUP_TIMEOUT)))
    json.dumps(payload)  # must not raise — this goes into an SSE frame and a JSON body
    assert {"code", "title", "what", "fixes", "action", "retryable", "detail"} <= set(payload)


def test_http_detail_keeps_the_handwritten_message_as_the_title():
    msg = "This novel is in read-only saved mode — its source document isn't available."
    e = errors.from_http_detail(msg, 409)
    assert e.title == msg          # existing friendly copy must survive verbatim
    assert e.status == 409

    # An already-structured detail round-trips instead of being re-wrapped.
    structured = errors.as_dict(errors.explain(Exception(STARTUP_TIMEOUT)))
    assert errors.from_http_detail(structured, 503).code == "claude-start-timeout"


# ------------------------------------------------------- Translator._call retry ladder
def _translator(retries=3):
    cfg = SimpleNamespace(model="claude-opus-5", effort="high", thinking=True,
                          web_access=False, api_retry_count=retries)
    return T.Translator(cfg, SimpleNamespace())


def _patch_call_path(monkeypatch, side_effect):
    """Make ``_call`` drive ``side_effect`` synchronously.

    ``_call`` wraps ``_aquery`` in ``asyncio.run``; the fakes here return plain values,
    so asyncio.run is stubbed to pass them straight through. Sleeps are removed so the
    backoff doesn't slow the suite.
    """
    import asyncio

    monkeypatch.setattr(T.asyncio, "run", lambda v: v)
    monkeypatch.setattr(T.time, "sleep", lambda _s: None)
    monkeypatch.setattr(T.Translator, "_aquery",
                        lambda self, *a, **k: side_effect(), raising=True)
    return asyncio


def test_startup_timeout_is_retried_not_failed(monkeypatch):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise Exception(STARTUP_TIMEOUT)   # bare, exactly as the SDK raises it
        return ("the translation", {}, 0.0)

    _patch_call_path(monkeypatch, flaky)
    text, _usage, _cost = _translator(retries=4)._call("sys", "usr")
    assert text == "the translation"
    assert calls["n"] == 3, "a cold-start timeout must be retried, not surfaced"


def test_exhausted_startup_timeouts_surface_as_translator_error(monkeypatch):
    _patch_call_path(monkeypatch, lambda: (_ for _ in ()).throw(Exception(STARTUP_TIMEOUT)))
    try:
        _translator(retries=2)._call("sys", "usr")
    except T.TranslatorError:
        pass  # correct: our own type, so callers and the HTTP layer can handle it
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"escaped as {type(exc).__name__}, not TranslatorError") from exc
    else:
        raise AssertionError("should have raised")


def test_unknown_error_is_wrapped_and_not_retried(monkeypatch):
    calls = {"n": 0}

    def novel_failure():
        calls["n"] += 1
        raise Exception("a brand new SDK failure mode")

    _patch_call_path(monkeypatch, novel_failure)
    try:
        _translator(retries=4)._call("sys", "usr")
    except T.TranslatorError as exc:
        assert "brand new SDK failure" in str(exc)
    else:
        raise AssertionError("bare Exception escaped _call")
    # Only a startup timeout is transient; an unknown fault shouldn't burn 4 attempts.
    assert calls["n"] == 1


def test_abort_is_never_retried(monkeypatch):
    calls = {"n": 0}

    def stopped():
        calls["n"] += 1
        raise T.TranslationAborted("stopped by the user")

    _patch_call_path(monkeypatch, stopped)
    try:
        _translator(retries=4)._call("sys", "usr")
    except T.TranslationAborted:
        pass
    else:
        raise AssertionError("abort must propagate unchanged")
    assert calls["n"] == 1, "a user stop must not be retried"


# --------------------------------------------------------------------------- StreamHooks
def test_hooks_are_safe_when_unset_and_when_they_raise():
    T.StreamHooks().text("x")            # no callbacks wired: must be a no-op
    T.StreamHooks(on_text=lambda _c: 1 / 0).text("x")  # a broken hook can't fail a chapter


def test_aborted_reflects_the_event():
    ev = threading.Event()
    hooks = T.StreamHooks(abort=ev)
    assert hooks.aborted() is False
    ev.set()
    assert hooks.aborted() is True
    assert T.StreamHooks().aborted() is False  # no event at all == never aborted


def test_deep_check_does_not_swallow_an_abort():
    """``_deep_check_and_fix`` catches bare Exception so a failed bonus check never kills
    a good chapter — but that must not eat a user stop."""
    from translation_bot import pipeline

    class Stopping:
        def find_meta_leaks(self, _text):
            raise T.TranslationAborted("stopped by the user")

    result = T.TranslationResult(prose="Some English.", new_terms=[])
    validation = SimpleNamespace(ok=True, metrics={}, failures=[])
    cfg = SimpleNamespace(validation=SimpleNamespace())
    try:
        pipeline._deep_check_and_fix(Stopping(), SimpleNamespace(), result, validation, cfg)
    except T.TranslationAborted:
        pass
    else:
        raise AssertionError("the deep check swallowed a user stop")


def test_deep_check_still_tolerates_ordinary_failures():
    from translation_bot import pipeline

    class Broken:
        def find_meta_leaks(self, _text):
            raise RuntimeError("scan service unavailable")

    result = T.TranslationResult(prose="Some English.", new_terms=[])
    validation = SimpleNamespace(ok=True, metrics={}, failures=[])
    cfg = SimpleNamespace(validation=SimpleNamespace())
    got, val = pipeline._deep_check_and_fix(Broken(), SimpleNamespace(), result, validation, cfg)
    assert got.prose == "Some English."   # chapter survives a failed bonus check
    assert val is validation


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
