"""Planning and recording chapter posts. The clicking happens in the browser extension.

Division of labour, chosen to keep the fragile part as small as possible: everything that
can be got right here — which chapters, what they are called, what HTML to send, what they
cost, and what has already been posted — is decided in Python and tested. The extension
only clicks, types, pastes, reads fields back, and reports what happened.

Nothing in this module touches a browser or a network. That is what lets the whole plan be
checked against a real series before anything is published.
"""

from __future__ import annotations

import json
import re
from collections import Counter
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path

from translation_bot.atomic import atomic_write_text, quarantine_unreadable
from translation_bot.config import Config
from translation_bot.mdhtml import markdown_to_html, strip_leading_heading
from translation_bot.pipeline import existing_chapter_file
from translation_bot.state import DONE_STATUSES, State

from . import projects as pj
from . import remote
from . import series as series_mod
from .titles import learn_naming, site_title_key

# A chapter has to be finished before it can go out. `validated` is the only status the
# engine treats as done; needs-review, failed and empty are all explicitly not.
POSTABLE_STATUSES = DONE_STATUSES


def adapters_dir() -> Path:
    # A function, not the constant, so the test suite's monkeypatched PROJECTS_DIR is
    # honoured here too.
    return pj.PROJECTS_DIR.parent / "adapters"


def load_adapter(name: str = "meiko") -> dict:
    """The site's locators and limits, as data the user can edit.

    Deliberately re-read on every call rather than cached: the whole point of keeping the
    selectors in a file is that a site redesign can be fixed by editing one line, and
    having to restart the app to pick that up would undo most of the benefit.
    """
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", name or ""):
        raise ValueError("Unknown site.")
    path = adapters_dir() / f"{name}.toml"
    if not path.exists():
        raise ValueError(f"No adapter for {name!r}.")
    with path.open("rb") as fh:
        return tomllib.load(fh)


# ---------------------------------------------------------------------------
# What to post
# ---------------------------------------------------------------------------


def _chapter_markdown(pid: str, index: int) -> str | None:
    """The finished translation for one chapter, or None.

    Resolved by exact index through ``existing_chapter_file``, which globs the project's
    own chapters/ directory NON-recursively. 28 of these projects carry a stray
    export_artifacts_backup_*/chapters/ copy of everything and five carry repin_backup_*/,
    so anything that walked the project folder would find up to three copies of every
    chapter.
    """
    project = pj.get_project(pid)
    if project is None:
        return None
    cfg = pj.project_config(Config(), project)
    path = existing_chapter_file(cfg.paths.output_dir, index)
    if path is None or not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _chapter_record(pid: str, index: int) -> tuple[str, str, bool]:
    """``(status, source hash, is that hash on another chapter of this project?)``.

    One state.json parse, which was already happening once per chapter -- the record
    carrying the hash was read and thrown away, so the hash costs nothing.

    The ambiguity flag is per PROJECT rather than per plan on purpose. A chapter range
    drops rows before the plan ever sees them, so counting digests across the plan's
    items can only see what is in range: ask for one chapter and a duplicate of it
    sitting outside the range becomes invisible. ``State.load`` has already parsed every
    record in this project, so answering from that same parse is free and complete.
    """
    project = pj.get_project(pid)
    if project is None:
        return "missing", "", False
    cfg = pj.project_config(Config(), project)
    state = State.load(cfg.paths.state_file)
    rec = state.get(index) or {}
    digest = rec.get("source_hash") or ""
    twice = bool(digest) and sum(
        1 for other in (state.chapters or {}).values()
        if (other or {}).get("source_hash") == digest) > 1
    return rec.get("status") or "pending", digest, twice


def source_digest(project_id: str, index: int | None) -> str:
    """The fingerprint of the Korean a chapter was translated from, for the ledger."""
    if not project_id or index is None:
        return ""
    try:
        return _chapter_record(project_id, int(index))[1]
    except (OSError, ValueError, TypeError):
        return ""


def chapter_title(row: dict, adapter: dict, side_ordinal: int | None = None,
                  naming: dict | None = None) -> str:
    """The chapter's display name on the site.

    Numbered chapters are "Chapter 122"; side stories are numbered within their own run.

    ``naming`` is what this novel's own chapter list says it calls things, learned by
    ``titles.learn_naming``. It WINS over the adapter default, and that is the whole fix
    for side stories: eight are live on one real series as "Side Chapter 1".."Side Chapter
    8" while the default here generates "Side Stories N", so nothing matches, the
    duplicate guard cannot see them, and all eight would post again. Generating the name
    the site already uses makes the shared title key the entire matcher -- no second code
    path to drift.

    Preference order, most specific first: what the site actually uses, then a per-target
    override somebody set by hand, then the adapter's default.
    """
    site = adapter.get("site") or {}
    learned = naming or {}

    # A prologue is not chapter one. It carries global 1 in the mapping, so without this
    # branch it renders as "Chapter 1" -- and on two real series the site carries BOTH a
    # "Prologue" and a "Chapter 1", so that name is already taken.
    if row.get("kind") == "prologue":
        return next((s for s in learned.get("singletons") or [] if s == "prologue"),
                    "Prologue").title()

    if row.get("kind") == "side":
        template = ((learned.get("side") or {}).get("template")
                    or site.get("side_title_template") or "Side Stories {n}")
        return template.replace("{n}", str(side_ordinal if side_ordinal else 1))

    template = ((learned.get("chapter") or {}).get("template")
                or site.get("title_template") or "Chapter {n}")
    return template.replace("{n}", str(row.get("global")))


def naming_for(sid: str, target_id: str) -> dict:
    """What this novel calls its chapters, learned from the site list already stored.

    One function so the plan and the payload cannot disagree. If they did, the title the
    run was planned under would differ from the title it actually posted -- which is a
    duplicate with extra steps.
    """
    site = load_site(sid, target_id)
    if not site.get("trusted", True):
        # Names learned from a reading of the wrong page would be the other novels' names.
        return {}
    adapter = load_adapter()
    return learn_naming(site["titles"],
                        (adapter.get("site") or {}).get("title_template"))


def title_number_re(adapter: dict) -> re.Pattern:
    """The inverse of the adapter's own title template.

    ``chapter_title`` builds "Chapter 122" out of ``title_template``; this reads it back.
    Deriving one from the other means the duplicate check compares like with like, and a
    user who edits the template does not silently break the parser.

    Head-anchored, then the number, then STOP -- the tail is deliberately ignored, so
    "Chapter 118: The Duel" parses to 118. The strict form would fail to parse it, find no
    118 on the site, and let a duplicate through. Leading zeros are eaten so "Chapter 007"
    is 7.
    """
    template = ((adapter.get("site") or {}).get("title_template") or "Chapter {n}")
    head, _, _tail = template.partition("{n}")
    return re.compile(r"^\s*" + re.escape(head) + r"0*(\d{1,5})\b", re.I)


def site_numbers(titles: list[str], adapter: dict) -> list[int]:
    """The chapter numbers the site's own titles state, ascending and unique."""
    pattern = title_number_re(adapter)
    found = set()
    for title in titles:
        m = pattern.match(str(title or ""))
        if m:
            found.add(int(m.group(1)))
    return sorted(found)


