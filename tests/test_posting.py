"""Tests for planning a posting run. The clicking itself lives in the browser extension.

Everything that can be decided without a browser is decided here — which chapters go out,
what they are called on the site, what HTML they carry, what they cost, and what is held
back and why. That split is the point: the plan can be checked against a real series
before anything is published, and only the DOM work is left unverifiable.

The checks that matter most:

* the free/paid cutoff, because getting it backwards is painful to undo once readers have
  been through it;
* naming, because the site's chapter list is the duplicate guard and a resume matches
  against it by name;
* blockers, because an unfinished chapter must never reach a live page;
* and that part markers are stripped from the payload — 21 real chapters begin with a bare
  number that is invisible in the reader and would be visible once published.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_posting.py``).
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server.app as A  # noqa: E402
import server.pages as P  # noqa: E402
import server.posting as posting  # noqa: E402
import server.projects as pj  # noqa: E402
import server.series as series_mod  # noqa: E402
from translation_bot.config import Config  # noqa: E402
from translation_bot.state import STATUS_VALIDATED, State  # noqa: E402

BODY = "그는 천천히 고개를 돌려 나를 바라보았다. " * 12
ADAPTER = """
[site]
name = "meiko"
label = "meiko.studio"
url_pattern = '^https://meiko\\.studio/'
max_chars = 100000
title_template = "Chapter {n}"
side_title_template = "Side Stories {n}"
"""


@pytest.fixture
def client(monkeypatch, tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    (tmp_path / "adapters").mkdir()
    (tmp_path / "adapters" / "meiko.toml").write_text(ADAPTER, encoding="utf-8")
    for module in (pj, P):
        monkeypatch.setattr(module, "PROJECTS_DIR", root)
    monkeypatch.setattr(A, "load_global_config", lambda: Config())
    A._chapter_cache.clear()
    A._offline_projects.clear()
    series_mod._invalidate_index()
    return TestClient(A.app, base_url="http://localhost",
                      client=("127.0.0.1", 50000))


def _novel(client, name, numbers, *, translated=(), status=STATUS_VALIDATED):
    """A text novel with source tabs and, optionally, finished translations on disk."""
    pid = client.post("/api/projects/text", json={
        "name": name, "text": "seed", "split_mode": "single"}).json()["id"]
    pdir = pj.PROJECTS_DIR / pid
    records = [
        {"title": f"Tab {i}",
         "paragraphs": [f"ridibooks.com/books/{1000 + i}/view\n\n노벨 제목 {n}화", BODY]}
        for i, n in enumerate(numbers, 1)
    ]
    (pdir / "source.json").write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    project = json.loads((pdir / "project.json").read_text(encoding="utf-8"))
    project["chapter_count"] = len(records)
    (pdir / "project.json").write_text(json.dumps(project, ensure_ascii=False), encoding="utf-8")

    cfg = pj.project_config(Config(), pj.get_project(pid))
    cfg.paths.output_dir.mkdir(parents=True, exist_ok=True)
    state = State.load(cfg.paths.state_file)
    for index, text in translated:
        (cfg.paths.output_dir / f"chapter-{index:02d}.md").write_text(text, encoding="utf-8")
        state.update(index, status=status, title=f"Tab {index}", source_hash="x")
    state.save(cfg.paths.state_file)
    A._chapter_cache.pop(pid, None)
    return pid


def _series(client, name, pids, *, free_through=0, coin_price=55):
    sid = client.post("/api/series", json={"name": name, "project_ids": pids}).json()["id"]
    client.get(f"/api/series/{sid}/mapping")
    client.post(f"/api/series/{sid}", json={"publish_targets": [{
        "id": "t1", "site": "meiko", "series_url": "https://meiko.studio/page/x/series/y/",
        "free_through": free_through, "coin_price": coin_price,
    }]})
    return sid


def _plan(client, sid, **kw):
    q = {"sid": sid, **{k: v for k, v in kw.items() if v is not None}}
    return client.get("/api/posting/plan", params=q).json()


# ---- the adapter ----------------------------------------------------------


def test_the_adapter_is_served_as_data(client):
    a = client.get("/api/posting/adapter").json()
    assert a["site"]["max_chars"] == 100000
    assert a["site"]["title_template"] == "Chapter {n}"


def test_an_unknown_site_is_refused(client):
    assert client.get("/api/posting/adapter", params={"site": "nope"}).status_code == 404


def test_a_path_cannot_be_smuggled_through_the_site_name(client):
    assert client.get("/api/posting/adapter",
                      params={"site": "../../etc/passwd"}).status_code == 404


# ---- matching the site's series export ------------------------------------

SITE_CSV = """Title,Type,Status,Views,Admin URL
"Match Novel","novel","ongoing",100,"https://meiko.studio/page/p/series/aaa/"
"That Other Novel","novel","ongoing",50,"https://meiko.studio/page/p/series/bbb/"
"  Spaced Novel  ","novel","ongoing",10,"https://meiko.studio/page/p/series/ccc/"
"Some Manga","manga","ongoing",10,"https://meiko.studio/page/p/series/ddd/"
"""


def test_an_exact_title_match_is_paired(client):
    a = _novel(client, "Match Novel", [1], translated=[(1, "One.\n")])
    b = _novel(client, "Match Novel2", [2], translated=[(1, "Two.\n")])
    sid = client.post("/api/series", json={
        "name": "Match Novel", "project_ids": [a, b]}).json()["id"]
    r = client.post("/api/posting/targets/match", json={"csv": SITE_CSV}).json()
    hit = next(e for e in r["exact"] if e["sid"] == sid)
    assert hit["url"].endswith("/aaa/")


def test_leading_and_trailing_spaces_in_the_export_do_not_defeat_matching(client):
    # Several real rows in this export carry stray spaces around the title.
    a = _novel(client, "Spaced Novel", [1], translated=[(1, "One.\n")])
    b = _novel(client, "Spaced Novel2", [2], translated=[(1, "Two.\n")])
    sid = client.post("/api/series", json={
        "name": "Spaced Novel", "project_ids": [a, b]}).json()["id"]
    r = client.post("/api/posting/targets/match", json={"csv": SITE_CSV}).json()
    assert any(e["sid"] == sid and e["url"].endswith("/ccc/") for e in r["exact"])


def test_a_reworded_title_is_proposed_not_applied(client):
    # "The Other Novel" here against "That Other Novel" on the site: a person recognises
    # this instantly, and no rule should be trusted to decide it.
    a = _novel(client, "The Other Novel", [1], translated=[(1, "One.\n")])
    b = _novel(client, "The Other Novel2", [2], translated=[(1, "Two.\n")])
    sid = client.post("/api/series", json={
        "name": "The Other Novel", "project_ids": [a, b]}).json()["id"]
    r = client.post("/api/posting/targets/match", json={"csv": SITE_CSV}).json()
    assert not any(e["sid"] == sid for e in r["exact"])
    assert any(f["sid"] == sid for f in r["fuzzy"])


def test_manga_rows_are_ignored(client):
    a = _novel(client, "Some Manga", [1], translated=[(1, "One.\n")])
    b = _novel(client, "Some Manga2", [2], translated=[(1, "Two.\n")])
    client.post("/api/series", json={"name": "Some Manga", "project_ids": [a, b]})
    r = client.post("/api/posting/targets/match", json={"csv": SITE_CSV}).json()
    assert r["exact"] == [] and r["fuzzy"] == []


def test_matching_writes_nothing_until_applied(client):
    a = _novel(client, "Match Novel", [1], translated=[(1, "One.\n")])
    b = _novel(client, "Match Novel2", [2], translated=[(1, "Two.\n")])
    sid = client.post("/api/series", json={
        "name": "Match Novel", "project_ids": [a, b]}).json()["id"]
    client.post("/api/posting/targets/match", json={"csv": SITE_CSV})
    assert client.get(f"/api/series/{sid}").json()["publish_targets"] == []
    client.post("/api/posting/targets/apply", json={"assignments": [
        {"sid": sid, "url": "https://meiko.studio/page/p/series/aaa/"}]})
    targets = client.get(f"/api/series/{sid}").json()["publish_targets"]
    assert targets[0]["series_url"].endswith("/aaa/")


def test_applying_a_link_never_disturbs_the_pricing(client):
    # Resetting these to zero would quietly turn every paid chapter free on the next run.
    a = _novel(client, "Priced", [1], translated=[(1, "One.\n")])
    b = _novel(client, "Priced2", [2], translated=[(1, "Two.\n")])
    sid = _series(client, "Priced", [a, b], free_through=47, coin_price=55)
    client.post("/api/posting/targets/apply", json={"assignments": [
        {"sid": sid, "url": "https://meiko.studio/page/p/series/zzz/"}]})
    t = client.get(f"/api/series/{sid}").json()["publish_targets"][0]
    assert t["series_url"].endswith("/zzz/")
    assert t["free_through"] == 47 and t["coin_price"] == 55


# ---- which series a page belongs to ---------------------------------------
#
# The extension knows its own URL and nothing else, so this is how it works out what it is
# standing in front of, rather than the user copying a series id into the browser.


def test_a_series_is_found_from_its_site_url(client):
    a = _novel(client, "Match", [1], translated=[(1, "One.\n")])
    sid = _series(client, "Match", [a], free_through=47, coin_price=55)
    m = client.get("/api/posting/match", params={
        "url": "https://meiko.studio/page/x/series/y/"}).json()
    assert m["found"] and m["sid"] == sid
    assert m["free_through"] == 47 and m["coin_price"] == 55
    assert m["resolved"] is True


def test_a_chapter_page_below_the_series_url_still_matches(client):
    # The extension is often on a chapter's own page, not the series landing page.
    a = _novel(client, "Below", [1], translated=[(1, "One.\n")])
    sid = _series(client, "Below", [a])
    m = client.get("/api/posting/match", params={
        "url": "https://meiko.studio/page/x/series/y/chapter/abc?tab=details"}).json()
    assert m["found"] and m["sid"] == sid


def test_an_unrelated_page_matches_nothing(client):
    a = _novel(client, "Unrelated", [1], translated=[(1, "One.\n")])
    _series(client, "Unrelated", [a])
    m = client.get("/api/posting/match", params={
        "url": "https://meiko.studio/page/x/series/SOMETHINGELSE/"}).json()
    assert m["found"] is False


# ---- what would be posted -------------------------------------------------


def test_planning_needs_the_numbering_resolved_first(client):
    a = _novel(client, "Plan", [1, 2])
    b = _novel(client, "Plan2", [3, 4])
    sid = client.post("/api/series", json={"name": "Plan", "project_ids": [a, b]}).json()["id"]
    client.post(f"/api/series/{sid}", json={"publish_targets": [{"id": "t1"}]})
    r = client.get("/api/posting/plan", params={"sid": sid})
    assert r.status_code == 400
    assert "numbering" in r.json()["detail"]["title"].lower()


def test_planning_needs_a_target(client):
    a = _novel(client, "NoTarget", [1, 2])
    b = _novel(client, "NoTarget2", [3, 4])
    sid = client.post("/api/series", json={"name": "NT", "project_ids": [a, b]}).json()["id"]
    client.get(f"/api/series/{sid}/mapping")
    assert client.get("/api/posting/plan", params={"sid": sid}).status_code == 400


def test_chapters_are_named_the_way_the_site_names_them(client):
    a = _novel(client, "Named", [1, 2], translated=[(1, "One.\n"), (2, "Two.\n")])
    b = _novel(client, "Named2", [3], translated=[(1, "Three.\n")])
    sid = _series(client, "Named", [a, b])
    titles = [i["title"] for i in _plan(client, sid)["items"]]
    assert titles == ["Chapter 1", "Chapter 2", "Chapter 3"]


def test_a_side_story_run_is_numbered_from_one(client):
    # The walkthrough's own convention: "Side Stories 1", restarting within its run,
    # rather than carrying the chapter numbering.
    a = _novel(client, "Sides", [1, 2, 3],
               translated=[(1, "One.\n"), (2, "Two.\n"), (3, "Three.\n")])
    sid = _series(client, "Sides", [a])
    client.put(f"/api/series/{sid}/mapping", json={"overrides": [
        {"project_id": a, "index": 2, "global": None, "kind": "side"},
        {"project_id": a, "index": 3, "global": None, "kind": "side"},
    ]})
    titles = [i["title"] for i in _plan(client, sid)["items"]]
    assert titles == ["Chapter 1", "Side Stories 1", "Side Stories 2"]


def test_the_free_and_paid_split_is_spelled_out(client):
    # The gate that catches the expensive mistake. Chapters at or below free_through are
    # free; everything after costs coins.
    a = _novel(client, "Coins", [1, 2, 3, 4],
               translated=[(i, f"Ch {i}.\n") for i in range(1, 5)])
    sid = _series(client, "Coins", [a], free_through=2, coin_price=55)
    plan = _plan(client, sid)
    assert plan["summary"] == {"free": [1, 2], "paid": [3, 4]}
    assert [i["coins"] for i in plan["items"]] == [0, 0, 55, 55]


def test_a_range_limits_what_goes_out(client):
    a = _novel(client, "Range", [1, 2, 3, 4],
               translated=[(i, f"Ch {i}.\n") for i in range(1, 5)])
    sid = _series(client, "Range", [a])
    got = [i["global"] for i in _plan(client, sid, start=2, end=3)["items"]]
    assert got == [2, 3]


# ---- what is held back, and why -------------------------------------------


def test_an_unfinished_chapter_is_blocked(client):
    a = _novel(client, "Unfinished", [1, 2], translated=[(1, "One.\n")],
               status="needs-review")
    sid = _series(client, "Unfinished", [a])
    plan = _plan(client, sid)
    assert plan["ready"] == 0
    assert any("not finished" in b for b in plan["items"][0]["blockers"])


def test_a_chapter_with_no_translation_on_disk_is_blocked(client):
    a = _novel(client, "Missing", [1, 2], translated=[(1, "One.\n")])
    sid = _series(client, "Missing", [a])
    plan = _plan(client, sid)
    blocked = [i for i in plan["items"] if i["blockers"]]
    assert [i["index"] for i in blocked] == [2]
    assert "no translation on disk" in blocked[0]["blockers"]


def test_a_guessed_chapter_number_is_held_back(client):
    # Numbering that restarts partway is a side-story run: the resolver refuses to give it a
    # chapter number and marks it low confidence. Posting one means publishing under a name
    # that was a guess, and renaming a chapter on the site means retyping it — so it waits.
    #
    # The low rows have to come from the RESOLVER, not from an override: confirming a row
    # deliberately raises it to high confidence, which is the whole point of confirming.
    a = _novel(client, "Guessy", [1, 2, 3, 1, 2],
               translated=[(i, f"Ch {i}.\n") for i in range(1, 6)])
    sid = _series(client, "Guessy", [a])
    plan = _plan(client, sid)
    low = [i for i in plan["items"] if i["confidence"] == "low"]
    assert low, "expected the restarted run to be low confidence"
    for item in low:
        assert any("not confirmed" in b for b in item["blockers"])
    # The confident rows of the same series still go out — the hold is per chapter.
    assert plan["ready"] == 3


def test_confirming_the_number_releases_it(client):
    # Otherwise the hold could never be cleared, and a series with one side story could
    # never be posted at all.
    a = _novel(client, "Released", [1, 2, 3, 1],
               translated=[(i, f"Ch {i}.\n") for i in range(1, 5)])
    sid = _series(client, "Released", [a])
    before = _plan(client, sid)
    stuck = next(i for i in before["items"] if i["confidence"] == "low")
    client.put(f"/api/series/{sid}/mapping", json={"overrides": [
        {"project_id": a, "index": stuck["index"], "global": 4}]})
    after = _plan(client, sid)
    freed = next(i for i in after["items"] if i["index"] == stuck["index"])
    assert freed["confidence"] == "high"
    assert not freed["blockers"]


# ---- taking a chapter back out of the ledger ------------------------------

# Until this existed the ledger could only ever be added to: no route, UI or script could
# amend it, so the one way past a stale "already posted" was to CHANGE THE TITLE -- the
# same title-keying that let one chapter be published three times under three different
# numbers. Repairing a real series meant hand-editing the JSON on disk.

ONE = "One." + chr(10)
TWO = "Two." + chr(10)


def _posted(client, sid, title, **kw):
    body = {"sid": sid, "target_id": "t1", "title": title, "status": "posted", **kw}
    return client.post("/api/posting/result", json=body)


def _forget(client, **params):
    return client.request("DELETE", "/api/posting/ledger", params=params)


def test_a_title_differing_only_in_case_updates_the_one_entry(client):
    # record() compared raw titles with !=, while plan_run reads the same entries back
    # through site_title_key. The two sides therefore disagreed about how many chapters
    # the ledger holds, and one chapter could occupy two rows.
    a = _novel(client, "Casing", [1], translated=[(1, ONE)])
    sid = _series(client, "Casing", [a])
    _posted(client, sid, "Chapter 1")
    _posted(client, sid, "  chapter 1  ")
    led = client.get("/api/posting/ledger", params={"sid": sid, "target_id": "t1"}).json()
    assert len(led["entries"]) == 1


def test_forgetting_a_chapter_lets_it_post_again(client):
    a = _novel(client, "Forget", [1, 2], translated=[(1, ONE), (2, TWO)])
    sid = _series(client, "Forget", [a])
    _posted(client, sid, "Chapter 1")
    first = next(i for i in _plan(client, sid)["items"] if i["global"] == 1)
    assert "already in the ledger as posted" in first["blockers"]

    r = _forget(client, sid=sid, target_id="t1", title="Chapter 1")
    assert r.status_code == 200
    assert [e["title"] for e in r.json()["removed"]] == ["Chapter 1"]

    freed = next(i for i in _plan(client, sid)["items"] if i["global"] == 1)
    assert freed["blockers"] == []


def test_a_chapter_can_be_forgotten_by_where_it_lives(client):
    # After a renumber the title has moved, so the title is the one thing you cannot use
    # to name the entry you want gone.
    a = _novel(client, "Moved", [1], translated=[(1, ONE)])
    sid = _series(client, "Moved", [a])
    _posted(client, sid, "Chapter 41", project_id=a, index=1)
    r = _forget(client, sid=sid, target_id="t1", project_id=a, index=1)
    assert r.status_code == 200
    assert [e["title"] for e in r.json()["removed"]] == ["Chapter 41"]
    assert r.json()["entries"] == []


def test_an_index_on_its_own_never_forgets_anything(client):
    # index is a tab's POSITION, re-derived on every source read. Without the document it
    # belongs to it identifies nothing, so it must never match on its own.
    a = _novel(client, "Loose", [1], translated=[(1, ONE)])
    sid = _series(client, "Loose", [a])
    _posted(client, sid, "Chapter 1", project_id=a, index=1)
    r = _forget(client, sid=sid, target_id="t1", index=1)
    assert r.status_code == 404
    led = client.get("/api/posting/ledger", params={"sid": sid, "target_id": "t1"}).json()
    assert len(led["entries"]) == 1


def test_forgetting_a_chapter_that_was_never_posted_says_so(client):
    a = _novel(client, "Never", [1], translated=[(1, ONE)])
    sid = _series(client, "Never", [a])
    assert _forget(client, sid=sid, target_id="t1", title="Chapter 9").status_code == 404


def test_a_ledger_block_says_whether_the_chapter_is_still_up(client):
    """The difference decides what you can do about it.

    A ledger entry for a chapter that is NO LONGER on the site is a stale record, and
    taking it back lets the chapter post again. One for a chapter that is still up there
    is simply true, and forgetting it would achieve nothing but a duplicate on the next
    run. Reported identically, the two were indistinguishable.
    """
    a = _novel(client, "BothOrOne", [1, 2], translated=[(1, ONE), (2, TWO)])
    sid = _series(client, "BothOrOne", [a])
    _posted(client, sid, "Chapter 1")
    _posted(client, sid, "Chapter 2")
    # the site's own list carries only the first of them
    client.post("/api/posting/site", json={
        "sid": sid, "target_id": "t1", "titles": ["Chapter 1"]})
    by = {i["title"]: i for i in _plan(client, sid)["items"]}
    assert by["Chapter 1"]["blockers"] == [
        "already in the ledger as posted, and still on the site"]
    assert by["Chapter 2"]["blockers"] == ["already in the ledger as posted"]


def test_forgetting_does_not_unpost_anything_on_the_site(client):
    # The site is the other record and is untouched: a forgotten chapter that is still
    # live stays blocked by the snapshot, which is the safe direction.
    a = _novel(client, "Live", [1], translated=[(1, ONE)])
    sid = _series(client, "Live", [a])
    _posted(client, sid, "Chapter 1")
    client.post("/api/posting/site", json={
        "sid": sid, "target_id": "t1", "titles": ["Chapter 1"]})
    _forget(client, sid=sid, target_id="t1", title="Chapter 1")
    first = next(i for i in _plan(client, sid)["items"] if i["global"] == 1)
    assert "already on the site" in first["blockers"]


def test_a_chapter_already_in_the_ledger_is_blocked(client):
    a = _novel(client, "Again", [1, 2],
               translated=[(1, "One.\n"), (2, "Two.\n")])
    sid = _series(client, "Again", [a])
    client.post("/api/posting/result", json={
        "sid": sid, "target_id": "t1", "title": "Chapter 1", "status": "posted"})
    plan = _plan(client, sid)
    first = next(i for i in plan["items"] if i["global"] == 1)
    assert "already in the ledger as posted" in first["blockers"]
    assert plan["ready"] == 1


# ---- the payload ----------------------------------------------------------


def test_the_payload_is_attribute_free_html(client):
    a = _novel(client, "Payload", [1], translated=[(1, "One.\n\nTwo.\n")])
    sid = _series(client, "Payload", [a])
    p = client.get("/api/posting/payload",
                   params={"sid": sid, "project_id": a, "index": 1}).json()
    assert p["html"] == "<p>One.</p>\n<p>Two.</p>"
    assert "class=" not in p["html"] and "data-para" not in p["html"]
    assert p["title"] == "Chapter 1"


def test_the_payload_strips_a_standalone_part_marker(client):
    # 21 real chapters open with a bare number. It is invisible in the reader, so
    # publishing it would show readers a number they have never seen, above the text,
    # that does not even match the chapter.
    a = _novel(client, "Marker", [1], translated=[(1, "33.\n\nThe real opening.\n")])
    sid = _series(client, "Marker", [a])
    p = client.get("/api/posting/payload",
                   params={"sid": sid, "project_id": a, "index": 1}).json()
    assert p["html"] == "<p>The real opening.</p>"


def test_a_payload_for_an_untranslated_chapter_is_refused(client):
    a = _novel(client, "NoText", [1, 2], translated=[(1, "One.\n")])
    sid = _series(client, "NoText", [a])
    r = client.get("/api/posting/payload",
                   params={"sid": sid, "project_id": a, "index": 2})
    assert r.status_code == 400


# ---- the ledger -----------------------------------------------------------


def test_a_result_is_recorded_and_read_back(client):
    a = _novel(client, "Ledger", [1], translated=[(1, "One.\n")])
    sid = _series(client, "Ledger", [a])
    client.post("/api/posting/result", json={
        "sid": sid, "target_id": "t1", "title": "Chapter 1", "project_id": a,
        "index": 1, "global": 1, "status": "posted", "price_coins": 55,
        "remote_url": "https://hirayatales.com/x"})
    entries = client.get("/api/posting/ledger",
                         params={"sid": sid, "target_id": "t1"}).json()["entries"]
    assert len(entries) == 1
    assert entries[0]["status"] == "posted" and entries[0]["price_coins"] == 55
    assert entries[0]["at"]


def test_recording_the_same_chapter_twice_replaces_rather_than_duplicates(client):
    # A retry has to update the record, not add a second one — the ledger is the thing a
    # resume reads to decide what is left.
    a = _novel(client, "Retry", [1], translated=[(1, "One.\n")])
    sid = _series(client, "Retry", [a])
    for status in ("needs-attention", "posted"):
        client.post("/api/posting/result", json={
            "sid": sid, "target_id": "t1", "title": "Chapter 1", "status": status})
    entries = client.get("/api/posting/ledger",
                         params={"sid": sid, "target_id": "t1"}).json()["entries"]
    assert len(entries) == 1 and entries[0]["status"] == "posted"


def test_a_failed_attempt_is_recorded_with_its_title(client):
    # This is what lets a retry find the half-finished chapter already on the site and
    # continue into it, instead of creating a second one that then has to be deleted by
    # retyping its name.
    a = _novel(client, "Failed", [1], translated=[(1, "One.\n")])
    sid = _series(client, "Failed", [a])
    client.post("/api/posting/result", json={
        "sid": sid, "target_id": "t1", "title": "Chapter 1",
        "status": "needs-attention", "error": "the paste did not land"})
    e = client.get("/api/posting/ledger",
                   params={"sid": sid, "target_id": "t1"}).json()["entries"][0]
    assert e["title"] == "Chapter 1" and e["status"] == "needs-attention"
    assert e["error"] == "the paste did not land"


def test_a_corrupt_ledger_is_kept_aside_rather_than_overwritten(client):
    a = _novel(client, "Corrupt", [1], translated=[(1, "One.\n")])
    sid = _series(client, "Corrupt", [a])
    path = posting.ledger_path(sid, "t1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")
    assert posting.load_ledger(sid, "t1") == {"entries": []}
    # The bytes survive, so a record of what was already published is recoverable.
    assert list(path.parent.glob("ledger.json.unreadable-*"))


# ---- what the site itself already has -------------------------------------

# The plan used to consult only this app's own ledger, so a series that was being posted
# by hand long before Night Reader existed read as entirely unposted: 105 "ready" chapters
# for one real series, every one of them already live. Python cannot read the site, so the
# extension pushes its chapter list here.


def test_a_chapter_already_on_the_site_is_blocked(client):
    a = _novel(client, "OnSite", [1, 2, 3],
               translated=[(1, "One.\n"), (2, "Two.\n"), (3, "Three.\n")])
    sid = _series(client, "OnSite", [a])
    posting.write_site(sid, "t1", ["Chapter 1", "Chapter 2"])
    plan = _plan(client, sid)
    by_number = {i["global"]: i for i in plan["items"]}
    assert "already on the site" in by_number[1]["blockers"]
    assert "already on the site" in by_number[2]["blockers"]
    assert by_number[3]["blockers"] == []
    assert plan["ready"] == 1


def test_the_site_snapshot_matches_regardless_of_case(client):
    # The extension lowercases when it diffs the live list; the ledger compares exactly.
    # This side has to agree with the extension, because the site is what it answers to.
    a = _novel(client, "Cased", [1, 2], translated=[(1, "One.\n"), (2, "Two.\n")])
    sid = _series(client, "Cased", [a])
    posting.write_site(sid, "t1", ["CHAPTER 1", "  chapter 2  "])
    plan = _plan(client, sid)
    assert plan["ready"] == 0


def test_planning_can_ignore_the_site(client):
    a = _novel(client, "Ignored", [1, 2], translated=[(1, "One.\n"), (2, "Two.\n")])
    sid = _series(client, "Ignored", [a])
    posting.write_site(sid, "t1", ["Chapter 1", "Chapter 2"])
    assert posting.plan_run(sid, "t1")["ready"] == 0
    assert posting.plan_run(sid, "t1", use_site=False)["ready"] == 2


def test_an_empty_chapter_list_never_clears_the_snapshot(client):
    # The one way the extension gets an empty result is scraping the wrong page, or
    # scraping too early. Storing that would report every posted chapter as ready again.
    a = _novel(client, "Empty", [1], translated=[(1, "One.\n")])
    sid = _series(client, "Empty", [a])
    posting.write_site(sid, "t1", ["Chapter 1"])
    with pytest.raises(ValueError):
        posting.write_site(sid, "t1", [])
    assert posting.load_site(sid, "t1")["titles"] == ["Chapter 1"]


def test_the_plan_says_when_nobody_has_looked_at_the_site(client):
    a = _novel(client, "Unlooked", [1], translated=[(1, "One.\n")])
    sid = _series(client, "Unlooked", [a])
    plan = _plan(client, sid)
    # None rather than a count of zero: never-looked and nothing-there are worth telling
    # apart, because only one of them means the counts below cannot be trusted.
    assert plan["site"]["fetched_at"] is None and plan["site"]["count"] == 0
    posting.write_site(sid, "t1", ["Chapter 9"])
    assert _plan(client, sid)["site"]["fetched_at"]


def test_a_corrupt_snapshot_is_kept_aside_rather_than_trusted(client):
    a = _novel(client, "Bad", [1], translated=[(1, "One.\n")])
    sid = _series(client, "Bad", [a])
    path = posting.site_path(sid, "t1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    site = posting.load_site(sid, "t1")
    assert site["titles"] == []
    assert list(path.parent.glob("site.json.unreadable-*"))
    # And NOT merely empty. An empty snapshot reads as "nothing is posted here", which is
    # the direction that posts a duplicate; unreadable has to mean "do not act on this".
    assert site["trusted"] is False
    assert posting.plan_run(sid, "t1")["ready"] == 0


# ---- a snapshot read off the wrong page -----------------------------------

# Not hypothetical. Two snapshots in the real library each hold 94 titles that are the
# names of OTHER novels: the extension scraped the studio's series-list table instead of a
# chapter list, and because the result was non-empty nothing rejected it. Both series then
# offered their whole backlog — 120 and 101 chapters — as ready to post a second time.
#
# The guard that existed was write_site refusing an EMPTY list, and its docstring claimed
# an empty result was "the one way" to have scraped the wrong page. These files are the
# counter-example.


def _catalogue(tmp_path, names):
    """The studio's own list of novels, as the extension pushes it."""
    import json as _json
    (tmp_path / "site_series.json").write_text(_json.dumps({
        "fetched_at": "2026-09-19T00:00:00+00:00", "studio_uid": "s",
        "series": [{"uid": f"u{i}", "name": n} for i, n in enumerate(names)],
    }), encoding="utf-8")


