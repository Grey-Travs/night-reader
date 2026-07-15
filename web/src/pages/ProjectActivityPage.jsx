import { useEffect, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { ProgressBar, StatCard } from '../components/ui'

// Live countdown to a target epoch-seconds timestamp, e.g. "in 42:10".
function Countdown({ until }) {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [])
  const left = Math.max(0, Math.floor(until * 1000 - now) / 1000)
  if (left <= 0) return <>any moment now…</>
  const h = Math.floor(left / 3600), m = Math.floor((left % 3600) / 60), s = Math.floor(left % 60)
  const pad = (n) => String(n).padStart(2, '0')
  return <>in {h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`}</>
}

// Per-novel monitoring: the live translation console, progress, pause/auto-resume
// state and the headline stats. The job state itself lives in ProjectLayout, so this
// page keeps showing live updates even though it isn't the one holding the stream.
export default function ProjectActivityPage() {
  const {
    data, counts, koreanTotal, done,
    running, submitting, log, paused, waiting, queue, totalQueued, cancelQueue, resumeJob,
  } = useOutletContext()

  return (
    <div className="page">
      {/* Queue bar */}
      {totalQueued > 0 ? (
        <div className="mb-5 flex flex-wrap items-center justify-between gap-2 rounded-card border border-line p-3 text-sm" style={{ background: 'var(--b-translating-bg)', color: 'var(--b-translating-tx)' }}>
          <span className="flex items-center gap-2">
            <span className="inline-block h-2 w-2 rounded-full animate-pulse" style={{ background: 'var(--accent)' }} />
            {waiting ? 'Waiting for Claude to refresh' : queue.current != null ? <>Translating <strong>chapter {queue.current}</strong></> : 'Queued'}
            {queue.pending.length > 0 && ` · ${queue.pending.length} waiting in queue`}
          </span>
          {queue.pending.length > 0 && (
            <button onClick={cancelQueue} className="btn btn-ghost shrink-0 px-3 py-1 text-xs">Clear queue</button>
          )}
        </div>
      ) : !running && (
        <div className="mb-5 rounded-card border border-line p-4 text-sm text-muted" style={{ background: 'var(--surface)' }}>
          Nothing translating right now. Start a translation from the <strong>Chapters</strong> tab and progress shows here live.
        </div>
      )}

      {waiting && (
        <div className="mb-5 flex flex-col gap-2 rounded-card border border-line p-3 text-sm sm:flex-row sm:items-center sm:justify-between" style={{ background: 'var(--b-queued-bg)', color: 'var(--b-queued-tx)' }}>
          <span className="flex items-center gap-2">
            <span className="inline-block h-2 w-2 shrink-0 rounded-full animate-pulse" style={{ background: 'var(--accent)' }} />
            <span>
              Waiting for Claude to refresh — resuming automatically{' '}
              {waiting.resume_at ? <>around <strong>{new Date(waiting.resume_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</strong> (<Countdown until={waiting.resume_at} />)</> : 'soon'}
              . Progress is saved — you can close this tab.
            </span>
          </span>
          <button onClick={() => resumeJob()} disabled={submitting} className="btn btn-ghost shrink-0 px-3 py-1.5 text-xs">Resume now</button>
        </div>
      )}

      {paused && (
        <div className="mb-5 flex flex-col gap-2 rounded-card border border-line p-3 text-sm sm:flex-row sm:items-center sm:justify-between" style={{ background: 'var(--b-queued-bg)', color: 'var(--b-queued-tx)' }}>
          <span>
            Your plan's limit was reached — progress is saved{paused.pending?.length ? ` (${paused.pending.length} left to do)` : ''}.
            {paused.resets_at ? ` Picks back up automatically around ${new Date(paused.resets_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}.` : ' Resume when your plan resets.'}
          </span>
          <button onClick={() => resumeJob()} disabled={running} className="btn btn-ghost shrink-0 px-3 py-1.5 text-xs">Resume now</button>
        </div>
      )}

      {/* Live console */}
      {(running || log.length > 0) && (
        <section className="sunken mb-6 p-4">
          <div className="mb-2 text-sm font-medium text-muted">{running ? 'In progress' : 'Last run'}</div>
          <div className="max-h-60 overflow-y-auto font-mono text-xs leading-relaxed text-muted">
            {log.map((line, i) => <div key={i} className="row-in">{line}</div>)}
            {running && <div className="animate-pulse" style={{ color: 'var(--accent-text)' }}>▋</div>}
          </div>
        </section>
      )}

      {/* Progress */}
      <section className="mb-6">
        <div className="mb-2 flex items-end justify-between">
          <h2 className="text-sm font-medium text-muted">Korean chapters translated</h2>
          <span className="text-sm tabular-nums text-hint">{done} / {koreanTotal}</span>
        </div>
        <ProgressBar value={done} total={koreanTotal} />
      </section>

      {/* Stat cards */}
      <section className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
        <StatCard label="Total tabs" value={data?.total ?? '—'} />
        <StatCard label="Translated" value={counts.validated || 0} />
        <StatCard label="To translate" value={counts.pending || 0} />
        <StatCard label="Needs review" value={counts['needs-review'] || 0} />
        <StatCard label="Already English" value={counts['english-source'] || 0} />
        <StatCard label="Plan usage" value={`$${(data?.totals?.cost_usd ?? 0).toFixed(2)}`} sub="equivalent" />
      </section>
    </div>
  )
}
