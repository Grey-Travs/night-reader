"""Tests for the Stats page's totals (server/stats.py and GET /api/stats).

The page is only as good as its sums, so these build a small library by hand and check
each number against it: tokens from all three places they are recorded, novels counted as
series plus loose projects, chapters posted from the ledgers, and a file that will not
parse skipped rather than moved aside.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server.posting as posting  # noqa: E402
import server.projects as pj  # noqa: E402
import server.series as series_mod  # noqa: E402
import server.stats as stats  # noqa: E402


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
