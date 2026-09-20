import { describe, expect, it } from 'vitest'

import {
  SNAPSHOT_STALE_HOURS, belowSite, blockedReason, chapterNumber, duplicates, eligible,
  formatRuns, nextRun,
  pickupLine, range, runs, snapshotWarning, suggestionLabel, unnumberedLabel,
} from './posting'

// These rules decide what gets posted, and both screens share them, so they are worth
// testing directly rather than only through whichever page happens to render them.
//
// The fixtures are the real library. "Becoming the Top's Older Brother" really has 196
// chapters on the site and 198, 199, 200 and 223 waiting here; the two corrupt snapshots
// really hold 94 titles of which none is a chapter.

const item = (global, over = {}) => ({
  global, title: global == null ? 'Side Stories 1' : `Chapter ${global}`,
  kind: global == null ? 'side' : 'chapter', paid: true, blockers: [], ...over,
})

describe('collapsing numbers into runs', () => {
  it('keeps range() reading exactly as it did', () => {
    // PhonePage.test.jsx asserts on this string, en dash and all.
    expect(range([item(72), item(73), item(74)])).toBe('72–74')
    expect(range([item(72), item(73), item(80)])).toBe('72–73, 80')
    expect(range([])).toBe('—')
  })

  it('ignores side stories, which have no number', () => {
    expect(range([item(5), item(null)])).toBe('5')
  })

  it('renders a server-sent run identically to a computed one', () => {
    expect(formatRuns([[1, 196]])).toBe('1–196')
    expect(formatRuns([[1, 45], [47, 196]])).toBe('1–45, 47–196')
    expect(formatRuns([])).toBe('—')
  })

  it('does not double-count a repeated number', () => {
    expect(runs([3, 3, 4])).toEqual([[3, 4]])
  })
})

describe('what to post next', () => {
  it('stops at the gap rather than at the highest number', () => {
    // The site orders by number, so posting 223 while 201-222 are missing publishes a
    // novel with a hole the reader can see.
    const next = nextRun([item(198), item(199), item(200), item(223)])
    expect(next).toMatchObject({ from: 198, to: 200, count: 3, apart: [223], covers: false })
  })

  it('prefers the first ready chapter over one past the site end', () => {
    // The real case: the site ends at 196, but 197 is not finished here. max_posted + 1
    // would name a chapter that cannot post; min(ready) names the one that can.
    expect(nextRun([item(198), item(199)]).from).toBe(198)
  })

  it('offers nothing when the novel is caught up', () => {
    expect(nextRun([])).toMatchObject({ from: null, count: 0, covers: true })
  })

  it('counts side stories apart from the range', () => {
    const next = nextRun([item(5), item(6), item(null)])
    expect(next).toMatchObject({ from: 5, to: 6, sides: 1 })
  })
})

describe('spotting a chapter that is already up', () => {
  const site = { posted: [[1, 196]], max_posted: 196, count: 198, numbered: 196 }

  it('refuses a number the site already carries', () => {
    // A real contradiction: the server said this is not posted because the TITLE matched
    // nothing, while the site says that NUMBER is there. That is title drift, and it is
    // how the same chapter gets posted twice.
    expect(duplicates([item(118)], site).map((i) => i.global)).toEqual([118])
  })

  it('leaves the genuinely next chapters alone', () => {
    expect(duplicates([item(198), item(199)], site)).toEqual([])
  })

  it('says nothing at all when the site has never been read', () => {
    expect(duplicates([item(1)], { posted: [], max_posted: null })).toEqual([])
  })

  it('treats a hole below the end as a warning, not a duplicate', () => {
    const holey = { posted: [[1, 45], [47, 196]], max_posted: 196 }
    expect(duplicates([item(46)], holey)).toEqual([])
    expect(belowSite([item(46)], holey).map((i) => i.global)).toEqual([46])
  })
})

describe('when the snapshot cannot be believed', () => {
  const fresh = {
    fetched_at: new Date().toISOString(), age_seconds: 60, count: 114, numbered: 114,
    trusted: true,
  }

  it('says nothing about a reading taken just now', () => {
    expect(snapshotWarning(fresh)).toBeNull()
  })

  it('names the series-list reading', () => {
    // The two real corrupt files: 94 titles, none of them a chapter.
    const warning = snapshotWarning({ ...fresh, count: 94, numbered: 0 })
    expect(warning).toMatch(/series list/i)
  })

  it('says when nobody has ever looked', () => {
    expect(snapshotWarning({ ...fresh, fetched_at: null })).toMatch(/nobody has read/i)
  })

  it('warns once a reading is old', () => {
    const old = new Date(Date.now() - (SNAPSHOT_STALE_HOURS + 1) * 3600 * 1000)
    const warning = snapshotWarning({
      ...fresh, fetched_at: old.toISOString(),
      age_seconds: (SNAPSHOT_STALE_HOURS + 1) * 3600,
    })
    expect(warning).toMatch(/not.*counted here/i)
  })

  it('leads with an unusable reading over anything else', () => {
    expect(snapshotWarning({ ...fresh, trusted: false, why: 'read off the series list' }))
      .toMatch(/cannot be used/i)
  })
})

