"""End-to-end tests for rewriting one paragraph of a finished chapter.

The model is stubbed throughout — these cover the plumbing, and running real
rewrites from a test would spend the user's plan allowance.

What has to hold:
- Generating writes NOTHING to the chapter. Only the reader's pick does. That is what
  removes the file race and lets generation run inline instead of queueing.
- An address that no longer matches is a clean 409, never a wrong overwrite.
- Applying preserves the chapter's paragraph count, so the validator's paragraph
  check can't be tripped by an edit.
- Applying does NOT touch source_hash (that fingerprints the Korean, which didn't
  change) and does NOT clear a needs-review flag (that stays "Mark as fine"'s job).
- Applying does NOT consume the single previous/ snapshot — paragraph history has its
  own storage, and burning previous/ would throw away the pre-translation copy.
- Rephrase keeps working where retranslate can't: no Korean, or no reliable alignment.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_paragraph_endpoints.py``).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server.app as A  # noqa: E402
import server.pages as P  # noqa: E402
import server.projects as pj  # noqa: E402
import server.variants as V  # noqa: E402
from translation_bot import state as state_mod  # noqa: E402
from translation_bot.config import Config  # noqa: E402
from translation_bot.state import State  # noqa: E402
from translation_bot.translator import Translator  # noqa: E402

KOREAN = "첫 번째 문단이다.\n\n두 번째 문단이다.\n\n세 번째 문단이다."
ENGLISH = "The first paragraph.\n\nThe second paragraph.\n\nThe third paragraph.\n"

REWRITE = "A completely rewritten second paragraph."


@pytest.fixture
def client(monkeypatch, tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    for module in (pj, P, V):
        monkeypatch.setattr(module, "PROJECTS_DIR", root)
    monkeypatch.setattr(A, "load_global_config", lambda: Config())
    A._chapter_cache.clear()
    A._offline_projects.clear()
    A._jobs.clear()
    A._active_job_by_project.clear()

    # Never let a test reach the real model.
    def _stub(self, **kw):
        return REWRITE, {"input_tokens": 10, "output_tokens": 5}, 0.001
    monkeypatch.setattr(Translator, "retranslate_paragraph", _stub)
    monkeypatch.setattr(Translator, "rephrase_paragraph", _stub)

    return TestClient(A.app, base_url="http://localhost")


@pytest.fixture
def novel(client):
    """A one-chapter novel with Korean source and a finished English translation."""
    pid = client.post("/api/projects/text", json={
        "name": "Test novel", "text": KOREAN, "split_mode": "single"}).json()["id"]
    cfg = pj.project_config(Config(), pj.get_project(pid))
    cfg.paths.output_dir.mkdir(parents=True, exist_ok=True)
    (cfg.paths.output_dir / "chapter-01.md").write_text(ENGLISH, encoding="utf-8")
    state = State.load(cfg.paths.state_file)
    state.update(1, status=state_mod.STATUS_VALIDATED, source_hash="original-hash")
    state.save(cfg.paths.state_file)
    return pid, cfg


def _blocks(cfg):
    return (cfg.paths.output_dir / "chapter-01.md").read_text(encoding="utf-8")


def _ref(paragraph=1, expected="The second paragraph.", **extra):
    return {"paragraph": paragraph, "expected_text": expected, **extra}


def _rephrase(client, pid, **extra):
    return client.post(f"/api/projects/{pid}/chapters/1/paragraph/rephrase",
                       json=_ref(**extra))


# ---- reading the history -----------------------------------------------------

def test_a_fresh_chapter_has_no_history(client, novel):
    pid, _cfg = novel
    res = client.get(f"/api/projects/{pid}/chapters/1/variants").json()
    assert res["groups"] == [] and res["paragraph_count"] == 3


def test_reading_the_history_writes_nothing_to_disk(client, novel):
    """It is a GET. It used to enter mutate_variants, which saves on exit, so simply
    opening a chapter created and rewrote variants/chapter-NN.json — mkdir, serialise,
    fsync, replace — even for a chapter that had never been rewritten."""
    pid, _cfg = novel
    path = V.variants_path(pid, 1, 1)

    assert client.get(f"/api/projects/{pid}/chapters/1/variants").status_code == 200
    assert not path.exists(), "reading paragraph history must not create a file"

    # And when history DOES exist, reading must not rewrite it.
    _rephrase(client, pid)
    assert path.exists()
    before = path.read_bytes()
    client.get(f"/api/projects/{pid}/chapters/1/variants")
    assert path.read_bytes() == before, "a GET must leave the file byte-identical"


def test_retranslate_is_available_when_the_korean_lines_up(client, novel):
    pid, _cfg = novel
    res = client.get(f"/api/projects/{pid}/chapters/1/variants").json()
    assert res["retranslate_available"] is True, res["retranslate_reason"]


def test_the_korean_window_is_returned_for_a_paragraph(client, novel):
    pid, _cfg = novel
    res = client.post(f"/api/projects/{pid}/chapters/1/paragraph/source", json=_ref()).json()
    assert res["available"] is True
    assert res["korean"][res["focus"]] == "두 번째 문단이다.", \
        "the focused Korean must be the matching paragraph"
    assert len(res["korean"]) > 1, "a window, never a bare point"


# ---- generating --------------------------------------------------------------

def test_generating_does_not_touch_the_chapter(client, novel):
    """The property that removes the file race and lets this run inline."""
    pid, cfg = novel
    before = _blocks(cfg)
    assert _rephrase(client, pid).status_code == 200
    assert _blocks(cfg) == before, "generating must never write to the chapter"


def test_a_generated_version_is_stored_but_not_current(client, novel):
    pid, _cfg = novel
    res = _rephrase(client, pid, instruction="less stiff").json()
    assert res["ok"] is True
    group = res["group"]
    assert group["current_id"] == "v0", "nothing changes until the reader picks"
    assert [v["kind"] for v in group["variants"]] == ["original", "rephrase"]
    assert group["variants"][0]["text"] == "The second paragraph.", \
        "the original is kept so revert always works"


def test_the_instruction_is_recorded_with_the_version(client, novel):
    pid, _cfg = novel
    res = _rephrase(client, pid, instruction="less stiff").json()
    assert res["group"]["variants"][1]["instruction"] == "less stiff"


def test_generating_twice_appends_rather_than_replacing(client, novel):
    pid, _cfg = novel
    first = _rephrase(client, pid).json()
    second = _rephrase(client, pid, group_id=first["group"]["id"]).json()
    assert len(second["group"]["variants"]) == 3, "every attempt is kept"


def test_the_attempt_is_billed_even_when_nothing_is_kept(client, novel):
    pid, cfg = novel
    _rephrase(client, pid)
    rec = State.load(cfg.paths.state_file).get(1)
    assert rec["cost_usd"] > 0, "the plan was used, so the usage must be recorded"


def test_a_stale_address_is_refused(client, novel):
    """The paragraph changed underneath — never overwrite the wrong one."""
    pid, _cfg = novel
    res = _rephrase(client, pid, expected="Text that is not in this chapter.")
    assert res.status_code == 409


def test_an_unusable_reply_is_reported_not_raised(client, novel, monkeypatch):
    """A malformed model reply is a normal outcome, not an error dialog."""
    pid, _cfg = novel
    monkeypatch.setattr(Translator, "rephrase_paragraph",
                        lambda self, **kw: ("First half.\n\nSecond half.", {}, 0.0))
    res = _rephrase(client, pid)
    assert res.status_code == 200
    assert res.json()["ok"] is False
    assert any("paragraphs" in r for r in res.json()["reasons"])


# ---- applying ----------------------------------------------------------------

def _apply_first_rewrite(client, pid):
    made = _rephrase(client, pid).json()
    return client.post(f"/api/projects/{pid}/chapters/1/paragraph/apply",
                       json={"group_id": made["group"]["id"], "variant_id": "v1"})


def test_applying_replaces_only_that_paragraph(client, novel):
    pid, cfg = novel
    res = _apply_first_rewrite(client, pid)
    assert res.status_code == 200, res.text
    text = _blocks(cfg)
    assert REWRITE in text
    assert "The first paragraph." in text and "The third paragraph." in text


def test_applying_preserves_the_paragraph_count(client, novel):
    pid, cfg = novel
    _apply_first_rewrite(client, pid)
    from translation_bot.paragraphs import split_blocks
    assert len(split_blocks(_blocks(cfg))) == 3, \
        "the validator's paragraph check must not be trippable by an edit"


def test_applying_returns_the_whole_chapter_for_an_in_place_update(client, novel):
    """The reader patches its state instead of reloading, so the scroll doesn't jump."""
    pid, cfg = novel
    body = _apply_first_rewrite(client, pid).json()
    assert body["translation"] == _blocks(cfg)


