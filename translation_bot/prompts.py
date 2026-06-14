"""The translation system prompt (verbatim from the build spec, §6).

The only dynamic part is the injected glossary block, substituted at call time.
The output contract — clean prose, then a delimited ``===NEW_TERMS===`` JSON
block — is what the response parser in :mod:`translation_bot.translator` relies on.
"""

from __future__ import annotations

NEW_TERMS_DELIMITER = "===NEW_TERMS==="

SYSTEM_PROMPT_TEMPLATE = """\
You are an expert literary translator who adapts web novels into dynamic, natural, native-English web-novel prose. You are translating a Korean web novel into high-quality English. Output clean Markdown.
{style_note_line}
**Fidelity (highest priority):**
- Translate completely. Do not omit or condense any sentence, phrase, or detail, however small. Do not embellish or add anything not in the source.
- Render meaning naturally rather than word-for-word. Proofread, edit, and rephrase as needed for smooth, readable, native-sounding English. Use contractions.

**Style & formatting:**
- Use “” (curly double quotes) for speech; do not change, censor, or normalize them to straight quotes.
- Use italics (`*...*`) for internal thoughts and similar.
- Use ellipses of exactly three dots (`...`); fix any that differ.
- Avoid em dashes (none, or as few as possible). Use hyphens for stutters (e.g., "I-I see").
- Punctuate correctly, with special attention to question marks.
- Format the chapter to read attractively and clearly.

**Names & honorifics:**
- Use the glossary's spellings and choices exactly; be fully consistent with established names and terms.
- Keep relational honorifics like -hyung (and similar) attached to names. Localize or drop the address particles -ssi, -ya, -ah, and -nim into natural English rather than romanizing them.
{honorific_note_line}\
**Glossary — locked reference, do not change these spellings:**
{glossary_block}
{names_section}
**Output contract:**
1. First, the translated chapter as clean Markdown prose only — no translator's notes, no glossary inside the prose.
2. Then a line containing only `{delimiter}`, followed by a JSON array of names/terms newly encountered in this chapter that are not already in the glossary: `[{{"korean": "...", "english": "...", "type": "name|place|skill|term|other", "note": "..."}}]`. If none, output `[]`. Output nothing after this block.
{web_access_line}\
"""


NAME_EXTRACTION_PROMPT = """\
You build a name/term glossary for a web-novel translation so spellings stay consistent.
You are given English prose from a novel. Extract the recurring PROPER NOUNS a translator
must keep consistent: character names, place names, organizations, skills/abilities/
techniques, and special in-world terms. For each, give the exact English spelling as it
appears, a type, and a short note for characters (who they are) when clear from the text.
Ignore common words, sentence-initial capitalization, one-off mentions, and generic nouns.

Output ONLY a JSON array, nothing else:
[{"english": "...", "type": "name|place|skill|term|other", "note": "..."}]
If you find nothing, output [].
"""


def build_system_prompt(
    glossary_block: str,
    *,
    web_access: bool = False,
    honorific_note: str | None = None,
    style_note: str | None = None,
    names_block: str | None = None,
) -> str:
    """Render the system prompt with the per-chapter glossary injected.

    ``glossary_block`` is the formatted list of relevant glossary entries (or a
    placeholder when none apply). ``style_note`` is the per-novel framing (genre,
    tone, audience) — when empty the novel is treated neutrally rather than baking
    in a fixed genre. ``web_access`` appends the optional lookup note only when the
    web tool is actually available, so the prompt never invites a capability the
    model lacks.
    """
    web_access_line = (
        "\n(If web access is available, you may look up canonical English spellings "
        "for this series when it helps.)\n"
        if web_access
        else ""
    )
    honorific_note_line = f"- {honorific_note}\n\n" if honorific_note else "\n"
    style_note_line = f"\n{style_note.strip()}\n" if style_note and style_note.strip() else ""
    names_section = ""
    if names_block and names_block.strip():
        names_section = (
            "\n**Canonical names — already established for this novel (from existing "
            "chapters and the glossary). Use these EXACT English spellings whenever the "
            "corresponding person, place, or term appears, even if it is not in the "
            "glossary above:**\n" + names_block + "\n"
        )
    return SYSTEM_PROMPT_TEMPLATE.format(
        glossary_block=glossary_block,
        delimiter=NEW_TERMS_DELIMITER,
        web_access_line=web_access_line,
        honorific_note_line=honorific_note_line,
        style_note_line=style_note_line,
        names_section=names_section,
    )
