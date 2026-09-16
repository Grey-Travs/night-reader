"""Tests for what the saved Google token is actually allowed to do.

The Doc export is the first thing in this app that WRITES to Google. Everything before it
read, so ``SCOPES`` has always been ``documents.readonly`` and every token on every
machine was granted exactly that. Widening the list is not enough to change it, and the
ways that fail are all quiet:

* ``from_authorized_user_file(path, SCOPES)`` ignores what the token file says and stamps
  the list it was handed onto the credentials -- so a read-only token would report itself
  as able to write, and the next refresh would persist that claim, destroying the only
  record of what was really granted;
* a cached read-only token stays perfectly VALID, so ``get_credentials`` short-circuits
  and the consent screen is never reached -- pressing Reconnect Google appears to do
  nothing at all;
* and the eventual failure is a 403, which the error layer used to report as the document
  not being shared -- sending the user to re-share a document they already own.

Runs under pytest (``pytest tests/``) and standalone.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from server import errors  # noqa: E402
from translation_bot import google_auth as ga  # noqa: E402

READ = "https://www.googleapis.com/auth/documents.readonly"
WRITE = "https://www.googleapis.com/auth/drive.file"


def _token(tmp_path, scopes, name="token.json"):
    """A signed-in token, the shape google-auth itself writes.

    The far-future ``expiry`` is load-bearing and was missing at first: google-auth
    defaults a token with no expiry to expiring NOW, so the fixture was born expired and
    every test of the short-circuit instead attempted a real network refresh.
    """
    path = tmp_path / name
    path.write_text(json.dumps({
        "token": "an-access-token",
        "refresh_token": "a-refresh-token",
        "client_id": "id",
        "client_secret": "secret",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": scopes,
        "expiry": "2099-01-01T00:00:00.000000Z",
    }), encoding="utf-8")
    return path


# ---- what the token says about itself -------------------------------------


def test_the_token_reports_the_scopes_it_was_really_granted(tmp_path):
    creds = ga._load_token(_token(tmp_path, [READ]))
    assert creds.scopes == [READ]


def test_a_read_only_token_is_not_allowed_to_write(tmp_path):
    creds = ga._load_token(_token(tmp_path, [READ]))
    assert ga.missing_scopes(creds, ga.WRITE_SCOPES) == [WRITE]


def test_a_widened_token_is_allowed_to_write(tmp_path):
    creds = ga._load_token(_token(tmp_path, [READ, WRITE]))
    assert ga.missing_scopes(creds, ga.WRITE_SCOPES) == []


def test_a_missing_token_file_is_simply_not_signed_in(tmp_path):
    assert ga._load_token(tmp_path / "nothing.json") is None


def test_a_corrupt_token_file_does_not_raise(tmp_path):
    path = tmp_path / "token.json"
    path.write_text("{ truncated", encoding="utf-8")
    assert ga._load_token(path) is None


# ---- refusing the write before anything is created ------------------------


def test_a_write_is_refused_up_front_and_says_which_permission(tmp_path):
    # Up front, rather than letting Google refuse it, so a half-built document is never
    # left in the user's Drive for them to find and clean up.
    creds = ga._load_token(_token(tmp_path, [READ]))
    with pytest.raises(ga.MissingScope) as caught:
        ga.require_scopes(creds, ga.WRITE_SCOPES)
    assert "drive.file" in str(caught.value)


def test_a_wide_enough_token_is_not_refused(tmp_path):
    creds = ga._load_token(_token(tmp_path, [READ, WRITE]))
    ga.require_scopes(creds, ga.WRITE_SCOPES)  # must not raise


def test_the_refusal_is_explained_as_a_permission_not_a_sharing_problem():
    e = errors.explain(ga.MissingScope("never granted drive.file"))
    assert e.code == "google-scope"
    assert e.action == errors.ACTION_RECONNECT_GOOGLE
    # The wrong turn it exists to prevent.
    assert not any("share" in f.lower() for f in e.fixes)


# ---- reaching the consent screen ------------------------------------------


def test_a_read_only_token_still_serves_a_read(tmp_path):
    # The ordinary path, and it must keep short-circuiting: reading a novel's chapters
    # cannot start opening browser windows.
    path = _token(tmp_path, [READ])
    assert ga.get_credentials(tmp_path / "client_secret.json", path) is not None


def test_asking_to_write_does_not_short_circuit_on_a_read_only_token(tmp_path):
    # The trap: that token is VALID, so every check passes and consent is never reached.
    # With no client secret on disk the consent path raises, which is the proof it was
    # taken at all -- a short-circuit would have returned the narrow token instead.
    path = _token(tmp_path, [READ])
    with pytest.raises(FileNotFoundError):
        ga.get_credentials(tmp_path / "client_secret.json", path,
                           scopes=ga.WRITE_SCOPES)


def test_a_token_that_can_already_write_is_reused(tmp_path):
    # The other half: once granted, the export must not ask again every time.
    path = _token(tmp_path, [READ, WRITE])
    creds = ga.get_credentials(tmp_path / "client_secret.json", path,
                               scopes=ga.WRITE_SCOPES)
    assert creds.scopes == [READ, WRITE]


def test_the_write_scope_is_the_narrowest_one_that_works():
    # drive.file reaches only files this app itself creates. `documents` or `drive` would
    # both hand over every novel's source document in the same Drive.
    assert ga.WRITE_SCOPES == [READ, WRITE]
    assert not any("auth/drive" == s.rsplit("/", 1)[-1] for s in ga.WRITE_SCOPES)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
