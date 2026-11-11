"""Shared test setup.

No test may reach the model. A photo uploaded into a phone chapter asks for a quick look
(server/quick_look.py), which would otherwise call Claude for real from a background
thread — so quick looks are off in every test unless a test switches them back on with
a stub of its own (tests/test_quick_look.py does).

No test may reach Telegram either, or read the real bot's token: the bot's two files are
a test's own, and its address goes nowhere.
"""

import os
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from server import quick_look  # noqa: E402
from server import telegram_bot  # noqa: E402
from translation_bot import ocr  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_quick_looks(monkeypatch):
    monkeypatch.setitem(quick_look._hooks, "config", None)

    def refuse(*_args, **_kwargs):
        raise AssertionError("a test reached the real quick look; stub ocr.quick_look_page")

    monkeypatch.setattr(ocr, "quick_look_page", refuse)


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch, tmp_path):
    monkeypatch.setenv("NR_TELEGRAM_SETTINGS", str(tmp_path / "telegram.json"))
    monkeypatch.setenv("NR_TELEGRAM_STATE", str(tmp_path / "telegram_state.json"))

    def offline(request, timeout):
        raise urllib.error.URLError("no Telegram in tests")

    monkeypatch.setattr(telegram_bot, "_open", offline)
    monkeypatch.setattr(telegram_bot, "_hooks", telegram_bot._hooks)
    yield
    # Before the files' paths go back to the real ones, so a bot thread a test started
    # can't write there on its way out.
    telegram_bot.stop(wait=5)
