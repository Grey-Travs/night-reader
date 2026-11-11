import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api'

// Where the chapters sent from the phone stand, from GET /api/phone/progress.
//
// Polled while the page is visible: every few seconds while something is moving, every
// half minute while nothing is, and not at all while the app is in the background — a
// phone in a pocket has no reason to keep a radio awake for this.

export const FAST_MS = 5000
export const SLOW_MS = 30000

// Steps the server is busy with, so worth checking on soon.
const MOVING = new Set(['capturing', 'reading', 'checking', 'building', 'translating'])

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`

export function isMoving(novels) {
  return (novels || []).some((novel) => novel.waiting
    || (novel.chapters || []).some((row) => MOVING.has(row.step)))
}

/** One line for where a chapter stands, as the phone says it. */
export function stepLabel(row) {
  const pages = row.pages || {}
  switch (row.step) {
    case 'capturing': return `Still being photographed · ${plural(row.photos || 0, 'photo')}`
    case 'reading': {
      const done = (row.photos || 0) - (pages.unread || 0) - (pages.reading || 0)
      return `Reading · ${done} of ${row.photos || 0} done`
    }
    case 'checking': return `Claude isn’t sure of ${plural(pages.to_check || 0, 'page')} — double-checking…`
    case 'needs-you': return row.message || 'Needs you'
    case 'waiting-turn': return row.message || 'Waiting for an earlier chapter'
    case 'building': return 'Building the chapter…'
    case 'translating': return 'Translating…'
    case 'translated': return 'Translated'
    case 'failed': return 'Couldn’t be translated — try again on the computer'
    default: return row.message || row.step || ''
  }
}

/** Every page that needs a person, across the novels given, in chapter order. */
export function pagesNeedingYou(novels) {
  const out = []
  for (const novel of novels || []) {
    for (const row of novel.chapters || []) {
      for (const item of row.needs_you || []) {
        if (item.page_id) out.push({ ...item, pid: novel.pid, novel: novel.name, number: row.number })
      }
    }
  }
  return out
}

/**
 * Polls the progress of one novel, or all of them. `box` marks that chapter as on
 * screen, which holds back the push for a problem the screen is already showing.
 * `every` fixes the interval, for a screen that wants answers sooner than the hub.
 */
export function usePhoneProgress({ pid = '', box = '', every = null, enabled = true } = {}) {
  const [novels, setNovels] = useState(null)
  const [error, setError] = useState(null)
  const timer = useRef(null)
  const alive = useRef(true)
  const ask = useRef(null)

  const load = useCallback(async () => {
    try {
      const res = await api.phoneProgress({ pid, box })
      if (!alive.current) return null
      setNovels(res?.novels || [])
      setError(null)
      return res?.novels || []
    } catch (e) {
      if (alive.current) setError(e)
      return null
    }
  }, [pid, box])

  useEffect(() => {
    alive.current = true
    if (!enabled) return () => { alive.current = false }
    const hidden = () => typeof document !== 'undefined' && document.visibilityState === 'hidden'
    const stop = () => { if (timer.current) { clearTimeout(timer.current); timer.current = null } }
    const tick = async () => {
      stop()
      if (hidden()) return
      const got = await load()
      if (!alive.current || hidden()) return
      timer.current = setTimeout(tick, every ?? (isMoving(got) ? FAST_MS : SLOW_MS))
    }
    ask.current = tick
    const onVisible = () => { if (!hidden()) tick() }
    document.addEventListener('visibilitychange', onVisible)
    tick()
    return () => {
      alive.current = false
      stop()
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [load, every, enabled])

  const refresh = useCallback(() => ask.current?.(), [])
  return { novels, error, refresh }
}
