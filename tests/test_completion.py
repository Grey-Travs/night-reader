"""Tests for noticing that a novel has ended.

When the source shows its finale ("138화 (완결)", or a closing "<title> 마침"), the novel's
status becomes "completed" without anyone setting it. What has to hold:

* a finished novel is marked when its source is first stored, even when it arrives whole;
* the user's own choice wins afterwards: setting it back to "ongoing" survives reading the
  same source again, while a NEW finale (side stories closed later) is noticed;
* a novel split across documents is marked on its series, including when a finished
  part joins the series after the fact;
* an ongoing novel is left exactly as it was.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_completion.py``).
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
from server import completion  # noqa: E402
from translation_bot.config import Config  # noqa: E402
from translation_bot.docs_extract import Chapter  # noqa: E402

TITLE = "웬 댓 데이 컴즈"
BODY = "그는 천천히 고개를 돌려 나를 바라보았다. " * 12


def _tab(index, heading):
    return Chapter(index=index, title=f"Tab {index}", paragraphs=[
        f"ridibooks.com/books/{3885049000 + index}/view", f"{TITLE} {heading}",
        "4-5 minutes", BODY])


ONGOING = [_tab(1, "1화"), _tab(2, "2화")]
FINISHED = [_tab(1, "137화"), _tab(2, "138화 (완결)")]


@pytest.fixture
def library(monkeypatch, tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    for module in (pj, P):
        monkeypatch.setattr(module, "PROJECTS_DIR", root)
    monkeypatch.setattr(A, "load_global_config", lambda: Config())
    A._chapter_cache.clear()
    A._offline_projects.clear()
    return root


def test_a_finished_novel_is_marked_completed_when_it_arrives(library):
    project = pj.create_text_project("When That Day Comes", FINISHED)
    assert project["status"] == "completed"
    assert project["ending"]["index"] == 2
    assert project["ending"]["signal"] == "heading"


def test_an_ongoing_novel_is_left_alone(library):
    project = pj.create_text_project("Still Going", ONGOING)
    assert "status" not in project and "ending" not in project


def test_setting_it_back_to_ongoing_survives_reading_the_same_source(library):
    pid = pj.create_text_project("When That Day Comes", FINISHED)["id"]
    pj.update_project(pid, status="ongoing")
    pj.cache_source(pid, FINISHED)  # a refresh of the same document
    assert pj.get_project(pid)["status"] == "ongoing"


def test_a_new_finale_is_noticed_again(library):
    pid = pj.create_text_project("When That Day Comes", FINISHED)["id"]
    pj.update_project(pid, status="ongoing")
    pj.cache_source(pid, [*FINISHED, _tab(3, "외전 1화"), _tab(4, "외전 2화 (완결)")])
    project = pj.get_project(pid)
    assert project["status"] == "completed"
    assert (project["ending"]["index"], project["ending"]["kind"]) == (4, "side")


def test_a_series_member_marks_the_series(library):
    first = pj.create_text_project("Novel 1", ONGOING)["id"]
    second = pj.create_text_project("Novel 2", ONGOING)["id"]
    sid = series_mod.create_series("Novel", [first, second])["id"]
    pj.cache_source(second, FINISHED)
    series = series_mod.get_series(sid)
    assert series["status"] == "completed"
    assert series["ending"]["project_id"] == second
    # Not written onto the document too: one novel has one status, on its series.
    assert "status" not in pj.get_project(second)


def test_a_finished_part_joining_its_series_finishes_the_series(library):
    client = TestClient(A.app, base_url="http://localhost", client=("127.0.0.1", 50000))
    first = pj.create_text_project("Novel 1", ONGOING)["id"]
    sid = series_mod.create_series("Novel", [first])["id"]
    second = pj.create_text_project("Novel 2", FINISHED)["id"]
    assert series_mod.get_series(sid).get("status") != "completed"

    res = client.post(f"/api/series/{sid}/members", json={"project_id": second})
    assert res.status_code == 200, res.text
    assert series_mod.get_series(sid)["status"] == "completed"


def test_a_failure_while_checking_never_stops_the_source_being_stored(library, monkeypatch):
    pid = pj.create_text_project("Novel", ONGOING)["id"]

    def boom(*_a, **_k):
        raise RuntimeError("broken")
    monkeypatch.setattr(completion, "record_ending", boom)
    pj.cache_source(pid, FINISHED)
    assert len(pj.load_text_chapters(pid)) == 2


def test_side_stories_numbered_in_their_own_sequence_are_not_duplicates():
    doc = {"members": {"a": {"rows": [
        {"index": 1, "global": 1, "kind": "chapter"},
        {"index": 2, "global": 2, "kind": "chapter"},
        {"index": 3, "global": 1, "kind": "side"},
        {"index": 4, "global": 2, "kind": "side"},
    ]}}}
    totals = series_mod._series_totals(doc)
    assert totals["duplicates"] == [] and totals["gaps"] == []
    assert (totals["first"], totals["last"], totals["total"]) == (1, 2, 2)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
