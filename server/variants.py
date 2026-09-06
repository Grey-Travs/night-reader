"""Per-paragraph rewrite history for one chapter.

Lives in ``projects/<pid>/variants/chapter-NN.json``, deliberately NOT in:

- ``previous/`` — that holds ONE whole-chapter snapshot, overwritten on every write.
  Five paragraph picks in a row would destroy the snapshot taken before the last
  translation or AI resolve.
- ``state.json`` — the worker's hot path. Paragraph prose would bloat it and every
  variant write would contend with the translation worker's lock.

A group is one paragraph's history. ``variants[0]`` is always the text as it stood
the first time that paragraph was touched, so reverting to the original is simply
picking it, and nothing is ever lost. ``current_id`` says which version is spliced
into the chapter file right now, which makes pick and revert the same code path.

Groups are keyed by a random id rather than by ordinal, so an unrelated edit
elsewhere in the chapter cannot reshuffle history onto the wrong paragraph.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from translation_bot.atomic import atomic_write_text
from translation_bot.pipeline import chapter_filename

from .locks import file_lock
from .projects import PROJECTS_DIR

VARIANTS_DIRNAME = "variants"

# Kinds a variant can be. "original" is reserved for index 0 of every group.
KIND_ORIGINAL = "original"
KIND_RETRANSLATE = "retranslate"
KIND_REPHRASE = "rephrase"

MAX_GROUPS = 200
MAX_VARIANTS = 12


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- paths -------------------------------------------------------------------

def variants_dir(pid: str) -> Path:
    return PROJECTS_DIR / pid / VARIANTS_DIRNAME


def variants_path(pid: str, index: int, total: int) -> Path:
    """Mirrors the chapter file's own name, including its zero padding."""
    return variants_dir(pid) / (Path(chapter_filename(index, total)).stem + ".json")


# ---- storage -----------------------------------------------------------------

def new_doc(index: int) -> dict:
    return {"version": 1, "chapter": index, "groups": []}


def load_variants(path: Path, index: int = 0) -> dict:
    """Read a chapter's history. A missing or corrupt file reads as empty rather than
    raising — losing edit history must never make a chapter unreadable."""
    if not Path(path).exists():
        return new_doc(index)
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return new_doc(index)
    if not isinstance(doc, dict):
        return new_doc(index)
    doc.setdefault("version", 1)
    doc.setdefault("chapter", index)
    if not isinstance(doc.get("groups"), list):
        doc["groups"] = []
    return doc


def save_variants(path: Path, doc: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(Path(path), json.dumps(doc, ensure_ascii=False, indent=2))


@contextmanager
def mutate_variants(path: Path, index: int = 0):
    """Load → mutate → save with no other thread interleaving.

    Yields a FRESHLY LOADED document: anything read before the lock was taken is
    already stale.
    """
    with file_lock(path):
        doc = load_variants(path, index)
        yield doc
        save_variants(path, doc)


# ---- groups and variants -----------------------------------------------------

def find_group(doc: dict, group_id: str) -> dict | None:
    for group in doc.get("groups", []):
        if group.get("id") == group_id:
            return group
    return None


def group_for_paragraph(doc: dict, paragraph: int) -> dict | None:
    for group in doc.get("groups", []):
        if group.get("paragraph") == paragraph and not group.get("stale"):
            return group
    return None


def new_group(doc: dict, paragraph: int, original: str, *,
              source_ko: str | None = None, alignment: dict | None = None) -> dict:
    """Start a paragraph's history, seeded with the text as it stands now.

    That seed IS revert-to-original, permanently — it is never pruned.
    """
    group = {
        "id": uuid.uuid4().hex[:8],
        "paragraph": paragraph,
        "original": original,
        "current_id": "v0",
        "stale": False,
        "source_ko": source_ko,
        "alignment": alignment,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "variants": [{
            "id": "v0",
            "kind": KIND_ORIGINAL,
            "text": original,
            "instruction": None,
            "usage": {},
            "cost_usd": 0.0,
            "warnings": [],
            "created_at": now_iso(),
        }],
    }
    doc.setdefault("groups", []).append(group)
    return group


def add_variant(group: dict, *, kind: str, text: str, instruction: str | None = None,
                usage: dict | None = None, cost: float = 0.0,
                warnings: tuple[str, ...] = ()) -> dict:
    """Append a generated version. Does NOT make it current — the reader picks."""
    variants = group.setdefault("variants", [])
    variant = {
        "id": f"v{len(variants)}",
        "kind": kind,
        "text": text,
        "instruction": instruction,
        "usage": usage or {},
        "cost_usd": round(float(cost or 0.0), 6),
        "warnings": list(warnings),
        "created_at": now_iso(),
    }
    variants.append(variant)
    group["updated_at"] = now_iso()
    return variant


def find_variant(group: dict, variant_id: str) -> dict | None:
    for variant in group.get("variants", []):
        if variant.get("id") == variant_id:
            return variant
    return None


def current_text(group: dict) -> str:
    variant = find_variant(group, group.get("current_id") or "v0")
    return (variant or {}).get("text", group.get("original", ""))


def prior_texts(group: dict) -> list[str]:
    """Everything already produced, for the model's do-not-repeat clause."""
    return [v.get("text", "") for v in group.get("variants", []) if v.get("text")]


# ---- re-anchoring ------------------------------------------------------------

def _unique_index(blocks: list[str], text: str) -> int | None:
    text = (text or "").strip()
    if not text:
        return None
    hits = [i for i, b in enumerate(blocks) if b == text]
    return hits[0] if len(hits) == 1 else None


def relocate(doc: dict, blocks: list[str]) -> None:
    """Re-point every group at the paragraph it now describes.

    A whole-chapter edit, an AI resolve or a consistency rename can move or rewrite
    paragraphs underneath this history. Anchor by content — the version currently in
    the chapter first, then the original — and mark anything that no longer matches
    ``stale`` rather than deleting it. History is never thrown away automatically;
    the reader is offered an explicit discard instead.
    """
    for group in doc.get("groups", []):
        index = _unique_index(blocks, current_text(group))
        if index is None:
            index = _unique_index(blocks, group.get("original", ""))
        if index is None:
            group["stale"] = True
        else:
            group["paragraph"] = index
            group["stale"] = False


def prune(doc: dict, *, max_groups: int = MAX_GROUPS,
          max_variants: int = MAX_VARIANTS) -> None:
    """Keep the file bounded, dropping from the middle.

    Never removes ``v0`` (revert-to-original) or whichever version is currently in
    the chapter.
    """
    for group in doc.get("groups", []):
        variants = group.get("variants", [])
        if len(variants) <= max_variants:
            continue
        # v0 is revert-to-original and the current one is in the chapter right now;
        # neither may be dropped. Otherwise drop oldest-first, keeping the order.
        protected = {variants[0].get("id"), group.get("current_id") or "v0"}
        droppable = [i for i, v in enumerate(variants) if v.get("id") not in protected]
        doomed = set(droppable[:len(variants) - max_variants])
        group["variants"] = [v for i, v in enumerate(variants) if i not in doomed]

    groups = doc.get("groups", [])
    if len(groups) > max_groups:
        # Oldest first, so the paragraphs being worked on now survive.
        doc["groups"] = groups[-max_groups:]


def public(doc: dict) -> list[dict]:
    """The groups as the reader's panel wants them."""
    return doc.get("groups", [])