def number_runs(numbers: list[int]) -> list[list[int]]:
    """Contiguous numbers collapsed to [first, last] pairs.

    Ten bytes for a healthy series, where a list of titles would be hundreds. It is also
    the shape the page wants: "1-196" is what a person reads, and a gap shows up as a
    second pair rather than having to be hunted for.
    """
    runs: list[list[int]] = []
    for n in numbers:
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    return runs


def audit_pricing(items: list[dict], site: dict) -> list[dict]:
    """Chapters already on the site that do not match what this series says they cost.

    The question the free cutoff cannot answer on its own. A series set to "free through
    50, then 55 coins" says what chapter 63 OUGHT to be; only the site says what it is. A
    chapter that went up free when it should be paid is revenue quietly gone, and one left
    Private is a chapter nobody can read -- and neither shows up anywhere today, because
    the plan only ever asked "is this title present?".

    Each item already carries its own expected ``paid`` and ``coins``, worked out from the
    cutoff, so this is a comparison rather than a second pricing rule.

    A row whose ``paid`` is None is NOT reported. Every snapshot written before rows
    existed knows titles alone, and calling that "free" would report a whole library as
    mispriced on the strength of a field the file never contained.
    """
    by_key = {site_title_key(r["title"]): r for r in site.get("rows") or []}
    out = []
    for item in items:
        row = by_key.get(site_title_key(item["title"]))
        if row is None or row.get("paid") is None:
            continue
        problems = []
        if bool(row["paid"]) != bool(item["paid"]):
            problems.append("is free on the site but should cost coins" if item["paid"]
                            else "costs coins on the site but should be free")
        elif item["paid"] and isinstance(row.get("coins"), int)                 and row["coins"] != item["coins"]:
            problems.append(f"costs {row['coins']} coins on the site, "
                            f"not {item['coins']}")
        state = row.get("state") or ""
        if state and state != "public":
            problems.append(f"is {state} on the site, so readers cannot see it")
        if problems:
            out.append({"title": item["title"], "global": item["global"],
                        "problems": problems})
    return out


def _side_ordinals(rows: list[dict]) -> dict[tuple[str, int], int]:
    """Number the side stories within each run, so they read 1, 2, 3 rather than by index."""
    out: dict[tuple[str, int], int] = {}
    n = 0
    previous_was_side = False
    for row in rows:
        if row.get("kind") == "side":
            n = n + 1 if previous_was_side else 1
            out[(row["project_id"], row["index"])] = n
            previous_was_side = True
        else:
            previous_was_side = False
    return out