def test_a_reading_of_the_series_list_is_refused(client, tmp_path):
    a = _novel(client, "Wrong", [1], translated=[(1, "One.\n")])
    sid = _series(client, "Wrong", [a])
    posting.write_site(sid, "t1", ["Chapter 1"])
    _catalogue(tmp_path, ["A Damned Bastard", "A Killer E-Rank Guide", "Cherry Pop!"])
    with pytest.raises(ValueError) as caught:
        posting.write_site(sid, "t1", ["A Damned Bastard", "A Killer E-Rank Guide",
                                       "Cherry Pop!"])
    assert "chapter list" in str(caught.value)
    # And the good snapshot is still there.
    assert posting.load_site(sid, "t1")["titles"] == ["Chapter 1"]


def test_a_series_list_already_on_disk_is_distrusted_on_read(client, tmp_path):
    # The write-side guard cannot help the two files that already exist, so the check runs
    # on the way out as well as on the way in.
    a = _novel(client, "Stored", [1], translated=[(1, "One.\n")])
    sid = _series(client, "Stored", [a])
    posting.write_site(sid, "t1", ["Chapter 1"])
    _catalogue(tmp_path, ["A Damned Bastard", "A Killer E-Rank Guide", "Cherry Pop!"])
    path = posting.site_path(sid, "t1")
    path.write_text(json.dumps({"fetched_at": "2026-09-19T00:00:00+00:00",
                                "titles": ["A Damned Bastard", "A Killer E-Rank Guide",
                                           "Cherry Pop!"]}), encoding="utf-8")
    site = posting.load_site(sid, "t1")
    assert site["trusted"] is False
    assert "series list" in site["why"]