def test_applying_does_not_touch_the_source_hash(client, novel):
    """It fingerprints the KOREAN, which didn't change. Setting it would mark a
    chapter whose source has since moved on as already done."""
    pid, cfg = novel
    _apply_first_rewrite(client, pid)
    assert State.load(cfg.paths.state_file).get(1)["source_hash"] == "original-hash"


def test_applying_marks_the_chapter_as_hand_edited(client, novel):
    pid, cfg = novel
    _apply_first_rewrite(client, pid)
    assert State.load(cfg.paths.state_file).get(1)["manual_edit"] is True


def test_applying_does_not_clear_a_needs_review_flag(client, novel):
    """Clearing a review flag stays 'Mark as fine''s job."""
    pid, cfg = novel
    state = State.load(cfg.paths.state_file)
    state.update(1, status=state_mod.STATUS_NEEDS_REVIEW, failures=[{"kind": "length"}])
    state.save(cfg.paths.state_file)

    _apply_first_rewrite(client, pid)
    rec = State.load(cfg.paths.state_file).get(1)
    assert rec["status"] == state_mod.STATUS_NEEDS_REVIEW
    assert rec["failures"], "the reason it was flagged must survive"


def test_applying_does_not_consume_the_previous_snapshot(client, novel):
    """previous/ holds ONE whole-chapter copy. Paragraph history lives elsewhere, so
    picking versions must not burn through it."""
    pid, cfg = novel
    prev = cfg.paths.output_dir.parent / "previous"
    _apply_first_rewrite(client, pid)
    assert not (prev / "chapter-01.md").exists(), \
        "a paragraph pick must not overwrite the pre-translation snapshot"