def plan_run(sid: str, target_id: str | None = None, *, start: int | None = None,
             end: int | None = None, adapter_name: str = "meiko",
             use_site: bool = True) -> dict:
    """Everything that would be posted, and every reason it might not be.

    Writes nothing and touches no browser. This is the gate that catches the expensive
    mistake — a free/paid cutoff set the wrong way round is painful to undo once readers
    have been through it.

    ``use_site=False`` ignores the site snapshot and plans from the ledger alone, which is
    what a test asking "what does this series contain" wants.
    """
    series = series_mod.get_series(sid)
    if series is None:
        raise ValueError("That series doesn't exist.")
    adapter = load_adapter(adapter_name)
    mapping = series_mod.load_mapping(sid)
    if not mapping:
        raise ValueError("Resolve the chapter numbering for this series first.")

    targets = series.get("publish_targets") or []
    target = next((t for t in targets if t.get("id") == target_id), None) or (
        targets[0] if len(targets) == 1 else None)
    if target is None:
        raise ValueError("Pick which site to post to.")

    free_through = int(target.get("free_through") or 0)
    coin_price = int(target.get("coin_price") or 0)
    # A side story carries no chapter number, so the free/paid cutoff cannot classify it
    # at all - and the default used to be "free", which on this site is wrong. Its own
    # chapter lists show side content as the earliest PAID entry on three of the four
    # series that have any, and side stories are the newest thing on those series, so
    # giving them away is the expensive direction. Paid unless a series says otherwise.
    side_paid = bool(target.get("side_paid", True))
    max_chars = int((adapter.get("site") or {}).get("max_chars") or 100000)

    # Read BEFORE the items are built, because the titles they carry depend on the naming
    # learned from this list -- what the novel's own chapter list says it calls things.
    site = (load_site(sid, target["id"]) if use_site
            else {"titles": [], "fetched_at": None, "trusted": True, "why": ""})
    numbers = site_numbers(site["titles"], adapter)
    age = _age_seconds(site.get("fetched_at"))
    # Names learned from a reading of the wrong page would be other novels' names, so an
    # untrusted snapshot teaches nothing and the adapter's defaults stand.
    naming = (learn_naming(site["titles"],
                       (adapter.get("site") or {}).get("title_template"))
              if site.get("trusted", True) else {})

    rows = series_mod.reading_order(series, mapping)
    ordinals = _side_ordinals(rows)
    # (project, index) -> (source hash, is that hash on another chapter there)
    digests: dict[tuple[str, int], tuple[str, bool]] = {}
    items = []
    for row in rows:
        number = row.get("global")
        # Only real chapters are range-selectable; side content is offered but never
        # auto-included. Keyed on KIND rather than on "has no global number", which is
        # what this used to test: two series number their side stories 1..10 in their own
        # sequence, and that collides exactly with their chapters 1..10 -- so a run for
        # chapters 1-10 silently swept ten side stories in with them.
        is_side = row.get("kind") == "side"
        if number is not None and not is_side:
            if start is not None and number < start:
                continue
            if end is not None and number > end:
                continue
        elif start is not None or end is not None:
            continue

        pid, index = row["project_id"], row["index"]
        title = chapter_title(row, adapter, ordinals.get((pid, index)), naming)
        md = _chapter_markdown(pid, index)
        status, digest, digest_twice = _chapter_record(pid, index)
        digests[(pid, index)] = (digest, digest_twice)
        html = markdown_to_html(strip_leading_heading(md or ""),
                                strip_part_markers=True) if md else ""
        # Real chapters go by the free/paid cutoff; side content follows the series' own
        # side_paid setting. Keyed on KIND, for the same reason as the range filter above.
        #
        # Testing "has no number" instead is what made every side story free on the two
        # series that number theirs: with globals 1..10 the first branch won, so each side
        # story's own ordinal was compared against the CHAPTER cutoff (66) and lost, while
        # side_paid sat at its default of True and was never consulted. Twenty side
        # stories across two series priced at zero, and giving them away is the direction
        # that cannot be undone by re-posting -- a reader who bought nothing keeps it.
        paid = side_paid if is_side else (number is not None and number > free_through)
        blockers = []
        if status not in POSTABLE_STATUSES:
            blockers.append(f"not finished ({status})")
        if not html:
            blockers.append("no translation on disk")
        if len(html) > max_chars:
            blockers.append(f"too long for the editor ({len(html)} > {max_chars})")
        # A number the resolver guessed rather than read is held back until a person has
        # confirmed it. This is per-CHAPTER on purpose: the confident rows of a series still
        # post, and only the handful that are genuinely uncertain wait. Almost all of them
        # are side-story runs, where the cost of being wrong is a chapter published under
        # the wrong name — and renaming one on the site means retyping it to confirm.
        if row.get("confidence") == "low":
            blockers.append("chapter number not confirmed — check this series' numbering")
        items.append({
            "project_id": pid,
            "index": index,
            "global": number,
            "kind": row.get("kind"),
            "title": title,
            "status": status,
            "paid": paid,
            "coins": coin_price if paid else 0,
            "html_chars": len(html),
            # Carried through so a caller can say WHY a chapter is waiting rather than
            # only that it is.
            "confidence": row.get("confidence"),
            "blockers": blockers,
        })

    ledger = load_ledger(sid, target["id"])
    posted = {site_title_key(e["title"])
              for e in ledger.get("entries", []) if e.get("status") == "posted"}

    # The same chapter under a different NAME.
    #
    # Every other guard here compares titles, and a renumber changes the title a chapter
    # GENERATES while leaving its Korean -- and so its hash -- untouched. That is exactly
    # how one novel was published three times under three different numbers: the ledger
    # missed it because the title moved, and the site snapshot missed it too because the
    # site still showed the old name.
    #
    # The stored hash is the provenance of the English already on disk. realign_chapter_
    # indices moves a chapter file and its state record together, so a translation keeps
    # its digest across a tab insertion that changes its index -- which is precisely
    # where (project_id, index) fails, and why identity is keyed on this instead.
    #
    # Entries written before this carry no hash and fall back to the title, so nothing
    # that used to post stops posting.
    posted_hashes: dict[str, str] = {}
    shared_in_ledger: set[str] = set()
    for entry in ledger.get("entries", []):
        if entry.get("status") != "posted":
            continue
        digest = entry.get("source_hash") or ""
        if not digest:
            continue
        if digest in posted_hashes:
            shared_in_ledger.add(digest)
        posted_hashes.setdefault(digest, entry.get("title") or "")

    # Counted across the WHOLE plan, and over every item rather than only the postable
    # ones: the one real collision in this library is two chapters that are both already
    # posted, which a postable-only count would never see. This catches a digest shared
    # across two MEMBERS of a series, which the per-project check inside _chapter_record
    # cannot see.
    plan_counts = Counter(digest for digest, _ in digests.values() if digest)
    # Chapters the browser said it was ABOUT to create and never reported finishing.
    #
    # The window this closes: the create succeeds on the site, the reply is lost, and the
    # only record of it lived in one tab's sessionStorage. Close the tab and an empty
    # chapter sits on the site that nothing knows about -- and once its title reaches the
    # snapshot, the real chapter is blocked by it forever.
    #
    # Deliberately NOT part of `posted`: an intent is not a chapter, and blocking on it
    # would let a create that never happened veto the real post.
    creating = [e["title"] for e in ledger.get("entries", [])
                if e.get("status") == "creating"]
    # What the site itself says it has, which outranks the ledger: it reflects reality
    # including anything posted by hand, from another machine, or before this app existed.
    #
    # Both sides go through site_title_key. This used to be an exact match against the
    # ledger and a casefold against the site, which was two of the FIVE spellings of this
    # comparison that existed across the two languages -- and any two of them disagreeing
    # is either a duplicate posted or a chapter permanently blocked. Widening the ledger
    # match from exact to the shared key only ever ADDS blocking, which is the safe
    # direction. See server/titles.py.
    on_site = {site_title_key(t) for t in site["titles"]}
    # A snapshot we cannot trust blocks EVERYTHING, rather than being treated as empty.
    #
    # The direction matters more than the rule. "Treat it as empty" reads as "nothing is
    # posted here", which is exactly what has two real series offering their whole backlog
    # -- 120 and 101 chapters -- as ready to post a second time. Blocking every row makes
    # eligible_items return nothing, which makes request_run refuse with "Nothing in that
    # range is ready to post", so no second mechanism is needed to stop the run.
    untrusted = (f"the site reading for this novel is unusable ({site['why']}) — open it "
                 f"on meiko.studio so its chapter list is read again"
                 ) if not site.get("trusted", True) else None
    # At most ONE of these is reported, because they answer the same question and the
    # site outranks the ledger. But "in the ledger" alone used to hide whether the
    # chapter is ALSO still up there, and the two need different actions: a ledger entry
    # for a chapter that is no longer on the site is a stale record and can be taken
    # back (DELETE /api/posting/ledger), while one that is still live is simply true and
    # forgetting it would achieve nothing but a duplicate on the next run.
    for item in items:
        key = site_title_key(item["title"])
        live = key in on_site
        digest, digest_twice = digests.get(
            (item["project_id"], item["index"]), ("", False))
        # Only ever acted on when the hash means ONE chapter: ambiguous either within its
        # own project, across the series, or in the ledger, and it identifies nothing.
        renamed = (posted_hashes.get(digest)
                   if digest and not digest_twice
                   and plan_counts[digest] == 1
                   and digest not in shared_in_ledger
                   else None)
        if untrusted:
            item["blockers"] = [*item["blockers"], untrusted]
        elif key in posted:
            item["blockers"] = [*item["blockers"], (
                "already in the ledger as posted, and still on the site" if live
                else "already in the ledger as posted")]
        elif renamed is not None and site_title_key(renamed) != key:
            # A re-translation does NOT clear this: the hash fingerprints the Korean, not
            # the English. DELETE /api/posting/ledger is the way out.
            item["blockers"] = [*item["blockers"],
                                f'already posted as "{renamed}" — the same Korean, '
                                f'under a different number']
        elif live:
            item["blockers"] = [*item["blockers"], "already on the site"]

    ready = [i for i in items if not i["blockers"]]
    return {
        "series": {"id": sid, "name": series.get("name") or ""},
        "target": target,
        "adapter": adapter,
        "items": items,
        "ready": len(ready),
        "blocked": len(items) - len(ready),
        "free_through": free_through,
        "coin_price": coin_price,
        "side_paid": side_paid,
        # Freshness, not the list: a hundred chapter names would dwarf everything else in
        # the response, and the page only needs to say how long ago this was read.
        "site": {"fetched_at": site["fetched_at"], "count": len(on_site),
                  # What the site charges, read off its own chapter rows. Offered to the
                  # page as a suggestion to confirm, never written onto the series here:
                  # a price is painful to undo, so it gets a person's click.
                  "observed_coins": site.get("observed_coins"),
                  # Whether this reading can be believed at all. False means every chapter
                  # is blocked, so the page must explain rather than show a bare zero.
                  "trusted": bool(site.get("trusted", True)),
                  "why": site.get("why") or "",
                  "source": site.get("source") or "",
                  # What the site actually holds, as contiguous [first, last] runs. The
                  # titles themselves are deliberately not sent -- a hundred chapter names
                  # would dwarf the response -- but the runs are three orders of magnitude
                  # smaller and strictly more useful.
                  "posted": number_runs(numbers),
                  "max_posted": max(numbers) if numbers else None,
                  # count vs numbered is the corruption tell: the two bad snapshots hold
                  # 94 titles of which 0 parse as chapters.
                  "numbered": len(numbers),
                  "age_seconds": age},
        # Chapters already up that do not match this series' own pricing. Empty until a
        # reading carries the site's paid/coins/state, which title-only snapshots do not.
        "audit": audit_pricing(items, site),
        # Half-made chapters to reconcile before anything new is created.
        "creating": creating,
        # What this novel calls its chapters, learned from its own list. Surfaced so the
        # page can say when nothing was learned and the defaults are in use.
        "naming": naming,
        # What the run would actually charge for, spelled out. Getting this backwards is
        # the mistake worth one extra click to avoid.
        "summary": {
            "free": [i["global"] for i in ready if not i["paid"]],
            "paid": [i["global"] for i in ready if i["paid"]],
        },
    }


