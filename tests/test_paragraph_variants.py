"""Tests for per-paragraph rewrite history.

Every regenerate appends a version and the reader picks between them, so nothing is
ever lost. That history lives in its own file rather than in previous/ (one
whole-chapter slot, overwritten on every write) or state.json (the worker's hot path).

What has to hold:
- variants[0] is always the text as it stood the first time the paragraph was
  touched, so reverting to the original is just picking it — and it is never pruned.
- Groups are keyed by a random id, so an edit elsewhere cannot reshuffle history onto
  the wrong paragraph.
- After a whole-chapter edit, groups re-anchor by CONTENT. A group that no longer
  matches anything is marked stale, never silently deleted.
- Pruning keeps the original and whichever version is currently in the chapter.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_paragraph_variants.py``).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import server.variants as V  # noqa: E402

ORIGINAL = "The door slid open."
PID = "abcdef012345"


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(V, "PROJECTS_DIR", tmp_path)
    return tmp_path


def _doc_with_group(paragraph=1, original=ORIGINAL):
    doc = V.new_doc(1)
    return doc, V.new_group(doc, paragraph, original)


# ---- the original is always kept ---------------------------------------------

def test_a_new_group_is_seeded_with_the_current_text():
    _doc, group = _doc_with_group()
    assert group["variants"][0]["kind"] == V.KIND_ORIGINAL
    assert group["variants"][0]["text"] == ORIGINAL
    assert group["current_id"] == "v0", "nothing has been picked yet"


def test_reverting_is_just_picking_the_original():
    _doc, group = _doc_with_group()
    V.add_variant(group, kind=V.KIND_REPHRASE, text="The door opened.")
    group["current_id"] = "v1"
    assert V.current_text(group) == "The door opened."
    group["current_id"] = "v0"
    assert V.current_text(group) == ORIGINAL, "revert and pick are one code path"


def test_adding_a_variant_does_not_make_it_current():
    """Generating never changes the chapter — only the reader's pick does."""
    _doc, group = _doc_with_group()
    V.add_variant(group, kind=V.KIND_RETRANSLATE, text="The door slid wide.")
    assert group["current_id"] == "v0"


def test_variants_get_distinct_ids():
    _doc, group = _doc_with_group()
    V.add_variant(group, kind=V.KIND_REPHRASE, text="one")
    V.add_variant(group, kind=V.KIND_REPHRASE, text="two")
    assert [v["id"] for v in group["variants"]] == ["v0", "v1", "v2"]


def test_prior_texts_feed_the_do_not_repeat_clause():
    _doc, group = _doc_with_group()
    V.add_variant(group, kind=V.KIND_REPHRASE, text="The door opened.")
    assert V.prior_texts(group) == [ORIGINAL, "The door opened."], \
        "without these the model returns the same text every time"


# ---- identity ----------------------------------------------------------------

def test_groups_are_keyed_by_id_not_ordinal():
    doc = V.new_doc(1)
    a = V.new_group(doc, 1, "first")
    b = V.new_group(doc, 2, "second")
    assert a["id"] != b["id"]
    assert V.find_group(doc, b["id"])["original"] == "second"


def test_a_stale_group_is_not_offered_for_its_old_paragraph():
    doc = V.new_doc(1)
    group = V.new_group(doc, 1, "first")
    group["stale"] = True
    assert V.group_for_paragraph(doc, 1) is None


# ---- re-anchoring after an edit elsewhere ------------------------------------

def test_a_group_follows_its_paragraph_when_others_move():
    doc = V.new_doc(1)
    group = V.new_group(doc, 2, "third paragraph")
    # A paragraph was inserted above it by a whole-chapter edit.
    V.relocate(doc, ["new first", "old first", "second", "third paragraph"])
    assert group["paragraph"] == 3 and not group["stale"]


def test_a_group_anchors_on_the_picked_version_not_the_original():
    doc = V.new_doc(1)
    group = V.new_group(doc, 0, ORIGINAL)
    V.add_variant(group, kind=V.KIND_REPHRASE, text="The door opened.")
    group["current_id"] = "v1"
    V.relocate(doc, ["intro", "The door opened."])
    assert group["paragraph"] == 1, "it follows the text actually in the chapter"


def test_a_group_whose_paragraph_vanished_goes_stale_but_is_kept():
    doc = V.new_doc(1)
    group = V.new_group(doc, 0, ORIGINAL)
    V.relocate(doc, ["something else entirely"])
    assert group["stale"] is True
    assert doc["groups"], "history is never deleted automatically"


def test_an_ambiguous_anchor_goes_stale_rather_than_guessing():
    doc = V.new_doc(1)
    group = V.new_group(doc, 0, "Same.")
    V.relocate(doc, ["Same.", "Middle.", "Same."])
    assert group["stale"] is True, "two identical paragraphs must not be guessed between"


def test_relocating_clears_stale_when_the_text_comes_back():
    doc = V.new_doc(1)
    group = V.new_group(doc, 0, ORIGINAL)
    V.relocate(doc, ["gone"])
    assert group["stale"] is True
    V.relocate(doc, ["intro", ORIGINAL])
    assert group["stale"] is False and group["paragraph"] == 1