def test_picking_the_original_reverts(client, novel):
    pid, cfg = novel
    made = _rephrase(client, pid).json()
    gid = made["group"]["id"]
    client.post(f"/api/projects/{pid}/chapters/1/paragraph/apply",
                json={"group_id": gid, "variant_id": "v1"})
    assert REWRITE in _blocks(cfg)

    res = client.post(f"/api/projects/{pid}/chapters/1/paragraph/apply",
                      json={"group_id": gid, "variant_id": "v0"})
    assert res.status_code == 200
    assert _blocks(cfg) == ENGLISH, "reverting restores the chapter exactly"


def test_applying_an_unknown_version_is_a_clean_404(client, novel):
    pid, _cfg = novel
    made = _rephrase(client, pid).json()
    res = client.post(f"/api/projects/{pid}/chapters/1/paragraph/apply",
                      json={"group_id": made["group"]["id"], "variant_id": "v99"})
    assert res.status_code == 404


# ---- when the Korean isn't usable --------------------------------------------

def test_rephrase_still_works_on_a_saved_copy_novel(client, novel):
    """No Korean available — rephrase is English-only, so it must degrade gracefully."""
    pid, _cfg = novel
    A._offline_projects.add(pid)
    try:
        assert _rephrase(client, pid).status_code == 200
    finally:
        A._offline_projects.discard(pid)


def test_retranslate_is_refused_with_a_reason_on_a_saved_copy_novel(client, novel):
    pid, _cfg = novel
    A._offline_projects.add(pid)
    try:
        res = client.post(f"/api/projects/{pid}/chapters/1/paragraph/retranslate",
                          json=_ref())
        assert res.status_code == 400
        assert "saved copy" in res.text, "the reason must be shown, not just a refusal"

        listing = client.get(f"/api/projects/{pid}/chapters/1/variants").json()
        assert listing["retranslate_available"] is False
        assert listing["retranslate_reason"], "the UI needs something to put in a tooltip"
    finally:
        A._offline_projects.discard(pid)


# ---- discarding --------------------------------------------------------------

def test_a_version_can_be_discarded(client, novel):
    pid, _cfg = novel
    made = _rephrase(client, pid).json()
    res = client.post(f"/api/projects/{pid}/chapters/1/paragraph/discard",
                      json={"group_id": made["group"]["id"], "variant_id": "v1"})
    assert res.status_code == 200
    assert len(res.json()["groups"][0]["variants"]) == 1


def test_the_original_cannot_be_discarded(client, novel):
    """It is revert-to-original; losing it would make the edit irreversible."""
    pid, _cfg = novel
    made = _rephrase(client, pid).json()
    res = client.post(f"/api/projects/{pid}/chapters/1/paragraph/discard",
                      json={"group_id": made["group"]["id"], "variant_id": "v0"})
    assert res.status_code == 400


def test_the_version_in_the_chapter_cannot_be_discarded(client, novel):
    pid, _cfg = novel
    made = _rephrase(client, pid).json()
    gid = made["group"]["id"]
    client.post(f"/api/projects/{pid}/chapters/1/paragraph/apply",
                json={"group_id": gid, "variant_id": "v1"})
    res = client.post(f"/api/projects/{pid}/chapters/1/paragraph/discard",
                      json={"group_id": gid, "variant_id": "v1"})
    assert res.status_code == 400


def test_a_whole_group_can_be_discarded(client, novel):
    pid, _cfg = novel
    made = _rephrase(client, pid).json()
    res = client.post(f"/api/projects/{pid}/chapters/1/paragraph/discard",
                      json={"group_id": made["group"]["id"]})
    assert res.json()["groups"] == []


# ---- history follows the text ------------------------------------------------

def test_history_re_anchors_after_a_whole_chapter_edit(client, novel):
    """A hand edit that inserts a paragraph must not leave history on the wrong one."""
    pid, cfg = novel
    made = _rephrase(client, pid).json()
    gid = made["group"]["id"]

    moved = "A brand new opening.\n\n" + ENGLISH
    client.put(f"/api/projects/{pid}/chapters/1", json={"translation": moved})

    listing = client.get(f"/api/projects/{pid}/chapters/1/variants").json()
    group = next(g for g in listing["groups"] if g["id"] == gid)
    assert group["paragraph"] == 2, "the group followed its paragraph down one"
    assert group["stale"] is False


def test_history_goes_stale_but_survives_when_its_paragraph_vanishes(client, novel):
    pid, _cfg = novel
    made = _rephrase(client, pid).json()
    gid = made["group"]["id"]

    client.put(f"/api/projects/{pid}/chapters/1",
               json={"translation": "Only one paragraph now."})

    listing = client.get(f"/api/projects/{pid}/chapters/1/variants").json()
    group = next(g for g in listing["groups"] if g["id"] == gid)
    assert group["stale"] is True
    assert len(group["variants"]) == 2, "history is kept for the reader to discard"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
