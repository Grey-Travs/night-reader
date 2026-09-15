"""Tests for the series endpoints: linking split documents and resolving their numbering.

A novel longer than ~100 chapters arrives as several Google Docs. These endpoints group
them in reading order and resolve each chapter's global number. What has to hold:

* ``series_root()`` follows the suite's ``PROJECTS_DIR`` monkeypatch -- a module-level
  constant would have kept writing into the user's real library during tests;
* ``/api/series/suggest`` stays declared before ``/api/series/{sid}``, or FastAPI captures
  "suggest" as a series id and the request silently 404s;
* a resolved number is never silently recomputed, because the offline path cannot read
  chapter text and would quietly produce different answers;
* a document whose own headers restart at 1 is still numbered from its place in the
  series;
* and a novel in no series behaves exactly as it did before.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_series_api.py``).
"""

import json
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

# Long enough, and Korean enough, not to be classified as front/back matter.
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
    return TestClient(A.app, base_url="http://localhost",
                      client=("127.0.0.1", 50000))


def _novel(client, name, numbers):
    """A text novel whose chapters carry source-export headers stating their numbers.

    ``numbers`` is one entry per tab: an int for a chapter that states its number, or
    None for one whose export lacked the header (30-85% coverage is normal in the real
    library). Text projects read from source.json, so this needs no network.
    """
    pid = client.post("/api/projects/text", json={
        "name": name, "text": "seed", "split_mode": "single"}).json()["id"]
    records = []
    for i, n in enumerate(numbers, 1):
        head = f"ridibooks.com/books/{1000 + i}/view\n\n노벨 제목"
        if n is not None:
            head += f" {n}화"
        records.append({"title": f"Tab {i}", "paragraphs": [head, BODY]})
    pdir = pj.PROJECTS_DIR / pid
    (pdir / "source.json").write_text(
        json.dumps(records, ensure_ascii=False), encoding="utf-8")
    project = json.loads((pdir / "project.json").read_text(encoding="utf-8"))
    project["chapter_count"] = len(records)
    (pdir / "project.json").write_text(
        json.dumps(project, ensure_ascii=False), encoding="utf-8")
    A._chapter_cache.pop(pid, None)
    return pid


def _rows(mapping, pid):
    return mapping["members"][pid]["rows"]


def _series(client, name, pids, *, resolve=True):
    sid = client.post("/api/series", json={"name": name, "project_ids": pids}).json()["id"]
    if resolve:
        client.get(f"/api/series/{sid}/mapping")
    return sid


# ---- test isolation -------------------------------------------------------


def test_series_root_follows_the_monkeypatched_library(client, tmp_path):
    # The trap this guards: as a module-level constant computed at import time,
    # SERIES_DIR would ignore the patch and read and write the real library.
    assert series_mod.series_root() == tmp_path / "series"


def test_nothing_is_written_until_a_series_is_created(client, tmp_path):
    assert client.get("/api/series").json() == {"series": []}
    assert not (tmp_path / "series").exists()


# ---- route order ----------------------------------------------------------


def test_suggest_is_not_captured_as_a_series_id(client):
    # /api/series/suggest must be declared before /api/series/{sid}.
    r = client.get("/api/series/suggest")
    assert r.status_code == 200
    assert "groups" in r.json()


# ---- linking --------------------------------------------------------------


def test_suggest_groups_documents_of_one_novel(client):
    _novel(client, "Split Novel", [1, 2, 3])
    _novel(client, "Split Novel2", [4, 5])
    _novel(client, "Something Else", [1, 2])
    groups = client.get("/api/series/suggest").json()["groups"]
    assert len(groups) == 1
    assert len(groups[0]["members"]) == 2


def test_suggest_stops_offering_a_series_once_it_is_linked(client):
    a = _novel(client, "Linked Novel", [1, 2, 3])
    b = _novel(client, "Linked Novel2", [4, 5])
    client.post("/api/series", json={"name": "Linked Novel", "project_ids": [a, b]})
    assert client.get("/api/series/suggest").json()["groups"] == []


def test_create_seeds_each_member_start_chapter(client):
    a = _novel(client, "Test Novel", [1, 2, 3, 4, 5])
    b = _novel(client, "Test Novel2", [6, 7, 8])
    s = client.post("/api/series", json={
        "name": "Test Novel", "project_ids": [a, b]}).json()
    assert [m["project_id"] for m in s["members"]] == [a, b]
    assert [m["start_chapter"] for m in s["members"]] == [1, 6]
    assert s["resolved"] is False


