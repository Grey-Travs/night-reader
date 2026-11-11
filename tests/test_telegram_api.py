"""Settings → Telegram, through the API.

The bot's token is the key to it: whoever has it can read what is sent to the bot and
speak as it. So it is set only on the computer itself, checked with Telegram before it
is kept, and never handed back — not even to the computer. Who may send pages in (the
pairing code, unpairing, removing the bot) is the computer's to decide too; a phone
using the app's link sees only whether the bot is working.

Telegram is a fake here.

Runs under pytest (``pytest tests/``).
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server.app as A  # noqa: E402
import server.pages as P  # noqa: E402
import server.projects as pj  # noqa: E402
from server import remote  # noqa: E402
from server import telegram_bot as T  # noqa: E402
from translation_bot.config import Config  # noqa: E402

HERE = ("127.0.0.1", 50000)
ELSEWHERE = ("203.0.113.5", 41234)
TOKEN = "123456789:" + "B" * 35


class Telegram:
    """Every Transport the app makes, answering as Telegram would."""

    def __init__(self):
        self.calls = []
        self.fail = {}
        self.webhook = ""

    def transport(self, token, opener=None):
        telegram = self

        class Fake:
            def __init__(self):
                self.token = token

            def call(self, method, params=None, timeout=35.0):
                telegram.calls.append((method, params or {}))
                if method in telegram.fail:
                    raise telegram.fail.pop(method)
                if method == "getMe":
                    return {"id": 42, "is_bot": True, "username": "night_reader_bot",
                            "first_name": "Night Reader"}
                if method == "getWebhookInfo":
                    return {"url": telegram.webhook}
                if method == "getUpdates":
                    time.sleep(0.2)
                    return []
                return True

            def download(self, file_path, limit=T.MAX_DOWNLOAD):
                raise AssertionError("nothing is downloaded here")

        return Fake()

    def asked(self, method):
        return [p for m, p in self.calls if m == method]


@pytest.fixture
def telegram(monkeypatch):
    fake = Telegram()
    monkeypatch.setattr(T, "Transport", fake.transport)
    return fake


@pytest.fixture
def library(monkeypatch, tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(pj, "PROJECTS_DIR", root)
    monkeypatch.setattr(P, "PROJECTS_DIR", root)
    monkeypatch.setattr(A, "load_global_config", lambda: Config())
    A._chapter_cache.clear()


@pytest.fixture
def here(library):
    return TestClient(A.app, base_url="http://localhost", client=HERE)


@pytest.fixture
def away(library, monkeypatch, tmp_path):
    """A phone using the app's link, with its key: let in, but not the computer."""
    monkeypatch.setenv("NR_REMOTE_SETTINGS", str(tmp_path / "remote.json"))
    doc = remote.load()
    doc["enabled"] = True
    doc["token"] = remote.new_token()
    remote.save(doc)
    return TestClient(A.app, base_url="http://localhost", client=ELSEWHERE,
                      headers={remote.TOKEN_HEADER: doc["token"]})


def _title(res):
    return res.json()["detail"]["title"]


def _paired():
    def change(settings):
        settings["paired"] = {"chat_id": 111, "user_id": 111, "name": "Sam",
                              "username": "sam", "at": 0}
        settings["pairing"] = None
    T.update_settings(change)


def test_nothing_is_set_up_at_first(here):
    res = here.get("/api/telegram")
    assert res.status_code == 200
    assert res.json() == {"configured": False, "bot_username": "", "paired": None,
                          "notify": True, "running": False, "last_poll_at": None,
                          "error": None, "local": True}


def test_a_token_is_checked_kept_and_never_handed_back(here, telegram):
    res = here.post("/api/telegram", json={"token": f"  {TOKEN} "})
    assert res.status_code == 200, res.text
    state = res.json()
    assert state["configured"] is True and state["bot_username"] == "night_reader_bot"
    assert state["running"] is True
    code = state["pairing"]["code"]
    assert len(code) == 6 and code.isdigit()
    assert state["pairing"]["link"] == f"https://t.me/night_reader_bot?start={code}"
    assert TOKEN not in res.text and TOKEN not in here.get("/api/telegram").text
    assert T.load_settings()["token"] == TOKEN
    assert telegram.asked("getMe") and telegram.asked("getWebhookInfo")


def test_a_token_in_the_wrong_shape_is_refused_before_asking_telegram(here, telegram):
    res = here.post("/api/telegram", json={"token": "not a token"})
    assert res.status_code == 400
    assert _title(res).startswith("That doesn't look like a bot token.")
    assert telegram.calls == []
    assert T.load_settings()["token"] == ""