# ---- pruning -----------------------------------------------------------------

def test_pruning_keeps_the_original_and_the_current_version():
    _doc, group = _doc_with_group()
    for i in range(1, 12):
        V.add_variant(group, kind=V.KIND_REPHRASE, text=f"version {i}")
    group["current_id"] = "v3"

    doc = {"groups": [group]}
    V.prune(doc, max_variants=5)

    ids = [v["id"] for v in group["variants"]]
    assert len(ids) == 5
    assert "v0" in ids, "revert-to-original must survive"
    assert "v3" in ids, "the version in the chapter must survive"


def test_pruning_preserves_order():
    _doc, group = _doc_with_group()
    for i in range(1, 10):
        V.add_variant(group, kind=V.KIND_REPHRASE, text=f"version {i}")
    V.prune({"groups": [group]}, max_variants=4)
    numbers = [int(v["id"][1:]) for v in group["variants"]]
    assert numbers == sorted(numbers)


def test_ids_are_never_reused_after_pruning():
    """The collision that let "Use this" splice the WRONG version's text.

    Ids were f"v{len(variants)}". prune() removes from the middle, so the list
    shrinks while the numbering does not, and the next variant reused a live id.
    find_variant returns the first match, so applying one version wrote another's
    text into the chapter and relocate then marked the group stale.
    """
    _doc, group = _doc_with_group()
    for i in range(1, 14):
        V.add_variant(group, kind=V.KIND_REPHRASE, text=f"version {i}")
    V.prune({"groups": [group]}, max_variants=12)
    V.add_variant(group, kind=V.KIND_REPHRASE, text="after the prune")

    ids = [v["id"] for v in group["variants"]]
    assert len(ids) == len(set(ids)), f"duplicate variant ids: {ids}"


def test_every_variant_resolves_to_its_own_text():
    _doc, group = _doc_with_group()
    for i in range(1, 14):
        V.add_variant(group, kind=V.KIND_REPHRASE, text=f"version {i}")
    V.prune({"groups": [group]}, max_variants=12)
    V.add_variant(group, kind=V.KIND_REPHRASE, text="after the prune")

    for variant in group["variants"]:
        assert V.find_variant(group, variant["id"])["text"] == variant["text"], \
            "picking a version must return that version's text, not another's"


def test_ids_are_not_reused_in_a_group_written_before_next_seq_existed():
    """Old variants.json files on disk have no next_seq — recover a safe floor."""
    group = {
        "id": "deadbeef", "paragraph": 0, "original": ORIGINAL, "current_id": "v0",
        "stale": False,
        "variants": [
            {"id": "v0", "kind": V.KIND_ORIGINAL, "text": ORIGINAL},
            {"id": "v3", "kind": V.KIND_REPHRASE, "text": "kept after an old prune"},
        ],
    }
    V.add_variant(group, kind=V.KIND_REPHRASE, text="new")
    ids = [v["id"] for v in group["variants"]]
    assert len(ids) == len(set(ids)), f"duplicate ids from a legacy group: {ids}"
    assert ids[-1] == "v4", "the new id must clear the highest id already present"


def test_pruning_leaves_a_short_history_alone():
    _doc, group = _doc_with_group()
    V.add_variant(group, kind=V.KIND_REPHRASE, text="one")
    V.prune({"groups": [group]}, max_variants=12)
    assert len(group["variants"]) == 2


def test_pruning_bounds_the_number_of_groups():
    doc = V.new_doc(1)
    for i in range(10):
        V.new_group(doc, i, f"paragraph {i}")
    V.prune(doc, max_groups=4)
    assert len(doc["groups"]) == 4
    assert doc["groups"][-1]["original"] == "paragraph 9", "recent work survives"


# ---- durability --------------------------------------------------------------

def test_history_round_trips_through_disk(store):
    path = V.variants_path(PID, 7, 30)
    with V.mutate_variants(path, 7) as doc:
        group = V.new_group(doc, 3, ORIGINAL)
        V.add_variant(group, kind=V.KIND_REPHRASE, text="문이 열렸다 rephrased")
        gid = group["id"]

    reloaded = V.load_variants(path, 7)
    assert V.find_group(reloaded, gid)["variants"][1]["text"] == "문이 열렸다 rephrased"


def test_the_file_name_mirrors_the_chapter_file(store):
    assert V.variants_path(PID, 7, 300).name == "chapter-007.json"
    assert V.variants_path(PID, 7, 30).name == "chapter-07.json"


def test_a_corrupt_history_reads_as_empty_instead_of_raising(store):
    path = V.variants_path(PID, 1, 10)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert V.load_variants(path, 1)["groups"] == [], \
        "losing edit history must never make a chapter unreadable"


def test_a_missing_history_reads_as_empty(store):
    assert V.load_variants(V.variants_path(PID, 1, 10), 1)["groups"] == []


def test_korean_source_is_stored_readably(store):
    path = V.variants_path(PID, 1, 10)
    with V.mutate_variants(path, 1) as doc:
        V.new_group(doc, 0, ORIGINAL, source_ko="그는 문을 열었다.")
    assert "그는" in path.read_text(encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8"))["groups"][0]["source_ko"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