def test_an_untrusted_snapshot_blocks_everything_rather_than_nothing(client, tmp_path):
    """The whole point, and the direction is what matters.

    Treating an unusable reading as "empty" means "nothing is posted", which is how two
    real series came to offer 120 and 101 already-published chapters as ready.
    """
    a = _novel(client, "Closed", [1, 2, 3],
               translated=[(1, "One.\n"), (2, "Two.\n"), (3, "Three.\n")])
    sid = _series(client, "Closed", [a])
    assert _plan(client, sid)["ready"] == 3          # nothing known about the site yet
    _catalogue(tmp_path, ["A Damned Bastard", "A Killer E-Rank Guide", "Cherry Pop!"])
    path = posting.site_path(sid, "t1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "fetched_at": "2026-09-19T00:00:00+00:00",
        "titles": ["A Damned Bastard", "A Killer E-Rank Guide", "Cherry Pop!"],
    }), encoding="utf-8")

    plan = _plan(client, sid)
    assert plan["ready"] == 0
    assert plan["site"]["trusted"] is False
    assert all("unusable" in b for i in plan["items"] for b in i["blockers"])
    # And so a run cannot be started at all, through the refusal that already exists.
    r = client.post("/api/posting/run", json={"sid": sid, "options": {}})
    assert r.status_code == 400


