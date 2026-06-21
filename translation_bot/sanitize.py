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
    r"|\blet\s+me\s+(?:just\s+|now\s+|simply\s+|carefully\s+|go\s+ahead\s+and\s+)?"
    r"(re-?do|re-?read|re-?translate|reset|rewrite|start\s+over|"
    r"translate|produce|render|write|continue|fix|correct|reconsider|use\b)"
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


# --- source-export header cruft (ridibooks etc.): URL · title · "4-5 minutes" · "NNN화" ---
_HDR_URL = re.compile(r"https?://|ridibooks\.com", re.I)
_HDR_MIN = re.compile(r"^\s*\d+\s*(?:[-–]\s*\d+\s*)?min(?:ute)?s?\.?\s*$", re.I)
# A header line ENDING in a chapter marker — "NNN화", "Chapter N", or "Title — Chapter N".
_HDR_ENDNUM = re.compile(r"(?:chapter|ch\.?|episode|ep\.?)\s+(\d+)\s*$|(\d+)\s*화\s*$", re.I)


def strip_source_header(text: str) -> tuple[str, str | None]:
    """Remove the leading export-header block (URL, novel title, reading-time, and the
    "NNN화"/"Chapter N" line) from a chapter, and return (clean_text, chapter_number).

    Deliberately KEEPS a bare part marker like "33." — those are sequential in-story
    section numbers, not junk. Stops at the first real line, so only the contiguous
    header at the very top is touched."""
    blocks = re.split(r"\n\s*\n", (text or "").strip())
    number: str | None = None
    i = 0
    while i < len(blocks):
        b = blocks[i].strip()
        if _HDR_URL.search(b):
            i += 1
            continue
        if _HDR_MIN.match(b):
            i += 1
            continue
        m = _HDR_ENDNUM.search(b)
        if m and len(b) <= 80:        # "114화" / "Chapter 114" / "<Title> Chapter 114"
            number = number or m.group(1) or m.group(2)
            i += 1
            continue
        break
    return "\n\n".join(blocks[i:]).strip(), number


def remove_korean_echoes(text: str) -> tuple[str, int]:
    """Remove paragraphs that are predominantly untranslated Korean — source the model
    echoed and then translated right after, leaving a redundant Korean copy. Keeps short
    bits (e.g. an in-line sound effect); only whole Korean sentences are dropped."""
    blocks = re.split(r"\n\s*\n", (text or "").strip())
    kept, removed = [], 0
    for b in blocks:
        if len(_HANGUL.findall(b)) > 8 and _hangul_fraction(b) > 0.5:
            removed += 1
            continue
        kept.append(b)
    return "\n\n".join(kept).strip(), removed


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
