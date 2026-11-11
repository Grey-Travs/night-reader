"""The Telegram bot: pages sent in a chat land in the novel as a chapter, in order.

What these guard:

- **Only the paired person gets in.** Pairing takes the code the computer shows, from a
  private chat, with a few tries and a time limit; once paired, anyone else is ignored
  without a word, and nothing they send is fetched.
- **Nothing is taken in twice or out of order.** Where the messages have got to is saved
  after each one, so a restart carries on there. A message taken in again after a crash
  adds no second page and no second chapter. An album lands in the order it was sent.
  A page that can't be fetched holds back the ones after it, and is given up on (with a
  message) only after a few tries.
- **A chapter is the chat's to finish.** Its box appears with its first page, /done
  finishes it, a chapter deleted on the computer is closed rather than quietly remade.
- **The token never shows** in an error, and notices reach the chat only when paired
  and wanted.

Telegram is a fake here; the novel is a real one in a temporary library.

Runs under pytest (``pytest tests/``).
"""

import itertools
import os
import sys
import time
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server.app as A  # noqa: E402
import server.pages as P  # noqa: E402
import server.projects as pj  # noqa: E402
from server import remote  # noqa: E402
from server import telegram_bot as T  # noqa: E402
from translation_bot.config import Config  # noqa: E402

_photos = itertools.count(1)
_updates = itertools.count(1000)
_messages = itertools.count(1)

SAM = {"id": 111, "is_bot": False, "first_name": "Sam", "username": "sam"}
SAM_CHAT = {"id": 111, "type": "private"}
STRANGER = {"id": 999, "is_bot": False, "first_name": "Someone"}
STRANGER_CHAT = {"id": 999, "type": "private"}
TOKEN = "123456789:" + "A" * 35


def _photo() -> bytes:
    return b"\xff\xd8\xff\xe0" + next(_photos).to_bytes(8, "big") + b"\x00" * 248


class FakeTelegram:
    """Telegram as the bot sees it: methods answered from tables, failures on demand."""

    def __init__(self):
        self.token = TOKEN
        self.calls = []
        self.files = {}
        self.fail = {}
        self.updates = []

    def add(self, file_id, data=None):
        self.files[file_id] = data if data is not None else _photo()
        return self.files[file_id]

    def _maybe_fail(self, method):
        queue = self.fail.get(method)
        if queue:
            exc = queue.pop(0)
            if exc is not None:
                raise exc

    def call(self, method, params=None, timeout=35.0):
        self.calls.append((method, params or {}))
        self._maybe_fail(method)
        if method == "getFile":
            return {"file_id": params["file_id"], "file_path": f"photos/{params['file_id']}.jpg"}
        if method == "getUpdates":
            return self.updates.pop(0) if self.updates else []
        if method == "getMe":
            return {"id": 42, "is_bot": True, "username": "night_reader_bot", "first_name": "NR"}
        if method == "getWebhookInfo":
            return {"url": ""}
        return True

    def download(self, file_path, limit=T.MAX_DOWNLOAD):
        self.calls.append(("download", {"file_path": file_path}))
        self._maybe_fail("download")
        return self.files[file_path.split("/")[-1].rsplit(".", 1)[0]]

    def said(self):
        return [p["text"] for m, p in self.calls if m == "sendMessage"]

    def asked(self, method):
        return [p for m, p in self.calls if m == method]


class Hooks:
    hostname = "DESKTOP-TEST"

    def __init__(self):
        self.nudged = []
        self.rows = []

    def novels(self):
        return A.scans_overview()["novels"]

    def nudge(self, pid):
        self.nudged.append(pid)

    def progress(self, pid):
        return {"stopped": False, "chapters": self.rows}


