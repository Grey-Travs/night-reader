"""Tests for grouping the Google Docs of one novel back into a series.

Google Docs caps a document at ~100 tabs and this app reads one tab per chapter, so a
long novel arrives as several documents that the library sees as unrelated novels.
Re-grouping them rests entirely on their NAMES -- and the 52 documents on disk name
themselves six different ways. The obvious matcher (strip a trailing digit) gets several
of them wrong, so this pins the real corpus:

* each of the 22 known series must collapse to exactly one key,
* no two different series may collide on one key, and
* the 7 standalone novels must stay standalone.

The expected grouping was derived INDEPENDENTLY of the code under test -- by a unique
English prefix per series, checked against the project list -- so these cannot pass
trivially. Each hard case below is also called out on its own, named for the assumption
it defeats, because when one regresses the failure should say which convention broke.

Runs under pytest (``pytest tests/``) and standalone
(``python tests/test_series_match.py``).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.series import (  # noqa: E402
    normalize_title,
    seed_start_chapters,
    split_title,
    suggest_series,
)

# ---- the real corpus, generated from projects/*/project.json ---------------

SERIES = [
    # Accidental Marriage
    [
        ('Accidental Marriage (어쩌다 메리지)', 100),
        ('Accidental Marriage (어쩌다 메리지)2', 14),
    ],
    # Are You Out Of Your Mind?
    [
        ('Are You Out Of Your Mind?', 100),
        ('Are You Out Of Your Mind?2', 4),
    ],
    # Becoming the Top
    [
        ("Becoming the Top's Older Brother (공의 형이 된다는 건)", 100),
        ("Becoming the Top's Older Brother (공의 형이 된다는 건) 2", 96),
    ],
    # Can Grim Reapers
    [
        ('Can Grim Reapers Be Possessed Too? (저승사자도 빙의가 되나요?)2', 23),
        ('Can Grim Reapers Be Possessed Too? (저승사자도 빙의가 되나요?)', 100),
    ],
    # Collateral Kitty
    [
        ('Collateral Kitty2', 13),
        ('Collateral Kitty', 100),
    ],
    # Deflower
    [
        ('Deflower me if you can 2', 79),
        ('Deflower Me If You Can', 100),
    ],
    # Do Not Pick Up
    [
        ('Do Not Pick Up the Crown Prince Who Became a Frog! (개구리가 된 황태자를 줍지 마세요!)', 100),
        ('Do Not Pick Up the Crown Prince Who Became a Frog!  2', 53),
    ],
    # Even a Fake Awakener
    [
        ('Even a Fake Awakener Has Its Uses (짭성자도 쓸모가 있다)2', 73),
        ('Even a Fake Awakener Has Its Uses (짭성자도 쓸모가 있다)', 100),
    ],
    # Everyone is Suspiciously
    [
        ('Everyone is Suspiciously Targeting Me (모두가 수상할 정도로 나를 노린다)', 100),
        ('Everyone is Suspiciously Targeting Me (모두가 수상할 정도로 나를 노린다)2', 20),
    ],
    # How the Villain Saves
    [
        ('How the Villain Saves the World2', 21),
        ('How the Villain Saves the World', 100),
    ],
    # I am the Convenience Store
    [
        ('I am the Convenience Store Owner Next to the Esper Management Bureau (능력자 관리국 옆 편의점 사장입니다', 100),
        ('I am the Convenience Store Owner Next to the Esper Management Bureau (능력자 관리국 옆 편의점 사장입니다)2', 27),
    ],
    # I Became the Terminally Ill
    [
        ('I Became the Terminally Ill Youngest Son of a Villain Family (악역 가문의 시한부 막내가 되었다) 2', 25),
        ('I Became the Terminally Ill Youngest Son of a Villain Family (악역 가문의 시한부 막내가 되었다)', 100),
    ],
    # I Only Cooked Rice
    [
        ('I Only Cooked Rice, But the Main Characters Confessed to Me (밥만 했는데 주인공들한테 고백받았다)2', 78),
        ('I Only Cooked Rice, But the Main Characters Confessed to Me (밥만 했는데 주인공들한테 고백받았다)', 100),
    ],
    # I Possessed a Character
    [
        ("I Possessed a Character in Some 'Verse' (무슨 버스에 빙의했다)2", 55),
        ("I Possessed a Character in Some 'Verse' (무슨 버스에 빙의했다)", 100),
    ],
    # I Unleashed a Mad Dog
    [
        ('I Unleashed a Mad Dog on the Raid Party That Abandoned Me 2', 13),
        ('I Unleashed a Mad Dog on the Raid Party That Abandoned Me', 100),
    ],
    # I Want to Be an Actor
    [
        ('I Want to Be an Actor, Not a War Hero (전쟁영웅 말고 배우가 하고 싶습니다)', 100),
        ('I Want to Be an Actor, Not a War Hero (전쟁영웅 말고 배우가 하고 싶습니다)2', 69),
    ],
    # My Boyfriend
    [
        ("My Boyfriend's Lies", 100),
        ("My Boyfriend's Lies2", 4),
    ],
    # Paste; Feeding My Guide
    [
        ('Paste; Feeding My Guide2', 56),
        ('Paste; Feeding My Guide (풀칠 ; 내 가이드 입에 풀칠하기)', 100),
    ],
    # The Abandoned Guide
    [
        ('The Abandoned Guide Enjoys an Easy Life (버림받은 가이드는 꿀 빱니다)', 100),
        ('The Abandoned Guide Enjoys an Easy Life pt.2', 49),
    ],
    # The Fluffy Intruder
    [
        ("The Fluffy Intruder of the Demon King's Castle (마왕성의 몽글몽글 침입자) 2", 21),
        ("The Fluffy Intruder of the Demon King's Castle (마왕성의 몽글몽글 침입자)", 100),
    ],
    # This Priest Only Saves
    [
        ('This Priest Only Saves Strong Men (이 사제님은 강한 남자만 살려드립니다)2', 100),
        ('This Priest Only Saves Strong Men (이 사제님은 강한 남자만 살려드립니다)', 100),
        ('This Priest Only Saves Strong Men (이 사제님은 강한 남자만 살려드립니다)3', 24),
    ],
    # Unintentional Transmigration
    [
        ('Unintentional Transmigration Operations 101-124', 34),
        ('Unintentional Transmigration Operations (타의적 빙의활동)', 100),
    ],
]

STANDALONE = [
    ('The Loathsome Scapegoat', 100),
    ('Liking You Would Be the Death of Me', 60),
    ('Cherry Pop!', 74),
    ('A Romantic Salvation Method for a Time-Limited Hunter (시한부 헌터를 위한 낭만적 구원 방법)', 100),
    ('Hard Control', 97),
    ('only chapter 16', 1),
    ('The Villain Cooks Way Too Well', 79),
]

ALL_NAMES = [x for group in SERIES for x in group] + STANDALONE


def _projects(rows=None):
    """The corpus shaped like ``list_projects()`` output."""
    rows = ALL_NAMES if rows is None else rows
    return [
        {
            "id": f"{i:012x}",
            "name": name,
            "chapter_count": count,
            # Distinct and sortable: part 1 of a series usually carries no volume
            # marker at all, so creation order is the last tiebreaker for ordering.
            "created_at": f"2026-01-01T00:{i:02d}:00+00:00",
        }
        for i, (name, count) in enumerate(rows, 1)
    ]


# ---- the corpus as a whole -------------------------------------------------


def test_each_known_series_collapses_to_one_key():
    for group in SERIES:
        keys = {normalize_title(name) for name, _ in group}
        assert len(keys) == 1, f"{[n for n, _ in group]} -> {sorted(keys)}"


def test_different_series_never_collide():
    keys = [normalize_title(group[0][0]) for group in SERIES]
    assert len(set(keys)) == len(SERIES) == 22


def test_standalone_novels_do_not_join_a_series():
    series_keys = {normalize_title(group[0][0]) for group in SERIES}
    for name, _ in STANDALONE:
        assert normalize_title(name) not in series_keys, name


def test_no_real_name_normalizes_to_nothing():
    # Guards the other direction from the matcher: over-eager stripping would silently
    # collapse unrelated novels into one empty key.
    for name, _ in ALL_NAMES:
        assert normalize_title(name), name


def test_suggest_finds_every_series_and_nothing_more():
    groups = suggest_series(_projects())
    assert len(groups) == 22
    assert sorted(len(g["members"]) for g in groups) == [2] * 21 + [3]


# ---- one test per naming convention, named for what it defeats -------------


def test_volume_digit_glued_to_terminal_punctuation():
    assert normalize_title("Are You Out Of Your Mind?") == normalize_title(
        "Are You Out Of Your Mind?2"
    )


def test_double_space_before_the_volume_digit():
    a = "Do Not Pick Up the Crown Prince Who Became a Frog! (\uac1c\uad6c\ub9ac\uac00 \ub41c \ud6c4\ud1a0\uc790)"
    b = "Do Not Pick Up the Crown Prince Who Became a Frog!  2"
    assert normalize_title(a) == normalize_title(b)


def test_pt_prefix_on_the_volume_number():
    a = "The Abandoned Guide Enjoys an Easy Life (\ubc84\ub9bc\ubc1b\uc740 \uac00\uc774\ub4dc)"
    b = "The Abandoned Guide Enjoys an Easy Life pt.2"
    assert normalize_title(a) == normalize_title(b)


def test_case_drift_between_members():
    assert normalize_title("Deflower Me If You Can") == normalize_title(
        "Deflower me if you can 2"
    )


def test_korean_title_present_on_only_one_member():
    a = "Paste; Feeding My Guide (\ud3ec\uce60 ; \ub0b4 \uac00\uc774\ub4dc)"
    b = "Paste; Feeding My Guide2"
    assert normalize_title(a) == normalize_title(b)


def test_unclosed_korean_parenthetical():
    # One real document is missing its closing bracket; its sequel has one.
    a = "I am the Convenience Store Owner (\ub2a5\ub825\uc790 \uad00\ub9ac\uad6d"
    b = "I am the Convenience Store Owner (\ub2a5\ub825\uc790 \uad00\ub9ac\uad6d)2"
    assert normalize_title(a) == normalize_title(b)


def test_english_parenthetical_is_not_stripped():
    # Only the Korean title comes off. "(Side Story)" is part of the title proper, and
    # collapsing it would merge a spin-off into its parent novel.
    assert normalize_title("Some Novel (Side Story)") != normalize_title("Some Novel")


def test_chapter_range_in_the_name_is_read_as_a_start_hint():
    name = "Unintentional Transmigration Operations 101-124"
    _, _, start = split_title(name)
    assert start == 101
    assert normalize_title(name) == normalize_title(
        "Unintentional Transmigration Operations (\ud0c0\uc758\uc801 \ubc19\uc758\ud65c\ub3d9)"
    )


def test_marker_order_does_not_matter():
    # The digit sits outside the Korean parenthetical on one document and glued to its
    # closing bracket on another, so stripping has to loop rather than run once.
    outside = split_title("Novel (\uac00\ub098\ub2e4) 2")
    glued = split_title("Novel (\uac00\ub098\ub2e4)2")
    assert outside[0] == glued[0] == "Novel"
    assert outside[1] == glued[1] == 2


# ---- chapter offsets ------------------------------------------------------


def test_start_chapters_run_continuously_across_members():
    groups = suggest_series(_projects())
    priest = next(g for g in groups if g["name"].startswith("This Priest"))
    assert [m["start_chapter"] for m in priest["members"]] == [1, 101, 201]
    assert priest["total_chapters"] == 224


def test_a_stated_range_overrides_the_running_total():
    # The running total would say 98 here. The document states 101, and a document that
    # states its own start wins -- otherwise one short or padded earlier volume shifts
    # the global numbering of everything after it.
    members = [
        {"chapter_count": 97, "start_hint": None},
        {"chapter_count": 34, "start_hint": 101},
    ]
    seed_start_chapters(members)
    assert [m["start_chapter"] for m in members] == [1, 101]


def test_one_chapter_scratch_document_is_never_grouped():
    rows = [
        ("Cherry Pop!", 74),
        ("Cherry Pop!2", 1),
    ]
    assert suggest_series(_projects(rows)) == []


if __name__ == "__main__":
    import traceback

    failures = 0
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"  ok  {_name}")
            except Exception:
                failures += 1
                print(f"FAIL  {_name}")
                traceback.print_exc()
    print("all passed" if not failures else f"{failures} failed")
    sys.exit(1 if failures else 0)
