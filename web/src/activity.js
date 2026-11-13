// What the app is doing, in words: shared by the sidebar's status line, the tab title and
// the Activity page, so they can never disagree. The figures come from /api/activity
// (server/app.py activity_snapshot); this only phrases them.
import { useEffect, useState } from 'react'
import { api } from './api'
import { TASK_LABEL, isPageTask } from './tasks'

/** "9:40 PM", from epoch seconds. */
export function clockTime(epoch) {
  return new Date(epoch * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

/** "45 s", "4 min", "1 h 5 min". */
export function duration(seconds) {
  if (seconds == null || !Number.isFinite(seconds)) return ''
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s} s`
  const m = Math.round(s / 60)
  if (m < 60) return `${m} min`
  const h = Math.floor(m / 60)
  return m % 60 ? `${h} h ${m % 60} min` : `${h} h`
}

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`

/** What one novel's queue is doing right now, as a short phrase. */
export function jobDoing(job) {
  if (job.waiting) {
    const at = job.waiting.resume_at
    return `Waiting for Claude's limit${at ? ` · back ~${clockTime(at)}` : ''}`
  }
  if (job.hold?.reason === 'memory') return 'Holding: memory is low'
  if (job.hold?.reason === 'paused') return 'Paused'
  if (job.current == null) return job.paused ? 'Paused' : 'Starting'
  const noun = isPageTask(job.kind) ? 'page' : 'ch'
  const label = TASK_LABEL[job.kind] || TASK_LABEL.translate
  return `${label} ${noun} ${job.current}`
}

/**
 * The sidebar's two lines: `headline` names the work, `detail` says how much is left and
 * when it should be done. null when nothing is running.
 */
export function statusLine(act) {
  const jobs = act?.jobs || []
  if (!jobs.length) return null
  const job = jobs.find((j) => j.current != null && !j.waiting && !j.hold) || jobs[0]
  const headline = `${jobDoing(job)} · ${job.name}`
  const left = plural(act.queued || 0, 'chapter') + ' left'
  const where = jobs.length > 1 ? ` in ${jobs.length} novels` : ''
  const parts = [left + where]
  if (act.eta) parts.push(`done ~${clockTime(act.eta)}`)
  if (job.paused && job.current != null) parts.push('pausing after this one')
  return { headline, detail: parts.join(' · '), job }
}

/** "Today: 14 done · 1 failed", or '' when nothing has happened today. */
export function todayLine(act) {
  const t = act?.today
  if (!t || (!t.done && !t.failed)) return ''
  return `Today: ${t.done} done${t.failed ? ` · ${t.failed} failed` : ''}`
}

/** The browser tab's title: the number in the queue, so it shows from another tab. */
export function tabTitle(act, base = 'Night Reader') {
  const n = act?.queued || 0
  return n > 0 ? `(${n}) ${base}` : base
}

/** Poll /api/activity. `brief` skips the failed-chapter list. */
export function useActivity({ brief = true, every = 5000 } = {}) {
  const [act, setAct] = useState(null)
  const [tick, setTick] = useState(0)
  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const d = await api.activity(brief)
        if (alive) setAct(d)
      } catch { /* a status line is not worth an error dialog */ }
    }
    load()
    const id = setInterval(load, every)
    return () => { alive = false; clearInterval(id) }
  }, [brief, every, tick])
  // Ask again now, after a button changed something.
  const reload = () => setTick((t) => t + 1)
  return [act, reload, setAct]
}
