"""Putting finished translations back on the tabs they were made from.

A chapter's index is its tab's POSITION, re-derived on every read, while everything the
translation cost money for is filed under that index. So inserting tabs anywhere but the
end renames every chapter after them.

This happened: 10 chapters were added to the FRONT of a 56-tab document, and all 56
finished translations ended up one tab short of their own source. Every one then read as
stale, and the library offered to re-translate 56 chapters that were already paid for.

The repair must never be told an offset -- a wrong one files every chapter under a wrong
number. It reads the mapping off the ``source_hash`` the app already records, so it is
evidence rather than arithmetic, and it refuses whenever the evidence is not conclusive.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.realign_chapter_indices import (  # noqa: E402
    apply_moves,
    apply_state,
    plan,
    verify,
)
from translation_bot.text_source import records_to_chapters  # noqa: E402

BODY = "그는 문을 열었다. " * 20


def _records(numbers):
    return [{"title": f"Tab {i}",
             "paragraphs": [f"ridibooks.com/books/{1000 + n}/view\n\n제목 {n}화",
                            f"{BODY}{n}"]}
            for i, n in enumerate(numbers, 1)]


def _hashes(records):
    return {c.index: c.metrics.content_hash for c in records_to_chapters(records)}


def _project(tmp_path, source_numbers, translated_numbers):
    """A project whose source holds ``source_numbers`` and whose finished translations
    were made from ``translated_numbers``, filed at indices 1..N in that order."""
    pdir = tmp_path / "proj"
    for sub in ("chapters", "previous", "audit"):
        (pdir / sub).mkdir(parents=True)
    records = _records(source_numbers)
    (pdir / "source.json").write_text(json.dumps(records, ensure_ascii=False),
                                      encoding="utf-8")

    # The hashes the translations were recorded against: whatever those chapters hashed
    # to, which we get by hashing a document containing exactly them.
    recorded = _hashes(_records(translated_numbers))
    chapters = {}
    for i, _n in enumerate(translated_numbers, 1):
        chapters[str(i)] = {"status": "validated", "source_hash": recorded[i]}
        for sub in ("chapters", "previous", "audit"):
            (pdir / sub / f"chapter-{i:02d}.md").write_text(f"english {i}",
                                                            encoding="utf-8")
    (pdir / "state.json").write_text(json.dumps({"chapters": chapters}), encoding="utf-8")
    return pdir


def test_ten_chapters_inserted_at_the_front_shift_everything_by_ten(tmp_path):
    # The real shape: translations made from 41..96, then 31..40 added above them.
    pdir = _project(tmp_path, list(range(31, 97)), list(range(41, 97)))
    moves, problems = plan(pdir)
    assert problems == []
    assert moves == {i: i + 10 for i in range(1, 57)}


def test_the_files_and_the_records_move_together(tmp_path):
    pdir = _project(tmp_path, list(range(31, 97)), list(range(41, 97)))
    moves, _ = plan(pdir)
    before = (pdir / "chapters" / "chapter-01.md").read_text(encoding="utf-8")

    apply_moves(pdir, moves, 2)
    apply_state(pdir, moves)

    assert verify(pdir) == []
    for sub in ("chapters", "previous", "audit"):
        names = sorted(f.name for f in (pdir / sub).glob("chapter-*.md"))
        assert names[0] == "chapter-11.md" and names[-1] == "chapter-66.md"
        assert len(names) == 56
    # the text itself moved, not merely the name
    assert (pdir / "chapters" / "chapter-11.md").read_text(encoding="utf-8") == before
    state = json.loads((pdir / "state.json").read_text(encoding="utf-8"))["chapters"]
    assert sorted(int(k) for k in state) == list(range(11, 67))
    assert {v["status"] for v in state.values()} == {"validated"}


def test_it_handles_a_rearrangement_that_is_not_a_shift(tmp_path):
    # Not just "everything moved by N": a block inserted in the MIDDLE moves only the
    # tail, and an offset would be wrong for half the document.
    pdir = _project(tmp_path, [1, 2, 90, 91, 3, 4], [1, 2, 3, 4])
    moves, problems = plan(pdir)
    assert problems == []
    assert moves == {3: 5, 4: 6}          # 1 and 2 stay put
    apply_moves(pdir, moves, 2)
    apply_state(pdir, moves)
    assert verify(pdir) == []


def test_a_translation_whose_source_is_gone_is_left_alone_and_reported(tmp_path):
    # Its tab was deleted or edited. Filing it against some other tab would attach it to
    # text it was never made from, so it keeps its index and is named in the report.
    pdir = _project(tmp_path, [1, 2], [1, 2, 3])
    moves, problems = plan(pdir)
    assert moves == {}
    assert any("not in the document" in p for p in problems)


def test_a_source_appearing_twice_is_refused_rather_than_guessed(tmp_path):
    # Two tabs holding identical text: "where does this translation belong" has two
    # answers, and picking one is a coin flip over a paid translation.
    pdir = _project(tmp_path, [7, 7], [7])
    moves, problems = plan(pdir)
    assert moves == {}
    assert any("ambiguous" in p for p in problems)


def test_an_already_aligned_project_is_a_no_op(tmp_path):
    pdir = _project(tmp_path, [1, 2, 3], [1, 2, 3])
    moves, problems = plan(pdir)
    assert moves == {} and problems == []


def test_nothing_is_lost_when_two_chapters_swap_places(tmp_path):
    # The case a naive in-place rename cannot do: 1 and 2 exchange indices, so either
    # order overwrites one of them. Staging is what makes it safe.
    pdir = _project(tmp_path, [2, 1], [1, 2])
    moves, problems = plan(pdir)
    assert problems == [] and moves == {1: 2, 2: 1}
    one = (pdir / "chapters" / "chapter-01.md").read_text(encoding="utf-8")
    two = (pdir / "chapters" / "chapter-02.md").read_text(encoding="utf-8")
    apply_moves(pdir, moves, 2)
    apply_state(pdir, moves)
    assert (pdir / "chapters" / "chapter-02.md").read_text(encoding="utf-8") == one
    assert (pdir / "chapters" / "chapter-01.md").read_text(encoding="utf-8") == two
    assert verify(pdir) == []
