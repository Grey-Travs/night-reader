"""Detect and strip AI "thinking out loud" that leaks into translated prose.

Even with the thinking channel enabled, the model occasionally writes meta-commentary
into the answer body — discussing names/the glossary, or producing a wrong draft and
then "redoing" it after a ``---`` separator. That text must never reach a chapter.

This module is the single source of truth used by:
- the translator (strip leaked reasoning from every fresh translation),
- validation (flag any residual leak as needs-review so it's never silently accepted),
- the one-off cleanup that scrubs already-saved chapters.

Patterns are deliberately PRECISE: a false positive would delete real story prose, so
phrases that characters actually say ("I apologize", "let me check") are NOT signals.
"""

from __future__ import annotations

import re

# ALWAYS signals — phrases/notation that don't occur in real web-novel prose, so they
# mark a block as meta even if it also contains dialogue quotes.
_ALWAYS = re.compile(
    r"""(?ix)
      \bglossary\b                                       # "the glossary says…", "per glossary"
    | the\ narrator(\ here)?\ is
    | the\ (original|source)(\ korean| \ text)?\ (say|read|is|wa|use|mean)
    | i'?ll\ use\ the\ spelling
    | re-?reading\ the\ (chapter|source|glossary|names?|passage)
    | romaniz(e|ed|ing|ation)
    | ===\s*new_terms
    | \bas\ an\ ai\b
    | translator'?s?\ note\b
    """
)

# Korean text immediately followed by an arrow to Latin — glossary-mapping notation
# leaking into prose (e.g. "고원 -> Go Won").
_ARROW = re.compile(r"[가-힣]\s*-+>\s*[A-Za-z]")

# SELF-CORRECTION / FRAMING — the model narrating its own task or addressing the
# reader ("Here is the translation", "Let me redo", "Sure, here you go"). Only counts
# when the block has NO dialogue quotes, so "'Let me redo my makeup,' she said" is safe.
_SELF = re.compile(
    r"(?i)\blet'?s?\s+re-?do\b"
    r"|\blet\s+me\s+(re-?do|re-?read|re-?translate|reset|rewrite|start\s+over|"
    r"translate|produce|fix|correct|reconsider|use\b)"
    r"|\bhere\s+(is|'?s)\s+(the\s+|your\s+|my\s+)?(translat|chapter\b)"
    r"|\bbelow\s+is\s+the\s+translat"
    r"|\bthe\s+translation\s+(is\s+(as\s+follows|below)|follows|begins)"
    r"|\bi\s+(will|'?ll|'?ve|have)\s+(now\s+)?translat"
    r"|\btranslated\s+chapter\s*:"
)

_QUOTE = re.compile(r'["“”「」『』]')
_HR = re.compile(r"^\s*(?:[-*_]\s*){3,}$")
_HANGUL = re.compile(r"[가-힣]")


def _hangul_fraction(block: str) -> float:
    body = re.sub(r"\s", "", block)
    return len(_HANGUL.findall(block)) / len(body) if body else 0.0


def _block_is_meta(block: str) -> bool:
    """A block of leaked AI reasoning/meta. Deliberately precise — a mostly-Korean
    block is NOT treated as meta here (that's an untranslated-source issue handled
    separately) so the strip never deletes a sound effect or a real passage."""
    if _ALWAYS.search(block) or _ARROW.search(block):
        return True
    return bool(_SELF.search(block) and not _QUOTE.search(block))


def korean_fraction(text: str) -> float:
    """Overall fraction of non-space characters that are Korean — used to flag a
    translation that left substantial untranslated source in it."""
    return _hangul_fraction(text)


def remove_snippets(text: str, snippets: list[str]) -> tuple[str, int]:
    """Remove exact verbatim substrings (e.g. from the AI deep-check) and tidy up the
    blank lines left behind. Only removes snippets that appear verbatim, so the user
    can trust that what they confirmed is exactly what goes."""
    removed = 0
    for snip in snippets:
        snip = (snip or "").strip()
        if snip and snip in text:
            text = text.replace(snip, "")
            removed += 1
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(), removed


def find_leaks(text: str) -> list[str]:
    """Return meta/reasoning blocks present in the text (empty if clean)."""
    blocks = re.split(r"\n\s*\n", (text or "").strip())
    return [b.strip()[:160] for b in blocks if _block_is_meta(b)]


def has_leak(text: str) -> bool:
    return bool(find_leaks(text))


def strip_reasoning(text: str) -> tuple[str, list[str]]:
    """Remove leaked reasoning. Returns (cleaned_text, removed_blocks).

    Two strategies:
    1. Redo recovery — a leaked draft + meta near the top, immediately followed by a
       ``---`` separator, means the model restarted; drop everything up to and
       including that separator and keep the redo.
    2. Otherwise drop the individual meta blocks wherever they appear.
    """
    text = (text or "").strip()
    blocks = re.split(r"\n\s*\n", text)
    flags = [_block_is_meta(b) for b in blocks]
    if not any(flags):
        return text, []

    first = flags.index(True)
    run_end = first
    while run_end + 1 < len(blocks) and flags[run_end + 1]:
        run_end += 1

    # 1. Redo recovery: meta in the first ~60% directly followed by a horizontal rule
    #    — a discarded draft + reasoning before the real (re)translation.
    if first <= max(1, int(len(blocks) * 0.6)):
        nxt = run_end + 1
        if nxt < len(blocks) - 1 and _HR.match(blocks[nxt].strip()):
            removed = blocks[: nxt + 1]
            cleaned, more = strip_reasoning("\n\n".join(blocks[nxt + 1:]))
            return cleaned, removed + more

    # 2. Leading preamble: when a "let me translate" marker sits in the first few
    #    blocks, drop the leading run of meta / Korean-echo / separator blocks up to
    #    the first clean English block (handles source-echo preambles without a ---).
    if any(flags[:3]):
        i = 0
        while i < len(blocks) and (flags[i] or _HR.match(blocks[i].strip()) or _HANGUL.search(blocks[i])):
            i += 1
        if 0 < i < len(blocks):
            removed = blocks[:i]
            cleaned, more = strip_reasoning("\n\n".join(blocks[i:]))
            return cleaned, removed + more

    # 3. Otherwise drop the individual meta blocks wherever they appear.
    kept = [b for b, m in zip(blocks, flags) if not m]
    removed = [b for b, m in zip(blocks, flags) if m]
    return "\n\n".join(kept).strip(), removed