def test_a_short_reading_cannot_lose_chapters(client):
    """A partial page must never take chapters OUT of the snapshot.

    A paginated table, a lazy render or a scrape taken mid-load yields a SHORT list. The
    empty check waves it through, so before merging existed a reading of 1 chapter replaced
    a reading of 20 and the other 19 became postable again.

    Merging is the default precisely because of the asymmetry: a merge that never shrinks
    can leave a deleted chapter's ghost, which BLOCKS a post that should have gone out and
    fails closed; a wrong replace drops a chapter that is really there, which PERMITS a
    duplicate onto a live paid site.
    """
    a = _novel(client, "Shrink", [1], translated=[(1, "One.\n")])
    sid = _series(client, "Shrink", [a])
    posting.write_site(sid, "t1", [f"Chapter {n}" for n in range(1, 21)])

    posting.write_site(sid, "t1", ["Chapter 20"])          # merge: the default
    assert len(posting.load_site(sid, "t1")["titles"]) == 20

    with pytest.raises(ValueError) as caught:
        posting.write_site(sid, "t1", ["Chapter 20"], mode="replace")
    assert "partial" in str(caught.value)
    assert len(posting.load_site(sid, "t1")["titles"]) == 20

    # A believable amount of shrinkage still replaces: chapters do get deleted.
    posting.write_site(sid, "t1", [f"Chapter {n}" for n in range(1, 18)], mode="replace")
    assert len(posting.load_site(sid, "t1")["titles"]) == 17


def test_a_merge_keeps_what_a_thinner_reading_does_not_know(client):
    # The DOM carries a title and a price; the site's own records carry the remote id and
    # the state as well. A later, thinner reading must not blank the richer fields — they
    # are exactly what the pricing audit needs.
    a = _novel(client, "Fields", [1], translated=[(1, "One.\n")])
    sid = _series(client, "Fields", [a])
    posting.write_site(sid, "t1", [
        {"title": "Chapter 1", "paid": True, "coins": 55, "state": "public",
         "remote_id": "abc"}], source="api")
    posting.write_site(sid, "t1", [{"title": "Chapter 1"}], source="dom")
    row = posting.load_site(sid, "t1")["rows"][0]
    assert row["remote_id"] == "abc"
    assert row["paid"] is True and row["coins"] == 55 and row["state"] == "public"


def test_a_title_only_snapshot_reads_back_as_unknown_not_free(client):
    """Every snapshot written before rows existed knows titles and nothing else.

    Reading those back as "free at zero coins" would invent an answer the file never
    contained, and a pricing audit built on it would report a whole library as mispriced.
    """
    a = _novel(client, "Legacy", [1], translated=[(1, "One.\n")])
    sid = _series(client, "Legacy", [a])
    path = posting.site_path(sid, "t1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched_at": "2026-09-19T00:00:00+00:00",
                                "titles": ["Chapter 1"]}), encoding="utf-8")
    site = posting.load_site(sid, "t1")
    assert site["source"] == "legacy"
    assert site["rows"][0]["paid"] is None and site["rows"][0]["coins"] is None
    assert site["titles"] == ["Chapter 1"]


# ---- what the site charges, against what this series says --------------

# The free cutoff says what chapter 63 OUGHT to cost. Only the site says what it does. A
# chapter that went up free when it should be paid is revenue quietly gone; one left
# private is a chapter nobody can read. Neither was visible before, because the plan only
# ever asked whether a title was present.


def _priced(client, name, coins=55, free_through=1):
    a = _novel(client, name, [1, 2], translated=[(1, "One.\n"), (2, "Two.\n")])
    sid = _series(client, name, [a])
    series = series_mod.get_series(sid)
    series["publish_targets"] = [{"id": "t1", "site": "meiko",
                                  "series_url": "https://meiko.studio/page/x/series/y/",
                                  "free_through": free_through, "coin_price": coins}]
    series_mod.write_series(series)
    return sid


def test_a_chapter_live_at_the_wrong_price_is_reported(client):
    sid = _priced(client, "Wrongprice")
    posting.write_site(sid, "t1", [
        {"title": "Chapter 1", "paid": False, "coins": 0, "state": "public"},
        {"title": "Chapter 2", "paid": True, "coins": 5, "state": "public"}],
        source="api")
    audit = {a["title"]: a["problems"] for a in _plan(client, sid)["audit"]}
    assert "Chapter 1" not in audit            # free, and free is right below the cutoff
    assert "5 coins" in audit["Chapter 2"][0]


def test_a_chapter_that_should_cost_coins_but_is_free_is_reported(client):
    sid = _priced(client, "Freebie")
    posting.write_site(sid, "t1", [
        {"title": "Chapter 2", "paid": False, "coins": 0, "state": "public"}],
        source="api")
    problems = _plan(client, sid)["audit"][0]["problems"]
    assert "should cost coins" in problems[0]


