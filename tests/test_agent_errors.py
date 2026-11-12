"""What a failed agent call tells the user (translation_bot.translator.error_detail).

The bug this guards: Claude Code reports a refused or cut-off reply as a result whose
subtype is "success" and whose TEXT is the error, and the app printed the subtype --
"agent error: success" -- throwing the actual reason away.
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from translation_bot.translator import error_detail  # noqa: E402


def _result(**over):
    base = dict(subtype="success", is_error=True, result=None, errors=None,
                api_error_status=None, stop_reason=None)
    return SimpleNamespace(**{**base, **over})


def test_the_message_in_the_result_text_is_what_is_shown():
    msg = _result(result="API Error: Claude Code is unable to respond to this request.",
                  stop_reason="refusal")
    assert error_detail(msg) == ("API Error: Claude Code is unable to respond to this "
                                 "request. — stopped: refusal")


def test_a_status_code_and_error_list_come_first():
    msg = _result(api_error_status=429, errors=["rate_limit_error"], result="Rate limit reached")
    assert error_detail(msg) == "HTTP 429 — rate_limit_error — Rate limit reached"


def test_a_long_text_is_cut_short():
    detail = error_detail(_result(result="x" * 1000))
    assert len(detail) == 301 and detail.endswith("…")


def test_an_ordinary_stop_is_not_reported_as_a_reason():
    assert error_detail(_result(result="Prompt is too long", stop_reason="end_turn")) == "Prompt is too long"


def test_the_subtype_is_only_the_last_resort():
    assert error_detail(_result()) == "success"
    assert error_detail(_result(subtype="error_max_turns")) == "error_max_turns"
    assert error_detail(_result(stop_reason="refusal")) == "stopped: refusal"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
