import { describe, expect, it } from 'vitest'

import { clockTime, duration, jobDoing, statusLine, tabTitle, todayLine } from './activity'

// The words for what the app is doing, shared by the sidebar, the tab title and the
// Activity page.

const job = (over = {}) => ({
  pid: 'p1', name: 'When That Day Comes', kind: 'translate', current: 87,
  waiting: null, hold: null, paused: false, pending: [88, 89], ...over,
})

describe('activity wording', () => {
  it('says how long in plain units', () => {
    expect(duration(45)).toBe('45 s')
    expect(duration(240)).toBe('4 min')
    expect(duration(3900)).toBe('1 h 5 min')
    expect(duration(7200)).toBe('2 h')
    expect(duration(null)).toBe('')
  })

  it('names the chapter, or why it is waiting', () => {
    expect(jobDoing(job())).toBe('Translating ch 87')
    expect(jobDoing(job({ kind: 'ocr', current: 4 }))).toBe('Reading page 4')
    expect(jobDoing(job({ hold: { reason: 'memory' } }))).toBe('Holding: memory is low')
    expect(jobDoing(job({ hold: { reason: 'paused' } }))).toBe('Paused')
    expect(jobDoing(job({ waiting: { resume_at: null } }))).toBe("Waiting for Claude's limit")
  })

  it('gives the sidebar a headline and how much is left and when', () => {
    const eta = 1_800_000_000
    const line = statusLine({ jobs: [job()], queued: 12, eta })
    expect(line.headline).toBe('Translating ch 87 · When That Day Comes')
    expect(line.detail).toBe(`12 chapters left · done ~${clockTime(eta)}`)
    expect(statusLine({ jobs: [], queued: 0 })).toBeNull()
  })

  it('counts novels when more than one is running, and says a pause is coming', () => {
    const line = statusLine({ jobs: [job({ paused: true }), job({ pid: 'p2', current: null })], queued: 5 })
    expect(line.detail).toBe('5 chapters left in 2 novels · pausing after this one')
  })

  it("puts today's tally and the queue in words", () => {
    expect(todayLine({ today: { done: 14, failed: 1 } })).toBe('Today: 14 done · 1 failed')
    expect(todayLine({ today: { done: 0, failed: 0 } })).toBe('')
    expect(tabTitle({ queued: 12 })).toBe('(12) Night Reader')
    expect(tabTitle({ queued: 0 })).toBe('Night Reader')
    expect(tabTitle(null)).toBe('Night Reader')
  })
})