def message(text=None, *, photo=None, document=None, caption=None, group=None, user=SAM,
            chat=SAM_CHAT, mid=None, **extra):
    body = {"message_id": mid or next(_messages), "from": user, "chat": chat, "date": 0, **extra}
    if text is not None:
        body["text"] = text
    if photo:
        # Telegram sends each photo in several sizes; only the largest is downloadable here.
        body["photo"] = [{"file_id": f"{photo}-small", "width": 90, "height": 120, "file_size": 900},
                         {"file_id": photo, "width": 960, "height": 1280, "file_size": 90000},
                         {"file_id": f"{photo}-mid", "width": 320, "height": 427, "file_size": 9000}]
    if document:
        body["document"] = document
    if caption:
        body["caption"] = caption
    if group:
        body["media_group_id"] = group
    return {"update_id": next(_updates), "message": body}


def tap(data, user=SAM, chat=SAM_CHAT):
    return {"update_id": next(_updates), "callback_query": {
        "id": f"cb{next(_messages)}", "from": user, "data": data,
        "message": {"message_id": next(_messages), "chat": chat}}}


@pytest.fixture
def library(monkeypatch, tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(pj, "PROJECTS_DIR", root)
    monkeypatch.setattr(P, "PROJECTS_DIR", root)
    monkeypatch.setattr(A, "load_global_config", lambda: Config())
    A._chapter_cache.clear()
    return TestClient(A.app, base_url="http://localhost", client=("127.0.0.1", 50000))


def _novel(library, name="달빛 소설"):
    res = library.post("/api/projects/images", json={"name": name})
    assert res.status_code == 200, res.text
    return res.json()["id"]


@pytest.fixture
def pid(library):
    return _novel(library)


@pytest.fixture
def tg():
    return FakeTelegram()


@pytest.fixture
def hooks():
    return Hooks()


@pytest.fixture
def bot(tg, hooks):
    return T.Bot(tg, hooks)


def _pair(chat_id=111, user_id=111):
    def change(settings):
        settings.update(token=TOKEN, bot={"id": 42, "username": "night_reader_bot"},
                        paired={"chat_id": chat_id, "user_id": user_id, "name": "Sam",
                                "username": "sam", "at": 0})
    T.update_settings(change)


@pytest.fixture
def ready(bot, pid):
    """Paired, with the novel chosen."""
    _pair()
    bot.handle_batch([tap(f"n:{pid}")])
    return bot


def _boxes(pid):
    return P.load_pages(pid)["batches"]


def _pages_in(pid, box_id):
    return [p for p in P.load_pages(pid)["pages"] if p["batch"] == box_id]


# ---- pairing ------------------------------------------------------------------------------------

def _open_pairing(clock=time.time):
    T.update_settings(lambda s: T.open_pairing(s, clock) and None)
    return T.load_settings()["pairing"]["code"]


def test_the_code_the_computer_shows_pairs_the_chat(bot, tg):
    T.update_settings(lambda s: s.update(token=TOKEN))
    code = _open_pairing()
    bot.handle_batch([message(f"/start {code}")])
    settings = T.load_settings()
    assert settings["paired"]["chat_id"] == 111 and settings["paired"]["user_id"] == 111
    assert settings["pairing"] is None
    assert tg.asked("setMyCommands")
    assert tg.said()[-1].startswith("Paired with Night Reader on DESKTOP-TEST.")


def test_the_bare_code_pairs_too(bot):
    code = _open_pairing()
    bot.handle_batch([message(code)])
    assert T.load_settings()["paired"]["user_id"] == 111


def test_wrong_codes_close_the_window_after_five(bot, tg):
    code = _open_pairing()
    wrong = "000000" if code != "000000" else "111111"
    bot.handle_batch([message(wrong) for _ in range(T.PAIR_TRIES)])
    assert T.load_settings()["pairing"] is None
    assert tg.said()[-1] == "Too many wrong codes. Ask the computer for a new one."
    bot.handle_batch([message(code)])
    assert T.load_settings()["paired"] is None
    assert tg.said()[-1].startswith("Pairing isn't open.")


def test_an_expired_code_does_not_pair(tg, hooks):
    now = [1_000_000.0]
    bot = T.Bot(tg, hooks, clock=lambda: now[0])
    code = _open_pairing(lambda: now[0])
    now[0] += T.PAIR_MINUTES * 60 + 1
    bot.handle_batch([message(code)])
    assert T.load_settings()["paired"] is None


def test_a_group_chat_cannot_pair_and_hears_nothing(bot, tg):
    code = _open_pairing()
    bot.handle_batch([message(code, chat={"id": -5, "type": "group"})])
    assert T.load_settings()["paired"] is None
    assert tg.said() == []


def test_words_that_are_not_a_code_are_not_a_try(bot, tg):
    _open_pairing()
    bot.handle_batch([message("안녕하세요"), message("/start")])
    assert T.load_settings()["pairing"]["attempts"] == 0
    assert all(text.startswith("To pair, send the 6-digit code") for text in tg.said())


def test_once_paired_someone_else_is_ignored_without_a_word(bot, tg, pid):
    _pair()
    tg.add("theirs")
    bot.handle_batch([message("123456", user=STRANGER, chat=STRANGER_CHAT),
                      message(photo="theirs", user=STRANGER, chat=STRANGER_CHAT),
                      tap(f"n:{pid}", user=STRANGER, chat=STRANGER_CHAT)])
    assert tg.said() == []
    assert tg.asked("getFile") == [] and tg.asked("answerCallbackQuery") == []
    assert T.load_settings()["paired"]["user_id"] == 111
    assert T.load_state()["offset"] > 0   # still taken in, so never asked about again


def test_the_paired_person_in_another_chat_is_ignored(ready, tg):
    tg.calls.clear()
    tg.add("p1")
    ready.handle_batch([message(photo="p1", chat={"id": -7, "type": "group"})])
    assert tg.asked("getFile") == []


# ---- choosing the novel and chapters -------------------------------------------------------------

def test_novel_offers_the_photographed_novels_with_their_next_chapter(bot, tg, library, pid):
    _pair()
    other = _novel(library, "Second")
    bot.handle_batch([message("/novel")])
    buttons = tg.asked("sendMessage")[-1]["reply_markup"]["inline_keyboard"]
    offered = {row[0]["callback_data"]: row[0]["text"] for row in buttons}
    assert offered[f"n:{pid}"] == "달빛 소설 — next: Chapter 1"
    assert f"n:{other}" in offered
    bot.handle_batch([tap(f"n:{pid}")])
    assert T.load_state()["session"] == {"pid": pid, "chapter": None}
    assert tg.asked("answerCallbackQuery")
    assert tg.said()[-1] == "Sending to 달빛 소설. Next is Chapter 1: send its pages, then /done."


def test_a_photo_with_no_chapter_open_starts_one(ready, tg, hooks, pid):
    tg.add("p1")
    ready.handle_batch([message(photo="p1")])
    [box] = _boxes(pid)
    assert box["origin"].startswith("tg:") and box["hands_free"] is True
    assert P.is_capturing(box)
    assert [p["name"] for p in _pages_in(pid, box["id"])][0].startswith("telegram-")
    assert hooks.nudged == [pid]
    assert tg.said()[-1].startswith("Started Chapter 1 — 1 page so far. Send the rest, then /done.")
    assert T.TIP in tg.said()[-1]   # a compressed photo: the tip, once


def test_the_largest_size_of_a_photo_is_the_one_fetched(ready, tg):
    tg.add("p1")
    ready.handle_batch([message(photo="p1")])
    assert [p["file_id"] for p in tg.asked("getFile")] == ["p1"]


def test_chapter_makes_no_box_until_its_first_page(ready, tg, pid):
    ready.handle_batch([message("/chapter 프롤로그")])
    assert _boxes(pid) == []
    assert tg.said()[-1] == "Chapter started: 프롤로그. Send its pages in order, then /done."
    tg.add("p1")
    ready.handle_batch([message(photo="p1")])
    [box] = _boxes(pid)
    assert box["label"] == "프롤로그"


def test_done_finishes_the_chapter_so_it_may_build(ready, tg, hooks, pid):
    for name in ("p1", "p2"):
        tg.add(name)
    ready.handle_batch([message(photo="p1"), message(photo="p2")])
    hooks.nudged.clear()
    ready.handle_batch([message("/done")])
    [box] = _boxes(pid)
    assert not P.is_capturing(box)
    assert hooks.nudged == [pid]
    assert tg.said()[-1] == f"Chapter 1 — 2 pages. {T.FINISHED}"
    assert T.load_state()["session"]["chapter"] is None


def test_done_says_when_hands_free_is_stopped(ready, tg, pid):
    tg.add("p1")
    ready.handle_batch([message(photo="p1")])
    with P.mutate_pages(pid) as doc:
        doc["hands_free_paused_at"] = "2026-10-02T09:00:00Z"
    ready.handle_batch([message("/done")])
    assert tg.said()[-1] == f"Chapter 1 — 1 page. {T.STOPPED}"


def test_done_with_nothing_sent_finishes_nothing(ready, tg, pid):
    ready.handle_batch([message("/chapter"), message("/done")])
    assert _boxes(pid) == []
    assert tg.said()[-1] == "Nothing was sent to that chapter, so there was nothing to finish."
    ready.handle_batch([message("/done")])
    assert tg.said()[-1] == "No chapter is open. Send its pages to start one, or /chapter."


def test_a_new_chapter_finishes_the_one_open(ready, tg, pid):
    tg.add("p1")
    tg.add("p2")
    ready.handle_batch([message(photo="p1"), message("/chapter"), message(photo="p2")])
    first, second = _boxes(pid)
    assert not P.is_capturing(first) and P.is_capturing(second)
    assert [len(_pages_in(pid, b["id"])) for b in (first, second)] == [1, 1]
    assert f"Chapter 1 — 1 page. {T.FINISHED}" in tg.said()


def test_choosing_another_novel_finishes_the_chapter_open(ready, tg, library, pid):
    other = _novel(library, "Second")
    tg.add("p1")
    ready.handle_batch([message(photo="p1"), tap(f"n:{other}")])
    [box] = _boxes(pid)
    assert not P.is_capturing(box)
    assert T.load_state()["session"] == {"pid": other, "chapter": None}


def test_choosing_the_same_novel_again_keeps_the_chapter_open(ready, tg, pid):
    tg.add("p1")
    ready.handle_batch([message(photo="p1"), tap(f"n:{pid}")])
    [box] = _boxes(pid)
    assert P.is_capturing(box)
    assert T.load_state()["session"]["chapter"]["box"] == box["id"]


def test_a_chapter_deleted_on_the_computer_is_closed_not_remade(ready, tg, pid):
    tg.add("p1")
    tg.add("p2")
    ready.handle_batch([message(photo="p1")])
    [box] = _boxes(pid)
    with P.mutate_pages(pid) as doc:
        P.delete_box(doc, box["id"])
    ready.handle_batch([message(photo="p2")])
    assert _boxes(pid) == []
    assert tg.said()[-1].startswith("That chapter was deleted on the computer, so it is closed.")
    assert T.load_state()["session"]["chapter"] is None


def test_a_novel_deleted_on_the_computer_is_let_go_of(ready, tg, library, pid):
    tg.add("p1")
    assert library.delete(f"/api/projects/{pid}").status_code == 200
    ready.handle_batch([message(photo="p1")])
    assert tg.asked("getFile") == []
    assert tg.said()[-1].startswith("That novel isn't there any more.")
    assert not (pj.PROJECTS_DIR / pid).exists()
    assert T.load_state()["session"] == {"pid": None, "chapter": None}


def test_photos_before_a_novel_is_chosen_are_turned_away(bot, tg, pid):
    _pair()
    tg.add("p1")
    bot.handle_batch([message(photo="p1")])
    assert tg.asked("getFile") == []
    assert tg.said()[-1] == "Choose the novel first, with /novel, then send the pages again."


def test_a_caption_chapter_starts_the_chapter_with_that_photo(ready, tg, pid):
    for name in ("p1", "p2"):
        tg.add(name)
    ready.handle_batch([message(photo="p1", caption="/chapter 2화", group="g1"),
                        message(photo="p2", group="g1")])
    [box] = _boxes(pid)
    assert box["label"] == "2화"
    assert len(_pages_in(pid, box["id"])) == 2


def test_a_caption_done_is_not_followed(ready, tg, pid):
    for name in ("p1", "p2"):
        tg.add(name)
    ready.handle_batch([message(photo="p1", caption="/done", group="g1"),
                        message(photo="p2", group="g1")])
    [box] = _boxes(pid)
    assert P.is_capturing(box) and len(_pages_in(pid, box["id"])) == 2
    assert "Send /done as a message of its own, after the pages." in tg.said()


def test_status_says_where_the_chapters_stand(ready, tg, hooks):
    hooks.rows = [{"number": 1, "step": "reading", "message": "Reading the photos."}]
    ready.handle_batch([message("/status")])
    assert tg.said()[-1] == "Sending to 달빛 소설.\nChapter 1: Reading the photos."


# ---- order and delivery -----------------------------------------------------------------------------

def test_an_album_lands_in_the_order_it_was_sent(ready, tg, pid):
    for name in ("a", "b", "c"):
        tg.add(name)
    # Numbered by Telegram out of the order they were sent in.
    batch = [message(photo="c", group="alb", mid=503), message(photo="a", group="alb", mid=501),
             message(photo="b", group="alb", mid=502), message("/done")]
    ready.handle_batch(batch)
    [box] = _boxes(pid)
    sent = {tg.files[n]: n for n in ("a", "b", "c")}
    order = [sent[(P.pages_dir(pid) / p["file"]).read_bytes()] for p in _pages_in(pid, box["id"])]
    assert order == ["a", "b", "c"]
    assert not P.is_capturing(box)   # /done came after the album, and stayed after it


def test_runs_keep_commands_in_place():
    first = message("/chapter")
    album = [message(photo="x", group="g", mid=12), message(photo="y", group="g", mid=11)]
    last = message("/done")
    runs = T._runs([last, album[0], first, album[1]])
    assert [[u["update_id"] for u in run] for run in runs] == [
        [first["update_id"]], [album[1]["update_id"], album[0]["update_id"]], [last["update_id"]]]


def test_where_it_got_to_is_saved_after_each_message(ready, tg, pid):
    tg.add("p1")
    tg.add("p2")
    batch = [message(photo="p1"), message(photo="p2")]
    ready.handle_batch(batch)
    assert T.load_state()["offset"] == batch[-1]["update_id"] + 1
    # Delivered again (Telegram does, until told otherwise): nothing more happens.
    tg.calls.clear()
    ready.handle_batch(batch)
    assert tg.asked("getFile") == [] and len(_pages_in(pid, _boxes(pid)[0]["id"])) == 2


def test_a_restart_carries_on_where_the_last_run_stopped(ready, tg, hooks, pid):
    tg.add("p1")
    ready.handle_batch([message(photo="p1")])
    offset = T.load_state()["offset"]
    tg.updates = [[]]
    T.Bot(tg, hooks).poll_once()
    assert tg.asked("getUpdates")[-1]["offset"] == offset


def test_taken_in_again_after_a_crash_adds_no_second_page_or_chapter(ready, tg, pid):
    tg.add("p1")
    tg.add("p2")
    before = T.load_state()
    batch = [message(photo="p1"), message(photo="p2")]
    ready.handle_batch(batch)
    # The crash: the pages were saved, but where the messages had got to was not.
    state = T.load_state()
    state["offset"] = before["offset"]
    T.save_state(state)
    ready.handle_batch(batch)
    [box] = _boxes(pid)
    assert len(_pages_in(pid, box["id"])) == 2
    assert "2 photos were in the novel already, so left where they were." in tg.said()[-1]


def test_a_chapter_whose_box_was_not_noted_before_a_crash_finds_it_again(ready, tg, pid):
    tg.add("p1")
    batch = [message(photo="p1")]
    ready.handle_batch(batch)
    # The crash: the page and its new box were saved, but not which box was the chapter's.
    state = T.load_state()
    state["session"]["chapter"]["box"] = None
    state["offset"] = batch[0]["update_id"]
    T.save_state(state)
    ready.handle_batch(batch)
    ready.handle_batch([message("/done")])
    [box] = _boxes(pid)
    assert not P.is_capturing(box)
    assert tg.said()[-1] == f"Chapter 1 — 1 page. {T.FINISHED}"


def test_a_photo_whose_message_failed_after_it_was_saved_stays_in_its_chapter(ready, tg, hooks, pid):
    tg.add("p1")
    batch = [message(photo="p1")]
    real_nudge = hooks.nudge

    def crash(_pid):
        hooks.nudge = real_nudge
        raise RuntimeError("the app went down here")

    hooks.nudge = crash
    with pytest.raises(RuntimeError):
        ready.handle_batch(batch)
    ready.handle_batch(batch)
    ready.handle_batch([message("/done")])
    [box] = _boxes(pid)
    assert not P.is_capturing(box)
    assert tg.said()[-1] == f"Chapter 1 — 1 page. {T.FINISHED}"


@pytest.mark.parametrize("failure", [T.Unreachable("down"), T.TelegramError(502, "Bad Gateway"),
                                     T.TelegramError(429, "Too Many Requests", 3)])
def test_a_page_that_cannot_be_fetched_holds_back_the_ones_after_it(ready, tg, pid, failure):
    for name in ("p1", "p2", "p3"):
        tg.add(name)
    tg.fail["download"] = [None, failure]
    batch = [message(photo="p1"), message(photo="p2"), message(photo="p3")]
    handled, stuck = ready.handle_batch(batch)
    assert (handled, stuck) == (1, True)
    [box] = _boxes(pid)
    assert len(_pages_in(pid, box["id"])) == 1
    assert T.load_state()["offset"] == batch[1]["update_id"]
    # Next time round it goes, and the one after it follows.
    ready.handle_batch(batch)
    names = [p["name"] for p in _pages_in(pid, box["id"])]
    assert names == [f"telegram-{u['message']['message_id']}.jpg" for u in batch]


def test_a_page_that_keeps_failing_is_left_out_with_a_message(ready, tg, pid):
    tg.add("p1")
    tg.add("p2")
    tg.fail["download"] = [T.Unreachable("down")] * T.RETRIES
    batch = [message(photo="p1"), message(photo="p2")]
    for _ in range(T.RETRIES - 1):
        assert ready.handle_batch(batch) == (0, True)
    assert ready.handle_batch(batch) == (2, False)
    assert "A page couldn't be fetched from Telegram, so it was left out. Send it again." in tg.said()
    [box] = _boxes(pid)
    assert len(_pages_in(pid, box["id"])) == 1
    assert T.load_state()["retries"] == {}


def test_poll_once_says_to_try_again_when_a_page_waits(ready, tg):
    tg.add("p1")
    tg.fail["download"] = [T.Unreachable("down")]
    tg.updates = [[message(photo="p1")]]
    with pytest.raises(T.Unreachable):
        ready.poll_once()


def test_an_album_retried_part_way_takes_in_only_the_rest(ready, tg, pid):
    for name in ("a", "b", "c"):
        tg.add(name)
    tg.fail["download"] = [None, T.Unreachable("down")]
    batch = [message(photo=n, group="alb", caption="/chapter 앨범" if n == "a" else None)
             for n in ("a", "b", "c")]
    ready.handle_batch(batch)
    ready.handle_batch(batch)
    boxes = _boxes(pid)
    assert len(boxes) == 1   # the caption's /chapter ran once, not again on the retry
    assert len(_pages_in(pid, boxes[0]["id"])) == 3


def test_back_after_a_day_away_says_what_may_be_lost(ready, tg, hooks):
    state = T.load_state()
    state["last_poll_at"] = time.time() - 25 * 3600
    T.save_state(state)
    tg.updates = [[]]
    T.Bot(tg, hooks).poll_once()
    assert tg.said()[-1].startswith("Night Reader is back.")


# ---- what can be sent -------------------------------------------------------------------------------

def test_a_file_over_twenty_megabytes_is_refused_before_fetching(ready, tg):
    ready.handle_batch([message(document={"file_id": "big", "file_name": "page.jpg",
                                          "mime_type": "image/jpeg",
                                          "file_size": T.MAX_DOWNLOAD + 1})])
    assert tg.asked("getFile") == []
    assert tg.said()[-1] == T.TOO_BIG


def test_a_heic_file_gets_advice_without_fetching(ready, tg):
    ready.handle_batch([message(document={"file_id": "h", "file_name": "IMG_1.HEIC",
                                          "mime_type": "image/heic", "file_size": 2000})])
    assert tg.asked("getFile") == []
    assert tg.said()[-1] == T.HEIC


def test_a_heic_file_without_its_name_is_caught_by_its_bytes(ready, tg):
    tg.add("h", b"\x00\x00\x00\x18ftypheic" + b"\x00" * 40)
    ready.handle_batch([message(document={"file_id": "h", "file_name": "page",
                                          "mime_type": "image/jpeg", "file_size": 60})])
    assert tg.said()[-1] == T.HEIC


def test_an_image_file_goes_in_uncompressed_without_the_tip(ready, tg, pid):
    tg.add("f1")
    ready.handle_batch([message(document={"file_id": "f1", "file_name": "scan-01.jpg",
                                          "mime_type": "image/jpeg", "file_size": 260})])
    [box] = _boxes(pid)
    assert [p["name"] for p in _pages_in(pid, box["id"])] == ["scan-01.jpg"]
    assert T.TIP not in tg.said()[-1]


def test_other_files_and_videos_get_help(ready, tg):
    ready.handle_batch([message(document={"file_id": "d", "file_name": "notes.docx",
                                          "mime_type": "application/msword", "file_size": 10}),
                        message(video={"file_id": "v"}), message(sticker={"file_id": "s"})])
    assert tg.asked("getFile") == []
    assert tg.said()[-3].startswith("That file isn't a photo or a PDF.")
    assert tg.said()[-2:] == [T.NOT_A_PAGE, T.NOT_A_PAGE]


def _fake_split(monkeypatch, pages):
    def split(pdf, out, most):
        files = []
        for i, data in enumerate(pages, start=1):
            path = out / f"page-{i:03d}.jpg"
            path.write_bytes(data)
            files.append(path)
        return files

    monkeypatch.setattr(T.pdf_pages, "split_in_child", split)


def _pdf(file_id="pdf1", unique="uniq1"):
    return {"file_id": file_id, "file_unique_id": unique, "file_name": "Lens scan.pdf",
            "mime_type": "application/pdf", "file_size": 5000}


def test_a_pdf_with_no_chapter_open_is_a_finished_chapter(ready, tg, hooks, pid, monkeypatch):
    _fake_split(monkeypatch, [_photo(), _photo()])
    tg.add("pdf1", b"%PDF-1.7 fake")
    ready.handle_batch([message(document=_pdf())])
    [box] = _boxes(pid)
    assert not P.is_capturing(box) and box["hands_free"] is True
    assert [p["name"] for p in _pages_in(pid, box["id"])] == ["Lens scan-001.jpg", "Lens scan-002.jpg"]
    assert tg.said()[-1] == f"Chapter 1 — 2 pages from the PDF. {T.FINISHED}"
    assert hooks.nudged == [pid]
    # The same PDF again finds its pages already in: no second chapter.
    ready.handle_batch([message(document=_pdf())])
    assert len(_boxes(pid)) == 1
    assert tg.said()[-1] == "Every page of that PDF is in the novel already."


def test_a_pdf_with_a_chapter_open_joins_it(ready, tg, pid, monkeypatch):
    _fake_split(monkeypatch, [_photo(), _photo()])
    tg.add("p1")
    tg.add("pdf1", b"%PDF-1.7 fake")
    ready.handle_batch([message(photo="p1"), message(document=_pdf())])
    [box] = _boxes(pid)
    assert P.is_capturing(box) and len(_pages_in(pid, box["id"])) == 3
    assert tg.said()[-1] == "Added 2 pages from the PDF to Chapter 1. /done when it's all in."


def test_a_pdf_that_cannot_be_split_says_why(ready, tg, monkeypatch):
    def broken(pdf, out, most):
        raise T.pdf_pages.PdfError("password", "That PDF is locked with a password.")

    monkeypatch.setattr(T.pdf_pages, "split_in_child", broken)
    tg.add("pdf1", b"%PDF-1.7 fake")
    ready.handle_batch([message(document=_pdf())])
    assert tg.said()[-1] == "That PDF is locked with a password."


# ---- running it ----------------------------------------------------------------------------------------

class _Stop:
    """Stands in for the runner's stop event: records each pause instead of sleeping."""

    def __init__(self):
        self.pauses = []
        self.stopped = False

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.pauses.append(seconds)
        return self.stopped


def test_the_poller_backs_off_waits_as_asked_and_stops_on_a_dead_token(tg, hooks):
    runner = T.Runner(TOKEN, hooks)
    runner.bot.transport = runner.transport = tg
    runner._stop = _Stop()
    tg.fail["getUpdates"] = [T.Unreachable("no network"), T.Unreachable("no network"),
                             T.TelegramError(429, "Too Many Requests", retry_after=7),
                             T.TelegramError(409, "Conflict"), None,
                             T.TelegramError(401, "Unauthorized")]
    runner._poll()   # returns by itself on the 401
    assert runner._stop.pauses == [1.0, 2.0, 7.0, 15.0]
    assert T.load_state()["error"]["kind"] == "token-rejected"


def test_a_good_poll_clears_the_error(tg, hooks):
    T.save_state({**T.STATE, "error": {"kind": "unreachable", "message": "x", "since": 0}})
    T.Bot(tg, hooks).poll_once()
    assert T.load_state()["error"] is None and T.load_state()["last_poll_at"]


def test_notices_reach_the_chat_only_when_paired_and_wanted(hooks):
    runner = T.Runner(TOKEN, hooks)
    runner.sink("Chapter 3 is translated", "")
    assert runner.outbox.empty()
    _pair()
    runner.sink("Chapter 3 is translated", "Night Reader")
    assert runner.outbox.get_nowait() == (111, "Chapter 3 is translated\n\nNight Reader")
    T.set_notify(False)
    runner.sink("Chapter 4 is translated", "")
    assert runner.outbox.empty()


def test_the_token_never_shows_in_an_error():
    def opener(request, timeout):
        raise urllib.error.URLError(f"cannot reach {request.full_url}")

    with pytest.raises(T.Unreachable) as caught:
        T.Transport(TOKEN, opener=opener).call("getMe")
    assert TOKEN not in str(caught.value) and "<token>" in str(caught.value)


def test_starting_needs_a_token_and_starting_again_replaces_the_runner(hooks):
    assert T.start(hooks) is False and not T.running()
    T.update_settings(lambda s: s.update(token=TOKEN, bot={"id": 42, "username": "b"}))
    T.save_state({**T.STATE, "bot_id": 7, "offset": 555})
    assert T.start(hooks) is True and T.running()
    first = T._runner
    assert T.load_state()["offset"] == 0   # another bot's place in its messages is dropped
    assert first.sink in remote._sinks
    assert T.start() is True
    assert first.stopped() and T._runner is not first and first.sink not in remote._sinks
    T.stop()
    assert not T.running() and remote._sinks == []


def test_settings_show_the_code_only_on_the_computer_and_never_the_token():
    T.update_settings(lambda s: s.update(token=TOKEN, bot={"id": 42, "username": "nr_bot"}))
    code = _open_pairing()
    here, away = T.status(True), T.status(False)
    assert here["pairing"]["code"] == code
    assert here["pairing"]["link"] == f"https://t.me/nr_bot?start={code}"
    assert "pairing" not in away
    assert TOKEN not in repr(here) and TOKEN not in repr(away)
