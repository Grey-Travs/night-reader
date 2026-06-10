import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { Badge, Dot, ProgressBar, StatCard } from './ui'
import ChapterReader from './ChapterReader'
import GlossaryPanel from './GlossaryPanel'
import ThemeToggle from './ThemeToggle'

export default function ProjectView({ pid, status, onBack, onSettings }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [reader, setReader] = useState(null)
  const [showGlossary, setShowGlossary] = useState(false)
  const [pendingCount, setPendingCount] = useState(0)

  const [running, setRunning] = useState(false)
  const [log, setLog] = useState([])
  const [paused, setPaused] = useState(null)
  const esRef = useRef(null)

  async function load(refresh = false) {
    setLoading(true)
    setError(null)
    try {
      const d = await api.chapters(pid, refresh)
      setData(d)
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setLoading(false)
    }
  }
  async function loadPending() {
    try { setPendingCount((await api.glossary(pid)).pending.length) } catch { /* ignore */ }
  }
  useEffect(() => { load(); loadPending() }, [pid])
  useEffect(() => () => esRef.current?.close(), [])

  function setRowStatus(index, statusVal) {
    setData((d) => d && {
      ...d,
      chapters: d.chapters.map((c) => (c.index === index ? { ...c, status: statusVal } : c)),
    })
  }

  async function startTranslate(indices) {
    if (running) return
    setPaused(null)
    setLog([])
    setRunning(true)
    try {
      const { job_id } = await api.translate(pid, indices ? { indices } : {})
      const es = new EventSource(api.streamUrl(pid, job_id))
      esRef.current = es
      es.onmessage = (ev) => {
        const e = JSON.parse(ev.data)
        if (e.type === 'start') {
          setRowStatus(e.index, 'translating')
          setLog((l) => [...l, `Translating chapter ${e.index}…`])
        } else if (e.type === 'chapter') {
          setRowStatus(e.index, e.status)
          const label = e.skipped ? 'already done' : e.status
          setLog((l) => [...l, `Chapter ${e.index}: ${label}`])
        } else if (e.type === 'paused') {
          setPaused(e.message)
          setLog((l) => [...l, `Paused: ${e.message}`])
          finish(es)
        } else if (e.type === 'done') {
          setLog((l) => [...l, 'Done.'])
          finish(es)
        }
      }
      es.onerror = () => { if (es.readyState === EventSource.CLOSED) finish(es) }
    } catch (e) {
      setError(String(e.message || e))
      setRunning(false)
    }
  }

  function finish(es) {
    es?.close()
    if (esRef.current === es) esRef.current = null
    setRunning(false)
    load()
    loadPending()
  }

  const counts = data?.counts || {}
  const koreanTotal =
    (counts.pending || 0) + (counts.validated || 0) + (counts['needs-review'] || 0) + (counts.failed || 0)
  const done = counts.validated || 0
  const remaining = counts.pending || 0

  return (
    <div className="min-h-screen bg-page text-ink">
      <header className="border-b border-line">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-3 px-6 py-4">
          <div className="flex min-w-0 items-center gap-3">
            <button onClick={onBack} className="btn btn-ghost px-2.5 py-1 text-sm">← Library</button>
            <div className="min-w-0">
              <h1 className="truncate font-reading text-lg font-medium leading-tight">{data?.project?.name || 'Novel'}</h1>
              <p className="text-xs text-hint">{data?.total ?? '—'} tabs</p>
            </div>
          </div>
          <div className="flex flex-wrap items-center justify-end gap-2 gap-y-2 text-sm text-muted sm:gap-3">
            <span className="hidden items-center gap-1.5 sm:flex"><Dot ok={status?.google_logged_in} /> Google</span>
            <span className="hidden items-center gap-1.5 sm:flex"><Dot ok={status?.claude_logged_in} /> Claude</span>
            <button onClick={() => setShowGlossary(true)} className="btn btn-ghost relative px-3 py-1.5">
              Glossary
              {pendingCount > 0 && (
                <span className="absolute -right-2 -top-2 rounded-full px-1.5 text-xs font-medium pill-review">{pendingCount}</span>
              )}
            </button>
            <button onClick={onSettings} className="btn btn-ghost px-3 py-1.5">Settings</button>
            <ThemeToggle />
            <button onClick={() => load(true)} className="btn btn-primary px-3 py-1.5">Refresh</button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-6 py-6">
        {error && <div className="mb-4 rounded-card px-3 py-2 text-sm pill-review">{error}</div>}

        {/* Translate */}
        <section className="card mb-6 flex flex-col gap-3 p-5 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <div className="font-medium">Translate</div>
            <div className="text-sm text-muted">
              {remaining > 0 ? `${remaining} Korean chapter${remaining === 1 ? '' : 's'} still to translate` : 'All Korean chapters translated'}
            </div>
          </div>
          <button onClick={() => startTranslate(null)} disabled={running || remaining === 0} className="btn btn-primary px-5 py-2.5">
            {running ? 'Translating…' : `Translate all remaining (${remaining})`}
          </button>
        </section>

        {paused && (
          <div className="mb-6 rounded-card border border-line p-3 text-sm" style={{ background: 'var(--b-queued-bg)', color: 'var(--b-queued-tx)' }}>
            Your plan's limit was reached. Translation will pick back up later — your progress is saved.
          </div>
        )}

        {/* Live console */}
        {(running || log.length > 0) && (
          <section className="sunken mb-6 p-4">
            <div className="mb-2 text-sm font-medium text-muted">{running ? 'In progress' : 'Last run'}</div>
            <div className="max-h-44 overflow-y-auto font-mono text-xs leading-relaxed text-muted">
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
        <section className="mb-8 grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
          <StatCard label="Total tabs" value={data?.total ?? '—'} />
          <StatCard label="Translated" value={counts.validated || 0} />
          <StatCard label="To translate" value={counts.pending || 0} />
          <StatCard label="Needs review" value={counts['needs-review'] || 0} />
          <StatCard label="Already English" value={counts['english-source'] || 0} />
          <StatCard label="Plan usage" value={`$${(data?.totals?.cost_usd ?? 0).toFixed(2)}`} sub="equivalent" />
        </section>

        {/* Chapters */}
        <section className="card overflow-hidden">
          <div className="border-b border-line px-4 py-3 text-sm font-medium text-muted">Chapters</div>
          {loading && !data ? (
            <div className="p-8 text-center text-hint">Loading your document…</div>
          ) : (
            <div className="max-h-[60vh] overflow-auto">
              <table className="w-full min-w-[560px] text-sm">
                <thead className="sticky top-0 text-left text-xs uppercase tracking-wide text-hint" style={{ background: 'var(--surface)' }}>
                  <tr>
                    <th className="px-4 py-2 font-medium">#</th>
                    <th className="px-4 py-2 font-medium">Title</th>
                    <th className="px-4 py-2 font-medium">Lang</th>
                    <th className="px-4 py-2 font-medium">Status</th>
                    <th className="px-4 py-2 text-right font-medium">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {data?.chapters.map((ch) => {
                    const canTranslate = ch.language === 'korean' && ['pending', 'needs-review', 'failed'].includes(ch.status)
                    return (
                      <tr key={ch.index} className="rowhover border-t border-line">
                        <td className="px-4 py-2 tabular-nums text-hint">{ch.index}</td>
                        <td className="px-4 py-2 font-medium">
                          <button onClick={() => setReader(ch.index)} className="text-left hover:text-accent-text hover:underline">{ch.title}</button>
                        </td>
                        <td className="px-4 py-2 text-xs text-muted">
                          {ch.language === 'korean' ? '🇰🇷' : ch.language === 'english' ? '🇬🇧' : '—'}
                        </td>
                        <td className="px-4 py-2"><Badge status={ch.status} /></td>
                        <td className="px-4 py-2 text-right">
                          {canTranslate ? (
                            <button onClick={() => startTranslate([ch.index])} disabled={running} className="btn btn-ghost px-2.5 py-1 text-xs">Translate</button>
                          ) : ch.has_output ? (
                            <button onClick={() => setReader(ch.index)} className="btn btn-ghost px-2.5 py-1 text-xs">Read</button>
                          ) : null}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </main>

      {reader != null && <ChapterReader pid={pid} index={reader} onClose={() => setReader(null)} />}
      {showGlossary && <GlossaryPanel pid={pid} onClose={() => setShowGlossary(false)} onChanged={loadPending} />}
    </div>
  )
}