def chapter_payload(sid: str, project_id: str, index: int,
                    *, adapter_name: str = "meiko",
                    target_id: str | None = None) -> dict:
    """The finished HTML for one chapter, ready to paste."""
    series, target = _resolve_target(sid, target_id)
    adapter = load_adapter(adapter_name)
    # The SAME learned naming the plan used. A title generated here that differs from the
    # planned one would be posted under a name nothing is watching for.
    naming = naming_for(sid, target["id"])
    mapping = series_mod.load_mapping(sid)
    rows = series_mod.reading_order(series, mapping)
    row = next((r for r in rows
                if r["project_id"] == project_id and r["index"] == index), None)
    if row is None:
        raise ValueError("That chapter is not part of this series' reading order.")
    md = _chapter_markdown(project_id, index)
    if not md:
        raise ValueError("That chapter has no translation on disk yet.")
    clean = strip_leading_heading(md)
    return {
        "project_id": project_id,
        "index": index,
        "global": row.get("global"),
        "kind": row.get("kind"),
        "title": chapter_title(row, adapter,
                               _side_ordinals(rows).get((project_id, index)), naming),
        "html": markdown_to_html(clean, strip_part_markers=True),
        # Sent alongside as the plain-text flavour of the same paste, exactly as the
        # reader's Copy button does.
        "text": clean,
    }


# ---------------------------------------------------------------------------
# Matching the site's own series list against the novels here
# ---------------------------------------------------------------------------

# Below this, two titles are not the same novel. Above it they are proposed and a human
# confirms — the two real near-misses are "The Loathsome Scapegoat" against "That
# Loathsome Scapegoat", and "…Youngest Son of a Villain Family" against "…Youngest of the
# Villain Family", both of which a person recognises instantly and no rule should decide.
_FUZZY_FLOOR = 0.80

# Only prose novels. The export also lists manga and comics, which have no chapters here.
_POSTABLE_TYPES = {"novel", ""}


def match_targets(csv_text: str) -> dict:
    """Pair each linked series with its admin page on the site.

    Takes the site's own series export (Title, Type, Status, Views, Admin URL) so the
    publishing links do not have to be pasted in one at a time. Titles drift between the
    two systems — leading and trailing spaces, case, and the odd reworded word — so exact
    matches are applied and near-matches are only proposed.
    """
    import csv as csv_mod
    import difflib
    import io

    rows = []
    for row in csv_mod.DictReader(io.StringIO(csv_text or "")):
        title = (row.get("Title") or "").strip()
        url = (row.get("Admin URL") or "").strip()
        kind = (row.get("Type") or "").strip().lower()
        if not title or not url or kind not in _POSTABLE_TYPES:
            continue
        rows.append({"title": title, "url": url,
                     "key": series_mod.normalize_title(title)})
    by_key = {r["key"]: r for r in rows}
    keys = list(by_key)

    exact, fuzzy, unmatched = [], [], []
    for series in series_mod.list_series():
        key = series_mod.normalize_title(series.get("name") or "")
        existing = (series.get("publish_targets") or [])
        current = str((existing[0] or {}).get("series_url") or "") if existing else ""
        row = {"sid": series["id"], "name": series.get("name") or "", "current": current}
        hit = by_key.get(key)
        if hit:
            exact.append({**row, "site_title": hit["title"], "url": hit["url"]})
            continue
        close = difflib.get_close_matches(key, keys, n=1, cutoff=_FUZZY_FLOOR)
        if close:
            hit = by_key[close[0]]
            fuzzy.append({
                **row, "site_title": hit["title"], "url": hit["url"],
                "score": round(difflib.SequenceMatcher(None, key, close[0]).ratio(), 3),
            })
        else:
            unmatched.append(row)
    return {
        "site_rows": len(rows),
        "exact": sorted(exact, key=lambda r: r["name"].casefold()),
        "fuzzy": sorted(fuzzy, key=lambda r: r["score"]),
        "unmatched": sorted(unmatched, key=lambda r: r["name"].casefold()),
    }


def apply_targets(assignments: list[dict]) -> dict:
    """Write a publishing URL onto each named series.

    Only the URL is set. ``free_through`` and ``coin_price`` are left exactly as they were,
    because those are per-series pricing decisions the export knows nothing about, and
    silently resetting them to zero would quietly turn paid chapters free on the next run.
    """
    applied, skipped = [], []
    for item in assignments or []:
        sid, url = item.get("sid"), str(item.get("url") or "").strip()
        series = series_mod.get_series(sid) if sid else None
        if series is None or not url:
            skipped.append({"sid": sid, "why": "unknown series or empty URL"})
            continue
        targets = series.get("publish_targets") or []
        if targets:
            targets[0] = {**targets[0], "site": "meiko", "series_url": url}
        else:
            targets = [{"id": "t1", "site": "meiko", "series_url": url,
                        "free_through": 0, "coin_price": 0}]
        series["publish_targets"] = targets
        series_mod.write_series(series)
        applied.append({"sid": sid, "name": series.get("name") or "", "url": url})
    return {"applied": applied, "skipped": skipped}


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def publish_dir(sid: str, target_id: str) -> Path:
    """Where everything about one series' posting to one site lives.

    ``target_id`` is sanitised rather than validated because it comes from a series
    manifest the user can hand-edit, and it is about to become a path segment.
    """
    safe = re.sub(r"[^a-zA-Z0-9_-]", "", target_id or "default") or "default"
    return series_mod.series_root() / sid / "publish" / safe


def _read_json(path: Path, fallback: dict) -> dict:
    """One file, or the fallback — quarantining anything unreadable first.

    Keep the bytes before anything writes over them: a transient read failure that
    returned "nothing here" would otherwise let the next post be recorded into an empty
    file, losing the record of everything already published.
    """
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        quarantine_unreadable(path)
        return dict(fallback)
    return data if isinstance(data, dict) else dict(fallback)


def _write_json(path: Path, doc: dict) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(doc, ensure_ascii=False, indent=2))
    return doc


def ledger_path(sid: str, target_id: str) -> Path:
    return publish_dir(sid, target_id) / "ledger.json"


def load_ledger(sid: str, target_id: str) -> dict:
    return _read_json(ledger_path(sid, target_id), {"entries": []})


def record(sid: str, target_id: str, entry: dict) -> dict:
    """Append or replace one chapter's record, keyed on its title.

    Keyed on the title rather than the chapter number because the title is what the site
    shows and what a resume has to match against the live chapter list — and because
    deleting a chapter there means retyping that exact name, so it is the identifier the
    user works with too.
    """
    doc = load_ledger(sid, target_id)
    # Through site_title_key, like every other comparison of two chapter titles in this
    # app. A raw != meant a title differing only in case or spacing produced a SECOND row
    # for one chapter -- and since plan_run reads the same entries back through
    # site_title_key, the two sides disagreed about how many chapters the ledger holds.
    # That is the fifth spelling server/titles.py was written to remove; record() was the
    # one that never got converted.
    key = site_title_key(entry.get("title"))
    entries = [e for e in doc.get("entries", []) if site_title_key(e.get("title")) != key]
    entries.append(entry)
    entries.sort(key=lambda e: (e.get("global") is None, e.get("global") or 0,
                                e.get("title") or ""))
    doc["entries"] = entries
    return _write_json(ledger_path(sid, target_id), doc)


