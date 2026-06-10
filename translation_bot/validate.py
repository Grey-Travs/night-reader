"""Automatic validation of a translated chapter — never silently accept suspect output.

Structural, length-ratio, dialogue, and surface checks flag likely omission,
embellishment, or formatting drift. A failed check triggers one corrective retry
upstream; a still-failing chapter is marked needs-review rather than written as good.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import ValidationConfig
from .docs_extract import Chapter, _QUOTE_RE


@dataclass
class ValidationResult:
    ok: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def _paragraphs(text: str) -> list[str]:
    return [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]


def _nonspace_len(text: str) -> int:
    return len(re.sub(r"\s", "", text))


def validate_translation(
    chapter: Chapter, translation: str, cfg: ValidationConfig
) -> ValidationResult:
    failures: list[str] = []
    warnings: list[str] = []

    src = chapter.metrics
    out_paras = _paragraphs(translation)
    out_para_count = len(out_paras)
    out_dialogue = sum(1 for p in out_paras if _QUOTE_RE.search(p))
    out_chars = _nonspace_len(translation)
    ratio = (out_chars / src.char_count) if src.char_count else 0.0

    metrics = {
        "source_paragraphs": src.paragraph_count,
        "output_paragraphs": out_para_count,
        "source_dialogue": src.dialogue_count,
        "output_dialogue": out_dialogue,
        "source_chars": src.char_count,
        "output_chars": out_chars,
        "length_ratio": round(ratio, 3),
    }

    # 1. Paragraph-count check (structural). Tolerance scales with chapter length so
    #    minor formatting merges don't flag, but a missing scene still does.
    para_tol = max(cfg.paragraph_tolerance, round(src.paragraph_count * cfg.paragraph_tolerance_pct))
    if abs(out_para_count - src.paragraph_count) > para_tol:
        failures.append(
            f"paragraph count {out_para_count} vs source {src.paragraph_count} "
            f"(tolerance {para_tol})"
        )

    # 2. Length-ratio check (omission / embellishment signal).
    if not src.char_count:
        warnings.append("source has zero counted characters; skipping length-ratio check")
    elif ratio < cfg.length_ratio_min:
        failures.append(
            f"length ratio {ratio:.2f} below {cfg.length_ratio_min} — likely omission/summarizing"
        )
    elif ratio > cfg.length_ratio_max:
        failures.append(
            f"length ratio {ratio:.2f} above {cfg.length_ratio_max} — likely embellishment"
        )

    # 3. Dialogue-line check (secondary signal — warn, don't fail).
    if abs(out_dialogue - src.dialogue_count) > cfg.dialogue_tolerance:
        warnings.append(
            f"dialogue lines {out_dialogue} vs source {src.dialogue_count} "
            f"(tolerance {cfg.dialogue_tolerance})"
        )

    # 4. Surface sanity (lightweight regex).
    if re.search(r"(?<!\.)\.\.(?!\.)", translation) or re.search(r"\.{4,}", translation):
        warnings.append("found ellipses that are not exactly three dots")
    if "…" in translation:
        warnings.append("found unicode ellipsis '…' (should be three ASCII dots)")
    if out_dialogue and ('"' in translation) and not re.search(r"[“”]", translation):
        warnings.append("dialogue present but no curly quotes found (straight quotes?)")

    return ValidationResult(
        ok=not failures, failures=failures, warnings=warnings, metrics=metrics
    )