def test_a_project_cannot_belong_to_two_series(client):
    # One set of chapters with two different global numbers would make the numbering --
    # and the glossary redirection -- ambiguous.
    a = _novel(client, "Once Novel", [1, 2])
    b = _novel(client, "Once Novel2", [3, 4])
    client.post("/api/series", json={"name": "Once", "project_ids": [a, b]})
    r = client.post("/api/series", json={"name": "Again", "project_ids": [a]})
    assert r.status_code == 400
    assert "already in a series" in r.json()["detail"]["title"].lower()


def test_unknown_project_is_rejected(client):
    r = client.post("/api/series", json={"name": "X", "project_ids": ["deadbeefcafe"]})
    assert r.status_code == 400


def test_reordering_must_list_exactly_the_current_members(client):
    a = _novel(client, "Order Novel", [1, 2])
    b = _novel(client, "Order Novel2", [3, 4])
    sid = client.post("/api/series", json={
        "name": "Order", "project_ids": [a, b]}).json()["id"]
    assert client.post(f"/api/series/{sid}", json={"members": [b]}).status_code == 400
    s = client.post(f"/api/series/{sid}", json={"members": [b, a]}).json()
    assert [m["project_id"] for m in s["members"]] == [b, a]


def test_deleting_a_series_leaves_the_novels_alone(client):
    a = _novel(client, "Keep Novel", [1, 2])
    b = _novel(client, "Keep Novel2", [3, 4])
    sid = client.post("/api/series", json={
        "name": "Keep", "project_ids": [a, b]}).json()["id"]
    assert client.delete(f"/api/series/{sid}").status_code == 200
    assert client.get(f"/api/series/{sid}").status_code == 404
    for pid in (a, b):
        assert pj.get_project(pid) is not None
        assert client.get(f"/api/projects/{pid}/chapters").status_code == 200


# ---- numbering ------------------------------------------------------------


def test_mapping_numbers_a_sequel_from_its_place_in_the_series(client):
    # THE important case: part 2's own headers restart at 1, so its measured offset is
    # negative. Its position in the series has to win, or the whole document is numbered
    # wrongly.
    a = _novel(client, "Numbered", [1, 2, 3, 4, 5])
    b = _novel(client, "Numbered2", [1, 2, 3])
    sid = client.post("/api/series", json={
        "name": "Numbered", "project_ids": [a, b]}).json()["id"]
    m = client.get(f"/api/series/{sid}/mapping").json()
    assert [r["global"] for r in _rows(m, a)] == [1, 2, 3, 4, 5]
    assert [r["global"] for r in _rows(m, b)] == [6, 7, 8]
    assert (m["first"], m["last"], m["total"]) == (1, 8, 8)
    assert m["gaps"] == [] and m["duplicates"] == []


def test_mapping_keeps_headers_that_are_already_global(client):
    a = _novel(client, "Global", [1, 2, 3])
    b = _novel(client, "Global2", [4, 5])
    sid = client.post("/api/series", json={
        "name": "Global", "project_ids": [a, b]}).json()["id"]
    m = client.get(f"/api/series/{sid}/mapping").json()
    assert [r["global"] for r in _rows(m, b)] == [4, 5]


def test_mapping_fills_tabs_whose_export_lacked_a_header(client):
    a = _novel(client, "Sparse", [1, None, None, 4])
    sid = client.post("/api/series", json={
        "name": "Sparse", "project_ids": [a]}).json()["id"]
    m = client.get(f"/api/series/{sid}/mapping").json()
    rows = _rows(m, a)
    assert [r["global"] for r in rows] == [1, 2, 3, 4]
    assert [r["confidence"] for r in rows] == ["high", "inferred", "inferred", "high"]


def test_a_real_gap_is_reported_across_the_whole_series(client):
    # Gap detection has to span members: a per-document check would never notice that
    # part 2 starts one too late.
    a = _novel(client, "Gappy", [1, 2, 3])
    b = _novel(client, "Gappy2", [5, 6])
    sid = client.post("/api/series", json={
        "name": "Gappy", "project_ids": [a, b]}).json()["id"]
    m = client.get(f"/api/series/{sid}/mapping").json()
    assert m["gaps"] == [4]


