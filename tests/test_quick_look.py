"""The quick look, and what the camera's badges are built from.

- The look runs on the small model, with no effort setting and no thinking, and only for
  photos the phone app took. A reply with no answer, a failure or the plan's limit gives
  ``unknown``, which flags nothing.
- What it says is saved only onto the photo it looked at: a photo replaced or deleted
  meanwhile keeps nothing.
- It can be switched off in Settings.
- Each photo's step says what the camera shows: being read, read, Claude isn't sure and
  is double-checking, what the double-check found, or couldn't be read.

Runs under pytest (``pytest tests/``).
"""

import itertools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server.app as A  # noqa: E402
import server.pages as P  # noqa: E402
import server.projects as pj  # noqa: E402
from server import hands_free as H  # noqa: E402
from server import intake, quick_look  # noqa: E402
from translation_bot import ocr  # noqa: E402
from translation_bot.config import AnthropicConfig, Config, TranslationConfig  # noqa: E402
from translation_bot.translator import RateLimitedError, Translator  # noqa: E402

_photos = itertools.count(1)


def _photo() -> bytes:
    return b"\xff\xd8\xff\xe0" + next(_photos).to_bytes(8, "big") + b"\x00" * 248


# ---- reading the reply -----------------------------------------------------------------------------

def test_a_clean_reply_says_ok_with_nothing_else():
    look = ocr.parse_quick_look('{"verdict": "ok", "problems": ["blurry"], "note": "fine"}')
    assert (look.verdict, look.problems, look.note) == ("ok", [], "")


def test_a_retake_keeps_known_problems_and_a_short_note():
    raw = ('Here you go: {"verdict": "retake", "problems": ["cut_off", "smudge", "cut_off"], '
           '"note": "the bottom three lines are cut off ' + "x" * 100 + '"}')
    look = ocr.parse_quick_look(raw)
    assert look.verdict == "retake"
    assert look.problems == ["cut_off"]
    assert look.note.startswith("the bottom three lines are cut off") and len(look.note) == 80


@pytest.mark.parametrize("raw", ["I can't open the file.", '{"verdict": "maybe"}', "[]"])
def test_a_reply_with_no_answer_flags_nothing(raw):
    assert ocr.parse_quick_look(raw).verdict == "unknown"


# ---- the small model ---------------------------------------------------------------------------------

def test_the_small_model_gets_no_effort_setting_and_no_thinking():
    haiku = AnthropicConfig(model="claude-haiku-4-5-20251001", effort="high", thinking=False)
    options = Translator(haiku, TranslationConfig())._options("system")
    assert options.model == "haiku"
    assert options.effort is None
    assert options.thinking == {"type": "disabled"}
    opus = Translator(AnthropicConfig(), TranslationConfig())._options("system")
    assert opus.effort == "high", "everything else is unchanged"


# ---- looking -------------------------------------------------------------------------------------------

