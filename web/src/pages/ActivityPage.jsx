import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api'
import { clockTime, duration, jobDoing, todayLine, useActivity } from '../activity'
import { isPageTask } from '../tasks'

// Global activity: every novel with work queued, live, plus what goes with it: chapters
// that failed (with Retry), free memory and the hold for it, and which notices to send.
// Each novel links to its own Activity tab, the chapter-by-chapter console.

const CHIPS = 30
const FAILED_SHOWN = 20

const size = (mb) => (mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`)

function ago(iso) {
  if (!iso) return ''
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000
  if (!Number.isFinite(seconds)) return ''
  if (seconds < 3600 * 20) return `${duration(seconds)} ago`
  return new Date(iso).toLocaleDateString([], { month: 'short', day: 'numeric' })
}

export default function ActivityPage() {
  const [act, reload, setAct] = useActivity({ brief: false, every: 4000 })
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')

  // One place for every button: show it working, report a refusal, then look again.
  const run = async (key, fn) => {
    setBusy(key)
    setError('')
    try {
      await fn()
      reload()
    } catch (e) {
      setError(e.message || 'That did not work.')
    } finally {
      setBusy('')
    }
  }

  const saveSettings = (change) => run('settings', async () => {
    const d = await api.activitySettings(change)
    setAct((a) => (a ? { ...a, settings: d.settings, memory: d.memory } : a))
  })

  const jobs = act?.jobs || []
  const allChapters = jobs.every((j) => !isPageTask(j.kind))
  const noun = allChapters ? 'chapter' : 'item'
  const today = todayLine(act)

  return (
    <div className="page page-narrow">
      <div className="mb-6">
        <h1 className="font-reading text-2xl font-medium">Activity</h1>
        <p className="text-sm text-hint">
          {act == null ? 'Loading…'
            : jobs.length === 0 ? 'Nothing running right now.'
            : `${act.queued} ${noun}${act.queued === 1 ? '' : 's'} across ${jobs.length} novel${jobs.length === 1 ? '' : 's'}`
              + (act.eta ? ` · done around ${clockTime(act.eta)}` : '')}
        </p>
        {today && <p className={`text-sm ${act.today.failed ? 'text-danger' : 'text-muted'}`}>{today}</p>}
        {error && <p role="alert" className="mt-2 text-sm text-danger">{error}</p>}
      </div>

      {act != null && jobs.length === 0 && (
        <div className="rounded-card border border-dashed border-line-strong p-10 text-center text-muted">
          Nothing is running. Open a novel and translate some chapters, or repair a flagged one — progress appears here and on the novel's Activity tab.
          <div className="mt-4"><Link to="/" className="btn btn-primary px-4 py-2">Go to library</Link></div>
        </div>
      )}

      <div className="space-y-3">
        {jobs.map((j) => <JobCard key={j.pid} job={j} busy={busy} run={run} />)}
      </div>

      {act?.failed?.length > 0 && <FailedCard failed={act.failed} busy={busy} run={run} />}
      {act && <MemoryCard act={act} busy={busy} save={saveSettings} />}
      {act && <NoticesCard settings={act.settings} busy={busy} save={saveSettings} />}
    </div>
  )
}

function JobCard({ job, busy, run }) {
  const navigate = useNavigate()
  const held = job.paused || job.hold
  const chapters = isPageTask(job.kind) ? [] : job.pending
  const waitingFor = job.waiting || job.hold
  let when = ''
  if (job.paused && job.current != null && !job.hold) when = 'Pausing after this chapter.'
  else if (job.paused) when = 'Paused. Press Continue to carry on.'
  else if (job.hold?.reason === 'memory') when = 'Carries on by itself once memory frees up.'
  else if (job.eta) when = `Done around ${clockTime(job.eta)}`
  return (
    <div className="card p-4">
      <div className="flex items-center justify-between gap-2">
        <button onClick={() => navigate(`/novel/${job.pid}/activity`)} className="truncate text-left font-medium hover:text-accent-text hover:underline">{job.name}</button>
        <div className="flex shrink-0 items-center gap-2">
          <span className="text-xs text-muted">{job.in_queue} in queue</span>
          {held ? (
            <button className="btn btn-ghost px-2 py-1 text-xs" disabled={busy === `pause:${job.pid}`}
              onClick={() => run(`pause:${job.pid}`, () => api.continueQueue(job.pid))}>Continue</button>
          ) : (
            <button className="btn btn-ghost px-2 py-1 text-xs" disabled={busy === `pause:${job.pid}`}
              title="Finish this chapter, then wait. The queue is kept; other novels carry on."
              onClick={() => run(`pause:${job.pid}`, () => api.pauseQueue(job.pid))}>Pause</button>
          )}
        </div>
      </div>
      <div className="mt-1 flex items-center gap-2 text-sm text-muted">
        <span
          className={`inline-block h-2 w-2 shrink-0 rounded-full ${waitingFor ? '' : 'animate-pulse'}`}
          style={{ background: waitingFor ? 'var(--b-review-tx)' : 'var(--accent)' }}
          aria-hidden
        />
        <span>
          {jobDoing(job)}
          {job.step ? ` · ${job.step}` : ''}
          {job.elapsed != null && !job.waiting ? ` · ${duration(job.elapsed)} so far` : ''}
        </span>
      </div>
      {when && <div className="mt-1 text-xs text-hint">{when}</div>}
      {chapters.length > 0 && (
        <div className="mt-2">
          <div className="text-xs text-hint">Waiting (click one to do it next):</div>
          <div className="mt-1 flex flex-wrap gap-1">
            {chapters.slice(0, CHIPS).map((i, pos) => (
              <button
                key={i}
                className="pill pill-queued !px-1.5 !py-0 text-[11px] disabled:opacity-60"
                disabled={pos === 0 || busy === `next:${job.pid}`}
                title={pos === 0 ? 'Next up' : `Translate chapter ${i} next`}
                onClick={() => run(`next:${job.pid}`, () => api.queueNext(job.pid, i))}
              >{i}</button>
            ))}
            {chapters.length > CHIPS && <span className="text-[11px] text-hint">+{chapters.length - CHIPS} more</span>}
          </div>
        </div>
      )}
    </div>
  )
}

function FailedCard({ failed, busy, run }) {
  const byNovel = failed.reduce((acc, f) => ({ ...acc, [f.pid]: [...(acc[f.pid] || []), f.index] }), {})
  const retryAll = () => run('retry:all', async () => {
    for (const [pid, indices] of Object.entries(byNovel)) await api.translate(pid, { indices })
  })
  return (
    <section className="card mt-6 p-4" aria-label="Failed chapters">
      <div className="flex items-center justify-between gap-2">
        <h2 className="font-medium">Failed chapters ({failed.length})</h2>
        <button className="btn btn-ghost px-2 py-1 text-xs" disabled={busy === 'retry:all'} onClick={retryAll}>
          Retry all
        </button>
      </div>
      <ul className="mt-2 divide-y divide-[var(--border)]">
        {failed.slice(0, FAILED_SHOWN).map((f) => (
          <li key={`${f.pid}:${f.index}`} className="flex items-start justify-between gap-3 py-2 text-sm">
            <div className="min-w-0">
              <div className="truncate">
                <Link to={`/novel/${f.pid}`} className="hover:underline">{f.name}</Link>
                <span className="text-muted"> · chapter {f.index}</span>
              </div>
              <div className="truncate text-xs text-hint" title={f.error}>{f.error || 'No reason was recorded.'}{f.at ? ` · ${ago(f.at)}` : ''}</div>
            </div>
            <button className="btn btn-ghost shrink-0 px-2 py-1 text-xs" disabled={busy === `retry:${f.pid}:${f.index}`}
              onClick={() => run(`retry:${f.pid}:${f.index}`, () => api.translate(f.pid, { indices: [f.index] }))}>
              Retry
            </button>
          </li>
        ))}
      </ul>
      {failed.length > FAILED_SHOWN && <p className="mt-1 text-xs text-hint">+{failed.length - FAILED_SHOWN} more. Retry all includes them.</p>}
    </section>
  )
}

function MemoryCard({ act, busy, save }) {
  const mem = act.memory
  const settings = act.settings || {}
  const [mb, setMb] = useState(String(settings.low_memory_mb ?? 500))
  useEffect(() => { setMb(String(settings.low_memory_mb ?? 500)) }, [settings.low_memory_mb])
  const holding = (act.jobs || []).some((j) => j.hold?.reason === 'memory')
  const commit = () => {
    const n = Number(mb)
    if (Number.isInteger(n) && n !== settings.low_memory_mb) save({ low_memory_mb: n })
  }
  return (
    <section className="card mt-6 p-4" aria-label="This PC">
      <h2 className="font-medium">This PC</h2>
      {mem ? (
        <>
          <p className={`mt-1 text-sm ${mem.low ? 'text-danger' : 'text-muted'}`}>
            {size(mem.free_mb)} free of {size(mem.total_mb)}{mem.low ? ' — low' : ''}
          </p>
          <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-[var(--b-muted-bg)]" aria-hidden>
            <div className="h-full rounded-full" style={{
              width: `${Math.min(100, Math.round(100 * (1 - mem.free_mb / mem.total_mb)))}%`,
              background: mem.low ? 'var(--danger)' : 'var(--accent)',
            }} />
          </div>
        </>
      ) : <p className="mt-1 text-sm text-hint">Free memory can't be read on this computer.</p>}
      <label className="mt-3 flex flex-wrap items-center gap-2 text-sm">
        <input type="checkbox" checked={!!settings.hold_on_low_memory} disabled={busy === 'settings'}
          onChange={(e) => save({ hold_on_low_memory: e.target.checked })} />
        Hold new chapters while free memory is below
        <input className="input w-20 px-2 py-0.5 text-sm" inputMode="numeric" value={mb} aria-label="Memory threshold in MB"
          onChange={(e) => setMb(e.target.value.replace(/\D/g, ''))} onBlur={commit}
          onKeyDown={(e) => { if (e.key === 'Enter') e.currentTarget.blur() }} />
        MB
      </label>
      <p className="mt-1 text-xs text-hint">
        {holding ? 'Holding now. ' : ''}A chapter already running finishes first. Each chapter starts a Claude process of its own, which is the part of the load this can take off.
      </p>
    </section>
  )
}

const NOTICES = [
  ['notify_finished', "A novel's queue finishes"],
  ['notify_failed', 'A chapter fails, with the reason'],
  ['notify_limit', "Claude's limit pauses the work, and when it carries on"],
]

function NoticesCard({ settings = {}, busy, save }) {
  return (
    <section className="card mt-6 p-4" aria-label="Notices">
      <h2 className="font-medium">Tell me when</h2>
      <div className="mt-2 space-y-1.5">
        {NOTICES.map(([key, label]) => (
          <label key={key} className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={!!settings[key]} disabled={busy === 'settings'}
              onChange={(e) => save({ [key]: e.target.checked })} />
            {label}
          </label>
        ))}
        <label className="flex items-center gap-2 border-t border-[var(--border)] pt-2 text-sm">
          <input type="checkbox" checked={!!settings.toasts} disabled={busy === 'settings'}
            onChange={(e) => save({ toasts: e.target.checked })} />
          Pop up by the clock on this PC
        </label>
      </div>
      <p className="mt-2 text-xs text-hint">
        These also go to your Telegram bot, if it's paired, and to phone notifications, if they're set up in Settings.
      </p>
    </section>
  )
}