def test_a_chapter_still_private_is_reported(client):
    sid = _priced(client, "Hidden")
    posting.write_site(sid, "t1", [
        {"title": "Chapter 2", "paid": True, "coins": 55, "state": "private"}],
        source="api")
    problems = _plan(client, sid)["audit"][0]["problems"]
    assert "readers cannot see it" in problems[0]


def test_a_title_only_snapshot_reports_nothing(client):
    """Unknown is not the same as wrong.

    Every snapshot written before rows existed knows titles alone. Reading those as "free
    at zero coins" would report an entire library as mispriced on a field the file never
    contained.
    """
    sid = _priced(client, "Quiet")
    posting.write_site(sid, "t1", ["Chapter 1", "Chapter 2"])
    assert _plan(client, sid)["audit"] == []


def test_the_endpoint_takes_the_rows_the_extension_sends(client):
    sid = _priced(client, "Endpoint")
    r = client.post("/api/posting/site", json={
        "sid": sid, "target_id": "t1", "source": "api", "mode": "merge",
        "rows": [{"title": "Chapter 1", "paid": True, "coins": 55, "state": "public",
                  "remote_id": "abc"}]})
    assert r.status_code == 200
    row = posting.load_site(sid, "t1")["rows"][0]
    assert row["remote_id"] == "abc" and row["coins"] == 55


def test_the_endpoint_still_takes_bare_titles(client):
    # An extension that has not been reloaded keeps working.
    sid = _priced(client, "Oldext")
    r = client.post("/api/posting/site", json={
        "sid": sid, "target_id": "t1", "titles": ["Chapter 1"], "prices": [55]})
    assert r.status_code == 200
    assert posting.load_site(sid, "t1")["titles"] == ["Chapter 1"]


# ---- a chapter we started and did not finish ------------------------------

# The window: the site makes the chapter, the reply is lost to a timeout or a suspended
# tab, and the only record of it lived in one tab's sessionStorage. Close the tab and an
# empty chapter sits on the site that nothing knows about -- and once its title reaches
# the snapshot it blocks the real chapter from ever being posted.


def _intent(client, sid, title, status="creating"):
    return client.post("/api/posting/result", json={
        "sid": sid, "target_id": "t1", "title": title, "status": status})


def test_an_intent_to_create_is_remembered_across_tabs(client):
    a = _novel(client, "Intent", [1, 2], translated=[(1, "One.\n"), (2, "Two.\n")])
    sid = _series(client, "Intent", [a])
    assert _plan(client, sid)["creating"] == []
    _intent(client, sid, "Chapter 2")
    assert _plan(client, sid)["creating"] == ["Chapter 2"]


def test_an_intent_never_blocks_the_chapter_it_names(client):
    """An intent is not a chapter.

    Blocking on it would let a create that never landed veto the real post -- the exact
    permanent-block this feature exists to undo, just arriving by another route.
    """
    a = _novel(client, "Unblocked", [1, 2], translated=[(1, "One.\n"), (2, "Two.\n")])
    sid = _series(client, "Unblocked", [a])
    _intent(client, sid, "Chapter 2")
    by_number = {i["global"]: i for i in _plan(client, sid)["items"]}
    assert by_number[2]["blockers"] == []


def test_finishing_the_chapter_clears_the_intent(client):
    # record() upserts on the title, so the later "posted" replaces the "creating" rather
    # than leaving both.
    a = _novel(client, "Finish", [1, 2], translated=[(1, "One.\n"), (2, "Two.\n")])
    sid = _series(client, "Finish", [a])
    _intent(client, sid, "Chapter 2")
    _intent(client, sid, "Chapter 2", status="posted")
    plan = _plan(client, sid)
    assert plan["creating"] == []
    by_number = {i["global"]: i for i in plan["items"]}
    assert "already in the ledger as posted" in by_number[2]["blockers"]


# ---- what the site holds, as numbers --------------------------------------

# The page needs to say "it has 1-196" and to spot a chapter whose NUMBER is already up
# even when its TITLE drifted. Both need the site's titles read back as numbers, and the
# parser for that is the inverse of the adapter's own template rather than a second regex
# that can disagree with it.


def test_the_plan_says_what_the_site_already_holds(client):
    a = _novel(client, "Holds", [1, 2, 3], translated=[(1, "One.\n")])
    sid = _series(client, "Holds", [a])
    posting.write_site(sid, "t1", [f"Chapter {n}" for n in (1, 2, 3, 4, 5)])
    site = _plan(client, sid)["site"]
    assert site["posted"] == [[1, 5]]
    assert site["max_posted"] == 5 and site["numbered"] == 5


def test_a_gap_on_the_site_survives_into_the_plan(client):
    # Rendered as "1-2, 4-5", which is exactly the shape that deserves a second look.
    a = _novel(client, "Gappy", [1], translated=[(1, "One.\n")])
    sid = _series(client, "Gappy", [a])
    posting.write_site(sid, "t1", ["Chapter 1", "Chapter 2", "Chapter 4", "Chapter 5"])
    assert _plan(client, sid)["site"]["posted"] == [[1, 2], [4, 5]]


def test_a_decorated_title_still_yields_its_chapter_number(client):
    """The tail is deliberately ignored.

    "Chapter 118: The Duel" must parse to 118. The strict form would fail to parse it,
    find no 118 on the site, and let a duplicate through -- which is the difference
    between catching one and posting one.
    """
    a = _novel(client, "Decorated", [1], translated=[(1, "One.\n")])
    sid = _series(client, "Decorated", [a])
    posting.write_site(sid, "t1", ["Chapter 118: The Duel", "Chapter 119 - the aftermath"])
    assert _plan(client, sid)["site"]["posted"] == [[118, 119]]


def test_a_snapshot_of_the_series_list_reports_no_chapter_numbers(client, tmp_path):
    # The regression test for the two real corrupt files: 94 titles, none a chapter. That
    # count-without-numbers shape is what the page warns on.
    a = _novel(client, "Listy", [1], translated=[(1, "One.\n")])
    sid = _series(client, "Listy", [a])
    path = posting.site_path(sid, "t1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched_at": "2026-09-19T00:00:00+00:00",
                                "titles": ["A Damned Bastard", "Cherry Pop!",
                                           "Hard Control"]}), encoding="utf-8")
    site = _plan(client, sid)["site"]
    assert site["count"] == 3 and site["numbered"] == 0
    assert site["posted"] == [] and site["max_posted"] is None


def test_the_site_numbers_follow_the_adapter_template(client, tmp_path):
    # Edit the template and the parser follows, because it is derived from it.
    (tmp_path / "adapters" / "meiko.toml").write_text(
        ADAPTER.replace('title_template = "Chapter {n}"', 'title_template = "Ep. {n}"'),
        encoding="utf-8")
    adapter = posting.load_adapter("meiko")
    assert posting.site_numbers(["Ep. 7", "Ep. 8", "Chapter 9"], adapter) == [7, 8]


def test_leading_zeros_do_not_make_a_different_chapter(client):
    adapter = posting.load_adapter("meiko")
    assert posting.site_numbers(["Chapter 007"], adapter) == [7]


# ---- what this novel calls its chapters -----------------------------------

# Measured: "I Possessed a Character" has eight side stories live as "Side Chapter 1" ..
# "Side Chapter 8", while this app generated "Side Stories N". Zero overlap, on every
# series that has side stories -- so the duplicate guard could not see them and all eight
# would have posted a second time. Rather than hard-code a second convention, the names
# are learned from the novel's own list and used to GENERATE as well as to match.


def test_the_side_story_naming_is_learned_from_the_site(client):
    sid = _with_sides(client, "Learned", free_through=1, coin_price=55)
    posting.write_site(sid, "t1", [
        "Chapter 1", "Side Chapter 1", "Side Chapter 2", "Side Chapter 3"])
    naming = _plan(client, sid)["naming"]
    assert naming["side"]["template"] == "Side Chapter {n}"
    assert naming["chapter"]["template"] == "Chapter {n}"


def test_a_side_story_already_on_the_site_is_no_longer_posted_twice(client):
    """The double-post this phase exists to stop.

    With the adapter default these generate "Side Stories 1" and "Side Stories 2", match
    nothing on the site, and go out as second copies. Learned, they generate the names the
    site already uses and are blocked.
    """
    sid = _with_sides(client, "NoDouble", free_through=1, coin_price=55)
    posting.write_site(sid, "t1", [
        "Chapter 1", "Side Chapter 1", "Side Chapter 2", "Side Chapter 3"])
    sides = [i for i in _plan(client, sid)["items"] if i["kind"] == "side"]
    assert [i["title"] for i in sides] == ["Side Chapter 1", "Side Chapter 2"]
    assert all("already on the site" in i["blockers"] for i in sides)


def test_without_a_learned_name_the_adapter_default_stands(client):
    # Eight of the real series have no side stories on the site at all, and a brand-new
    # name cannot be a duplicate, so this is a fallback rather than a failure.
    sid = _with_sides(client, "Unlearned", free_through=1, coin_price=55)
    posting.write_site(sid, "t1", ["Chapter 1", "Chapter 2", "Chapter 3"])
    sides = [i for i in _plan(client, sid)["items"] if i["kind"] == "side"]
    assert sides[0]["title"] == "Side Stories 1"