def forget(sid: str, target_id: str, *, title: str | None = None,
           project_id: str | None = None, index: int | None = None) -> dict:
    """Remove ledger entries, so a chapter the ledger blocks can be posted again.

    The ledger is the only record that says "this is already published", and until now
    nothing could amend it: the endpoints only ever appended, and no UI, route or script
    could take an entry back. The single accidental way out was to CHANGE THE CHAPTER'S
    TITLE, because every match in plan_run is keyed on the title -- which is the same
    defect that let one chapter be published three times under three different numbers.

    So this exists before that defect is tightened, not after. Any rule strict enough to
    catch a renamed duplicate is also strict enough to block a chapter wrongly, and the
    honest answer to "what do I do when it is wrong?" cannot be "hand-edit the JSON",
    which is what repairing this series actually required.

    Matched by title (through the shared key) or by (project_id, index), whichever is
    given. Returns the ledger, with ``removed`` listing the entries taken out so the
    caller can show what it just forgot -- and so it is recoverable by re-posting.
    """
    doc = load_ledger(sid, target_id)
    entries = doc.get("entries", [])
    key = site_title_key(title) if title is not None else None

    def matches(e: dict) -> bool:
        if key is not None and site_title_key(e.get("title")) == key:
            return True
        # Deliberately requires BOTH, never index alone: index is a tab's POSITION and is
        # re-derived on every source read, so it means nothing without the document it
        # belongs to -- and even then only for as long as no tab is inserted before it.
        return (project_id is not None and index is not None
                and e.get("project_id") == project_id and e.get("index") == index)

    removed = [e for e in entries if matches(e)]
    if not removed:
        raise ValueError("no ledger entry matches that chapter")
    doc["entries"] = [e for e in entries if not matches(e)]
    saved = _write_json(ledger_path(sid, target_id), doc)
    return {**saved, "removed": removed}


# ---------------------------------------------------------------------------
# What the site itself already has
# ---------------------------------------------------------------------------


def site_path(sid: str, target_id: str) -> Path:
    return publish_dir(sid, target_id) / "site.json"


# Three is enough. A chapter genuinely titled after another novel is possible once; three
# of them is not a coincidence, and the real failures match on all 94.
_WRONG_PAGE_HITS = 3


def snapshot_looks_wrong(sid: str, titles: list[str]) -> str | None:
    """Whether a chapter list was in fact read off some OTHER page. Reason, or None.

    This is not hypothetical. Two snapshots on disk -- "The Fluffy Intruder of the Demon
    King's Castle" and "Everyone is Suspiciously Targeting Me" -- each hold 94 titles of
    which every one is the name of a different novel in the studio. The extension scraped
    the studio's series-list table instead of a chapter list, and because the result was
    non-empty nothing rejected it. Both series consequently report their whole backlog
    (120 and 101 chapters) as unposted, and a run on either would post them all a second
    time.

    The existing guard is ``write_site`` refusing an EMPTY list, whose docstring says "the
    one way to get an empty result is to have scraped the wrong page". The two files above
    are the counter-example: the wrong page can be read perfectly well and still be the
    wrong page.

    Matching is by ``site_title_key``, so the check agrees with every other title
    comparison in the app.
    """
    keys = {site_title_key(t) for t in titles or []}
    keys.discard("")
    if not keys:
        return None

    # A local import: server/importing.py imports THIS module, so a module-level import
    # here would be a cycle.
    from . import importing

    catalogue = {site_title_key(row.get("name"))
                 for row in (importing.load_catalogue().get("series") or [])}
    catalogue.discard("")
    hits = keys & catalogue
    if len(hits) >= _WRONG_PAGE_HITS:
        return (f"{len(hits)} of these {len(keys)} titles are names of novels in the "
                f"studio rather than chapters, so this was read off the series list")

    # Works even when no catalogue has been pushed, which is the case for most libraries.
    others = {site_title_key(row.get("name")) for row in series_mod.list_series()
              if row.get("id") != sid}
    others.discard("")
    hits = keys & others
    if len(hits) >= _WRONG_PAGE_HITS:
        return (f"{len(hits)} of these {len(keys)} titles are names of other novels in "
                f"your library rather than chapters of this one")
    return None


def clean_row(row) -> dict | None:
    """One chapter as the site describes it, or None if it is not one.

    Accepts a bare string so the original title-only callers keep working unchanged.

    ``paid`` and ``coins`` are None when unknown, which is NOT the same as free at zero.
    Every snapshot written before this knew titles only, and reading those back as "free"
    would invent an answer the file never contained -- and the pricing audit built on top
    would then confidently report a whole library as mispriced.
    """
    if isinstance(row, str):
        row = {"title": row}
    if not isinstance(row, dict):
        return None
    title = str(row.get("title") or row.get("displayName") or "").strip()
    if not title:
        return None
    paid = row.get("paid")
    paid = None if paid is None else bool(paid is True or paid == 1 or paid == "1")
    coins = row.get("coins")
    coins = int(coins) if isinstance(coins, (int, float)) and not isinstance(coins, bool)         else None
    return {"title": title, "paid": paid, "coins": coins,
            "state": str(row.get("state") or "").strip().lower(),
            "remote_id": str(row.get("remote_id") or row.get("id") or "")}


def load_site(sid: str, target_id: str) -> dict:
    """The chapter names last read off the site, or an empty snapshot.

    Empty and stale are deliberately different things: ``fetched_at`` is None only when
    nobody has ever looked, which is worth saying out loud on the Posting page rather than
    reporting a hundred chapters as ready to post.

    ``trusted`` is the third state, and the important one. A snapshot can be present,
    non-empty and recent and still be a reading of the wrong page -- see
    ``snapshot_looks_wrong``. Checked on the way OUT rather than only on the way in,
    because the two bad files are already on disk and a write-side guard would never look
    at them.

    Read here rather than through ``_read_json`` so that "the file is corrupt" and "nobody
    has looked yet" stay distinguishable. They lead to opposite advice, and the shared
    helper cannot tell them apart.
    """
    path = site_path(sid, target_id)
    empty = {"titles": [], "rows": [], "source": "", "fetched_at": None,
             "observed_coins": None, "trusted": True, "why": ""}
    if not path.exists():
        return empty
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise ValueError("not an object")
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
        # Keep the bytes, as every other reader here does -- and do NOT report it as an
        # empty snapshot, which reads as "nothing is posted" and is the direction that
        # posts a duplicate.
        quarantine_unreadable(path)
        return {**empty, "trusted": False,
                "why": "the last reading of this novel could not be read back"}

    stored = doc.get("rows")
    if isinstance(stored, list) and stored:
        rows = [r for r in (clean_row(r) for r in stored) if r]
        source = str(doc.get("source") or "")
    else:
        # A title-only file, which is every snapshot written before rows existed. Read it
        # as titles with everything else unknown rather than migrating it: a migration is a
        # write over the only record of what is published, and these upgrade themselves on
        # the next push anyway.
        titles = doc.get("titles")
        rows = [r for r in (clean_row(t) for t in
                            (titles if isinstance(titles, list) else [])) if r]
        source = "legacy"

    titles = [r["title"] for r in rows]
    coins = doc.get("observed_coins")
    why = snapshot_looks_wrong(sid, titles)
    return {"titles": titles,
            "rows": rows,
            "source": source,
            "fetched_at": doc.get("fetched_at"),
            "observed_coins": coins if isinstance(coins, int) and coins > 0 else None,
            "trusted": why is None,
            "why": why or ""}


