import { useEffect, useRef, useState } from 'react'
import { NavLink, Outlet, useNavigate, useOutletContext, useParams } from 'react-router-dom'
import { api } from '../api'
import { clearPausedJob, getLastRead, getPausedJob, setPausedJob } from '../prefs'

// The persistent shell for one novel. It OWNS the translation job (the SSE stream,
// the queue, pause/auto-resume and the live log) and the chapter list, and exposes
// them to the sub-pages (Chapters · Activity · Glossary · Settings · Reader) through
// the router Outlet context. Because the layout stays mounted while only the Outlet
// swaps, switching tabs never tears down a running stream.

function ensureNotifyPermission() {
  try { if (window.Notification && Notification.permission === 'default') Notification.requestPermission() } catch { /* ignore */ }
}
function notify(title, body) {
  try { if (window.Notification && Notification.permission === 'granted') new Notification(title, { body }) } catch { /* ignore */ }
}

const subClass = ({ isActive }) => `subtab ${isActive ? 'subtab-active' : ''}`

export default function ProjectLayout() {
  const { pid } = useParams()
  const navigate = useNavigate()
  const { status } = useOutletContext()

  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [pendingCount, setPendingCount] = useState(0)
  const [glossary, setGlossary] = useState([]) // locked terms, for reader tooltips
  const [showExport, setShowExport] = useState(false)

  const [running, setRunning] = useState(false)
  const [log, setLog] = useState([])
  const [paused, setPaused] = useState(null) // { message, resets_at, pending } | null
  const [queue, setQueue] = useState({ current: null, pending: [] })
  const esRef = useRef(null)
  const jobIdRef = useRef(null)
  const resumeRef = useRef(null)
  const resumeJobRef = useRef(null)

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
    try {
      const g = await api.glossary(pid)
      setPendingCount(g.pending.length)
      setGlossary(g.locked || [])
    } catch { /* ignore */ }
  }

  // Reset everything and re-attach when the novel changes (or on first mount). The
  // `alive` flag guards the async reattach: React Router keeps this layout mounted
  // across an in-place :pid change (back/forward between novels, a deep link), so a
  // late activeJob promise from the previous novel must NOT attach its stream or
  // restore its pause banner into the novel now being viewed.
  useEffect(() => {
    let alive = true
    setData(null); setLog([]); setPaused(null); setQueue({ current: null, pending: [] }); setRunning(false); setGlossary([])
    load(); loadPending()
    api.activeJob(pid).then((j) => {
      if (!alive) return
      if (j.job_id) {
        setRunning(true)
        setQueue({ current: j.current ?? null, pending: j.pending || [] })
        attachStream(j.job_id)
      } else restorePause()
    }).catch(() => { if (alive) restorePause() })
    return () => { alive = false; esRef.current?.close(); esRef.current = null; jobIdRef.current = null; clearResumeTimer() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pid])

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
    if (ms <= 0 || ms > 6 * 3600 * 1000) return
    resumeRef.current = setTimeout(() => resumeJobRef.current?.(), ms)
  }
  function resumeJob() {
    const pend = (paused?.pending && paused.pending.length ? paused.pending : getPausedJob(pid)?.pending) || []
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

  async function enqueue(indices, force = false, { foldBacklog = true } = {}) {
    const wasIdle = !esRef.current
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
    if (es && esRef.current !== es) return
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

  function setProjectMeta(updated) {
    setData((d) => d && { ...d, project: { ...d.project, ...updated } })
  }

  // ---- derived ----
  const chapters = data?.chapters || []
  const offline = !!data?.offline
  const counts = {}
  for (const c of chapters) counts[c.status] = (counts[c.status] || 0) + 1
  const koreanTotal =
    (counts.pending || 0) + (counts.translating || 0) + (counts.validated || 0) +
    (counts['needs-review'] || 0) + (counts.failed || 0)
  const done = counts.validated || 0
  const remaining = counts.pending || 0
  const totalQueued = (queue.current != null ? 1 : 0) + queue.pending.length
  const lastRead = getLastRead(pid)

  const ctx = {
    pid, status, data, loading, error, reload: load, loadPending, pendingCount, glossary,
    setRowStatus, setProjectMeta,
    chapters, offline, counts, koreanTotal, done, remaining,
    running, log, paused, queue, totalQueued, enqueue, cancelQueue, resumeJob,
  }

  return (
    <div className="min-h-screen">
      <header className="border-b border-line" style={{ background: 'var(--surface)' }}>
        <div className="mx-auto max-w-6xl px-6 pt-4">
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <h1 className="truncate font-reading text-xl font-medium leading-tight">{data?.project?.name || 'Novel'}</h1>
              <p className="text-xs text-hint">{data?.total ?? '—'} tabs{data?.project?.source_type === 'text' ? ' · pasted text' : ''}</p>
            </div>
            <div className="flex flex-wrap items-center justify-end gap-2">
              {lastRead != null && (
                <button onClick={() => navigate(`/novel/${pid}/chapter/${lastRead}`)} className="btn btn-ghost px-3 py-1.5 text-sm">Continue · Ch {lastRead}</button>
              )}
              {done > 0 && (
                <div className="relative">
                  <button onClick={() => setShowExport((v) => !v)} className="btn btn-ghost px-3 py-1.5 text-sm">Export</button>
                  {showExport && (
                    <div className="absolute right-0 top-full z-20 mt-1 w-48 rounded-card border border-line p-1 text-sm shadow-lg" style={{ background: 'var(--elevated)' }} onMouseLeave={() => setShowExport(false)}>
                      {[['epub', 'EPUB (e-reader)'], ['md', 'Markdown'], ['txt', 'Plain text']].map(([fmt, label]) => (
                        <a key={fmt} href={api.exportUrl(pid, fmt)} onClick={() => setShowExport(false)} className="block rounded-btn px-3 py-2 hover:bg-[color-mix(in_oklab,var(--ink)_6%,transparent)]">{label}</a>
                      ))}
                      <div className="my-1 border-t border-line" />
                      <a href={api.bundleUrl(pid)} onClick={() => setShowExport(false)} className="block rounded-btn px-3 py-2 hover:bg-[color-mix(in_oklab,var(--ink)_6%,transparent)]" title="Download this whole novel as a .zip to move to another device or keep as a backup">Back up / move (.zip)</a>
                    </div>
                  )}
                </div>
              )}
              <button onClick={() => load(true)} className="btn btn-primary px-3 py-1.5 text-sm">Refresh</button>
            </div>
          </div>

          {/* sub-navigation within the novel */}
          <nav className="mt-3 flex items-center gap-5 overflow-x-auto">
            <NavLink to={`/novel/${pid}`} end className={subClass}>Chapters</NavLink>
            <NavLink to={`/novel/${pid}/activity`} className={subClass}>
              Activity {running && <span className="inline-block h-1.5 w-1.5 rounded-full animate-pulse" style={{ background: 'var(--accent)' }} />}
            </NavLink>
            <NavLink to={`/novel/${pid}/glossary`} className={subClass}>
              Glossary {pendingCount > 0 && <span className="pill pill-review !px-1.5 !py-0 text-[11px]">{pendingCount}</span>}
            </NavLink>
            <NavLink to={`/novel/${pid}/consistency`} className={subClass}>Consistency</NavLink>
            <NavLink to={`/novel/${pid}/settings`} className={subClass}>Settings</NavLink>
          </nav>
        </div>
      </header>

      {error && <div className="mx-auto mt-4 max-w-6xl px-6"><div className="rounded-card px-3 py-2 text-sm pill-review">{error}</div></div>}

      <Outlet context={ctx} />
    </div>
  )
}
