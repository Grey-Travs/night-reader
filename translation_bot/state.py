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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class State:
    def __init__(self, data: dict | None = None):
        self.chapters: dict[str, dict] = (data or {}).get("chapters", {})

    @classmethod
    def load(cls, path: str | Path) -> "State":
        path = Path(path)
        if not path.exists():
            return cls()
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps({"chapters": self.chapters}, ensure_ascii=False, indent=2),
            encoding="utf-8",
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
        rec.update(fields)
        rec["updated_at"] = _now()
        rec.setdefault("created_at", rec["updated_at"])
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