def _modal_price(prices: list[int] | None) -> int | None:
    """The coin price the site charges, read off its own chapter rows.

    The one number the site export does not carry, and the reason 21 series had no price:
    it is only visible on the series page itself, in the coins box on each paid row. The
    extension already reads those, so this is the app learning the price rather than
    anyone typing it 21 times.

    Modal rather than maximum or first, because a series can carry a handful of oddly
    priced chapters — an early promotion, a double-length one — and the price that matters
    is the one nearly every paid chapter has. Zeroes are the free rows and are ignored.
    """
    paid = [int(p) for p in prices or [] if isinstance(p, (int, float)) and int(p) > 0]
    if not paid:
        return None
    counts: dict[int, int] = {}
    for price in paid:
        counts[price] = counts.get(price, 0) + 1
    # Ties break towards the lower price: charging less than the site does is a smaller
    # mistake than charging more, and this is a suggestion a person still confirms.
    return min(counts, key=lambda p: (-counts[p], p))


def _merge_rows(previous: list[dict], incoming: list[dict]) -> list[dict]:
    """Incoming rows win field by field, but only where they actually know something.

    A DOM reading carries a title and a price; an API reading carries the remote id and
    the state as well. Letting a thinner reading blank what a richer one already
    established would lose exactly the fields the pricing audit needs.
    """
    by_key = {site_title_key(r["title"]): dict(r) for r in previous}
    order = [site_title_key(r["title"]) for r in previous]
    for row in incoming:
        key = site_title_key(row["title"])
        old = by_key.get(key)
        if old is None:
            by_key[key] = dict(row)
            order.insert(0, key)   # newest first, the way the site lists them
            continue
        merged = dict(old)
        for field, value in row.items():
            if value not in (None, ""):
                merged[field] = value
        by_key[key] = merged
    seen: set[str] = set()
    out = []
    for key in order:
        if key in seen:
            continue
        seen.add(key)
        out.append(by_key[key])
    return out


def write_site(sid: str, target_id: str, rows, prices: list[int] | None = None,
               mode: str = "merge", source: str = "") -> dict:
    """Record the site's own chapter list, pushed here by the extension.

    Python cannot read the site — it has no session and no business having one — so this is
    the only way the plan can know what is already published. Without it a series posted
    before Night Reader existed reads as entirely unposted.

    ``rows`` may be bare titles (the original shape, still accepted) or dicts carrying the
    site's own ``paid``, ``coins``, ``state`` and ``remote_id``. The richer form is what
    lets the plan say a chapter went up at the wrong price, or is still private.

    **Merge is the default, and the asymmetry is the reason.** A merge that never shrinks
    can leave a deleted chapter's ghost in the snapshot, which BLOCKS a post that should
    have gone out — annoying, and it fails closed. A wrong replace drops a chapter that is
    really there, which PERMITS a duplicate — and that fails open, onto a live paid site.
    ``replace`` is for a reading known to be complete, and is still subject to the shrink
    guard below.

    Three ways a push is refused, each learned from a real file:

    * **Empty.** Storing it would clear a good snapshot and report everything as postable.
    * **The wrong page.** An earlier version of this docstring claimed an empty result was
      "the one way" to have scraped the wrong page. It is not: two snapshots on disk hold
      94 perfectly well-formed titles that are the names of other novels, read off the
      studio's series list. See ``snapshot_looks_wrong``.
    * **A suspicious shrink.** A partial render, a paginated table or a scrape taken
      mid-load yields a SHORT list, which the empty check waves through — and on a replace
      114 chapters can become 1 with nothing to say so.
    """
    if mode not in ("merge", "replace"):
        raise ValueError("Write the site list either by merging or by replacing.")
    incoming = [r for r in (clean_row(r) for r in rows or []) if r]
    if not incoming:
        raise ValueError("Empty chapter list — refusing to overwrite the last snapshot.")

    wrong = snapshot_looks_wrong(sid, [r["title"] for r in incoming])
    if wrong:
        raise ValueError(
            f"This does not look like one novel's chapter list — {wrong}. Open the novel's "
            f"own page (the one listing its chapters) and let it finish loading.")

    before = load_site(sid, target_id)
    if (mode == "replace" and before["rows"]
            and len(incoming) < SHRINK_FLOOR * len(before["rows"])):
        raise ValueError(
            f"Only {len(incoming)} chapters were read where the last reading had "
            f"{len(before['rows'])} — refusing to shrink the snapshot on what looks like a "
            f"partial page. Let the chapter list finish loading and try again.")

    ordered = (_merge_rows(before["rows"], incoming) if mode == "merge"
               else _merge_rows([], incoming))

    doc = {"version": 2, "fetched_at": _now(),
           "source": source or before.get("source") or "",
           "rows": ordered,
           # Kept alongside the rows, always derived from them. Every existing reader --
           # plan_run, the page's count, the tests -- goes through this, so the richer
           # format costs them nothing.
           "titles": [r["title"] for r in ordered]}

    # The coin price, preferring what this reading actually saw.
    observed = _modal_price(prices if prices is not None else
                            [r["coins"] for r in ordered
                             if r["paid"] and isinstance(r["coins"], int)])
    if observed is not None:
        doc["observed_coins"] = observed
    elif before.get("observed_coins"):
        # Keep what an earlier reading learned. A page showing only free chapters says
        # nothing about the price of the paid ones, and treating that as "no price" would
        # throw away a good answer.
        doc["observed_coins"] = before["observed_coins"]
    return _write_json(site_path(sid, target_id), doc)


# ---------------------------------------------------------------------------
# The run request - how the app asks the browser to post
# ---------------------------------------------------------------------------

# A run waits for a browser, which means it waits across a possible restart of this
# process. So it lives on disk beside the ledger rather than in the in-memory job registry,
# which dies with the server. That is also why progress is polled rather than streamed:
# there is one event per chapter, roughly every ten to twenty seconds, and the durable copy
# is worth more than the latency.

RUN_STATES = ("queued", "running", "done", "cancelled", "failed")

def _age_seconds(stamp) -> int | None:
    """How long ago a snapshot was read, or None if nobody ever has."""
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - when).total_seconds()))


# How much of the previous snapshot a new reading must still cover. Below this it is
# treated as a partial page rather than as chapters having been deleted. Deleting a fifth
# of a novel in one sitting is rare; a table that rendered half its rows is not.
SHRINK_FLOOR = 0.8

# A running run whose browser has not been heard from in this long is reported as stalled.
# Nothing reaps it: staleness is computed when the file is read, so there is no timer to
# leak, and closing the browser mid-run leaves a run that says so and offers to resume.
STALE_AFTER_SECONDS = 90

# How often the extension asks for work. Stated here so the page can say what the wait is.
CLAIM_POLL_SECONDS = 30

POST_STATES = ("private", "public")


def run_path(sid: str, target_id: str) -> Path:
    return publish_dir(sid, target_id) / "run.json"