def test_a_token_telegram_does_not_know_is_refused(here, telegram):
    telegram.fail["getMe"] = T.TelegramError(401, "Unauthorized")
    res = here.post("/api/telegram", json={"token": TOKEN})
    assert res.status_code == 400
    assert _title(res) == "Telegram doesn't accept that token. Copy it again from @BotFather."
    assert T.load_settings()["token"] == ""


def test_telegram_out_of_reach_says_so(here, telegram):
    telegram.fail["getMe"] = T.Unreachable("down")
    res = here.post("/api/telegram", json={"token": TOKEN})
    assert res.status_code == 502
    assert _title(res).startswith("Can't reach Telegram from this computer.")


def test_a_bot_already_connected_elsewhere_is_refused_and_left_alone(here, telegram):
    telegram.webhook = "https://example.com/hook"
    res = here.post("/api/telegram", json={"token": TOKEN})
    assert res.status_code == 409
    assert T.load_settings()["token"] == ""
    assert telegram.asked("deleteWebhook") == []


def test_from_a_phone_only_the_state_shows_and_only_notices_change(here, away, telegram):
    here.post("/api/telegram", json={"token": TOKEN})
    state = away.get("/api/telegram").json()
    assert state["configured"] is True and state["local"] is False
    assert "pairing" not in state
    assert away.post("/api/telegram", json={"token": TOKEN}).status_code == 403
    for action in ("pair", "unpair", "forget"):
        res = away.post(f"/api/telegram/{action}")
        assert res.status_code == 403, action
        assert _title(res) == "This can only be done on the computer itself."
    res = away.post("/api/telegram", json={"notify": False})
    assert res.status_code == 200 and res.json()["notify"] is False
    assert T.load_settings()["token"] == TOKEN


def test_pairing_again_needs_unpairing_first(here, telegram):
    here.post("/api/telegram", json={"token": TOKEN})
    _paired()
    state = T.load_state()
    state["session"] = {"pid": "abc", "chapter": None}
    T.save_state(state)
    res = here.post("/api/telegram/pair")
    assert res.status_code == 409
    res = here.post("/api/telegram/unpair")
    assert res.status_code == 200 and res.json()["paired"] is None
    assert T.load_state()["session"] == {"pid": None, "chapter": None}
    res = here.post("/api/telegram/pair")
    assert res.status_code == 200 and len(res.json()["pairing"]["code"]) == 6


def test_pairing_needs_a_token(here):
    res = here.post("/api/telegram/pair")
    assert res.status_code == 400
    assert _title(res) == "Save the bot's token first."


def test_the_test_message_goes_to_the_paired_chat(here, telegram):
    here.post("/api/telegram", json={"token": TOKEN})
    res = here.post("/api/telegram/test")
    assert res.status_code == 400 and _title(res) == "Pair the bot first."
    _paired()
    res = here.post("/api/telegram/test")
    assert res.status_code == 200
    assert telegram.asked("sendMessage")[-1] == {"chat_id": 111,
                                                 "text": "Night Reader can reach you here."}


def test_a_new_bot_forgets_who_was_paired_with_the_old_one(here, telegram, monkeypatch):
    here.post("/api/telegram", json={"token": TOKEN})
    _paired()
    # The same bot again keeps its pairing.
    assert here.post("/api/telegram", json={"token": TOKEN}).json()["paired"]["name"] == "Sam"
    original = telegram.transport

    def other_bot(token, opener=None):
        fake = original(token, opener)
        real_call = fake.call
        fake.call = lambda method, params=None, timeout=35.0: (
            {"id": 77, "username": "other_bot", "first_name": "Other"} if method == "getMe"
            else real_call(method, params, timeout))
        return fake

    monkeypatch.setattr(T, "Transport", other_bot)
    state = here.post("/api/telegram", json={"token": "987654321:" + "C" * 35}).json()
    assert state["paired"] is None and state["bot_username"] == "other_bot"
    assert "pairing" in state


def test_removing_the_bot_wipes_its_files_and_stops_it(here, telegram):
    here.post("/api/telegram", json={"token": TOKEN})
    T.save_state({**T.STATE, "offset": 9})
    res = here.post("/api/telegram/forget")
    assert res.status_code == 200
    assert res.json()["configured"] is False and res.json()["running"] is False
    assert not T.settings_path().exists() and not T.state_path().exists()


def test_the_app_starts_the_bot_only_when_one_is_set_up(library, telegram):
    with TestClient(A.app, base_url="http://localhost", client=HERE):
        assert not T.running()
    T.update_settings(lambda s: s.update(token=TOKEN, bot={"id": 42, "username": "b"}))
    with TestClient(A.app, base_url="http://localhost", client=HERE) as client:
        assert T.running()
        assert client.get("/api/telegram").json()["running"] is True
    assert not T.running()
