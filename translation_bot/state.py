"""Per-chapter run state for idempotent, resumable, non-re-billing runs.

``state.json`` tracks each chapter's status, source content hash, token usage,
cost, and timestamps. Re-running skips chapters that are already done unless the
source hash changed (or the user forces a re-translate), so a crash or rate-limit
never forces a full, re-billed re-run.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .atomic import atomic_write_text, quarantine_unreadable

# Lifecycle: pending -> translated -> validated, or -> needs-review / failed.
STATUS_PENDING = "pending"
STATUS_TRANSLATED = "translated"
STATUS_VALIDATED = "validated"
STATUS_NEEDS_REVIEW = "needs-review"
STATUS_FAILED = "failed"
STATUS_EMPTY = "empty"  # tab has no prose (blank placeholder) — nothing to translate
STATUS_ENGLISH = "english-source"  # tab already in English — skipped, not translated

# Statuses that count as "done" for resumability (won't be redone unless forced
# or the source hash changed).
DONE_STATUSES = {STATUS_VALIDATED}

# Statuses a chapter reaches only by Claude having translated it, whether or not the
# checks then passed.
TRANSLATED_STATUSES = {STATUS_VALIDATED, STATUS_NEEDS_REVIEW}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def translated_at(rec: dict) -> str | None:
    """When this chapter was FIRST translated, as near as the record can say.

    ``translated_at`` is stamped once and never moved, so re-translating, fixing pronouns
    or editing the text later leaves it alone (``updated_at`` moves on every one of those).
    Records from before it existed fall back to ``created_at``: the record is normally
    created by the very update that saves the first translation, so that is when it
    happened, give or take a rate-limit pause. :meth:`State.update` uses this same answer
    when it stamps one of those old records, so a chapter never jumps month on the
    Stats page just because it was touched again.
    """
    return rec.get("translated_at") or rec.get("created_at")


class State:
    def __init__(self, data: dict | None = None):
        self.chapters: dict[str, dict] = (data or {}).get("chapters", {})

    @classmethod
    def load(cls, path: str | Path) -> "State":
        path = Path(path)
        if not path.exists():
            return cls()
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
        except (json.JSONDecodeError, OSError, ValueError):
            # A truncated/garbled state.json (e.g. the process was killed mid-write)
            # must not crash the whole library — start fresh for this project instead.
            #
            # But "start fresh" is how a MOMENTARY read failure became permanent: the
            # caller mutates this empty state and mutate_state saves it back, erasing
            # every chapter's status, usage and cost for the novel — which then reads
            # as entirely untranslated and re-bills to redo. Keep the bytes first.
            quarantine_unreadable(path)
            data = {}
        return cls(data if isinstance(data, dict) else {})

    def save(self, path: str | Path) -> None:
        # Atomic write: serialize to a temp file in the same dir, then os.replace — a
        # concurrent reader (now common with enqueue-while-running) never sees a
        # half-written file, only the old or the new one. The server saves this file from
        # several threads at once, so the temp name MUST be unique per call;
        # atomic_write_text owns that (see translation_bot/atomic.py).
        atomic_write_text(
            path, json.dumps({"chapters": self.chapters}, ensure_ascii=False, indent=2)
        )

    def get(self, index: int) -> dict | None:
        return self.chapters.get(str(index))

    def is_done(self, index: int, source_hash: str) -> bool:
        rec = self.get(index)
        return bool(
            rec
            and rec.get("status") in DONE_STATUSES
            and rec.get("source_hash") == source_hash
        )

    def update(self, index: int, **fields) -> dict:
        rec = self.chapters.setdefault(str(index), {})
        before = rec.get("status")
        rec.update(fields)
        rec["updated_at"] = _now()
        rec.setdefault("created_at", rec["updated_at"])
        if fields.get("status") in TRANSLATED_STATUSES and "translated_at" not in rec:
            rec["translated_at"] = translated_at(rec) if before in TRANSLATED_STATUSES else rec["updated_at"]
        return rec

    def add_usage(self, index: int, usage: dict, cost: float) -> None:
        rec = self.chapters.setdefault(str(index), {})
        acc = rec.setdefault("usage", {})
        for k, v in usage.items():
            acc[k] = acc.get(k, 0) + v
        rec["cost_usd"] = round(rec.get("cost_usd", 0.0) + cost, 6)

    def totals(self) -> dict:
        total_cost = 0.0
        total_tokens: dict[str, int] = {}
        for rec in self.chapters.values():
            total_cost += rec.get("cost_usd", 0.0)
            for k, v in rec.get("usage", {}).items():
                total_tokens[k] = total_tokens.get(k, 0) + v
        return {"cost_usd": round(total_cost, 4), "tokens": total_tokens}