@pytest.fixture
def novel(monkeypatch, tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(pj, "PROJECTS_DIR", root)
    monkeypatch.setattr(P, "PROJECTS_DIR", root)
    monkeypatch.setattr(A, "load_global_config", lambda: Config())
    A._chapter_cache.clear()
    client = TestClient(A.app, base_url="http://localhost", client=("127.0.0.1", 50000))
    pid = client.post("/api/projects/images", json={"name": "Scanned"}).json()["id"]
    return client, pid


class Looker:
    def __init__(self, monkeypatch, reply=None, error=None):
        self.models = []
        self.reply = reply or ocr.QuickLook(verdict="retake", problems=["blurry"],
                                            note="the page is blurry", cost_usd=0.002)
        self.error = error
        monkeypatch.setattr(ocr, "quick_look_page", self.look)

    def look(self, tr, path):
        self.models.append((tr.cfg.model, tr.cfg.thinking))
        if self.error:
            raise self.error
        return self.reply


def _configure(monkeypatch, cfg=None, looked=None):
    monkeypatch.setitem(quick_look._hooks, "config", lambda pid: cfg or Config())
    monkeypatch.setitem(quick_look._hooks, "looked", looked)


def _phone_page(pid):
    return intake.add_photo(pid, _photo(), origin="phone:look0001", hands_free=True).page


def _look_of(pid, page_id):
    return P.find_page(P.load_pages(pid), page_id).get("look")


def test_a_phone_photo_is_looked_at_by_the_small_model(novel, monkeypatch):
    _client, pid = novel
    looker = Looker(monkeypatch)
    told = []
    _configure(monkeypatch, looked=told.append)
    page = _phone_page(pid)
    quick_look._look(pid, page["id"], page["sha256"])
    assert looker.models == [("claude-haiku-4-5-20251001", False)]
    look = _look_of(pid, page["id"])
    assert look["verdict"] == "retake" and look["why"] == ["blurry"]
    assert look["note"] == "the page is blurry"
    assert told == [pid], "hands-free hears of it: a doubted photo gets the double-check"
    assert P.load_pages(pid)["totals"]["cost_usd"] == pytest.approx(0.002)


def test_only_photos_the_phone_app_took_are_looked_at(novel, monkeypatch):
    _client, pid = novel
    looker = Looker(monkeypatch)
    _configure(monkeypatch)
    desk = intake.add_photo(pid, _photo()).page
    shared = intake.add_photo(pid, _photo(), origin="share:abc", hands_free=True).page
    for page in (desk, shared):
        quick_look._look(pid, page["id"], page["sha256"])
    assert looker.models == []


def test_a_photo_replaced_meanwhile_keeps_nothing(novel, monkeypatch):
    _client, pid = novel
    page = _phone_page(pid)

    class Replacing(Looker):
        def look(self, tr, path):
            intake.replace_page(pid, page["id"], _photo())
            return super().look(tr, path)

    Replacing(monkeypatch)
    _configure(monkeypatch)
    quick_look._look(pid, page["id"], page["sha256"])
    assert all("look" not in p for p in P.load_pages(pid)["pages"])


def test_switched_off_it_never_looks(novel, monkeypatch):
    _client, pid = novel
    looker = Looker(monkeypatch)
    cfg = Config()
    cfg.phone.quick_look = False
    _configure(monkeypatch, cfg=cfg)
    page = _phone_page(pid)
    quick_look._look(pid, page["id"], page["sha256"])
    assert looker.models == [] and _look_of(pid, page["id"]) is None


@pytest.mark.parametrize("error", [RateLimitedError(None), RuntimeError("boom")])
def test_a_failed_look_is_unknown(novel, monkeypatch, error):
    _client, pid = novel
    Looker(monkeypatch, error=error)
    _configure(monkeypatch)
    page = _phone_page(pid)
    quick_look._look(pid, page["id"], page["sha256"])
    assert _look_of(pid, page["id"])["verdict"] == "unknown"


def test_an_upload_from_the_phone_asks_for_a_look(novel, monkeypatch):
    client, pid = novel
    Looker(monkeypatch)
    _configure(monkeypatch)
    box = client.post(f"/api/projects/{pid}/pages/batches",
                      json={"origin": "phone:look0002", "hands_free": True}).json()["batch"]
    page = client.post(f"/api/projects/{pid}/pages", content=_photo(),
                       headers={"Content-Type": "image/jpeg"},
                       params={"batch": box["id"]}).json()["page"]
    quick_look.wait_idle()
    assert _look_of(pid, page["id"])["verdict"] == "retake"


# ---- the badges ---------------------------------------------------------------------------------------

def _rows(doc, box_id):
    return {r["n"]: r for r in H.photo_rows(doc, box_id)}


def test_each_photo_says_where_it_stands():
    doc = P.new_doc()
    box = P.create_box(doc, origin="phone:badges01", hands_free=True)
    pages = [P.add_page(doc, ext="jpg", data_len=1, digest=f"d{i}", batch=box["id"]) for i in range(7)]
    pages[1].update(status=P.STATUS_OK, text="본문")
    pages[2].update(status=P.STATUS_NEEDS_CHECK, text="본문", verify=None)
    pages[3].update(status=P.STATUS_NEEDS_CHECK, text="본문", verify={
        "at": "t", "verdict": "discrepancies",
        "issues": [{"kind": "missing", "page_says": "마지막 줄"}, {"kind": "wrong"}]})
    pages[4].update(status=P.STATUS_FAILED, auto_retried=True)
    pages[5].update(status=P.STATUS_QUEUED, prior_status=P.STATUS_NEEDS_CHECK, text="본문")
    pages[6].update(status=P.STATUS_OK, text="본문",
                    look={"verdict": "retake", "why": ["cut_off"], "note": "bottom cut off"})
    rows = _rows(doc, box["id"])
    assert rows[1]["step"] == "reading" and rows[1]["looking"] is True
    assert rows[2]["step"] == "read"
    assert rows[3]["step"] == "double-checking"
    assert rows[4]["step"] == "needs-you"
    assert rows[4]["note"] == "Some text on the photo was missed: 마지막 줄 (and 1 more)."
    assert rows[5]["step"] == "failed" and rows[5]["note"] == "Claude couldn't read this photo."
    assert rows[6]["step"] == "double-checking"
    assert rows[7]["look"] == {"verdict": "retake", "note": "bottom cut off", "why": ["cut_off"]}
    assert rows[7]["looking"] is False


def test_an_unclear_double_check_says_so():
    doc = P.new_doc()
    box = P.create_box(doc, origin="phone:badges02", hands_free=True)
    page = P.add_page(doc, ext="jpg", data_len=1, digest="d", batch=box["id"])
    page.update(status=P.STATUS_NEEDS_CHECK, text="본문", verify={"at": "t", "verdict": "unclear"})
    assert _rows(doc, box["id"])[1]["note"] == "The double-check gave no clear answer."


def test_progress_carries_the_photos_of_the_chapter_on_screen(novel, monkeypatch):
    client, pid = novel
    _configure(monkeypatch)
    monkeypatch.setitem(quick_look._hooks, "config", None)   # no looks in this one
    page = _phone_page(pid)
    res = client.get("/api/phone/progress", params={"pid": pid, "box": page["batch"]}).json()
    [novel_row] = res["novels"]
    assert [p["id"] for p in novel_row["photos"]] == [page["id"]]
    assert "photos" not in client.get("/api/phone/progress", params={"pid": pid}).json()["novels"][0]


# ---- the switch -----------------------------------------------------------------------------------------

def test_the_switch_is_saved_under_phone(novel, monkeypatch, tmp_path):
    client, _pid = novel
    path = tmp_path / "config.toml"
    path.write_text('[anthropic]\nmodel = "claude-opus-5"\n', encoding="utf-8")
    monkeypatch.setattr(A, "CONFIG_PATH", path)
    assert client.post("/api/settings", json={"quick_look": False}).status_code == 200
    assert "[phone]\nquick_look = false" in path.read_text(encoding="utf-8")
    client.post("/api/settings", json={"quick_look": True})
    text = path.read_text(encoding="utf-8")
    assert text.count("quick_look") == 1 and "quick_look = true" in text
