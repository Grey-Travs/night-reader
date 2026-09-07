import { describe, expect, it } from 'vitest'

import {
  PAGE_TASK_KINDS,
  chapterScopedQueue,
  describeTask,
  isPageTask,
  taskNoun,
} from './tasks'

// One worker queue carries both chapter work and scanned-page work, and while an OCR
// job runs its `current` / `pending` are PAGE SEQUENCE NUMBERS. Four separate places
// compared them straight against a chapter index — ChaptersPage's badges, the
// reader's busy gate, the Review inbox's row lock, and the Chapters banner — so
// reading page 7 of a photographed novel made CHAPTER 7 unusable: repair buttons
// disabled, paragraph rewrites refused, and a "Queued" pill on a chapter no worker
// was ever going to touch.
//
// The numbers are indistinguishable once separated from `kind`, so the guard has to
// live with the kind. That is what these pin.

describe('chapterScopedQueue', () => {
  it('passes chapter work straight through', () => {
    expect(chapterScopedQueue({ kind: 'translate', current: 3, pending: [4, 5] }))
      .toEqual({ current: 3, pending: [4, 5] })
  })

  it.each(PAGE_TASK_KINDS)('empties the queue for %s, whose numbers are pages', (kind) => {
    expect(chapterScopedQueue({ kind, current: 7, pending: [8, 9] }))
      .toEqual({ current: null, pending: [] })
  })

  it('keeps repair kinds, which are chapter work', () => {
    // resolve and pronouns run on a chapter index — they must NOT be filtered out, or
    // a chapter genuinely being repaired would stop looking busy.
    for (const kind of ['resolve', 'pronouns']) {
      expect(chapterScopedQueue({ kind, current: 2, pending: [3] }))
        .toEqual({ current: 2, pending: [3] })
    }
  })

  it('treats an unknown kind as chapter work', () => {
    // The server is the source of truth for kinds; a new one it adds is far more
    // likely to be chapter work, and guessing "page" would silently hide real work.
    expect(chapterScopedQueue({ kind: 'something-new', current: 1, pending: [] }))
      .toEqual({ current: 1, pending: [] })
  })

  it('survives a missing or half-built queue', () => {
    // ProjectLayout renders before the first queue response lands.
    expect(chapterScopedQueue(undefined)).toEqual({ current: null, pending: [] })
    expect(chapterScopedQueue({})).toEqual({ current: null, pending: [] })
    expect(chapterScopedQueue({ kind: 'translate', current: 0, pending: null }))
      .toEqual({ current: 0, pending: [] })
  })

  it('keeps chapter 0 distinguishable from "nothing running"', () => {
    // `current ?? null`, not `current || null` — chapter indices start at 1 so this is
    // defensive, but a `||` here is the classic way to lose a legitimate 0.
    expect(chapterScopedQueue({ kind: 'translate', current: 0, pending: [] }).current)
      .toBe(0)
  })
})

describe('task labels', () => {
  it('names the right noun per kind', () => {
    expect(taskNoun('translate')).toBe('chapter')
    expect(taskNoun('ocr')).toBe('page')
    expect(taskNoun('ocr-verify')).toBe('page')
  })

  it('describes page work as pages, not chapters', () => {
    // The Chapters banner announced "Translating chapter 7" while the worker was
    // reading PAGE 7.
    expect(describeTask('ocr', 7)).toBe('Reading page 7')
    expect(describeTask('ocr-verify', 7)).toBe('Double-checking page 7')
    expect(describeTask('translate', 3)).toBe('Translating chapter 3')
    expect(describeTask('resolve', 3)).toBe('AI resolve on chapter 3')
  })

  it('falls back to translate wording for an unknown kind', () => {
    expect(describeTask('mystery', 2)).toBe('Translating chapter 2')
  })

  it('agrees with isPageTask', () => {
    for (const kind of PAGE_TASK_KINDS) expect(isPageTask(kind)).toBe(true)
    expect(isPageTask('translate')).toBe(false)
    expect(isPageTask(undefined)).toBe(false)
  })
})
