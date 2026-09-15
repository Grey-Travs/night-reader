// The extension is explicit so that plain Node can load this module: the parity test
// shells out to Node, which does not resolve extensionless relative specifiers the way
// Vite does.
import { splitBlocks } from './blocks.js'

// Markdown -> HTML, in the browser.
//
// This is a deliberate mirror of translation_bot/mdhtml.py. The reader's Copy button
// renders here, and the posting payload renders there, so if the two drift then what you
// see when you copy a chapter stops matching what actually gets published.
// tests/test_mdhtml_parity.py runs both over the same fixtures and fails if they disagree.
//
// The paragraph boundary comes from splitBlocks rather than a local `/\n\s*\n/`, because
// `\s` is not the same character set in Python and JavaScript — see the long explanation
// in translation_bot/textsplit.py. This file used to re-declare it, which made it the
// fifteenth copy of a separator that module exists to keep singular.
//
// The output is a bare sequence of block elements: no wrapper, no classes, no inline
// styles. That is what makes it paste cleanly into anything, and what lets a
// ProseMirror/TipTap-style editor parse it with its default schema and no custom rules.

const BARE_NUMBER_RE = /^\d{1,4}\.?$/
const RULE_RE = /^(?:[-*_] *){3,}$/
const HEADING_RE = /^(#{1,6})\s+(.*)$/
const QUOTE_PREFIX_RE = /^[ \t]{0,3}>[ \t]?/gm

// Only & < > are escaped, matching html.escape(..., quote=False) on the Python side.
// Escaping apostrophes too would be harmless in a text node, but these chapters are full
// of them and every one would make the two outputs differ byte for byte.
export function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

export function inlineHtml(s) {
  return escapeHtml(s)
    // Order matters: the three-star form must match before the two-star one, or
    // "***word***" comes out bold with stray stars around it.
    .replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/(?<!\w)_(.+?)_(?!\w)/g, '<em>$1</em>')
    // Inline code drops to plain text: the translator never emits it deliberately, so a
    // stray backtick is punctuation, not markup.
    .replace(/`([^`]+)`/g, '$1')
}

// Drop a leading "# 12" title block so only the prose remains. Only the first block, and
// only when it is a heading.
export function stripLeadingHeading(md) {
  return (md || '').replace(/^﻿?\s*#{1,6}[ \t]+[^\n]*(?:\n+|$)/, '')
}

export function markdownToHtml(md, { xhtml = false, stripPartMarkers = false } = {}) {
  const br = xhtml ? '<br/>' : '<br>'
  const rule = xhtml ? '<hr/>' : '<hr>'
  const out = []
  for (const { text } of splitBlocks((md || '').replace(/\r\n/g, '\n'))) {
    const block = text
    if (!block) continue
    // A block that is nothing but a number is the source novel's own section marker.
    // Invisible in the reader (an empty ordered-list item, and lists are unstyled), so
    // publishing it as a visible <p>33.</p> would show a number nobody has ever seen.
    if (stripPartMarkers && BARE_NUMBER_RE.test(block)) continue
    if (RULE_RE.test(block)) { out.push(rule); continue }
    const h = HEADING_RE.exec(block)
    if (h) {
      const level = Math.min(h[1].length, 6)
      out.push(`<h${level}>${inlineHtml(h[2].trim())}</h${level}>`)
      continue
    }
    if (block.startsWith('>')) {
      const inner = block.replace(QUOTE_PREFIX_RE, '').split('\n').map(inlineHtml).join(br)
      out.push(`<blockquote><p>${inner}</p></blockquote>`)
      continue
    }
    out.push(`<p>${block.split('\n').map(inlineHtml).join(br)}</p>`)
  }
  return out.join('\n')
}
