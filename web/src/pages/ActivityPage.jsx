import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api'

// Global translation activity — every novel with a running/queued job, live. Each
// row links to that novel's own Activity tab (the chapter-by-chapter console).
export default function ActivityPage() {
  const navigate = useNavigate()
  const [jobs, setJobs] = useState(null)

  useEffect(() => {
    let alive = true
    const tick = async () => {
      try { const d = await api.queueOverview(); if (alive) setJobs(d.jobs || []) } catch { /* ignore */ }
    }
    tick()
    const id = setInterval(tick, 4000)
    return () => { alive = false; clearInterval(id) }
  }, [])

  const totalQueued = (jobs || []).reduce((n, j) => n + (j.current != null ? 1 : 0) + j.pending.length, 0)

  return (
    <div className="page page-narrow">
      <div className="mb-6">
        <h1 className="font-reading text-2xl font-medium">Activity</h1>
        <p className="text-sm text-hint">
          {jobs == null ? 'Loading…'
            : jobs.length === 0 ? 'Nothing translating right now.'
            : `${totalQueued} chapter${totalQueued === 1 ? '' : 's'} across ${jobs.length} novel${jobs.length === 1 ? '' : 's'}.`}
        </p>
      </div>

      {jobs != null && jobs.length === 0 && (
        <div className="rounded-card border border-dashed border-line-strong p-10 text-center text-muted">
          No translations are running. Open a novel and translate some chapters — progress will appear here and on the novel's Activity tab.
          <div className="mt-4"><Link to="/" className="btn btn-primary px-4 py-2">Go to library</Link></div>
        </div>
      )}

      <div className="space-y-3">
        {(jobs || []).map((j) => {
          const inQueue = (j.current != null ? 1 : 0) + j.pending.length
          const preview = j.pending.slice(0, 16).join(', ')
          return (
            <div key={j.pid} className="card p-4">
              <div className="flex items-center justify-between gap-2">
                <button onClick={() => navigate(`/novel/${j.pid}/activity`)} className="truncate text-left font-medium hover:text-accent-text hover:underline">{j.name}</button>
                <span className="shrink-0 text-xs text-muted">{inQueue} in queue</span>
              </div>
              <div className="mt-1 flex items-center gap-2 text-sm text-muted">
                <span className="inline-block h-2 w-2 shrink-0 rounded-full animate-pulse" style={{ background: 'var(--accent)' }} />
                {j.current != null ? <span>Translating <strong>chapter {j.current}</strong></span> : <span>Queued</span>}
              </div>
              {j.pending.length > 0 && (
                <div className="mt-1 text-xs text-hint">waiting: {preview}{j.pending.length > 16 ? ` +${j.pending.length - 16} more` : ''}</div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
