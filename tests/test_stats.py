"""Tests for the Stats page's totals (server/stats.py and GET /api/stats).

The page is only as good as its sums, so these build a small library by hand and check
each number against it: tokens from all three places they are recorded, novels counted as
series plus loose projects, chapters posted from the ledgers, and a file that will not
parse skipped rather than moved aside.
"""

import json
import os
import sys
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server.posting as posting  # noqa: E402
import server.projects as pj  # noqa: E402
import server.series as series_mod  # noqa: E402
import server.stats as stats  # noqa: E402
import translation_bot.state as state_mod  # noqa: E402


def _write(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _usage(i, o, cw=0, cr=0):
    return {"input_tokens": i, "output_tokens": o,
            "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr}


@pytest.fixture(autouse=True)
def library(monkeypatch, tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(pj, "PROJECTS_DIR", root)
    series_mod._invalidate_index()

    # Two projects that are one novel (a series), and one loose, archived project.
    for pid, archived in (("aaaaaaaaaaaa", False), ("bbbbbbbbbbbb", False), ("cccccccccccc", True)):
        _write(root / pid / "project.json", {"id": pid, "name": pid, "archived": archived,
                                             "created_at": f"2026-0{1 + 'abc'.index(pid[0])}-01T00:00:00+00:00"})
    _write(root / "aaaaaaaaaaaa" / "state.json", {"chapters": {
        "1": {"status": "validated", "usage": _usage(100, 50, 10, 1000), "cost_usd": 0.5},
        "2": {"status": "needs-review", "usage": _usage(20, 5), "cost_usd": 0.1},
        "3": {"status": "english-source"},
    }})
    _write(root / "bbbbbbbbbbbb" / "state.json", {"chapters": {
        "1": {"status": "validated", "usage": _usage(1, 2, 3, 4), "cost_usd": 0.25},
    }})
    _write(root / "cccccccccccc" / "pages.json", {
        "pages": [{"status": "ok"}, {"status": "needs-check"}, {"status": "new"}],
        "totals": {"cost_usd": 0.05, **_usage(7, 8)},
    })
    _write(root / "cccccccccccc" / "variants" / "chapter-01.json", {"groups": [{"variants": [
        {"kind": "original", "usage": _usage(999, 999)},
        {"kind": "retranslate", "usage": _usage(3, 4), "cost_usd": 0.02},
    ]}]})
    _write(root / "cccccccccccc" / "glossary.json", [{"english": "Mira"}])

    sid = "s1"
    _write(series_mod.series_root() / sid / "series.json", {
        "id": sid, "name": "Novel", "members": [
            {"project_id": "aaaaaaaaaaaa", "start_chapter": 1},
            {"project_id": "bbbbbbbbbbbb", "start_chapter": 2}],
        "publish_targets": [{"id": "t1", "site": "meiko"}],
    })
    _write(series_mod.series_root() / sid / "glossary.json", [{"english": "Ari"}, {"english": "Bo"}])
    _write(posting.ledger_path(sid, "t1"), {"entries": [
        {"title": "Chapter 1", "status": "posted", "at": "2026-03-01T00:00:00+00:00"},
        {"title": "Chapter 2", "status": "posted", "at": "2026-03-05T00:00:00+00:00"},
        {"title": "Chapter 3", "status": "creating", "at": "2026-03-06T00:00:00+00:00"},
    ]})
    return root


def test_tokens_add_up_from_every_place_they_are_recorded():
    out = stats.compute()
    tok = out["tokens"]
    assert tok["input_tokens"] == 100 + 20 + 1 + 7 + 3
    assert tok["output_tokens"] == 50 + 5 + 2 + 8 + 4
    assert tok["cache_creation_input_tokens"] == 10 + 3
    assert tok["cache_read_input_tokens"] == 1000 + 4
    assert tok["total"] == sum(tok[k] for k in stats.TOKEN_KEYS)
    # The original text of a paragraph cost nothing; only the rewrite is counted.
    assert out["tokens"]["by_source"]["rewrites"]["input_tokens"] == 3
    assert out["cost_usd"] == round(0.5 + 0.1 + 0.25 + 0.05 + 0.02, 2)


def test_chapters_novels_posts_and_the_rest():
    out = stats.compute()
    assert out["chapters"] == {"translated": 2, "needs_review": 1, "english": 1}
    # One series (two projects) plus one loose project.
    assert out["novels"] == {"total": 2, "archived": 1, "active": 1}
    # A chapter still being created is not posted.
    assert out["posted"]["chapters"] == 2
    assert out["posted"]["novels"] == 1
    assert out["posted"]["last_at"] == "2026-03-05T00:00:00+00:00"
    assert out["pages_read"] == 2
    assert out["rewrites"] == 1
    assert out["glossary_names"] == 3
    assert out["since"] == "2026-01-01T00:00:00+00:00"
    assert out["unreadable"] == 0
    # The fixture's chapters carry no dates at all: counted, but in no month.
    assert out["monthly"] == {"months": [], "undated": 2}


def test_chapters_are_counted_in_the_month_they_were_first_translated(library):
    noon = lambda day: f"{day}T12:00:00+00:00"  # midday, so no time zone moves the month
    _write(library / "aaaaaaaaaaaa" / "state.json", {"chapters": {
        # Translated in June and edited in September: it stays in June.
        "1": {"status": "validated", "translated_at": noon("2026-06-10"),
              "created_at": noon("2026-06-10"), "updated_at": noon("2026-09-01")},
        # From before the stamp existed: its created_at stands in.
        "2": {"status": "validated", "created_at": noon("2026-06-20"), "updated_at": noon("2026-08-01")},
        # Still waiting for review: not done, so not counted anywhere.
        "3": {"status": "needs-review", "created_at": noon("2026-07-01")},
        "4": {"status": "validated", "created_at": noon("2026-08-15")},
    }})
    _write(library / "bbbbbbbbbbbb" / "state.json", {"chapters": {
        "1": {"status": "validated", "created_at": noon("2026-06-15")},
    }})
    out = stats.compute()
    rows = {r["month"]: r for r in out["monthly"]["months"]}
    # Two documents of the same series are one novel.
    assert rows["2026-06"] == {"month": "2026-06", "chapters": 3, "novels": 1}
    assert rows["2026-07"] == {"month": "2026-07", "chapters": 0, "novels": 0}
    assert rows["2026-08"]["chapters"] == 1
    assert "2026-09" in rows and rows["2026-09"]["chapters"] == 0
    # The months add up to the headline count, and run without gaps up to this month.
    assert sum(r["chapters"] for r in out["monthly"]["months"]) == out["chapters"]["translated"]
    months = [r["month"] for r in out["monthly"]["months"]]
    assert months[0] == "2026-06"
    assert months[-1] == stats._month(datetime.now().astimezone().isoformat())
    assert out["monthly"]["undated"] == 0


def test_months_run_on_across_a_new_year():
    rows = stats.monthly(Counter({"2026-11": 2, "2027-02": 1}), {"2026-11": {"x", "y"}}, "2027-01")
    assert [(r["month"], r["chapters"], r["novels"]) for r in rows] == [
        ("2026-11", 2, 2), ("2026-12", 0, 0), ("2027-01", 0, 0), ("2027-02", 1, 0)]
    assert stats.monthly(Counter(), {}, "2026-11") == []


# --- the date a chapter was translated (translation_bot/state.py)

@pytest.fixture
def clock(monkeypatch):
    """A clock that says whatever the test last set it to."""
    now = {"t": "2026-11-01T00:00:00+00:00"}
    monkeypatch.setattr(state_mod, "_now", lambda: now["t"])
    return now


def test_the_first_translation_is_stamped_and_later_work_leaves_it(clock):
    st = state_mod.State()
    st.update(1, status="pending")
    assert "translated_at" not in st.get(1)
    clock["t"] = "2026-11-02T00:00:00+00:00"
    st.update(1, status="needs-review")
    assert st.get(1)["translated_at"] == "2026-11-02T00:00:00+00:00"
    # Approved, re-translated and edited later: still the day Claude first translated it.
    for later, fields in (("2026-11-05", {"status": "validated"}),
                          ("2026-12-01", {"status": "validated"}),
                          ("2026-12-02", {"manual_edit": True})):
        clock["t"] = f"{later}T00:00:00+00:00"
        st.update(1, **fields)
    assert st.get(1)["translated_at"] == "2026-11-02T00:00:00+00:00"
    assert st.get(1)["updated_at"] == "2026-12-02T00:00:00+00:00"


def test_an_old_chapter_keeps_its_month_when_touched_again(clock):
    st = state_mod.State({"chapters": {"1": {"status": "validated",
                                             "created_at": "2026-06-10T00:00:00+00:00"}}})
    clock["t"] = "2026-11-20T00:00:00+00:00"
    st.update(1, status="validated")  # e.g. re-translated today
    assert st.get(1)["translated_at"] == "2026-06-10T00:00:00+00:00"


def test_an_unreadable_file_is_skipped_and_left_where_it_is(library):
    bad = library / "bbbbbbbbbbbb" / "state.json"
    bad.write_text("{ not json", encoding="utf-8")
    out = stats.compute()
    assert out["unreadable"] == 1
    assert out["chapters"]["translated"] == 1
    assert bad.exists() and bad.read_text(encoding="utf-8") == "{ not json"


def test_app_updates_say_unknown_without_git(monkeypatch):
    monkeypatch.setattr(stats, "_git", lambda *a: None)
    assert stats.app_updates() == {"count": None, "since": None}


def test_the_endpoint_serves_the_same_numbers():
    import server.app as A

    # client= matters: the default host "testclient" is not loopback, so the remote-access
    # gate would answer 403 (the same reason test_importing.py passes it).
    client = TestClient(A.app, base_url="http://localhost", client=("127.0.0.1", 50000))
    res = client.get("/api/stats")
    assert res.status_code == 200
    body = res.json()
    assert body["tokens"]["total"] == stats.compute()["tokens"]["total"]
    assert body["novels"]["total"] == 2


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
