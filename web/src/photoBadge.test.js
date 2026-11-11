// @vitest-environment node
import { describe, expect, it } from 'vitest'

import { chapterTally, photoBadge } from './photoBadge'

// What each thumbnail says while the chapter is being shot. The computer's later word
// wins over the phone's guess, and every flag carries the words for the list of photos
// to take again.

const photo = (over = {}) => ({ id: 'f1', order: 4, state: 'sent', pageId: 'pg4', check: { flags: [] }, ...over })
const row = (over = {}) => ({ id: 'pg4', n: 4, step: 'reading', note: '', look: null, looking: false, ...over })

describe('a photo’s badge', () => {
  it('flags the phone’s own check until the computer has read it', () => {
    const blurry = photo({ state: 'waiting', check: { flags: ['blurry'] } })
    expect(photoBadge(blurry)).toEqual({ tone: 'warn', short: 'Blurry?', long: 'Photo 4 looks blurry.', flag: true })
    expect(photoBadge(photo({ state: 'waiting' }))).toMatchObject({ short: 'Saved', flag: false })
  })

  it('says what the quick look found', () => {
    const b = photoBadge(photo(), row({ look: { verdict: 'retake', note: 'the bottom lines are cut off' } }))
    expect(b).toMatchObject({ tone: 'warn', short: 'Hard to read', flag: true })
    expect(b.long).toBe('Photo 4: Hard to read: the bottom lines are cut off.')
  })

  it('says Claude isn’t sure while it double-checks, then what it found', () => {
    expect(photoBadge(photo(), row({ step: 'double-checking' })).long)
      .toBe('Photo 4: Claude isn’t sure — double-checking…')
    const found = photoBadge(photo(), row({ step: 'needs-you', note: 'Some words were read wrong.' }))
    expect(found).toMatchObject({ tone: 'bad', short: 'Needs you', flag: true })
    expect(found.long).toBe('Photo 4: Some words were read wrong.')
    expect(photoBadge(photo(), row({ step: 'failed', note: 'Claude couldn’t read this photo.' })))
      .toMatchObject({ tone: 'bad', short: 'Couldn’t read' })
  })

  it('lets a clean read clear the phone’s guess', () => {
    const b = photoBadge(photo({ check: { flags: ['blurry'] } }), row({ step: 'read' }))
    expect(b).toMatchObject({ tone: 'good', short: '✓', flag: false })
  })

  it('keeps the phone’s guess while the quick look is still to come', () => {
    expect(photoBadge(photo({ check: { flags: ['dark'] } }), row({ looking: true })))
      .toMatchObject({ short: 'Too dark?', flag: true })
    expect(photoBadge(photo({ check: { flags: ['dark'] } }), row({ looking: false })))
      .toMatchObject({ short: 'Reading…', flag: false })
  })

  it('says when a photo is already in another chapter', () => {
    expect(photoBadge(photo({ state: 'elsewhere', elsewhere: { number: 2 } })).long)
      .toBe('Photo 4 is already in chapter 2.')
  })
})

describe('how the chapter is getting on', () => {
  it('counts read, double-checking, still reading and still sending', () => {
    const photos = [
      photo({ id: 'a', pageId: 'p1' }), photo({ id: 'b', pageId: 'p2' }), photo({ id: 'c', pageId: 'p3' }),
      photo({ id: 'd', state: 'waiting', pageId: null }), photo({ id: 'e', pageId: 'p5' }),
    ]
    const rows = new Map([
      ['p1', row({ step: 'read' })], ['p2', row({ step: 'double-checking' })], ['p3', row({ step: 'reading' })],
    ])
    expect(chapterTally(photos, rows)).toEqual({ read: 1, checking: 1, sending: 1, reading: 2, total: 5 })
  })
})
