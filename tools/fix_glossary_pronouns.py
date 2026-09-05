#!/usr/bin/env python
"""Correct glossary pronoun pins that the Korean source contradicts.

A ``pronoun`` in glossary.json is authoritative: it is injected into the translation
prompt, so a wrong pin makes the model render that character as the wrong gender in every
chapter, consistently and silently. That is worse than a wrong sentence — nothing in the
app disagrees with it, because the chapter check measures the prose against the same bad
pin and finds them in perfect agreement.

Each correction below was confirmed against the Korean source twice: once by an auditor
that found the evidence, once by an independent checker that had to locate its own
evidence rather than trust the first.

Dry run by default; ``--apply`` writes, backing up each glossary first.

    python tools/fix_glossary_pronouns.py
    python tools/fix_glossary_pronouns.py --apply
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

PROJECTS = pathlib.Path(__file__).resolve().parent.parent / "projects"

# (project, english, korean, from, to, note replacing the wrong one, evidence)
CORRECTIONS = [
    # 관악구도지사 — a muscular gym-rat WOMAN whose in-game avatar is an orc in a pink
    # princess dress. The avatar is the joke that made the glossary read her as male.
    ("e781039ee87d", "District Governor", "도지사", "he", "she",
     "Female. Guild member; muscular, plays a heavyweight avatar. Korean: 여자/그녀, "
     "addressed 누님, self-refers 언니/누나.",
     "audit/chapter-08.md:150 누님 · chapter-09.md:214,270 언니 · chapter-08.md:74 오빠/동생"),
    ("e781039ee87d", "GwanakDistrictGovernor", "", "he", "she",
     "Female. Same character as 도지사 (long form of the handle).",
     "audit/chapter-09.md:200/204 uses 관악구도지사 and 도지사 for one continuous action"),
    ("f02c5ab4d190", "District Governor", "도지사", "he", "she",
     "Female. Guild member; muscular. Korean: 여자/그녀, addressed 누님, self-refers 언니/누나.",
     "audit/chapter-009.md:171 걸걸한 여자의 목소리… 그녀가 말할 때마다 · chapter-083.md:133 "
     "내가 여자라고 봐줄 것 같아? · chapter-083.md:23 근육녀"),
    ("f02c5ab4d190", "GwanakDistrictGovernor", "", "he", "she",
     "Female. Same character as 도지사 (long form of the handle).",
     "audit/chapter-010.md:113 누나라고 불러! · chapter-011.md:72, 017.md:222 오빠"),
    # 천천 — introduced as 남자 in the same scene where he gives his name.
    ("8285a4d6e034", "Chenchen", "천천", "she", "he",
     "Male. Guide from the Shanghai branch, paired with Linhao.",
     "audit/chapter-065.md:121 그에게 말을 걸었던 남자는… then :123 난 천천이라고 해"),
    ("97e1960019a8", "Chenchen", "천천", "she", "he",
     "Male. Guide from the Shanghai branch, paired with Linhao.",
     "8285a4d6e034/audit/chapter-065.md:121 남자 · :123 self-introduction as 천천"),
    # 시윤 — pinned "they" while the same person's other two entries said "he", so the
    # translator used singular they for him across fifteen chapters.
    ("8285a4d6e034", "Shiyoon", "시윤", "they", "he",
     "Male. Same person as Lee Shiyun/Lee Siyoon (이시윤); Chinese name 리스윈.",
     "audit/chapter-081.md:80 시윤을… 그에게 · :84 그의 말과는 달리 그는 · :82 리스윈 = 이시윤"),
    ("97e1960019a8", "Shiyoon", "시윤", "they", "he",
     "Male. Same person as Lee Shiyun/Lee Siyoon (이시윤); Chinese name 리스윈.",
     "8285a4d6e034/audit/chapter-081.md:80,82,84"),
    # 다율이 is 다율 plus the familiar -이 suffix: one character, and the project's other
    # two entries already say she.
    ("02d365a913c1", "Da-yul", "다율이", "he", "she",
     "Female. Same character as 다율 / 백다율 — 다율이 is the familiar vocative form.",
     "same project: 다율 and 백다율 are both pinned she"),
    # The prose already gets Heis right; only the pin was wrong, so this one is pure
    # prevention — it stops a future re-translation from turning her male.
    ("f53f63fc122c", "Heis", "헤이스", "he", "she",
     "Female. A palace maid who combed the young crown prince's hair; referred to "
     "throughout as 그녀.",
     "audit/chapter-06.md: 그녀의 이름이 '헤이스'라는 걸 · 그전까지 그녀는 그저 '메이드'였다 · "
     "어느 날 그녀는 휴가를 떠났다 (5 female-marked contexts, 0 male)"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (backs up first)")
    ap.add_argument("--stamp", default="20260828")
    args = ap.parse_args()

    changed = 0
    problems: list[str] = []
    by_project: dict[str, list] = {}
    for c in CORRECTIONS:
        by_project.setdefault(c[0], []).append(c)

    for pid, items in sorted(by_project.items()):
        gf = PROJECTS / pid / "glossary.json"
        if not gf.exists():
            problems.append(f"{pid}: no glossary.json")
            continue
        entries = json.loads(gf.read_text(encoding="utf-8-sig"))
        touched = False
        print(f"\n{pid}")
        for _, english, korean, old, new, note, evidence in items:
            hits = [e for e in entries
                    if e.get("english") == english and (e.get("korean") or "") == korean]
            if not hits:
                problems.append(f"{pid}: no entry {english!r} korean={korean!r}")
                continue
            for e in hits:
                if e.get("pronoun") == new:
                    print(f"   {english:<24} already {new!r}")
                    continue
                if e.get("pronoun") != old:
                    problems.append(
                        f"{pid}: {english!r} is {e.get('pronoun')!r}, expected {old!r} — skipped")
                    continue
                print(f"   {english:<24} {old!r} -> {new!r}   [{evidence[:60]}]")
                changed += 1
                touched = True
                if args.apply:
                    e["pronoun"] = new
                    e["note"] = note
        if args.apply and touched:
            backup = gf.with_name(f"glossary.json.pronoun-backup-{args.stamp}")
            if not backup.exists():
                shutil.copy2(gf, backup)
            gf.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(f"{'APPLIED' if args.apply else 'DRY RUN'} — {changed} pin(s) "
          f"{'corrected' if args.apply else 'would be corrected'}")
    if problems:
        print("\nnot applied:")
        for p in problems:
            print(f"   {p}")
    if not args.apply:
        print("\nre-run with --apply to write (glossary.json is backed up first)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
