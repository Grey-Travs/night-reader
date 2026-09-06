import { describe, expect, it } from 'vitest'

import { blockIndexAt, isPlainParagraph, splitBlocks } from './blocks'

// blocks.js decides which paragraph a rewrite is addressed to. The server resolves
// that ordinal with translation_bot/paragraphs.py and proves it against the exact
// text, so a disagreement here sends a rewrite to the WRONG paragraph.
//
// tests/test_paragraph_split_parity.py already compares the two splitters directly;
// these cover the JS-only behaviour that has no Python counterpart.

const CHAPTER = 'The door slid open.\n\n"Are you coming?" she asked.\n\nHe said nothing.\n'

describe('splitBlocks', () => {
  it('returns one block per paragraph with exact spans', () => {
    const blocks = splitBlocks(CHAPTER)
    expect(blocks).toHaveLength(3)
    for (const b of blocks) {
      expect(CHAPTER.slice(b.start, b.end)).toBe(b.text)
    }
  })

  it('numbers blocks from zero, in order', () => {
    expect(splitBlocks(CHAPTER).map((b) => b.i)).toEqual([0, 1, 2])
  })

  it('drops blank and whitespace-only blocks', () => {
    expect(splitBlocks('one\n\n   \n\ntwo').map((b) => b.text)).toEqual(['one', 'two'])
  })

  it('treats a separator containing whitespace as a break', () => {
    // /\n\s*\n/, not /\n{2,}/ — the two disagree on exactly this, and two copies in
    // ChapterReader still use the other one.
    expect(splitBlocks('a\n \nb')).toHaveLength(2)
  })

  it('handles empty and nullish input', () => {
    expect(splitBlocks('')).toEqual([])
    expect(splitBlocks(null)).toEqual([])
    expect(splitBlocks(undefined)).toEqual([])
  })

  it('ignores a trailing newline', () => {
    expect(splitBlocks('only one\n')).toHaveLength(1)
  })
})

describe('blockIndexAt', () => {
  it('maps a markdown node offset back to its block', () => {
    const blocks = splitBlocks(CHAPTER)
    for (const b of blocks) {
      expect(blockIndexAt(blocks, b.start)).toBe(b.i)
    }
  })

  it('returns null for an offset outside every block', () => {
    const blocks = splitBlocks(CHAPTER)
    expect(blockIndexAt(blocks, 999)).toBeNull()
    expect(blockIndexAt(blocks, null)).toBeNull()
    expect(blockIndexAt(blocks, undefined)).toBeNull()
  })
})

describe('isPlainParagraph', () => {
  it('accepts ordinary prose', () => {
    expect(isPlainParagraph('The door slid open.')).toBe(true)
    expect(isPlainParagraph('그는 천천히 문을 열었다.')).toBe(true)
  })

  it('rejects markdown structure a rewrite must not be offered on', () => {
    // The rewrite prompt returns ONE paragraph, so offering it on a heading, a rule
    // or a list item would ask the model to preserve structure it was never shown.
    for (const s of ['# A heading', '> a quotation', '```\ncode\n```',
                     '- a list item', '1. a numbered item', '***', '---']) {
      expect(isPlainParagraph(s), s).toBe(false)
    }
  })

  it("rejects the author's own part markers", () => {
    // The translation prompt preserves a standalone number line verbatim; rewriting
    // one is meaningless.
    expect(isPlainParagraph('33.')).toBe(false)
    expect(isPlainParagraph('7')).toBe(false)
  })

  it('rejects empty input', () => {
    expect(isPlainParagraph('')).toBe(false)
    expect(isPlainParagraph('   ')).toBe(false)
    expect(isPlainParagraph(null)).toBe(false)
  })

  it('accepts prose that merely starts with a number', () => {
    expect(isPlainParagraph('33 years had passed since then.')).toBe(true)
  })
})
