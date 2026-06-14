import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { Badge, Dot, ProgressBar, StatCard, STATUS_LABEL } from './ui'
import ChapterReader from './ChapterReader'
import GlossaryPanel from './GlossaryPanel'
import ProjectSettingsModal from './ProjectSettingsModal'
import ThemeToggle from './ThemeToggle'
import { clearPausedJob, getLastRead, getPausedJob, setPausedJob } from '../prefs'

// Korean chapters that can be (re)translated — validated ones included, so they can
// be bulk-selected for a re-translate. (Empty/English/in-flight excluded.)
const SELECTABLE = ['pending', 'needs-review', 'failed', 'validated']
const FILTER_ORDER = ['pending', 'validated', 'needs-review', 'failed', 'english-source', 'empty']

const isSelectable = (ch) => ch.language === 'korean' && SELECTABLE.includes(ch.status)

function ensureNotifyPermission() {
  try { if (window.Notification && Notification.permission === 'default') Notification.requestPermission() } catch { /* ignore */ }
}
function notify(title, body) {
  try { if (window.Notification && Notification.permission === 'granted') new Notification(title, { body }) } catch { /* ignore */ }
}

export default function ProjectView({ pid, status, initialChapter = null, onBack, onSettings }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [reader, setReader] = useState(initialChapter)
  const [showGlossary, setShowGlossary] = useState(false)
  const [showNovelSettings, setShowNovelSettings] = useState(false)
  const [showExport, setShowExport] = useState(false)
  const [pendingCount, setPendingCount] = useState(0)
  const [lastRead, setLastReadState] = useState(() => getLastRead(pid))

  const [running, setRunning] = useState(false)
  const [log, setLog] = useState([])
  const [paused, setPaused] = useState(null) // { message, resets_at, pending } | null
  const [queue, setQueue] = useState({ current: null, pending: [] })
  const esRef = useRef(null)
  const jobIdRef = useRef(null)     // id of the job our stream is attached to
  const resumeRef = useRef(null)
  const resumeJobRef = useRef(null) // latest resumeJob, for stale-free auto-resume

  const [selected, setSelected] = useState(() => new Set())
  const [filter, setFilter] = useState('all')
  const [searchQ, setSearchQ] = useState('')
  const [searchResults, setSearchResults] = useState(null)
  const [searching, setSearching] = useState(false)

  async function load(refresh = false) {
    setLoading(true)
    setError(null)
    try {
      setData(await api.chapters(pid, refresh))
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setLoading(false)
    }
  }
  async function loadPending() {
    try { setPendingCount((await api.glossary(pid)).pending.length) } catch { /* ignore */ }
  }

  useEffect(() => {
    load(); loadPending()
    // Reattach to an in-flight job (survives a reload or leaving and returning).
    api.activeJob(pid).then((j) => {
      if (j.job_id) {
        setRunning(true)
        setQueue({ current: j.current ?? null, pending: j.pending || [] })
        attachStream(j.job_id)
      } else restorePause()
    }).catch(restorePause)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pid])

  useEffect(() => () => { esRef.current?.close(); clearResumeTimer() }, [])

  // Keep the selection in sync with what's actually selectable on reload.
  useEffect(() => {
    if (!data) return
    const selectable = new Set(data.chapters.filter(isSelectable).map((c) => c.index))
    setSelected((s) => {
      const next = new Set([...s].filter((i) => selectable.has(i)))
      return next.size === s.size ? s : next
    })
  }, [data])

  // Rehydrate a rate-limit pause persisted before a reload: show the banner and
  // re-arm auto-resume so the run still picks back up unattended.
  function restorePause() {
    const p = getPausedJob(pid)
    if (!p) return
    setPaused({ message: p.message || "Your plan's limit was reached.", resets_at: p.resets_at, pending: p.pending || [] })
    scheduleResume(p.resets_at)
  }

  function setRowStatus(index, statusVal) {
    setData((d) => d && {
      ...d,
      chapters: d.chapters.map((c) => (c.index === index ? { ...c, status: statusVal } : c)),
    })
  }

  function clearResumeTimer() {
    if (resumeRef.current) { clearTimeout(resumeRef.current); resumeRef.current = null }
  }
  function scheduleResume(resetsAt) {
    clearResumeTimer()
    if (!resetsAt) return
    const ms = resetsAt * 1000 - Date.now() + 3000
    if (ms <= 0 || ms > 6 * 3600 * 1000) return // ignore absent/absurd reset times
    resumeRef.current = setTimeout(() => resumeJobRef.current?.(), ms)
  }
  function resumeJob() {
    const pend = (paused?.pending && paused.pending.length ? paused.pending : getPausedJob(pid)?.pending) || []
    // Clear the pause first so enqueue() doesn't also try to fold the same backlog in.
    clearResumeTimer()
    clearPausedJob(pid)
    setPaused(null)
    if (pend.length) enqueue(pend, true, { foldBacklog: false })
  }
  resumeJobRef.current = resumeJob

  function attachStream(jobId) {
    const es = new EventSource(api.streamUrl(pid, jobId))
    esRef.current = es
    jobIdRef.current = jobId
    es.onmessage = (ev) => {
      const e = JSON.parse(ev.data)
      if ('pending' in e) setQueue({ current: e.current ?? null, pending: e.pending || [] })
      if (e.type === 'start') {
        setRowStatus(e.index, 'translating')
        setLog((l) => [...l, `Translating chapter ${e.index}…`])
      } else if (e.type === 'chapter') {
        setRowStatus(e.index, e.status)
        setLog((l) => [...l, `Chapter ${e.index}: ${e.skipped ? 'already done' : e.status}`])
      } else if (e.type === 'queued') {
        setLog((l) => [...l, `Queued ${(e.added || []).length} chapter${(e.added || []).length === 1 ? '' : 's'}`])
      } else if (e.type === 'paused') {
        const pend = e.pending || []
        setPaused({ message: e.message, resets_at: e.resets_at, pending: pend })
        setLog((l) => [...l, `Paused: ${e.message}`])
        notify('Translation paused', e.message)
        setPausedJob(pid, { message: e.message, resets_at: e.resets_at, pending: pend })
        scheduleResume(e.resets_at)
        finish(es)
      } else if (e.type === 'done') {
        setLog((l) => [...l, 'Done.'])
        clearPausedJob(pid)
        notify('Translation complete', `${data?.project?.name || 'Your novel'} — chapters are ready.`)
        finish(es)
      }
    }
    es.onerror = () => { if (es.readyState === EventSource.CLOSED) finish(es) }
  }

  // Enqueue chapters. Starts a worker if none is running, otherwise appends to the
  // running one — so you never wait for the current chapter to finish.
  async function enqueue(indices, force = false, { foldBacklog = true } = {}) {
    const wasIdle = !esRef.current
    // Fold any outstanding paused backlog into this run so a normal Translate click
    // while a pause banner is showing never silently discards it. (resumeJob passes
    // foldBacklog:false because it already enqueues the backlog itself.)
    const backlog = wasIdle && foldBacklog
      ? ((paused?.pending?.length ? paused.pending : getPausedJob(pid)?.pending) || [])
      : []
    if (wasIdle) {
      clearResumeTimer()
      clearPausedJob(pid)
      setPaused(null)
      setLog([])
      setRunning(true)
      ensureNotifyPermission()
    }
    try {
      const res = await api.translate(pid, { ...(indices ? { indices } : {}), force })
      setQueue({ current: res.current ?? null, pending: res.pending || [] })
      // Attach to whatever job the backend actually used — covers a fresh start AND
      // the done→finish window where the backend spawns a brand-new job.
      if (jobIdRef.current !== res.job_id) {
        esRef.current?.close()
        attachStream(res.job_id)
        setRunning(true)
      }
      if (backlog.length) {
        const more = await api.translate(pid, { indices: backlog, force: true })
        setQueue({ current: more.current ?? null, pending: more.pending || [] })
      }
    } catch (e) {
      setError(String(e.message || e))
      if (wasIdle) setRunning(false)
    }
  }

  function finish(es) {
    es?.close()
    if (es && esRef.current !== es) return // a stale stream we already replaced
    esRef.current = null
    jobIdRef.current = null
    setRunning(false)
    setQueue({ current: null, pending: [] })
    load()
    loadPending()
  }

  async function cancelQueue() {
    try {
      const r = await api.cancelQueue(pid)
      setQueue({ current: r.current ?? null, pending: r.pending || [] })
      setLog((l) => [...l, 'Queue cleared.'])
    } catch (e) { setError(String(e.message || e)) }
  }

  async function doSearch(e) {
    e?.preventDefault()
    const q = searchQ.trim()
    if (!q) { setSearchResults(null); return }
    setSearching(true)
    try {
      setSearchResults((await api.searchChapters(pid, q)).results)
    } catch (err) {
      setError(String(err.message || err))
    } finally {
      setSearching(false)
    }
  }

  function openReader(index) { setReader(index) }
  function closeReader() { setReader(null); setLastReadState(getLastRead(pid)) }

  const chapters = data?.chapters || []
  const counts = {}
  for (const c of chapters) counts[c.status] = (counts[c.status] || 0) + 1
  const koreanTotal =
    (counts.pending || 0) + (counts.translating || 0) + (counts.validated || 0) +
    (counts['needs-review'] || 0) + (counts.failed || 0)
  const done = counts.validated || 0
  const remaining = counts.pending || 0
  const queuedSet = new Set(queue.pending)

  const visible = filter === 'all'
    ? chapters
    : chapters.filter((c) => c.status === filter || c.status === 'translating')
  const selectableVisible = visible.filter(isSelectable)
  const allVisibleSelected = selectableVisible.length > 0 && selectableVisible.every((c) => selected.has(c.index))
  const availableFilters = FILTER_ORDER.filter((s) => (counts[s] || 0) > 0)

  function toggleSelect(index) {
    setSelected((s) => { const n = new Set(s); n.has(index) ? n.delete(index) : n.add(index); return n })
  }
  function toggleSelectAll() {
    setSelected((s) => {
      const n = new Set(s)
      if (allVisibleSelected) selectableVisible.forEach((c) => n.delete(c.index))
      else selectableVisible.forEach((c) => n.add(c.index))
      return n
    })
  }
  function clearSelection() { setSelected(new Set()) }
  function translateSelected() {
    if (!selected.size) return
    const indices = [...selected].sort((a, b) => a - b)
    clearSelection()
    enqueue(indices, true) // force: re-translate validated, translate the rest
  }

  function onNovelSaved(updated) {
    setData((d) => d && { ...d, project: { ...d.project, ...updated } })
  }

  const totalQueued = (queue.current != null ? 1 : 0) + queue.pending.length

  return (
    <div className="min-h-screen bg-page text-ink">
      <header className="border-b border-line">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-3 px-6 py-4">
          <div className="flex min-w-0 items-center gap-3">
            <button onClick={onBack} className="btn btn-ghost px-2.5 py-1 text-sm">← Library</button>
            <div className="min-w-0">
              <h1 className="truncate font-reading text-lg font-medium leading-tight">{data?.project?.name || 'Novel'}</h1>
              <p className="text-xs text-hint">{data?.total ?? '—'} tabs{data?.project?.source_type === 'text' ? ' · pasted text' : ''}</p>
            </div>
          </div>
          <div className="flex flex-wrap items-center justify-end gap-2 gap-y-2 text-sm text-muted sm:gap-3">
            {lastRead != null && (
              <button onClick={() => openReader(lastRead)} className="btn btn-ghost px-3 py-1.5">Continue · Ch {lastRead}</button>
            )}
            <span className="hidden items-center gap-1.5 lg:flex"><Dot ok={status?.google_logged_in} /> Google</span>
            <span className="hidden items-center gap-1.5 lg:flex"><Dot ok={status?.claude_logged_in} /> Claude</span>
            <button onClick={() => setShowGlossary(true)} className="btn btn-ghost relative px-3 py-1.5">
              Glossary
              {pendingCount > 0 && (
                <span className="absolute -right-2 -top-2 rounded-full px-1.5 text-xs font-medium pill-review">{pendingCount}</span>
              )}
            </button>
            <button onClick={() => setShowNovelSettings(true)} className="btn btn-ghost px-3 py-1.5">Novel</button>
            {done > 0 && (
              <div className="relative">
                <button onClick={() => setShowExport((v) => !v)} className="btn btn-ghost px-3 py-1.5">Export</button>
                {showExport && (
                  <div className="absolute right-0 top-full z-20 mt-1 w-40 rounded-card border border-line p-1 text-sm shadow-lg" style={{ background: 'var(--elevated)' }} onMouseLeave={() => setShowExport(false)}>
                    {[['epub', 'EPUB (e-reader)'], ['md', 'Markdown'], ['txt', 'Plain text']].map(([fmt, label]) => (
                      <a key={fmt} href={api.exportUrl(pid, fmt)} onClick={() => setShowExport(false)} className="block rounded-btn px-3 py-2 hover:bg-[color-mix(in_oklab,var(--ink)_6%,transparent)]">{label}</a>
                    ))}
                  </div>
                )}
              </div>
            )}
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
              {' · tick chapters to (re)translate just those — they queue up'}
            </div>
          </div>
          <button onClick={() => enqueue(null, false)} disabled={remaining === 0} className="btn btn-primary px-5 py-2.5">
            {running ? `Queue all remaining (${remaining})` : `Translate all remaining (${remaining})`}
          </button>
        </section>

        {/* Queue bar */}
        {totalQueued > 0 && (
          <div className="mb-6 flex flex-wrap items-center justify-between gap-2 rounded-card border border-line p-3 text-sm" style={{ background: 'var(--b-translating-bg)', color: 'var(--b-translating-tx)' }}>
            <span>
              {queue.current != null ? <>Translating <strong>chapter {queue.current}</strong></> : 'Queued'}
              {queue.pending.length > 0 && ` · ${queue.pending.length} waiting in queue`}
            </span>
            {queue.pending.length > 0 && (
              <button onClick={cancelQueue} className="btn btn-ghost shrink-0 px-3 py-1 text-xs">Clear queue</button>
            )}
          </div>
        )}

        {paused && (
          <div className="mb-6 flex flex-col gap-2 rounded-card border border-line p-3 text-sm sm:flex-row sm:items-center sm:justify-between" style={{ background: 'var(--b-queued-bg)', color: 'var(--b-queued-tx)' }}>
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
          <div className="flex flex-col gap-3 border-b border-line px-4 py-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="mr-1 text-sm font-medium text-muted">Chapters</span>
                {data && ['all', ...availableFilters].map((f) => {
                  const label = f === 'all' ? 'All' : (STATUS_LABEL[f] || f)
                  const count = f === 'all' ? chapters.length : (counts[f] || 0)
                  return (
                    <button key={f} onClick={() => setFilter(f)} className={`btn px-2.5 py-1 text-xs ${filter === f ? 'btn-primary' : 'btn-ghost'}`}>
                      {label} <span className="opacity-70">{count}</span>
                    </button>
                  )
                })}
              </div>
              {selected.size > 0 ? (
                <div className="flex items-center gap-2">
                  <span className="text-xs text-muted">{selected.size} selected</span>
                  <button onClick={translateSelected} className="btn btn-primary px-3 py-1.5 text-xs">Translate selected</button>
                  <button onClick={clearSelection} className="btn btn-ghost px-2.5 py-1 text-xs">Clear</button>
                </div>
              ) : done > 0 ? (
                <form onSubmit={doSearch} className="flex items-center gap-1.5">
                  <input value={searchQ} onChange={(e) => setSearchQ(e.target.value)} placeholder="Search translations…" className="input !py-1 text-xs" />
                  <button type="submit" disabled={searching} className="btn btn-ghost px-2.5 py-1 text-xs">{searching ? '…' : 'Search'}</button>
                  {searchResults != null && <button type="button" onClick={() => { setSearchResults(null); setSearchQ('') }} className="btn btn-ghost px-2.5 py-1 text-xs">Clear</button>}
                </form>
              ) : null}
            </div>
          </div>

          {searchResults != null ? (
            <div className="max-h-[60vh] overflow-auto p-3">
              {searchResults.length === 0 ? (
                <div className="p-6 text-center text-hint">No matches for “{searchQ}”.</div>
              ) : (
                <div className="space-y-2">
                  {searchResults.map((r) => (
                    <button key={r.index} onClick={() => openReader(r.index)} className="block w-full rounded-card border border-line p-3 text-left rowhover">
                      <div className="text-sm font-medium">Ch {r.index} · {r.title}</div>
                      <div className="mt-0.5 text-xs text-muted">{r.snippet}</div>
                    </button>
                  ))}
                </div>
              )}
            </div>
          ) : loading && !data ? (
            <div className="p-8 text-center text-hint">Loading your document…</div>
          ) : (
            <div className="max-h-[60vh] overflow-auto">
              <table className="w-full min-w-[600px] text-sm">
                <thead className="sticky top-0 text-left text-xs uppercase tracking-wide text-hint" style={{ background: 'var(--surface)' }}>
                  <tr>
                    <th className="w-9 px-3 py-2">
                      <input type="checkbox" checked={allVisibleSelected} disabled={selectableVisible.length === 0} onChange={toggleSelectAll} aria-label="Select all" style={{ accentColor: 'var(--accent)' }} />
                    </th>
                    <th className="px-4 py-2 font-medium">#</th>
                    <th className="px-4 py-2 font-medium">Title</th>
                    <th className="px-4 py-2 font-medium">Lang</th>
                    <th className="px-4 py-2 font-medium">Status</th>
                    <th className="px-4 py-2 text-right font-medium">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {visible.length === 0 && (
                    <tr><td colSpan={6} className="px-4 py-8 text-center text-hint">No chapters match this filter.</td></tr>
                  )}
                  {visible.map((ch) => {
                    const selectable = isSelectable(ch)
                    const isTranslatable = ch.language === 'korean' && ['pending', 'needs-review', 'failed'].includes(ch.status)
                    const translated = ch.has_output && ch.language === 'korean'
                    const canRead = ch.has_output || ch.language === 'english'
                    const inQueue = queuedSet.has(ch.index)
                    const isCurrent = queue.current === ch.index
                    return (
                      <tr key={ch.index} className="rowhover border-t border-line">
                        <td className="px-3 py-2">
                          {selectable ? (
                            <input type="checkbox" checked={selected.has(ch.index)} onChange={() => toggleSelect(ch.index)} aria-label={`Select chapter ${ch.index}`} style={{ accentColor: 'var(--accent)' }} />
                          ) : null}
                        </td>
                        <td className="px-4 py-2 tabular-nums text-hint">{ch.index}</td>
                        <td className="px-4 py-2 font-medium">
                          <button onClick={() => openReader(ch.index)} className="text-left hover:text-accent-text hover:underline">{ch.title}</button>
                        </td>
                        <td className="px-4 py-2 text-xs text-muted">
                          {ch.language === 'korean' ? '🇰🇷' : ch.language === 'english' ? '🇬🇧' : '—'}
                        </td>
                        <td className="px-4 py-2"><Badge status={ch.status} /></td>
                        <td className="px-4 py-2 text-right whitespace-nowrap">
                          {isCurrent ? (
                            <span className="text-xs text-muted">Translating…</span>
                          ) : inQueue ? (
                            <span className="text-xs text-hint">Queued</span>
                          ) : isTranslatable ? (
                            <button onClick={() => enqueue([ch.index], false)} className="btn btn-ghost px-2.5 py-1 text-xs">Translate</button>
                          ) : (
                            <>
                              {canRead && <button onClick={() => openReader(ch.index)} className="btn btn-ghost px-2.5 py-1 text-xs">Read</button>}
                              {translated && <button onClick={() => enqueue([ch.index], true)} className="btn btn-ghost ml-1.5 px-2.5 py-1 text-xs">Re-translate</button>}
                            </>
                          )}
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

      {reader != null && (
        <ChapterReader
          pid={pid}
          index={reader}
          chapters={chapters}
          onClose={closeReader}
          onNavigate={setReader}
          onChanged={load}
          onRetranslate={(i) => enqueue([i], true)}
        />
      )}
      {showGlossary && (
        <GlossaryPanel
          pid={pid}
          onClose={() => setShowGlossary(false)}
          onChanged={loadPending}
          onRetranslate={(indices) => indices.length && enqueue(indices, true)}
        />
      )}
      {showNovelSettings && (
        <ProjectSettingsModal pid={pid} project={data?.project} onClose={() => setShowNovelSettings(false)} onSaved={onNovelSaved} />
      )}
    </div>
  )
}
