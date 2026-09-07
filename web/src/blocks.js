// Paragraph addressing in the reader.
//
// This is a deliberate mirror of translation_bot/paragraphs.py::split_blocks, and it
// has to STAY a mirror: the server addresses a paragraph by the ordinal this produces
// and proves it with the text this returns. If the two splitters ever disagree, a
// rewrite lands on the wrong paragraph.
//
// Both sides used to write /\n\s*\n/ and call that identical. It is not: `\s` is a
// different character set in the two languages. Python's matches \x1c–\x1f (the
// file/group/record/unit separators) and \x85 (NEL), which JavaScript's does not;
// JavaScript's matches U+FEFF (a byte-order mark), which Python's does not. On a
// chapter carrying any of those six on an otherwise blank line the two disagreed
// about where the paragraphs were, and every rewrite on it was refused with a 409
// that nothing explained. A BOM is the realistic one — invisible, and it survives a
// copy-paste out of a downloaded file.
//
// So each side adds exactly what its own `\s` lacks; see translation_bot/textsplit.py
// for the Python half. The union, not the intersection: a line carrying only an
// invisible control character looks blank, so it should be blank to both.
// tests/test_paragraph_split_parity.py runs both over the same fixtures.
//
// A source string, not a shared RegExp: a /g/ regex carries `lastIndex` between
// calls, so a single shared instance would make the SECOND call to splitBlocks
// start mid-string and silently drop every paragraph before that point.
const SEP_SOURCE = '\\n[\\s\\u001c-\\u001f\\u0085]*\\n'

// Trimming has to match too, and for the same reason. A block's SPAN is what makes
// a splice lossless, so the whitespace skipped at each end must be the same set on
// both sides — and .trim() strips a BOM while Python's str.strip() does not, which
// put every span in a BOM-prefixed chapter off by one on the server side only.
// Non-global, so these are stateless and safe to share.
const LEAD_RE = new RegExp('^[\\s\\u001c-\\u001f\\u0085]+')
const TRAIL_RE = new RegExp('[\\s\\u001c-\\u001f\\u0085]+$')

export function splitBlocks(text) {
  const src = text || ''
  const blocks = []

  const push = (start, end) => {
    const raw = src.slice(start, end)
    const lead = (raw.match(LEAD_RE) || [''])[0].length
    const trail = (raw.match(TRAIL_RE) || [''])[0].length
    const text = raw.slice(lead, raw.length - trail)
    if (!text) return
    blocks.push({ i: blocks.length, start: start + lead, end: end - trail, text })
  }

  const re = new RegExp(SEP_SOURCE, 'g')
  let pos = 0
  let m
  while ((m = re.exec(src)) !== null) {
    push(pos, m.index)
    pos = m.index + m[0].length
  }
  push(pos, src.length)
  return blocks
}

// Which block a Markdown AST node's character offset falls inside.
// react-markdown passes `node.position.start.offset` straight through from mdast.
export function blockIndexAt(blocks, offset) {
  if (offset == null) return null
  for (const b of blocks) {
    if (offset >= b.start && offset <= b.end) return b.i
  }
  return null
}

// Whether a block is ordinary prose worth offering a rewrite on.
//
// mdast paragraphs and blank-line blocks genuinely disagree for headings, quotes,
// lists and horizontal rules — and the ADDRESS is always block-based, so the handle
// is only offered where the two coincide. `33.` is the author's own part divider,
// which the translation prompt preserves verbatim; rewriting it is meaningless.
export function isPlainParagraph(text) {
  const raw = (text || '').trim()
  if (!raw) return false
  if (/^(#|>|```)/.test(raw)) return false
  if (/^(?:[-*+]\s|\d+[.)]\s)/.test(raw)) return false
  if (/^(?:[-*_]\s*){3,}$/.test(raw)) return false   // *** / --- horizontal rule
  if (/^\d+\.?$/.test(raw)) return false             // a standalone part marker
  return true
}
