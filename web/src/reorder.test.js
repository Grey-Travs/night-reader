import { describe, expect, it } from 'vitest'

import { reorderIds } from './reorder'

// Page order decides chapter order, so a drag that lands one position short quietly
// reorders someone's novel. These pin the two bugs that lived here while the maths
// was inline in PagesPage.

const IDS = ['a', 'b', 'c', 'd', 'e']

describe('reorderIds', () => {
  it('drops a page onto one BELOW it and lands after that page', () => {
    // The off-by-one: inserting before the target always left a downward drag one
    // short, so users dragged twice and overshot.
    expect(reorderIds(IDS, 'b', 'd')).toEqual(['a', 'c', 'd', 'b', 'e'])
  })

  it('drops a page onto one ABOVE it and lands before that page', () => {
    expect(reorderIds(IDS, 'd', 'b')).toEqual(['a', 'd', 'b', 'c', 'e'])
  })

  it('moves a page to the very front', () => {
    expect(reorderIds(IDS, 'c', 'a')).toEqual(['c', 'a', 'b', 'd', 'e'])
  })

  it('moves a page to the very end', () => {
    expect(reorderIds(IDS, 'a', 'e')).toEqual(['b', 'c', 'd', 'e', 'a'])
  })

  it('always returns a permutation — never drops or duplicates a page', () => {
    for (const from of IDS) {
      for (const target of IDS) {
        const next = reorderIds(IDS, from, target)
        if (next === null) continue
        expect([...next].sort()).toEqual([...IDS].sort())
        expect(new Set(next).size).toBe(IDS.length)
      }
    }
  })

  it('refuses a move whose ids are no longer in the list', () => {
    // The background poll can remove a page mid-drag. Returning null lets the caller
    // leave the order alone instead of rendering a rail with holes in it.
    expect(reorderIds(IDS, 'gone', 'b')).toBeNull()
    expect(reorderIds(IDS, 'b', 'gone')).toBeNull()
  })

  it('treats a drop onto itself as a no-op', () => {
    expect(reorderIds(IDS, 'c', 'c')).toBeNull()
  })

  it('handles a two-page novel in both directions', () => {
    expect(reorderIds(['a', 'b'], 'a', 'b')).toEqual(['b', 'a'])
    expect(reorderIds(['a', 'b'], 'b', 'a')).toEqual(['b', 'a'])
  })
})