describe('the refusal', () => {
  const base = { willPost: [item(5)], savedCoins: 55, savedFree: 1, priceDirty: false }

  it('still puts money first', () => {
    // A duplicate is expensive; giving paid chapters away is worse.
    const why = blockedReason({ ...base, savedCoins: 0, dupes: [item(5)] })
    expect(why).toMatch(/0 coins/)
  })

  it('refuses a duplicate', () => {
    expect(blockedReason({ ...base, dupes: [item(118)] })).toMatch(/already on the site/i)
  })

  it('behaves exactly as before when no duplicates are passed', () => {
    // The argument is defaulted so every existing caller and test is untouched.
    expect(blockedReason(base)).toBeNull()
    expect(blockedReason({ ...base, priceDirty: true })).toMatch(/save the pricing/i)
    expect(blockedReason({ ...base, willPost: [] })).toMatch(/nothing in that range/i)
  })
})

describe('what the offer says', () => {
  it('names the evidence and the effect', () => {
    const next = nextRun([item(198), item(199), item(200)])
    expect(suggestionLabel(next, { max_posted: 196 }))
      .toBe('the site ends at 196 — post 198–200')
  })

  it('drops the evidence when the site end is unknown', () => {
    expect(suggestionLabel(nextRun([item(7)]), { max_posted: null })).toBe('post 7')
  })

  it('offers nothing when there is nothing numbered to offer', () => {
    expect(suggestionLabel(nextRun([]), {})).toBe('')
  })
})

describe('where what you have meets what is up there', () => {
  it('says so plainly when the novel is caught up', () => {
    // The state 15 of 26 real series are in, and the direct answer to "will I post twice?"
    expect(pickupLine(nextRun([]))).toBe('Nothing of yours is missing from it.')
  })

  it('names the pickup point and warns about the gap', () => {
    const next = nextRun([item(198), item(199), item(200), item(223)])
    expect(pickupLine(next)).toBe('Picks up at 198 — 3 chapters, then a gap before 223.')
  })

  it('says nothing about a gap when there is none', () => {
    expect(pickupLine(nextRun([item(5)]))).toBe('Picks up at 5 — 1 chapter.')
  })
})

describe('saying what a range leaves out', () => {
  it('names side stories as such', () => {
    expect(unnumberedLabel([item(5), item(null)])).toBe('1 side story is')
  })

  it('pluralises, and falls back when they are not side stories', () => {
    const odd = [item(null, { kind: 'extra' }), item(null, { kind: 'extra' })]
    expect(unnumberedLabel(odd)).toBe('2 chapters without a number are')
  })

  it('says nothing when everything has a number', () => {
    expect(unnumberedLabel([item(5)])).toBe('')
  })
})

// ---- a side story that carries its own number ----------------------------

// Two series number their side stories 1..10 in their own sequence, colliding exactly with
// their chapters 1..10. Every helper here used to read `i.global` directly, so a side
// story's ordinal passed for a chapter number: a range of 1-10 swept ten side stories in,
// and the site's own posted range 1..125 marked all ten as duplicates of real chapters.
//
// The fixture above cannot express this — it derives `kind` from `global == null` — which
// is why it went unnoticed on both sides of the app.
const numberedSide = (n) => ({
  global: n, title: `Side Stories ${n}`, kind: 'side', paid: true, blockers: [],
})

describe('a side story that carries its own number', () => {
  it('has no chapter number, however it is numbered', () => {
    expect(chapterNumber(item(7))).toBe(7)
    expect(chapterNumber(numberedSide(7))).toBe(null)
    expect(chapterNumber(item(null))).toBe(null)
  })

  it('is never swept into a chapter range', () => {
    const ready = [item(1), item(2), numberedSide(1), numberedSide(2)]
    const got = eligible(ready, { from: '1', to: '2' }).map((i) => i.title)
    expect(got).toEqual(['Chapter 1', 'Chapter 2'])
  })

  it('is not reachable by a stop-at bound either', () => {
    const ready = [item(1), numberedSide(1)]
    expect(eligible(ready, { upTo: '5' }).map((i) => i.title)).toEqual(['Chapter 1'])
  })

  it('is not a duplicate of the chapter that shares its number', () => {
    // The site holds chapters 1-125. Side story 1 is not chapter 1.
    const site = { posted: [[1, 125]], max_posted: 125 }
    expect(duplicates([numberedSide(1)], site)).toEqual([])
    expect(duplicates([item(1)], site).map((i) => i.title)).toEqual(['Chapter 1'])
    expect(belowSite([numberedSide(1)], site)).toEqual([])
  })

  it('counts as unreachable, so the page can say so', () => {
    expect(unnumberedLabel([item(5), numberedSide(1)])).toBe('1 side story is')
  })

  it('does not lend its ordinal to the next-run suggestion', () => {
    const next = nextRun([numberedSide(1), numberedSide(2), item(198), item(199)])
    expect(next.from).toBe(198)
    expect(next.to).toBe(199)
    expect(next.sides).toBe(2)
  })

  it('stays out of a displayed range of chapter numbers', () => {
    expect(range([item(67), item(68), numberedSide(1)])).toBe('67–68')
  })
})