def _positive(value, field: str) -> int | None:
    """A chapter number or a count, or nothing. Zero is nothing, not a range boundary."""
    if value in (None, "", 0):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number.") from None
    if number < 1:
        raise ValueError(f"{field} must be 1 or more.")
    return number


def clean_options(options: dict | None) -> dict:
    opts = options or {}
    start = _positive(opts.get("start"), "Start chapter")
    end = _positive(opts.get("end"), "End chapter")
    if start is not None and end is not None and end < start:
        raise ValueError("The range finishes before it starts.")
    state = str(opts.get("post_state") or "public").strip().lower()
    if state not in POST_STATES:
        raise ValueError("Post either private or public.")
    return {
        "start": start,
        "end": end,
        "up_to": _positive(opts.get("up_to"), "Stop at chapter"),
        "limit": _positive(opts.get("limit"), "Chapter limit"),
        "post_state": state,
    }


def eligible_items(plan: dict, options: dict) -> list[dict]:
    """The chapters a run would actually post, in order.

    The authority for what a run covers. The Posting page filters the same way to show its
    one-line summary before you press Start, and the extension's buildQueue filters again
    against the live chapter list - keep the three in step, and prefer this one when they
    disagree.
    """
    rows = [i for i in plan.get("items") or [] if not i.get("blockers")]
    up_to = options.get("up_to")
    if up_to is not None:
        # "stop at chapter N" names the finishing line, so a run can be repeated without
        # working out how many are left. Rows with no global number are side stories, which
        # no numeric range should sweep up.
        rows = [i for i in rows if i.get("global") is not None and i["global"] <= up_to]
    limit = options.get("limit")
    if limit is not None:
        rows = rows[:limit]
    return rows


def _stale(run: dict) -> bool:
    if run.get("state") != "running":
        return False
    beat = run.get("heartbeat_at") or run.get("claimed_at")
    if not beat:
        return True
    try:
        when = datetime.fromisoformat(beat)
    except (TypeError, ValueError):
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() > STALE_AFTER_SECONDS


def run_view(run: dict | None) -> dict | None:
    """The run as the page should see it - with staleness worked out, minus the token.

    claimed_by is withheld deliberately: it is the only thing stopping a second tab from
    taking over a live run, so it stays between the server and the browser holding it.
    """
    if run is None:
        return None
    return {k: v for k, v in run.items() if k != "claimed_by"} | {"stalled": _stale(run)}


def load_run(sid: str, target_id: str) -> dict | None:
    doc = _read_json(run_path(sid, target_id), {})
    return doc or None


def _live(run: dict | None) -> bool:
    """Whether a run still has a claim on this target."""
    if run is None:
        return False
    if run.get("state") == "queued":
        return True
    return run.get("state") == "running" and not _stale(run)


def _resolve_target(sid: str, target_id: str | None) -> tuple[dict, dict]:
    series = series_mod.get_series(sid)
    if series is None:
        raise ValueError("That series doesn't exist.")
    targets = series.get("publish_targets") or []
    target = next((t for t in targets if t.get("id") == target_id), None) or (
        targets[0] if len(targets) == 1 else None)
    if target is None:
        raise ValueError("Pick which site to post to.")
    return series, target


def check_pricing(target: dict, queue: list[dict]) -> None:
    """Refuse a run that would charge for chapters at a price nobody has stated.

    Asked of the RUN rather than of the series, because that is the only way to get it
    exactly right. The obvious version — refuse when both fields are zero — has a hole
    wide enough to drive real money through: a series with ``free_through`` set to 47 and
    no coin price passes it, and then every chapter from 48 on posts as *paid at zero
    coins*, which is worse than posting it free because readers see a locked chapter they
    cannot buy and the revenue is simply gone.

    So the question is the precise one: does this run contain a chapter the site will mark
    paid, and if so is there a price for it? A series that is genuinely free all the way
    has no paid chapter in the run, needs no price, and passes without anyone having to
    express "free" as a magic number.

    Counted today: 38 publish targets across 39 series, 28 of them priced and 10 with a
    free cutoff but no coin price -- exactly the shape described above. Not one chapter
    has ever been posted above one of those cutoffs, which is this check doing its job
    rather than the hole being narrow. (The figure here used to read "21 of the 22 linked
    series", which was true of a much smaller library and had long since stopped being.)
    """
    coin_price = int(target.get("coin_price") or 0)
    if coin_price > 0:
        return
    paid = [i for i in queue if i.get("paid")]
    if not paid:
        return
    free_through = int(target.get("free_through") or 0)
    numbers = [i.get("global") for i in paid if i.get("global") is not None]
    where = f" (from chapter {min(numbers)})" if numbers else ""
    raise ValueError(
        f"{len(paid)} of the {len(queue)} chapters in this run are past the free cutoff "
        f"of chapter {free_through}{where}, so they need a coin price — otherwise they "
        f"post as paid at 0 coins and readers cannot buy them. Set the coins, or move the "
        f"free cutoff past the end of the series to publish it all free.")


def request_run(sid: str, target_id: str | None = None, *, options: dict | None = None,
                adapter_name: str = "meiko") -> dict:
    """Queue a posting run for an open browser to pick up.

    Writes nothing to the site. All this does is leave a request where the extension will
    find it on its next poll — which is also what makes triggering from a phone work, since
    the run waits in a file rather than in whatever tab pressed the button.
    """
    _series, target = _resolve_target(sid, target_id)
    opts = clean_options(options)

    existing = load_run(sid, target["id"])
    if _live(existing):
        raise ValueError("A run is already going for this series. Cancel it first.")

    plan = plan_run(sid, target["id"], start=opts["start"], end=opts["end"],
                    adapter_name=adapter_name)
    queue = eligible_items(plan, opts)
    if not queue:
        raise ValueError("Nothing in that range is ready to post.")
    # After the queue, not before: whether a price is needed depends on whether this run
    # actually reaches past the free cutoff. See check_pricing.
    check_pricing(target, queue)

    run = {
        "id": uuid.uuid4().hex[:12],
        "series_id": sid,
        "series_name": plan["series"]["name"],
        "target_id": target["id"],
        "series_url": str(target.get("series_url") or ""),
        "state": "queued",
        "options": opts,
        # Copied in rather than read live, so editing the coin price mid-run cannot change
        # what the rest of the run charges.
        # All THREE inputs to what a chapter costs. side_paid runs the identical path to
        # coin_price -- read live at plan time, it decides `paid` for every side row and
        # so decides its coins -- and twenty side stories have already been given away
        # through it. Snapshotting two of the three and calling it a pricing guard would
        # be overclaiming.
        "pricing": {"free_through": int(target.get("free_through") or 0),
                    "coin_price": int(target.get("coin_price") or 0),
                    "side_paid": bool(target.get("side_paid", True))},
        "requested_at": _now(),
        "claimed_by": None,
        "claimed_at": None,
        "heartbeat_at": None,
        "finished_at": None,
        "progress": {
            "current": None,
            "posted": [],
            "failed": [],
            "total": len(queue),
            "first": queue[0]["title"],
            "last": queue[-1]["title"],
        },
    }
    _write_json(run_path(sid, target["id"]), run)
    return run_view(run)