def test_an_untrusted_reading_teaches_no_names(client, tmp_path):
    # Names learned from the studio's series list would be other novels' names.
    sid = _with_sides(client, "Untaught", free_through=1, coin_price=55)
    posting.write_site(sid, "t1", ["Chapter 1"])
    _catalogue(tmp_path, ["A Damned Bastard", "A Killer E-Rank Guide", "Cherry Pop!"])
    posting.site_path(sid, "t1").write_text(json.dumps({
        "fetched_at": "2026-09-20T00:00:00+00:00",
        "titles": ["A Damned Bastard", "A Killer E-Rank Guide", "Cherry Pop!"],
    }), encoding="utf-8")
    plan = _plan(client, sid)
    assert plan["naming"] == {}
    assert plan["site"]["trusted"] is False


def test_a_prologue_is_not_chapter_one(client):
    """Two real series carry BOTH a "Prologue" and a "Chapter 1" on the site.

    A prologue row holds global 1 in the mapping, so without its own branch it rendered as
    "Chapter 1" -- a name already taken by a different chapter.
    """
    a = _novel(client, "Prolog", [1, 2], translated=[(1, "One.\n"), (2, "Two.\n")])
    sid = _series(client, "Prolog", [a])
    client.put(f"/api/series/{sid}/mapping", json={"overrides": [
        {"project_id": a, "index": 1, "global": 1, "kind": "prologue"}]})
    titles = [i["title"] for i in _plan(client, sid)["items"]]
    assert "Prologue" in titles
    assert titles.count("Chapter 1") == 0


def test_the_plan_and_the_payload_agree_on_the_title(client):
    """They must, or a run posts under a name nothing is watching for.

    Two call sites generate titles -- plan_run and chapter_payload -- and they learn the
    naming separately. This is the assertion that they learn the same thing.
    """
    sid = _with_sides(client, "Agree", free_through=1, coin_price=55)
    posting.write_site(sid, "t1", [
        "Chapter 9", "Side Chapter 1", "Side Chapter 2", "Side Chapter 3"])
    side = next(i for i in _plan(client, sid)["items"] if i["kind"] == "side")
    payload = client.get("/api/posting/payload", params={
        "sid": sid, "project_id": side["project_id"], "index": side["index"],
        "target_id": "t1"}).json()
    assert payload["title"] == side["title"]


# ---- the run request ------------------------------------------------------

# How the app asks the browser to post. Everything here is checkable without a browser,
# which is the point: the extension's job shrinks to "claim, post, report".

SERIES_URL = "https://meiko.studio/page/x/series/y/"


def _run_novel(client, name, *, coin_price=55, free_through=0):
    a = _novel(client, name, [1, 2, 3, 4],
               translated=[(i, f"Body {i}.\n") for i in (1, 2, 3, 4)])
    return _series(client, name, [a], coin_price=coin_price, free_through=free_through)


def test_a_run_is_queued_for_the_browser_to_find(client):
    sid = _run_novel(client, "Queued")
    run = posting.request_run(sid, "t1", options={"post_state": "public"})
    assert run["state"] == "queued"
    assert run["progress"]["total"] == 4
    assert run["progress"]["first"] == "Chapter 1"
    assert run["progress"]["last"] == "Chapter 4"
    # Copied in, so editing the price mid-run cannot change what the rest of it charges.
    assert run["pricing"] == {"free_through": 0, "coin_price": 55}


def test_a_run_will_not_start_without_a_stated_price(client):
    # 21 of the 22 real linked series are in this state. Every chapter would go out as
    # paid at zero coins, and a price is painful to change once readers have been through.
    sid = _run_novel(client, "Unpriced", coin_price=0, free_through=0)
    with pytest.raises(ValueError, match="coin price"):
        posting.request_run(sid, "t1")
    assert posting.load_run(sid, "t1") is None


def test_a_deliberately_free_series_can_start(client):
    # Stated as free, rather than left unset: a cutoff past the last chapter.
    sid = _run_novel(client, "Free", coin_price=0, free_through=9999)
    assert posting.request_run(sid, "t1")["state"] == "queued"


def test_a_second_run_is_refused_while_one_is_going(client):
    sid = _run_novel(client, "Twice")
    posting.request_run(sid, "t1")
    with pytest.raises(ValueError, match="already going"):
        posting.request_run(sid, "t1")


def test_only_one_browser_can_claim_a_run(client):
    # Two tabs on the same series would otherwise post every chapter twice, and on a coin
    # platform a double post means refunds.
    sid = _run_novel(client, "Claimed")
    posting.request_run(sid, "t1")
    first = posting.claim_run(SERIES_URL, "tab-one")
    assert first and first["state"] == "running" and first["token"] == "tab-one"
    assert posting.claim_run(SERIES_URL, "tab-two") is None


def test_a_claim_only_matches_the_series_the_tab_is_on(client):
    sid = _run_novel(client, "Scoped")
    posting.request_run(sid, "t1")
    assert posting.claim_run("https://meiko.studio/page/x/series/other/", "t") is None
    # A chapter inside the series still counts as being on it.
    assert posting.claim_run(SERIES_URL + "chapter/123", "t") is not None


def test_the_browser_token_is_never_handed_to_the_page(client):
    sid = _run_novel(client, "Token")
    posting.request_run(sid, "t1")
    posting.claim_run(SERIES_URL, "secret-tab")
    assert "claimed_by" not in posting.run_view(posting.load_run(sid, "t1"))


def test_progress_is_recorded_and_survives_a_reload(client):
    # The durability the in-memory job registry could not have given us: this is why the
    # run is a file and progress is polled rather than streamed.
    sid = _run_novel(client, "Progress")
    posting.request_run(sid, "t1")
    posting.claim_run(SERIES_URL, "tab")
    posting.record_progress(sid, "t1", "tab", {"kind": "current", "title": "Chapter 1"})
    posting.record_progress(sid, "t1", "tab", {
        "kind": "posted", "title": "Chapter 1", "global": 1, "coins": 55})
    run = posting.run_view(posting.load_run(sid, "t1"))
    assert run["progress"]["current"] is None
    assert [p["title"] for p in run["progress"]["posted"]] == ["Chapter 1"]
    assert run["progress"]["posted"][0]["coins"] == 55


def test_a_report_from_another_browser_is_told_to_stop(client):
    sid = _run_novel(client, "Interloper")
    posting.request_run(sid, "t1")
    posting.claim_run(SERIES_URL, "mine")
    out = posting.record_progress(sid, "t1", "theirs",
                                  {"kind": "posted", "title": "Chapter 1"})
    assert out["cancelled"] is True
    assert out["run"]["progress"]["posted"] == []


def test_cancelling_reaches_the_browser_on_its_next_report(client):
    sid = _run_novel(client, "Cancelled")
    posting.request_run(sid, "t1")
    posting.claim_run(SERIES_URL, "tab")
    posting.cancel_run(sid, "t1")
    assert posting.record_progress(sid, "t1", "tab", {"kind": "beat"})["cancelled"] is True


def test_one_failure_stops_the_run(client):
    # Never retry blindly into a broken state: the usual cause is an expired session, and
    # pressing on would turn one problem into thirty.
    sid = _run_novel(client, "Broken")
    posting.request_run(sid, "t1")
    posting.claim_run(SERIES_URL, "tab")
    out = posting.record_progress(sid, "t1", "tab", {
        "kind": "failed", "title": "Chapter 1", "error": "session expired"})
    assert out["run"]["state"] == "failed"
    assert out["run"]["progress"]["failed"][0]["error"] == "session expired"


def test_finishing_closes_the_run(client):
    sid = _run_novel(client, "Finished")
    posting.request_run(sid, "t1")
    posting.claim_run(SERIES_URL, "tab")
    out = posting.record_progress(sid, "t1", "tab", {"kind": "finished"})
    assert out["run"]["state"] == "done" and out["run"]["finished_at"]


def test_a_run_whose_browser_went_quiet_reads_as_stalled(client):
    # Computed on read rather than by a timer, so there is nothing to leak and closing the
    # browser mid-run leaves a run that says so plainly.
    sid = _run_novel(client, "Quiet")
    posting.request_run(sid, "t1")
    posting.claim_run(SERIES_URL, "tab")
    path = posting.run_path(sid, "t1")
    doc = json.loads(path.read_text(encoding="utf-8"))
    gone = datetime.now(timezone.utc) - timedelta(
        seconds=posting.STALE_AFTER_SECONDS + 30)
    doc["heartbeat_at"] = gone.isoformat()
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert posting.run_view(posting.load_run(sid, "t1"))["stalled"] is True
    # Still on disk, untouched: reporting it is not the same as reaping it.
    assert json.loads(path.read_text(encoding="utf-8"))["state"] == "running"
    # And the target is free again, because the quiet browser lost its claim.
    assert posting.request_run(sid, "t1")["state"] == "queued"


def test_a_stopped_run_can_be_resumed_keeping_its_progress(client):
    sid = _run_novel(client, "Resumed")
    posting.request_run(sid, "t1")
    posting.claim_run(SERIES_URL, "tab")
    posting.record_progress(sid, "t1", "tab",
                            {"kind": "posted", "title": "Chapter 1", "global": 1})
    posting.cancel_run(sid, "t1")
    run = posting.resume_run(sid, "t1")
    assert run["state"] == "queued"
    assert [p["title"] for p in run["progress"]["posted"]] == ["Chapter 1"]
    # A fresh browser can take it, and the old token no longer holds it.
    assert posting.claim_run(SERIES_URL, "new-tab")


def test_a_live_run_cannot_be_resumed(client):
    sid = _run_novel(client, "Live")
    posting.request_run(sid, "t1")
    with pytest.raises(ValueError, match="still going"):
        posting.resume_run(sid, "t1")


def test_stop_at_chapter_caps_the_run(client):
    sid = _run_novel(client, "UpTo")
    run = posting.request_run(sid, "t1", options={"up_to": 2})
    assert run["progress"]["total"] == 2 and run["progress"]["last"] == "Chapter 2"


