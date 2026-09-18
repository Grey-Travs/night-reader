"""Tests for the ongoing / completed / hiatus label on a novel.

The label is cosmetic -- nothing in the translation or posting pipeline reads it -- so
what these tests pin down is not behaviour but *agreement*. A novel split across four
Google Docs shows four cards on the shelf, and the one thing that must never happen is
those four cards disagreeing about whether the story is finished. So the status of a
series member is the SERIES' status, on the way out (``project_summary``) and on the way
back in (``POST /api/projects/{pid}``), and a standalone novel keeps its own.

The other thing worth pinning: an unknown status is refused rather than stored. A stored
typo would show as a blank pill and match no filter, which reads as data loss.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_novel_status.py``).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server.app as A  # noqa: E402
import server.pages as P  # noqa: E402
import server.projects as pj  # noqa: E402
import server.series as series_mod  # noqa: E402
from translation_bot.config import Config  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    for module in (pj, P):
        monkeypatch.setattr(module, "PROJECTS_DIR", root)
    monkeypatch.setattr(A, "load_global_config", lambda: Config())
    A._chapter_cache.clear()
    series_mod._invalidate_index()
    return TestClient(A.app, base_url="http://localhost",
                      client=("127.0.0.1", 50000))


def _novel(client, name):
    return client.post("/api/projects/text", json={
        "name": name, "text": "seed", "split_mode": "single"}).json()["id"]


def _shelf(client):
    """The library shelf, keyed by project id -- what the cards actually render from."""
    return {p["id"]: p for p in client.get("/api/projects").json()["projects"]}


# ---- the default ----------------------------------------------------------


def test_a_new_novel_is_ongoing_without_anything_being_written(client, tmp_path):
    pid = _novel(client, "Fresh")
    assert _shelf(client)[pid]["status"] == "ongoing"
    # The default is computed, not stored: every novel that existed before this feature
    # has no status key at all, and they must not need a migration to show up correctly.
    import json
    stored = json.loads((tmp_path / "projects" / pid / "project.json").read_text("utf-8"))
    assert "status" not in stored


# ---- a standalone novel keeps its own --------------------------------------


def test_a_standalone_novel_stores_its_own_status(client):
    pid = _novel(client, "Alone")
    out = client.post(f"/api/projects/{pid}", json={"status": "completed"}).json()
    assert out["status"] == "completed"
    assert out["status_from"] == "novel"
    assert out["series_id"] is None
    assert _shelf(client)[pid]["status"] == "completed"


def test_status_and_archived_are_different_things(client):
    # Worth asserting because "completed" and "archived" sound alike and the app already
    # had the second one. Archiving hides a novel from the shelf; completing it is the
    # opposite -- a finished novel is the kind you most want to still see.
    pid = _novel(client, "Both")
    client.post(f"/api/projects/{pid}", json={"status": "completed"})
    row = _shelf(client)[pid]
    assert row["status"] == "completed"
    assert not row.get("archived")


# ---- a bad value is refused, not stored ------------------------------------


@pytest.mark.parametrize("bad", ["finished", "", "ONGOING!", "hiatus2"])
def test_an_unknown_status_is_refused_and_changes_nothing(client, bad):
    pid = _novel(client, "Picky")
    client.post(f"/api/projects/{pid}", json={"status": "hiatus"})
    assert client.post(f"/api/projects/{pid}", json={"status": bad}).status_code == 400
    assert _shelf(client)[pid]["status"] == "hiatus"


def test_status_is_normalised_rather_than_stored_as_typed(client):
    pid = _novel(client, "Shouty")
    assert client.post(
        f"/api/projects/{pid}", json={"status": "  Completed "}).json()["status"] == "completed"


def test_an_unknown_status_on_a_series_is_refused(client):
    a = _novel(client, "Series bad")
    sid = client.post("/api/series", json={"name": "S", "project_ids": [a]}).json()["id"]
    assert client.post(f"/api/series/{sid}", json={"status": "cancelled"}).status_code == 400
    assert client.get(f"/api/series/{sid}").json()["status"] == "ongoing"


# ---- a multi-document novel is ONE novel -----------------------------------


def test_every_member_of_a_series_reports_the_series_status(client):
    a, b, c = (_novel(client, n) for n in ("Priest", "Priest 2", "Priest 3"))
    sid = client.post("/api/series", json={
        "name": "Priest", "project_ids": [a, b, c]}).json()["id"]
    client.post(f"/api/series/{sid}", json={"status": "completed"})

    shelf = _shelf(client)
    assert [shelf[p]["status"] for p in (a, b, c)] == ["completed"] * 3
    assert [shelf[p]["status_from"] for p in (a, b, c)] == ["series"] * 3
    assert [shelf[p]["series_id"] for p in (a, b, c)] == [sid] * 3
    assert shelf[a]["series_name"] == "Priest"


def test_setting_status_on_one_member_sets_it_for_the_whole_novel(client):
    # The card the user clicks is a document's card, but the thing they mean is the novel.
    # Writing it onto the document would leave the other three cards saying "ongoing".
    a, b = _novel(client, "Mad Dog"), _novel(client, "Mad Dog 2")
    sid = client.post("/api/series", json={
        "name": "Mad Dog", "project_ids": [a, b]}).json()["id"]

    client.post(f"/api/projects/{b}", json={"status": "hiatus"})

    shelf = _shelf(client)
    assert shelf[a]["status"] == "hiatus"
    assert shelf[b]["status"] == "hiatus"
    assert client.get(f"/api/series/{sid}").json()["status"] == "hiatus"


def test_a_members_own_stale_status_never_wins_over_the_series(client):
    # A novel labelled while standalone, then linked into a series. The per-project value
    # stays on disk (nothing deletes it, so unlinking can restore it) and must lose.
    a, b = _novel(client, "Kitty"), _novel(client, "Kitty 2")
    client.post(f"/api/projects/{a}", json={"status": "completed"})
    sid = client.post("/api/series", json={
        "name": "Kitty", "project_ids": [a, b]}).json()["id"]

    assert _shelf(client)[a]["status"] == "ongoing"  # the series' default, not "completed"
    client.post(f"/api/series/{sid}", json={"status": "hiatus"})
    assert _shelf(client)[a]["status"] == "hiatus"


def test_renaming_a_series_does_not_disturb_its_status(client):
    # `update_series_route` patches fields one at a time; a rename that dropped the
    # status would be the easy regression here.
    a = _novel(client, "Rename me")
    sid = client.post("/api/series", json={"name": "Old", "project_ids": [a]}).json()["id"]
    client.post(f"/api/series/{sid}", json={"status": "completed"})
    out = client.post(f"/api/series/{sid}", json={"name": "New"}).json()
    assert out["name"] == "New"
    assert out["status"] == "completed"


def test_status_survives_alongside_a_normal_project_edit(client):
    pid = _novel(client, "Edit me")
    client.post(f"/api/projects/{pid}", json={"status": "hiatus"})
    out = client.post(f"/api/projects/{pid}", json={"name": "Edited"}).json()
    assert out["name"] == "Edited"
    assert out["status"] == "hiatus"


def test_setting_only_a_status_on_a_member_does_not_blank_the_project(client):
    # The member branch pops "status" out of the field dict, which can leave it empty --
    # the update must then be skipped rather than called with nothing and losing the name.
    a, b = _novel(client, "Keep"), _novel(client, "Keep 2")
    client.post("/api/series", json={"name": "Keep", "project_ids": [a, b]})
    out = client.post(f"/api/projects/{a}", json={"status": "completed"}).json()
    assert out["name"] == "Keep"
    assert out["status"] == "completed"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