def test_a_resolved_number_is_never_silently_recomputed(client):
    # The offline path cannot read chapter text, so re-deriving would quietly produce
    # different answers. Once resolved, the stored mapping is served as-is.
    a = _novel(client, "Frozen", [1, 2, 3])
    sid = client.post("/api/series", json={
        "name": "Frozen", "project_ids": [a]}).json()["id"]
    first = client.get(f"/api/series/{sid}/mapping").json()
    (pj.PROJECTS_DIR / a / "source.json").unlink()
    A._chapter_cache.pop(a, None)
    again = client.get(f"/api/series/{sid}/mapping").json()
    assert [r["global"] for r in _rows(again, a)] == [r["global"] for r in _rows(first, a)]


def test_an_unreadable_member_does_not_blank_the_series(client):
    a = _novel(client, "Partial", [1, 2, 3])
    b = _novel(client, "Partial2", [4, 5])
    sid = client.post("/api/series", json={
        "name": "Partial", "project_ids": [a, b]}).json()["id"]
    client.get(f"/api/series/{sid}/mapping")
    (pj.PROJECTS_DIR / b / "source.json").unlink()
    A._chapter_cache.pop(b, None)
    m = client.get(f"/api/series/{sid}/mapping?refresh=true").json()
    # Member a still resolves; member b keeps the rows it already had.
    assert [r["global"] for r in _rows(m, a)] == [1, 2, 3]
    assert [r["global"] for r in _rows(m, b)] == [4, 5]


# ---- confirming the review table ------------------------------------------


def test_confirming_a_row_marks_it_manual_and_high_confidence(client):
    a = _novel(client, "Review", [1, None, 3])
    sid = client.post("/api/series", json={
        "name": "Review", "project_ids": [a]}).json()["id"]
    client.get(f"/api/series/{sid}/mapping")
    m = client.put(f"/api/series/{sid}/mapping", json={"overrides": [
        {"project_id": a, "index": 2, "global": 99},
    ]}).json()
    row = next(r for r in _rows(m, a) if r["index"] == 2)
    assert row["global"] == 99
    assert row["source"] == "manual" and row["confidence"] == "high"


def test_clearing_a_global_takes_a_row_out_of_the_sequence(client):
    a = _novel(client, "Exclude", [1, 2, 3])
    sid = client.post("/api/series", json={
        "name": "Exclude", "project_ids": [a]}).json()["id"]
    client.get(f"/api/series/{sid}/mapping")
    m = client.put(f"/api/series/{sid}/mapping", json={"overrides": [
        {"project_id": a, "index": 3, "global": None, "kind": "extra",
         "label": "Author note"},
    ]}).json()
    row = next(r for r in _rows(m, a) if r["index"] == 3)
    assert row["global"] is None and row["kind"] == "extra"
    assert m["total"] == 2 and m["last"] == 2
    # The index itself is untouched: it keys state.json and chapter-NNN.md.
    assert [r["index"] for r in _rows(m, a)] == [1, 2, 3]


def test_an_override_survives_a_refresh(client):
    a = _novel(client, "Sticky", [1, 2, 3])
    sid = client.post("/api/series", json={
        "name": "Sticky", "project_ids": [a]}).json()["id"]
    client.get(f"/api/series/{sid}/mapping")
    client.put(f"/api/series/{sid}/mapping", json={"overrides": [
        {"project_id": a, "index": 3, "global": None, "kind": "side"},
    ]})
    m = client.get(f"/api/series/{sid}/mapping").json()
    row = next(r for r in _rows(m, a) if r["index"] == 3)
    assert row["global"] is None and row["kind"] == "side"


def test_confirming_before_resolving_is_refused(client):
    a = _novel(client, "Early", [1, 2])
    sid = client.post("/api/series", json={
        "name": "Early", "project_ids": [a]}).json()["id"]
    r = client.put(f"/api/series/{sid}/mapping", json={"overrides": [
        {"project_id": a, "index": 1, "global": 5}]})
    assert r.status_code == 400


# ---- reading straight across a document boundary --------------------------
#
# The whole point of the feature: a Google Doc holds about 100 tabs, so the story carries
# on in a second document, and the reader has to step from the last chapter of one into
# the first chapter of the next without the user opening anything.


