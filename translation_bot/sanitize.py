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
    | the\ (original|source)\ korean\b                    # "the original Korean says…" (translator-speak)
    | i'?ll\ use\ the\ spelling
    | re-?reading\ the\ (chapter|source|glossary|names?|passage)
    | romaniz(e|ed|ing|ation)
    | ===\s*new_terms
    | \bas\ an\ ai\b
    | translator'?s?\ note\b
    """
)

# Hangul characters. Plain [가-힣] only covers composed syllables and misses
# isolated/compatibility jamo (ㅋㅋㅋ, ㅎㅎ, ㅏ), conjoining jamo, and half-width
# Hangul — which would let a jamo-heavy Korean chapter look "already English" and
# get skipped, or an untranslated jamo echo slip past the leak checks.
_HANGUL_CHARS = "가-힣ᄀ-ᇿ㄰-㆏ﾠ-ￜ"

# Korean text joined to a Latin gloss by an arrow or an equals sign — source->target
# mapping notation leaking into prose (e.g. "고원 -> Go Won", "고원 → Go Won",
# "승연 = Seung Yeon", or a quoted target like '세레나데 → "Serenade"'). Requires Hangul
# immediately before AND Latin after, so a decorative in-story arrow run ("→→↓↔") that has
# no Hangul beside it is never matched.
_ARROW = re.compile(
    f"[{_HANGUL_CHARS}]" + r"[\s.,!?…\"'”’]*(?:-+>|=+>|=|→|⇒|➔|⟶)\s*[\"“'‘(\[]?\s*[A-Za-z]"
)

# SELF-CORRECTION / FRAMING — the model narrating its own task or addressing the
# reader ("Here is the translation", "Let me redo"). Only counts when the block has
# NO dialogue quotes, so "'Let me redo my makeup,' she said" is safe. Verbs after
# "let me" are restricted to unambiguous translation-meta (redo/re-read/rewrite/…);
# broad story verbs (write/continue/fix/translate-without-an-object) are deliberately
# NOT matched, so real narration like "Let me write you a letter, she decided" or
# "I will translate the runes" is never deleted. Object-less meta leaks are caught by
# the AI deep-check instead — losing a leak is recoverable, deleting prose is not.
_SELF = re.compile(
    r"(?i)\blet'?s?\s+re-?do\b"
    r"|\blet\s+me\s+(?:just\s+|now\s+|simply\s+|carefully\s+|go\s+ahead\s+and\s+)?"
    r"(?:re-?do|re-?read|re-?translate|rewrite|start\s+over)\b"
    # "let me / I'll translate|render|… THE (full/whole) chapter/translation/text" — an
    # explicit self-referential object (the chapter/translation itself) is required, with
    # an optional size adjective, so ordinary narration ("let me translate their language
    # for you") can't trip it.
    r"|\b(?:i'?ll|i\s+will|let\s+me|let'?s)\s+(?:just\s+|now\s+|simply\s+|go\s+ahead\s+and\s+)?"
    r"(?:translate|render|produce|provide|rewrite|give\s+you)\s+"
    r"(?:the\s+|this\s+|your\s+|my\s+|a\s+)?(?:full\s+|whole\s+|entire\s+|rest\s+of\s+the\s+)?"
    r"(?:translat\w*|chapter|text|passage|version|content|section|following)\b"
    # Bare task announcement that is essentially the WHOLE block — "Let me translate." /
    # "I'll render." standing alone. Anchored to the block so it NEVER fires on a marker
    # trailing real prose (e.g. "…vivid sensation. Let me translate."): deleting that block
    # would drop the real English before it. Such MIXED leaks (Korean echo/English + a
    # trailing marker) are caught by validation's untranslated-Korean flag instead — routed
    # to needs-review, not silently deleted. (Losing a leak is recoverable; deleting prose
    # is not.)
    r"|^\s*(?:i'?ll|i\s+will|let\s+me|let'?s)\s+(?:just\s+|now\s+|simply\s+|go\s+ahead\s+and\s+)?"
    r"(?:translate|render)\s*[.!?…]?\s*$"
    r"|\bhere(?:\s+is|'?s)\s+(?:the\s+|your\s+|my\s+)?translat"
    r"|\bbelow\s+is\s+the\s+translat"
    r"|\bthe\s+translation\s+(?:is\s+(?:as\s+follows|below)|follows|begins)"
    r"|\btranslated\s+chapter\s*:"
)

_QUOTE = re.compile(r'["“”「」『』]')
_HR = re.compile(r"^\s*(?:[-*_]\s*){3,}$")

# Untranslated-Korean detection counts ONLY composed Hangul SYLLABLES (가-힣) — actual
# words. Compatibility jamo (ㅠㅠ, ㅋㅋㅋ, ㅇㅇ, ㅜ, ㅡㅡ) are text-emoticons/laughter that
# these web novels legitimately keep in chat/SNS scenes, so they must NEVER count as a
# leak or be stripped. (Source-language detection in docs_extract.py is intentionally
# broader and DOES include jamo — a jamo-heavy tab is still a Korean tab to translate.)
_HANGUL = re.compile(r"[가-힣]")
# A leaked untranslated Korean PHRASE: two or more Korean syllable-words separated by
# whitespace (a lone emoticon or a single kept term/sound-effect can't match).
_KOREAN_PHRASE = re.compile(r"[가-힣]+[.,!?…\"'”’)\]]*\s+[가-힣]")


def _hangul_fraction(block: str) -> float:
    body = re.sub(r"\s", "", block)
    return len(_HANGUL.findall(block)) / len(body) if body else 0.0


def _is_korean_echo(block: str) -> bool:
    """A whole paragraph that is predominantly untranslated Korean (a source echo) —
    not a short sound effect, an inline Korean term, or a text-emoticon, which we keep."""
    return len(_HANGUL.findall(block)) > 8 and _hangul_fraction(block) > 0.5


def has_korean_leak(text: str, *, min_syllables: int = 8) -> bool:
    """Untranslated Korean beyond a single short token remains: either a multi-word Korean
    phrase (a leaked source sentence) or a substantial run of Korean syllables. Used by
    validation to FLAG such a chapter for review — never to delete text. Counts composed
    syllables only, so kept text-emoticons (ㅠㅠ/ㅋㅋ) and one intentional short term or
    sound-effect are NOT flagged."""
    text = text or ""
    return bool(_KOREAN_PHRASE.search(text)) or len(_HANGUL.findall(text)) >= min_syllables


def _block_is_meta(block: str) -> bool:
    """A block of leaked AI reasoning/meta. Deliberately precise — a mostly-Korean
    block is NOT treated as meta here (that's an untranslated-source issue handled
    separately) so the strip never deletes a sound effect or a real passage."""
    if _ALWAYS.search(block) or _ARROW.search(block):
        return True
    return bool(_SELF.search(block) and not _QUOTE.search(block))


def korean_fraction(text: str) -> float:
    """Overall fraction of non-space characters that are untranslated Korean SYLLABLES
    (composed 가-힣, not emoticon jamo) — used to flag a translation that left substantial
    untranslated source in it."""
    return _hangul_fraction(text)


# --- source-export header cruft (ridibooks etc.): URL · title · "4-5 minutes" · "NNN화" ---
_HDR_URL = re.compile(r"https?://|ridibooks\.com", re.I)
_HDR_MIN = re.compile(r"^\s*\d+\s*(?:[-–]\s*\d+\s*)?min(?:ute)?s?\.?\s*$", re.I)
# A header line ENDING in a chapter marker — "NNN화", "Chapter N", or "Title — Chapter N".
_HDR_ENDNUM = re.compile(r"(?:chapter|ch\.?|episode|ep\.?)\s+(\d+)\s*$|(\d+)\s*화\s*$", re.I)
# A block that is nothing but a number: "29" / "29."
_BARE_NUM = re.compile(r"^(\d{1,4})\s*\.?$")


def is_chapter_number_block(block: str, number: str | None) -> bool:
    """True when ``block`` is a bare number that merely repeats the chapter's own number.

    The single rule for "this digit block is export cruft, not content", shared by the
    source stripper and by the one-off repair of already-saved chapters, so the two can
    never drift apart. Without a known chapter number nothing qualifies — a bare number
    we cannot tie to the header is in-story content.
    """
    if not number:
        return False
    m = _BARE_NUM.match((block or "").strip())
    return bool(m) and int(m.group(1)) == int(number)


def strip_source_header(text: str) -> tuple[str, str | None]:
    """Remove the leading export-header block (URL, novel title, reading-time, and the
    "NNN화"/"Chapter N" line) from a chapter, and return (clean_text, chapter_number).

    The export then repeats the chapter number on its own ("…29화" and, right under it,
    "29."), and that bare block used to be kept — which put a stray "29." at the top of
    the saved translation. It is invisible in the reader, because a lone "29." is an EMPTY
    ordered-list item in Markdown and the reading CSS gives lists no styling, but it is
    still in the file and shows up the moment a chapter is copied or edited.

    A bare number is only dropped when it REPEATS the number the header line already gave
    us. A part marker that differs ("33." inside chapter 29) is in-story content and is
    kept, as is any bare number in a chapter whose header never established a number —
    removing those would be a guess.

    Stops at the first real line, so only the contiguous header at the very top is touched.
    """
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
    if i < len(blocks) and is_chapter_number_block(blocks[i], number):
        i += 1
    return "\n\n".join(blocks[i:]).strip(), number


# The closing rights notice every RIDI export carries, in Korean and in the English the
# model produces when it translates it instead of dropping it.
_FOOTER_MARK = re.compile(
    r"본\s*저작물의\s*권리|저작권자에게\s*있습니다|무단\s*전재|"
    r"rights?\s+(?:to|in|of)\s+this\s+work|copyright\s+holder|"
    r"criminal\s+(?:punishment|penalt)",
    re.I)


def strip_export_footer(text: str) -> str:
    """Remove the export's closing copyright notice from the END of a chapter.

    "※ 본 저작물의 권리는 저작권자에게 있습니다…" is the last paragraph of every RIDI tab.
    Nothing used to remove it, so the model saw it, and in 132 chapters translated it —
    leaving a legal boilerplate paragraph glued to the end of the prose.

    Only TRAILING blocks are considered: a line about rights in the middle of a chapter is
    part of the story. The length cap keeps a long paragraph that merely mentions a
    copyright holder from being mistaken for the notice.
    """
    blocks = re.split(r"\n\s*\n", (text or "").strip())
    j = len(blocks)
    while j > 0:
        b = blocks[j - 1].strip()
        if b and len(b) <= 400 and _FOOTER_MARK.search(b):
            j -= 1
            continue
        break
    return "\n\n".join(blocks[:j]).strip()


def remove_korean_echoes(text: str) -> tuple[str, int]:
    """Remove paragraphs that are predominantly untranslated Korean — source the model
    echoed and then translated right after, leaving a redundant Korean copy. Keeps short
    bits (e.g. an in-line sound effect); only whole Korean sentences are dropped."""
    blocks = re.split(r"\n\s*\n", (text or "").strip())
    kept, removed = [], 0
    for b in blocks:
        if _is_korean_echo(b):
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
        # Only walk past meta blocks, horizontal rules, and WHOLE-paragraph Korean
        # echoes — never a real prose block that merely contains some Hangul (a sound
        # effect or kept term), which would silently delete translated content.
        while i < len(blocks) and (flags[i] or _HR.match(blocks[i].strip()) or _is_korean_echo(blocks[i])):
            i += 1
        if 0 < i < len(blocks):
            removed = blocks[:i]
            cleaned, more = strip_reasoning("\n\n".join(blocks[i:]))
            return cleaned, removed + more

    # 3. Otherwise drop the individual meta blocks wherever they appear.
    kept = [b for b, m in zip(blocks, flags) if not m]
    removed = [b for b, m in zip(blocks, flags) if m]
    return "\n\n".join(kept).strip(), removed