def test_at_most_n_caps_the_run(client):
    sid = _run_novel(client, "Limit")
    run = posting.request_run(sid, "t1", options={"limit": 3})
    assert run["progress"]["total"] == 3 and run["progress"]["last"] == "Chapter 3"


def test_a_range_that_finishes_before_it_starts_is_refused(client):
    sid = _run_novel(client, "Backwards")
    with pytest.raises(ValueError, match="finishes before"):
        posting.request_run(sid, "t1", options={"start": 4, "end": 2})


def test_a_run_with_nothing_ready_is_refused(client):
    sid = _run_novel(client, "Nothing")
    posting.write_site(sid, "t1", [f"Chapter {n}" for n in (1, 2, 3, 4)])
    with pytest.raises(ValueError, match="Nothing in that range"):
        posting.request_run(sid, "t1")


def test_only_private_or_public_can_be_asked_for(client):
    sid = _run_novel(client, "State")
    with pytest.raises(ValueError, match="private or public"):
        posting.request_run(sid, "t1", options={"post_state": "draft"})
    run = posting.request_run(sid, "t1", options={"post_state": "private"})
    assert run["options"]["post_state"] == "private"



# ---- the run request, over HTTP -------------------------------------------

# The page speaks to /run and /site; the extension speaks to /claim and /progress. Both
# halves are checked here because the whole point of this round of work is that the app can
# start a run at all.


def test_the_page_can_start_a_run_and_read_it_back(client):
    sid = _run_novel(client, "ApiStart")
    started = client.post("/api/posting/run", json={
        "sid": sid, "options": {"post_state": "public", "limit": 2}}).json()
    assert started["state"] == "queued" and started["progress"]["total"] == 2
    shown = client.get("/api/posting/run", params={"sid": sid, "target_id": "t1"}).json()
    assert shown["run"]["id"] == started["id"]
    assert shown["run"]["stalled"] is False
    # Stated so the page can say how long a silent browser has before it counts as stalled.
    assert shown["stale_after"] == posting.STALE_AFTER_SECONDS


def test_no_run_reads_as_no_run_rather_than_an_error(client):
    sid = _run_novel(client, "ApiNone")
    r = client.get("/api/posting/run", params={"sid": sid, "target_id": "t1"})
    assert r.status_code == 200 and r.json()["run"] is None


def test_an_unpriced_series_is_refused_with_a_reason(client):
    sid = _run_novel(client, "ApiUnpriced", coin_price=0, free_through=0)
    r = client.post("/api/posting/run", json={"sid": sid})
    assert r.status_code == 400
    # Errors are wrapped by server/errors.py, so the sentence is on detail.title.
    assert "coin price" in r.json()["detail"]["title"]


def test_a_full_round_trip_through_the_extension_endpoints(client):
    sid = _run_novel(client, "ApiRound")
    client.post("/api/posting/run", json={"sid": sid, "options": {"limit": 1}})
    claimed = client.post("/api/posting/claim", json={
        "url": SERIES_URL, "token": "tab-1"}).json()["run"]
    assert claimed["series_name"] == "ApiRound"
    assert claimed["options"]["post_state"] == "public"
    assert claimed["token"] == "tab-1"
    body = {"sid": sid, "target_id": "t1", "token": "tab-1"}
    client.post("/api/posting/progress", json={
        **body, "event": {"kind": "posted", "title": "Chapter 1", "global": 1,
                          "coins": 55}})
    done = client.post("/api/posting/progress", json={
        **body, "event": {"kind": "finished"}}).json()
    assert done["cancelled"] is False and done["run"]["state"] == "done"
    shown = client.get("/api/posting/run", params={"sid": sid, "target_id": "t1"}).json()
    assert len(shown["run"]["progress"]["posted"]) == 1


def test_a_tab_on_nothing_gets_no_work(client):
    sid = _run_novel(client, "ApiElsewhere")
    client.post("/api/posting/run", json={"sid": sid})
    r = client.post("/api/posting/claim", json={
        "url": "https://meiko.studio/page/x/series/zzz/", "token": "t"})
    assert r.status_code == 200 and r.json()["run"] is None


def test_cancelling_from_the_page_stops_the_browser(client):
    sid = _run_novel(client, "ApiCancel")
    client.post("/api/posting/run", json={"sid": sid})
    client.post("/api/posting/claim", json={"url": SERIES_URL, "token": "tab"})
    client.delete("/api/posting/run", params={"sid": sid, "target_id": "t1"})
    out = client.post("/api/posting/progress", json={
        "sid": sid, "target_id": "t1", "token": "tab", "event": {"kind": "beat"}}).json()
    assert out["cancelled"] is True


def test_a_stopped_run_is_resumable_from_the_page(client):
    sid = _run_novel(client, "ApiResume")
    client.post("/api/posting/run", json={"sid": sid})
    client.delete("/api/posting/run", params={"sid": sid, "target_id": "t1"})
    r = client.post("/api/posting/run/resume", json={"sid": sid, "target_id": "t1"})
    assert r.status_code == 200 and r.json()["run"]["state"] == "queued"


def test_the_extension_can_push_the_chapter_list(client):
    sid = _run_novel(client, "ApiSite")
    r = client.post("/api/posting/site", json={
        "sid": sid, "target_id": "t1", "titles": ["Chapter 1", "Chapter 2"]})
    assert r.status_code == 200 and r.json()["fetched_at"]
    plan = _plan(client, sid)
    assert plan["ready"] == 2 and plan["site"]["count"] == 2


def test_an_empty_push_is_refused_over_http_too(client):
    sid = _run_novel(client, "ApiEmpty")
    client.post("/api/posting/site", json={
        "sid": sid, "target_id": "t1", "titles": ["Chapter 1"]})
    r = client.post("/api/posting/site", json={
        "sid": sid, "target_id": "t1", "titles": []})
    assert r.status_code == 400
    assert posting.load_site(sid, "t1")["titles"] == ["Chapter 1"]


def test_an_unknown_option_is_refused_rather_than_ignored(client):
    # extra="forbid" on the options, so a renamed field fails loudly instead of quietly
    # posting the whole backlog when a cap was meant to apply.
    sid = _run_novel(client, "ApiTypo")
    r = client.post("/api/posting/run", json={"sid": sid, "options": {"upto": 2}})
    assert r.status_code == 422

def test_the_browser_can_correct_the_run_total(client):
    # The run is planned from the site snapshot, which can be behind the chapter list the
    # browser actually reads on arrival. Without this the page sits at "3 of 5" on a run
    # that only ever had three chapters to do, which reads like it gave up.
    sid = _run_novel(client, "Recount")
    posting.request_run(sid, "t1")
    posting.claim_run(SERIES_URL, "tab")
    out = posting.record_progress(sid, "t1", "tab", {
        "kind": "queue", "total": 2, "first": "Chapter 3", "last": "Chapter 4"})
    progress = out["run"]["progress"]
    assert progress["total"] == 2
    assert progress["first"] == "Chapter 3" and progress["last"] == "Chapter 4"


def test_a_nonsense_total_is_ignored(client):
    sid = _run_novel(client, "Nonsense")
    posting.request_run(sid, "t1")
    posting.claim_run(SERIES_URL, "tab")
    out = posting.record_progress(sid, "t1", "tab", {"kind": "queue", "total": "lots"})
    assert out["run"]["progress"]["total"] == 4


def test_a_free_cutoff_without_a_price_is_still_refused(client):
    # The hole in the first version of this guard, and an expensive one. Checking only for
    # "both fields are zero" let a series with a cutoff at chapter 2 and no coin price
    # through, and everything past the cutoff then posted as paid at 0 coins — a locked
    # chapter readers cannot buy, with the revenue simply gone.
    sid = _run_novel(client, "Cutoff", coin_price=0, free_through=2)
    with pytest.raises(ValueError, match="coin price"):
        posting.request_run(sid, "t1")
    assert posting.load_run(sid, "t1") is None


def test_the_free_side_of_the_cutoff_posts_without_a_price(client):
    # And the other half of being exact about it: a run that never reaches past the cutoff
    # charges for nothing, so it needs no price. This is what lets a free backlog go out
    # before the coin price has been read off the site.
    sid = _run_novel(client, "FreeSide", coin_price=0, free_through=2)
    run = posting.request_run(sid, "t1", options={"up_to": 2})
    assert run["state"] == "queued" and run["progress"]["total"] == 2


def test_the_refusal_says_how_many_and_from_where(client):
    sid = _run_novel(client, "Named", coin_price=0, free_through=2)
    with pytest.raises(ValueError) as caught:
        posting.request_run(sid, "t1")
    message = str(caught.value)
    assert "2 of the 4" in message and "chapter 3" in message


# ---- learning the coin price off the site ---------------------------------

# The site export carries no price anywhere, which is why 21 of the 22 linked series had
# none. It is only visible on the series page, in the box on each paid row, and the
# extension already reads those - so the app learns it instead of it being typed 21 times.


def test_the_price_the_site_charges_is_learned_and_offered(client):
    sid = _run_novel(client, "Learned", coin_price=0, free_through=2)
    posting.write_site(sid, "t1", ["Chapter 9", "Chapter 8", "Chapter 7"],
                       [55, 55, 0])
    assert posting.load_site(sid, "t1")["observed_coins"] == 55
    # Offered to the page, and never written onto the series: a price gets a human nod.
    assert posting.plan_run(sid, "t1")["site"]["observed_coins"] == 55
    assert client.get(f"/api/series/{sid}").json()[
        "publish_targets"][0]["coin_price"] == 0


