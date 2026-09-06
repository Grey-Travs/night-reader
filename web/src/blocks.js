// Paragraph addressing in the reader.
//
// This is a deliberate mirror of translation_bot/paragraphs.py::split_blocks, and it
// has to STAY a mirror: the server addresses a paragraph by the ordinal this produces
// and proves it with the text this returns. If the two splitters ever disagree, a
// rewrite lands on the wrong paragraph — so the split is the same /\n\s*\n/ used
// everywhere else in the app, and nothing more.

export function splitBlocks(text) {
  const src = text || ''
  const blocks = []

  const push = (start, end) => {
    const raw = src.slice(start, end)
    if (!raw.trim()) return
    const lead = raw.length - raw.trimStart().length
    const trail = raw.length - raw.trimEnd().length
    blocks.push({ i: blocks.length, start: start + lead, end: end - trail, text: raw.trim() })
  }

  const re = /\n\s*\n/g
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