def _detail(client, pid, index):
    return client.get(f"/api/projects/{pid}/chapters/{index}").json()


def test_the_last_chapter_of_a_document_leads_into_the_next_one(client):
    a = _novel(client, "Flow", [1, 2, 3])
    b = _novel(client, "Flow2", [4, 5])
    sid = _series(client, "Flow", [a, b])
    d = _detail(client, a, 3)
    assert d["next"]["project_id"] == b      # a DIFFERENT Google Doc
    assert d["next"]["index"] == 1           # its first tab
    assert d["next"]["global"] == 4          # presented as chapter 4
    assert d["global"] == 3
    assert d["series"]["name"] == "Flow" and d["series"]["part"] == 1


def test_the_first_chapter_of_a_sequel_leads_back(client):
    a = _novel(client, "Back", [1, 2, 3])
    b = _novel(client, "Back2", [4, 5])
    sid = _series(client, "Back", [a, b])
    d = _detail(client, b, 1)
    assert d["prev"]["project_id"] == a and d["prev"]["index"] == 3
    assert d["global"] == 4
    assert d["series"]["part"] == 2


def test_the_ends_of_a_series_have_nowhere_to_go(client):
    a = _novel(client, "Ends", [1, 2])
    b = _novel(client, "Ends2", [3, 4])
    _series(client, "Ends", [a, b])
    assert _detail(client, a, 1)["prev"] is None
    assert _detail(client, b, 2)["next"] is None


def test_stepping_skips_a_tab_that_is_not_a_chapter(client):
    # A duplicate is the same text again and an extra is an author note — turning a page
    # onto either would be a bug. A side story is different: see the next test.
    a = _novel(client, "Skip", [1, 2, 3])
    sid = _series(client, "Skip", [a])
    client.get(f"/api/series/{sid}/mapping")
    client.put(f"/api/series/{sid}/mapping", json={"overrides": [
        {"project_id": a, "index": 2, "global": None, "kind": "extra"}]})
    assert _detail(client, a, 1)["next"]["index"] == 3
    assert _detail(client, a, 3)["prev"]["index"] == 1


def test_stepping_does_not_skip_a_side_story(client):
    # Side stories are real content the reader wants — they just do not take a chapter
    # number. Skipping them would hide 19 chapters of one real novel.
    a = _novel(client, "Side", [1, 2, 3])
    sid = _series(client, "Side", [a])
    client.get(f"/api/series/{sid}/mapping")
    client.put(f"/api/series/{sid}/mapping", json={"overrides": [
        {"project_id": a, "index": 2, "global": None, "kind": "side"}]})
    step = _detail(client, a, 1)["next"]
    assert step["index"] == 2 and step["kind"] == "side"


def test_the_chapter_list_carries_global_numbers(client):
    a = _novel(client, "Listed", [1, 2])
    b = _novel(client, "Listed2", [3, 4])
    sid = _series(client, "Listed", [a, b])
    rows = client.get(f"/api/projects/{b}/chapters").json()
    assert [r["global"] for r in rows["chapters"]] == [3, 4]
    # `number` is what the source header claimed and is left alone — it is not always
    # the global number.
    assert [r["number"] for r in rows["chapters"]] == ["3", "4"]
    assert rows["series"]["id"] == sid and rows["series"]["parts"] == 2


def test_an_unresolved_series_still_reads_one_document_at_a_time(client):
    # Linked but numbering not resolved yet: no global numbers and no cross-document
    # step, but the novel must stay perfectly readable on its own.
    a = _novel(client, "Unres", [1, 2, 3])
    b = _novel(client, "Unres2", [4, 5])
    client.post("/api/series", json={"name": "Unres", "project_ids": [a, b]})
    d = _detail(client, a, 3)
    assert d["next"] is None and d["global"] is None
    assert d["series"]["resolved"] is False
    assert d["translation"] is None or isinstance(d["translation"], str)


# ---- the novels that are in no series -------------------------------------


def test_a_novel_in_no_series_is_unaffected(client):
    pid = _novel(client, "Lonely Novel", [1, 2, 3])
    assert series_mod.series_for_project(pid) is None
    r = client.get(f"/api/projects/{pid}/chapters")
    assert r.status_code == 200
    assert r.json()["total"] == 3


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