def test_an_odd_chapter_price_does_not_become_the_series_price(client):
    # A series can carry a handful of oddly priced chapters. The price that matters is the
    # one nearly every paid chapter has, so it is the modal value, not the first or the max.
    sid = _run_novel(client, "Modal")
    posting.write_site(sid, "t1", [f"Chapter {n}" for n in range(1, 7)],
                       [55, 55, 55, 55, 120, 30])
    assert posting.load_site(sid, "t1")["observed_coins"] == 55


def test_a_page_of_only_free_chapters_does_not_forget_the_price(client):
    # Scrolled to the early free chapters, the page says nothing about what the paid ones
    # cost. Treating that as "no price" would throw away a good answer.
    sid = _run_novel(client, "Forget")
    posting.write_site(sid, "t1", ["Chapter 9"], [55])
    posting.write_site(sid, "t1", ["Chapter 2", "Chapter 1"], [0, 0])
    assert posting.load_site(sid, "t1")["observed_coins"] == 55


def test_the_extension_can_push_prices_over_http(client):
    sid = _run_novel(client, "PricePush")
    r = client.post("/api/posting/site", json={
        "sid": sid, "target_id": "t1",
        "titles": ["Chapter 1", "Chapter 2"], "prices": [47, 47, 47]})
    assert r.status_code == 200
    assert _plan(client, sid)["site"]["observed_coins"] == 47


# ---- pricing a side story -------------------------------------------------

# A side story has no chapter number, so the free/paid cutoff cannot classify it. The first
# version of this defaulted them to FREE, which on this site is the wrong way round: its own
# chapter lists show side content as the earliest PAID entry on three of the four series
# that have any, and side stories are the newest thing on those series - so giving them
# away is the expensive direction. 48 of them are waiting on confirmed numbers.


def _with_sides(client, name, **kw):
    a = _novel(client, name, [1, 2, 3],
               translated=[(1, "One.\n"), (2, "Two.\n"), (3, "Three.\n")])
    sid = _series(client, name, [a], **kw)
    client.put(f"/api/series/{sid}/mapping", json={"overrides": [
        {"project_id": a, "index": 2, "global": None, "kind": "side"},
        {"project_id": a, "index": 3, "global": None, "kind": "side"},
    ]})
    return sid


def test_a_side_story_costs_the_same_as_a_paid_chapter(client):
    sid = _with_sides(client, "PaidSides", free_through=1, coin_price=55)
    by_title = {i["title"]: i for i in _plan(client, sid)["items"]}
    assert by_title["Chapter 1"]["paid"] is False
    assert by_title["Side Stories 1"]["paid"] is True
    assert by_title["Side Stories 1"]["coins"] == 55
    assert by_title["Side Stories 2"]["coins"] == 55


def test_a_series_can_say_its_side_stories_are_free(client):
    sid = _with_sides(client, "FreeSides", free_through=1, coin_price=55)
    series = client.get(f"/api/series/{sid}").json()
    target = {**series["publish_targets"][0], "side_paid": False}
    client.post(f"/api/series/{sid}", json={"publish_targets": [target]})
    plan = _plan(client, sid)
    assert plan["side_paid"] is False
    by_title = {i["title"]: i for i in plan["items"]}
    assert by_title["Side Stories 1"]["paid"] is False
    assert by_title["Side Stories 1"]["coins"] == 0


def test_side_stories_default_to_paid_when_a_series_has_never_said(client):
    # The stored targets carry no side_paid key at all, so the default is what decides.
    sid = _with_sides(client, "SilentSides", free_through=1, coin_price=55)
    target = client.get(f"/api/series/{sid}").json()["publish_targets"][0]
    assert "side_paid" not in target
    assert _plan(client, sid)["side_paid"] is True


def test_a_run_of_only_side_stories_still_needs_a_price(client):
    # The guard has to see side stories as chargeable too, or the one case where the price
    # matters most - the newest content on the series - slips through unpriced.
    sid = _with_sides(client, "SidesUnpriced", free_through=99, coin_price=0)
    with pytest.raises(ValueError, match="coin price"):
        posting.request_run(sid, "t1")


def test_free_side_stories_need_no_price(client):
    sid = _with_sides(client, "SidesFree", free_through=99, coin_price=0)
    series = client.get(f"/api/series/{sid}").json()
    target = {**series["publish_targets"][0], "side_paid": False}
    client.post(f"/api/series/{sid}", json={"publish_targets": [target]})
    run = posting.request_run(sid, "t1")
    assert run["state"] == "queued" and run["progress"]["total"] == 3


# ---- a side story that carries its own number -----------------------------

# The case every test above this one missed: they all set ``global`` to None, so the
# side_paid branch was the only branch they could reach.
#
# Two real series number their side stories 1..10 in their OWN sequence, which collides
# exactly with their chapters 1..10. Both the pricing rule and the range filter asked "does
# this row have a number?" rather than "is this side content", so those twenty rows took
# the CHAPTER branch: each side story's ordinal was compared against the chapter cutoff
# (66) and lost, and all twenty planned as free while side_paid sat at its default of True
# and was never consulted. Giving them away is the direction that cannot be undone -- a
# reader who paid nothing keeps the chapter.


def _with_numbered_sides(client, name, **kw):
    """Two chapters (1, 2) and two side stories numbered 1 and 2 in their OWN sequence.

    So side story 1 shares the number 1 with chapter 1, and side story 2 shares 2 with
    chapter 2 -- the exact collision the two real series have.
    """
    a = _novel(client, name, [1, 2, 3, 4],
               translated=[(1, "One.\n"), (2, "Two.\n"), (3, "Three.\n"), (4, "Four.\n")])
    sid = _series(client, name, [a], **kw)
    client.put(f"/api/series/{sid}/mapping", json={"overrides": [
        {"project_id": a, "index": 3, "global": 1, "kind": "side"},
        {"project_id": a, "index": 4, "global": 2, "kind": "side"},
    ]})
    return sid


def test_a_numbered_side_story_is_priced_by_the_series_not_by_the_cutoff(client):
    # Both side stories carry numbers at or below the cutoff, so the chapter rule would
    # call them free. The series says side content is paid, and that is what decides.
    sid = _with_numbered_sides(client, "NumberedSides", free_through=5, coin_price=55)
    by_title = {i["title"]: i for i in _plan(client, sid)["items"]}
    assert by_title["Side Stories 1"]["paid"] is True
    assert by_title["Side Stories 1"]["coins"] == 55
    assert by_title["Side Stories 2"]["paid"] is True
    # ...and the real chapter sharing the number 1 is still judged by the cutoff.
    assert by_title["Chapter 1"]["paid"] is False


def test_a_numbered_side_story_can_still_be_made_free(client):
    # The other direction, so this cannot pass by always answering "paid": with the cutoff
    # at 0 the chapter rule would price BOTH side stories as paid.
    sid = _with_numbered_sides(client, "NumberedFree", free_through=0, coin_price=55)
    series = client.get(f"/api/series/{sid}").json()
    target = {**series["publish_targets"][0], "side_paid": False}
    client.post(f"/api/series/{sid}", json={"publish_targets": [target]})
    by_title = {i["title"]: i for i in _plan(client, sid)["items"]}
    assert by_title["Side Stories 1"]["paid"] is False
    assert by_title["Side Stories 1"]["coins"] == 0
    assert by_title["Chapter 1"]["paid"] is True


def test_a_chapter_range_cannot_reach_a_numbered_side_story(client):
    # 1-2 names the chapters. The side stories carry 1 and 2 in a different sequence, and
    # a chapter range must not confuse the two -- it used to sweep them both in.
    sid = _with_numbered_sides(client, "RangedSides", free_through=0, coin_price=55)
    titles = [i["title"] for i in posting.plan_run(sid, start=1, end=2)["items"]]
    assert "Chapter 1" in titles and "Chapter 2" in titles
    assert [t for t in titles if t.startswith("Side")] == []


# ---- the panel and the claim must agree on where they are -----------------

# Two matchers now exist: /api/posting/match, which tells the panel which novel it is
# looking at, and claim_run, which decides whether a tab may take a queued run. If they
# ever disagree, a run becomes claimable on a page the panel calls unmatched - or, worse,
# not claimable on one it calls matched, and a queued run simply never starts.


def test_the_panel_matcher_and_the_claim_matcher_agree(client):
    a = _novel(client, "Agree", [1], translated=[(1, "One.\n")])
    _series(client, "Agree", [a])
    base = "https://meiko.studio/page/x/series/y"
    for url in (base, base + "/", base + "/chapters", base + "/chapter/abc?tab=details",
                base + "/?tab=chapters",
                "https://meiko.studio/page/x/",  # the studio list: not a series
                "https://meiko.studio/",
                "https://meiko.studio/series/y/"):  # a shape with no /page/ segment
        panel = client.get("/api/posting/match", params={"url": url}).json()["found"]
        claim = posting._url_matches("https://meiko.studio/page/x/series/y/", url)
        assert panel == claim, url


def test_the_studio_list_is_not_a_series_page(client):
    # The entry point when you open the site: it lists every novel and belongs to none of
    # them. The panel used to ask which series it was on exactly once, here, and then never
    # again however many novels you clicked into - so it stayed red for the whole session.
    a = _novel(client, "Studio", [1], translated=[(1, "One.\n")])
    _series(client, "Studio", [a])
    assert client.get("/api/posting/match", params={
        "url": "https://meiko.studio/page/x/"}).json()["found"] is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
