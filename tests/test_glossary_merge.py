"""Tests for merging a series' per-document glossaries into one.

This is the step that fixes a real bug. A novel split across several Google Docs keeps one
glossary per document, so a character's locked spelling resets when the story carries on
into the next document and can drift from there — the exact thing the glossary exists to
prevent. What has to hold:

* documents that already agree merge to agreement, not to a wall of conflicts (two real
  documents hold byte-identical 346-entry glossaries);
* a genuine disagreement is never settled silently — ``Glossary.add`` replaces by Korean
  key, so a naive member-by-member merge would let the LAST document win every time;
* English-only names are compared case-insensitively, because ``add`` dedupes them by
  ``english.lower()`` and would otherwise destroy the evidence of a casing disagreement;
* every merged pending item keeps both ``korean`` and ``english`` keys, because
  ``_queue_new_terms_locked`` subscripts them directly and a missing key raises KeyError
  on the translation worker's thread, mid-chapter;
* pending ``chapter`` numbers are rewritten from local to global, or every suggestion
  points at the wrong chapter;
* conflict ranking counts each chapter ONCE — 28 of the user's projects carry a stray
  backup copy of their whole chapter tree, and an rglob would let whichever spelling
  happened to be backed up more decide every conflict;
* a novel is only pointed at the series glossary once the merge has actually run, and
  unlinking the series puts it back.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_glossary_merge.py``).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server.app as A  # noqa: E402
import server.glossary_merge as gm  # noqa: E402
import server.pages as P  # noqa: E402
import server.projects as pj  # noqa: E402
import server.series as series_mod  # noqa: E402
from translation_bot.config import Config  # noqa: E402
from translation_bot.glossary import Glossary, glossary_lock, load_pending  # noqa: E402

BODY = "그는 천천히 고개를 돌려 나를 바라보았다. " * 12


@pytest.fixture
def client(monkeypatch, tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    for module in (pj, P):
        monkeypatch.setattr(module, "PROJECTS_DIR", root)
    monkeypatch.setattr(A, "load_global_config", lambda: Config())
    A._chapter_cache.clear()
    A._offline_projects.clear()
    series_mod._invalidate_index()
    return TestClient(A.app, base_url="http://localhost",
                      client=("127.0.0.1", 50000))


def _term(korean, english, **kw):
    return {"korean": korean, "english": english, "type": kw.get("type", "name"),
            "note": kw.get("note", ""), "pronoun": kw.get("pronoun", ""),
            "register": kw.get("register", "")}


def _novel(client, name, numbers, *, glossary=(), pending=(), chapters=()):
    """A text novel with its own glossary, pending queue and translated chapter files."""
    pid = client.post("/api/projects/text", json={
        "name": name, "text": "seed", "split_mode": "single"}).json()["id"]
    pdir = pj.PROJECTS_DIR / pid
    records = []
    for i, n in enumerate(numbers, 1):
        head = f"ridibooks.com/books/{1000 + i}/view\n\n노벨 제목 {n}화"
        records.append({"title": f"Tab {i}", "paragraphs": [head, BODY]})
    (pdir / "source.json").write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    project = json.loads((pdir / "project.json").read_text(encoding="utf-8"))
    project["chapter_count"] = len(records)
    (pdir / "project.json").write_text(json.dumps(project, ensure_ascii=False), encoding="utf-8")
    (pdir / "glossary.json").write_text(
        json.dumps(list(glossary), ensure_ascii=False), encoding="utf-8")
    if pending:
        (pdir / "glossary_pending.json").write_text(
            json.dumps(list(pending), ensure_ascii=False), encoding="utf-8")
    (pdir / "chapters").mkdir(exist_ok=True)
    for i, text in enumerate(chapters, 1):
        (pdir / "chapters" / f"chapter-{i:02d}.md").write_text(text, encoding="utf-8")
    A._chapter_cache.pop(pid, None)
    return pid


def _series(client, name, pids, *, resolve=True):
    sid = client.post("/api/series", json={"name": name, "project_ids": pids}).json()["id"]
    if resolve:
        client.get(f"/api/series/{sid}/mapping")
    return sid


def _merge(client, sid, picks=None, dry_run=True):
    return client.post(f"/api/series/{sid}/glossary/merge",
                       json={"picks": picks or {}, "dry_run": dry_run}).json()


# ---- agreement ------------------------------------------------------------


def test_identical_glossaries_merge_to_agreement(client):
    # Two real documents hold byte-identical 346-entry glossaries (copy_glossary was
    # used). Reporting hundreds of conflicts for entries that agree would be useless.
    terms = [_term("백정우", "Baek Jeongwoo", pronoun="he"), _term("각인", "marking", type="term")]
    a = _novel(client, "Same", [1, 2], glossary=terms)
    b = _novel(client, "Same2", [3, 4], glossary=terms)
    r = _merge(client, _series(client, "Same", [a, b]))
    assert r["conflicts"] == []
    assert r["locked"] == 2


def test_merging_is_idempotent(client):
    terms = [_term("백정우", "Baek Jeongwoo")]
    a = _novel(client, "Idem", [1, 2], glossary=terms)
    b = _novel(client, "Idem2", [3, 4], glossary=terms)
    sid = _series(client, "Idem", [a, b])
    first = _merge(client, sid, dry_run=False)
    second = _merge(client, sid, dry_run=False)
    assert first["locked"] == second["locked"] == 1
    assert second["conflicts"] == []


def test_the_union_of_distinct_terms_is_kept(client):
    a = _novel(client, "Union", [1, 2], glossary=[_term("가", "Ga")])
    b = _novel(client, "Union2", [3, 4], glossary=[_term("나", "Na")])
    r = _merge(client, _series(client, "Union", [a, b]))
    assert r["locked"] == 2 and r["conflicts"] == []


# ---- the drift bug --------------------------------------------------------


def test_the_same_korean_with_two_spellings_is_a_conflict(client):
    # THE bug this feature exists to fix: one character, two locked romanizations.
    a = _novel(client, "Drift", [1, 2], glossary=[_term("백정우", "Baek Jeongwoo")])
    b = _novel(client, "Drift2", [3, 4], glossary=[_term("백정우", "Baek Jeong-u")])
    r = _merge(client, _series(client, "Drift", [a, b]))
    assert len(r["conflicts"]) == 1
    c = r["conflicts"][0]
    assert c["kind"] == "english" and c["korean"] == "백정우"
    assert {x["english"] for x in c["candidates"]} == {"Baek Jeongwoo", "Baek Jeong-u"}


def test_the_spelling_used_in_more_chapters_is_suggested(client):
    # The spelling readers have actually seen wins, not whichever document came last.
    a = _novel(client, "Rank", [1, 2, 3], glossary=[_term("백정우", "Baek Jeongwoo")],
               chapters=["Baek Jeongwoo arrived.", "Baek Jeongwoo left.", "Baek Jeongwoo."])
    b = _novel(client, "Rank2", [4, 5], glossary=[_term("백정우", "Baek Jeong-u")],
               chapters=["Baek Jeong-u waited."])
    c = _merge(client, _series(client, "Rank", [a, b]))["conflicts"][0]
    assert c["suggested"] == "Baek Jeongwoo"
    by = {x["english"]: x["in_chapters"] for x in c["candidates"]}
    assert by == {"Baek Jeongwoo": 3, "Baek Jeong-u": 1}


def test_backup_chapter_trees_are_not_counted(client):
    # 28 real projects carry export_artifacts_backup_*/chapters/ holding a full copy of
    # every chapter. An rglob would count those and let the losing spelling win.
    a = _novel(client, "Backup", [1, 2], glossary=[_term("백정우", "Baek Jeongwoo")],
               chapters=["Baek Jeongwoo arrived.", "Baek Jeongwoo left."])
    b = _novel(client, "Backup2", [3, 4], glossary=[_term("백정우", "Baek Jeong-u")],
               chapters=["Baek Jeong-u waited."])
    stray = pj.PROJECTS_DIR / b / "export_artifacts_backup_20260828" / "chapters"
    stray.mkdir(parents=True)
    for i in range(1, 9):
        (stray / f"chapter-{i:02d}.md").write_text("Baek Jeong-u " * 20, encoding="utf-8")
    c = _merge(client, _series(client, "Backup", [a, b]))["conflicts"][0]
    assert c["suggested"] == "Baek Jeongwoo"
    assert {x["english"]: x["in_chapters"] for x in c["candidates"]} == {
        "Baek Jeongwoo": 2, "Baek Jeong-u": 1}


def test_a_pick_overrides_the_suggestion(client):
    a = _novel(client, "Pick", [1, 2], glossary=[_term("백정우", "Baek Jeongwoo")],
               chapters=["Baek Jeongwoo arrived.", "Baek Jeongwoo left."])
    b = _novel(client, "Pick2", [3, 4], glossary=[_term("백정우", "Baek Jeong-u")])
    sid = _series(client, "Pick", [a, b])
    _merge(client, sid, picks={"백정우": "Baek Jeong-u"}, dry_run=False)
    merged = Glossary.load(series_mod.series_root() / sid / "glossary.json")
    assert merged.get("백정우").english == "Baek Jeong-u"


def test_an_unanswered_conflict_still_locks_a_spelling(client):
    # Leaving a contested name OUT of the glossary until someone clicks would let the
    # next translation drift worse than before the merge.
    a = _novel(client, "Unan", [1, 2], glossary=[_term("백정우", "Baek Jeongwoo")],
               chapters=["Baek Jeongwoo arrived."])
    b = _novel(client, "Unan2", [3, 4], glossary=[_term("백정우", "Baek Jeong-u")])
    sid = _series(client, "Unan", [a, b])
    _merge(client, sid, dry_run=False)
    merged = Glossary.load(series_mod.series_root() / sid / "glossary.json")
    assert merged.get("백정우") is not None


def test_a_disagreeing_pronoun_is_a_conflict(client):
    a = _novel(client, "Pron", [1, 2], glossary=[_term("김수려", "Kim Suryeo", pronoun="he")])
    b = _novel(client, "Pron2", [3, 4], glossary=[_term("김수려", "Kim Suryeo", pronoun="she")])
    c = _merge(client, _series(client, "Pron", [a, b]))["conflicts"][0]
    assert c["kind"] == "pronoun"
    assert {x["pronoun"] for x in c["candidates"]} == {"he", "she"}


def test_an_english_only_casing_difference_is_surfaced(client):
    # Glossary.add dedupes English-only entries by english.lower(), which would collapse
    # these two and destroy the evidence that they disagree.
    a = _novel(client, "Case", [1, 2], glossary=[_term("", "Sylph", type="term")])
    b = _novel(client, "Case2", [3, 4], glossary=[_term("", "sylph", type="term")])
    r = _merge(client, _series(client, "Case", [a, b]))
    assert [c["kind"] for c in r["conflicts"]] == ["casing"]
    assert {x["english"] for x in r["conflicts"][0]["candidates"]} == {"Sylph", "sylph"}


# ---- the pending queue ----------------------------------------------------


def test_pending_chapter_numbers_are_rewritten_to_global(client):
    a = _novel(client, "Pend", [1, 2])
    b = _novel(client, "Pend2", [3, 4],
               pending=[dict(_term("손세연", "Son Yeonhee"), chapter=2)])
    sid = _series(client, "Pend", [a, b])
    _merge(client, sid, dry_run=False)
    items = load_pending(series_mod.series_root() / sid / "glossary_pending.json")
    assert len(items) == 1
    # Member b starts at chapter 3, so its local chapter 2 is globally 4.
    assert items[0]["chapter"] == 4
    assert items[0]["local_chapter"] == 2


def test_every_merged_pending_item_keeps_both_keys(client):
    # _queue_new_terms_locked builds its dedupe set with p["korean"] and p["english"].
    # A missing key raises KeyError on a worker thread in the middle of a translation.
    a = _novel(client, "Keys", [1, 2], pending=[{"english": "Nameless", "chapter": 1}])
    b = _novel(client, "Keys2", [3, 4], pending=[{"korean": "가", "chapter": 1}])
    sid = _series(client, "Keys", [a, b])
    _merge(client, sid, dry_run=False)
    items = load_pending(series_mod.series_root() / sid / "glossary_pending.json")
    assert items
    for item in items:
        assert "korean" in item and "english" in item
    # And the real consumer can build its key set without blowing up.
    assert {(p["korean"], p["english"]) for p in items}


def test_pending_is_deduped_and_settled_items_dropped(client):
    shared = dict(_term("손세연", "Son Yeonhee"), chapter=1)
    a = _novel(client, "Dedupe", [1, 2], pending=[shared],
               glossary=[_term("백정우", "Baek Jeongwoo")])
    b = _novel(client, "Dedupe2", [3, 4],
               pending=[shared, dict(_term("백정우", "Baek Jeongwoo"), chapter=1)],
               glossary=[_term("백정우", "Baek Jeongwoo")])
    sid = _series(client, "Dedupe", [a, b])
    _merge(client, sid, dry_run=False)
    items = load_pending(series_mod.series_root() / sid / "glossary_pending.json")
    # The duplicate suggestion appears once; the one the merge already locked is gone.
    assert [i["english"] for i in items] == ["Son Yeonhee"]


def test_a_pending_item_says_which_document_it_came_from(client):
    a = _novel(client, "Origin", [1, 2])
    b = _novel(client, "Origin2", [3, 4],
               pending=[dict(_term("손세연", "Son Yeonhee"), chapter=1)])
    sid = _series(client, "Origin", [a, b])
    _merge(client, sid, dry_run=False)
    items = load_pending(series_mod.series_root() / sid / "glossary_pending.json")
    assert items[0]["project_id"] == b


# ---- the redirect ---------------------------------------------------------


def test_a_member_uses_its_own_glossary_until_the_merge_runs(client):
    # Pointing a member at an empty series glossary the moment it was linked would look
    # exactly like every locked name had been wiped.
    a = _novel(client, "Redir", [1, 2], glossary=[_term("가", "Ga")])
    b = _novel(client, "Redir2", [3, 4], glossary=[_term("가", "Ga")])
    _series(client, "Redir", [a, b])
    cfg = pj.project_config(Config(), pj.get_project(a))
    assert cfg.paths.glossary_json == pj.PROJECTS_DIR / a / "glossary.json"


def test_after_the_merge_both_members_share_one_glossary(client):
    a = _novel(client, "Shared", [1, 2], glossary=[_term("가", "Ga")])
    b = _novel(client, "Shared2", [3, 4], glossary=[_term("나", "Na")])
    sid = _series(client, "Shared", [a, b])
    _merge(client, sid, dry_run=False)
    sdir = series_mod.series_root() / sid
    for pid in (a, b):
        cfg = pj.project_config(Config(), pj.get_project(pid))
        assert cfg.paths.glossary_json == sdir / "glossary.json"
        assert cfg.paths.glossary_pending == sdir / "glossary_pending.json"
    # Both members now see both terms — this is the drift fix, end to end.
    assert len(Glossary.load(sdir / "glossary.json").entries()) == 2


def test_members_of_a_merged_series_share_one_lock(client):
    # glossary_lock keys on the file's parent directory, so moving the glossary into
    # series/<sid>/ makes every member share a lock with no new locking code — the
    # cross-document race that test_glossary_race guards, covered for free.
    a = _novel(client, "Lock", [1, 2], glossary=[_term("가", "Ga")])
    b = _novel(client, "Lock2", [3, 4], glossary=[_term("나", "Na")])
    sid = _series(client, "Lock", [a, b])
    _merge(client, sid, dry_run=False)
    paths = [pj.project_config(Config(), pj.get_project(p)).paths.glossary_json
             for p in (a, b)]
    assert glossary_lock(paths[0]) is glossary_lock(paths[1])


def test_unlinking_puts_every_novel_back_on_its_own_glossary(client):
    # The rollback: each member's glossary.json was never rewritten, so removing the
    # series is the whole undo.
    a = _novel(client, "Undo", [1, 2], glossary=[_term("가", "Ga")])
    b = _novel(client, "Undo2", [3, 4], glossary=[_term("나", "Na")])
    sid = _series(client, "Undo", [a, b])
    _merge(client, sid, dry_run=False)
    client.delete(f"/api/series/{sid}")
    cfg = pj.project_config(Config(), pj.get_project(a))
    assert cfg.paths.glossary_json == pj.PROJECTS_DIR / a / "glossary.json"
    assert [e.english for e in Glossary.load(cfg.paths.glossary_json).entries()] == ["Ga"]


# ---- the dry run ----------------------------------------------------------


def test_a_dry_run_writes_nothing(client):
    a = _novel(client, "Dry", [1, 2], glossary=[_term("백정우", "Baek Jeongwoo")])
    b = _novel(client, "Dry2", [3, 4], glossary=[_term("백정우", "Baek Jeong-u")])
    sid = _series(client, "Dry", [a, b])
    r = _merge(client, sid, dry_run=True)
    assert r["dry_run"] is True and len(r["conflicts"]) == 1
    assert not (series_mod.series_root() / sid / "glossary.json").exists()
    # And the redirect must not have engaged.
    cfg = pj.project_config(Config(), pj.get_project(a))
    assert cfg.paths.glossary_json == pj.PROJECTS_DIR / a / "glossary.json"


def test_the_result_admits_near_duplicates_are_kept(client):
    # Entries filed under DIFFERENT Korean keys for the same concept are invisible to
    # this merge. One real glossary holds one character under three Korean keys,
    # romanized two ways. "0 conflicts" must not be sold as "0 duplicates".
    a = _novel(client, "Near", [1, 2], glossary=[_term("똑헤츠", "Knock-Hertz")])
    b = _novel(client, "Near2", [3, 4], glossary=[_term("똑헤츠 (이웃집)", "Knock-Hertz")])
    r = _merge(client, _series(client, "Near", [a, b]))
    assert r["conflicts"] == []
    assert r["locked"] == 2          # both carried across, untouched
    assert r["near_duplicates_kept"] is True


def test_a_one_member_series_cannot_be_merged(client):
    a = _novel(client, "Solo", [1, 2], glossary=[_term("가", "Ga")])
    sid = client.post("/api/series", json={"name": "Solo", "project_ids": [a]}).json()["id"]
    r = client.post(f"/api/series/{sid}/glossary/merge", json={"dry_run": True})
    assert r.status_code == 400


def test_merge_is_refused_for_an_unknown_series(client):
    assert client.post("/api/series/deadbeefcafe/glossary/merge",
                       json={"dry_run": True}).status_code == 404


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