def _url_matches(series_url: str, tab_url: str) -> bool:
    """Whether a browser tab is on the series a run belongs to.

    Prefix rather than equality, because the extension may be sitting on a chapter inside
    the series rather than on its chapter list.
    """
    base = (series_url or "").strip().rstrip("/").casefold()
    here = (tab_url or "").strip().rstrip("/").casefold()
    return bool(base) and bool(here) and here.startswith(base)


def claim_run(tab_url: str, token: str) -> dict | None:
    """Hand the queued run for this tab's series to the browser asking for it, once.

    A compare-and-set: only a ``queued`` run is handed out, and handing it out stamps the
    caller's token into it. A second tab — or a second Chrome window on the same series —
    asks a moment later, finds the run already running, and gets nothing. Without that, two
    tabs post the same chapter twice, and on a coin platform a double post means refunds.
    """
    if not (token or "").strip():
        raise ValueError("Missing browser token.")
    for series in series_mod.list_series():
        for target in series.get("publish_targets") or []:
            if not _url_matches(str(target.get("series_url") or ""), tab_url):
                continue
            tid = target.get("id") or ""
            run = load_run(series["id"], tid)
            if run is None or run.get("state") != "queued":
                continue
            run["state"] = "running"
            run["claimed_by"] = token
            run["claimed_at"] = _now()
            run["heartbeat_at"] = run["claimed_at"]
            _write_json(run_path(series["id"], tid), run)
            # The token IS returned here, and only here: the browser needs it to prove on
            # every later report that the run is still the one it claimed.
            return {**run_view(run), "token": token}
    return None


def record_progress(sid: str, target_id: str, token: str,
                    event: dict | None = None) -> dict:
    """A heartbeat, and optionally one thing that just happened.

    Returns ``cancelled`` so the extension learns to stop on its next report rather than
    needing anything pushed at it — the same cooperative stop the translation worker uses,
    polled instead of awaited.
    """
    run = load_run(sid, target_id)
    if run is None:
        raise ValueError("No run for this series.")
    if run.get("claimed_by") and token and run["claimed_by"] != token:
        # Another browser holds this run. Tell this one to stop rather than letting two of
        # them interleave progress into one file.
        return {"cancelled": True, "why": "another browser is running this",
                "run": run_view(run)}

    progress = run.setdefault("progress",
                              {"posted": [], "failed": [], "current": None})
    kind = str((event or {}).get("kind") or "beat")
    run["heartbeat_at"] = _now()

    if kind == "queue":
        # The browser has seen the site's own chapter list, which can be ahead of the
        # snapshot the total was planned from. Its count is the truer one, so the page does
        # not sit at "3 of 5" on a run that only ever had three chapters to do.
        total = (event or {}).get("total")
        if isinstance(total, int) and total >= 0:
            progress["total"] = total
        for field in ("first", "last"):
            if (event or {}).get(field):
                progress[field] = (event or {})[field]
    elif kind == "current":
        progress["current"] = (event or {}).get("title")
    elif kind == "posted":
        progress["current"] = None
        progress.setdefault("posted", []).append({
            "title": (event or {}).get("title"),
            "global": (event or {}).get("global"),
            "coins": (event or {}).get("coins"),
            "at": run["heartbeat_at"],
        })
    elif kind == "failed":
        progress["current"] = None
        progress.setdefault("failed", []).append({
            "title": (event or {}).get("title"),
            "error": str((event or {}).get("error") or "")[:500],
            "at": run["heartbeat_at"],
        })
        # One failure stops the run. Never retry blindly into a broken state: the usual
        # cause is an expired session or a chapter the site refused, and pressing on would
        # turn one problem into thirty.
        if run.get("state") == "running":
            run["state"] = "failed"
            run["finished_at"] = run["heartbeat_at"]
            # The whole point of starting a run from a phone is that nobody is at the desk
            # when it stops, so a failure that only shows in the app is one nobody sees for
            # hours. No-op until a notification address is configured.
            remote.notify(
                f"Posting stopped: {run.get('series_name') or 'a series'}",
                f"{(event or {}).get('title') or 'A chapter'} - "
                f"{(event or {}).get('error') or 'unknown error'}. "
                f"{len(progress.get('posted') or [])} posted before it stopped.")
    elif kind == "finished":
        if run.get("state") == "running":
            run["state"] = "done"
            run["finished_at"] = run["heartbeat_at"]
            done = len(progress.get("posted") or [])
            remote.notify(
                f"Posting finished: {run.get('series_name') or 'a series'}",
                f"{done} chapter{'' if done == 1 else 's'} posted.")
        progress["current"] = None

    _write_json(run_path(sid, target_id), run)
    return {"cancelled": run.get("state") == "cancelled", "run": run_view(run)}


def cancel_run(sid: str, target_id: str | None = None) -> dict | None:
    """Stop a run. The browser finds out on its next report and stops there.

    A chapter already in flight is allowed to finish — interrupting between the create and
    the fill is what leaves a half-made chapter behind, and deleting one on the site means
    retyping its name.
    """
    _series, target = _resolve_target(sid, target_id)
    run = load_run(sid, target["id"])
    if run is None:
        return None
    if run.get("state") in ("queued", "running"):
        run["state"] = "cancelled"
        run["finished_at"] = _now()
        _write_json(run_path(sid, target["id"]), run)
    return run_view(run)


def resume_run(sid: str, target_id: str | None = None) -> dict:
    """Put a stalled or stopped run back in the queue for whichever browser picks it up.

    Its progress is kept, so the page keeps showing what already went out; the ledger and
    the site snapshot are what stop those chapters going out a second time.
    """
    _series, target = _resolve_target(sid, target_id)
    run = load_run(sid, target["id"])
    if run is None:
        raise ValueError("No run to resume.")
    if _live(run):
        raise ValueError("That run is still going.")
    # What actually holds a price steady is the PLAN, not this run: the browser fetches
    # one plan per claim and posts the coins it carries, so a price edited while a batch
    # is running cannot reach that batch. A resume fetches a SECOND plan, and plan_run
    # reads the target live -- so the rest of the run would go out at whatever the price
    # is now, priced differently from the part already published, with no way to tell
    # afterwards which chapter got which. The snapshot was written at request time and
    # read by nothing until here; this is what it was for.
    now = {"free_through": int(target.get("free_through") or 0),
           "coin_price": int(target.get("coin_price") or 0),
           "side_paid": bool(target.get("side_paid", True))}
    # Compared over the keys the RUN carries, not over `now`: a run written before a
    # field existed carries fewer of them, and must not be refused for what it could
    # never have stored.
    was = run.get("pricing") or {}
    # Named rather than described, so the message is true whichever field moved: quoting
    # the coin price when side_paid is what changed would print two identical numbers.
    changed = [key for key, value in was.items() if key in now and now[key] != value]
    if changed:
        moved = ", ".join(f"{key} {was[key]} -> {now[key]}" for key in changed)
        raise ValueError(
            f"The pricing changed after this run was requested ({moved}). Resuming "
            f"would charge the rest of it differently from the part already posted — "
            f"cancel it and press Start to re-plan at the new price.")
    run["state"] = "queued"
    run["claimed_by"] = None
    run["claimed_at"] = None
    run["heartbeat_at"] = None
    run["finished_at"] = None
    (run.get("progress") or {})["current"] = None
    _write_json(run_path(sid, target["id"]), run)
    return run_view(run)
