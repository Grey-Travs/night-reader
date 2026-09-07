import { useEffect, useRef, useState } from 'react'
import { NavLink, Outlet, useNavigate, useOutletContext, useParams } from 'react-router-dom'
import { api } from '../api'
import { errorTitle } from '../errors'
import { useError } from './ErrorDialog'
import { clearPausedJob, getLastRead, getPausedJob, setPausedJob } from '../prefs'
import { TASK_LABEL, chapterScopedQueue } from '../tasks'

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

// Events for a page carry page_id; chapter events never do. (kind alone also settles
// it — see isPageTask in ../tasks — but page_id is the stronger signal on an event.)
const noun = (e) => (e.page_id ? 'page' : 'chapter')
const Noun = (e) => (e.page_id ? 'Page' : 'Chapter')

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
  const [submitting, setSubmitting] = useState(false)
  const [log, setLog] = useState([])
  // The chapter in flight: { index, title, chars, source: [], english, chunk }. Kept
  // OUT of `log` on purpose — streamed text arrives many times per chapter and would
  // grow that array without bound while rerendering every line.
  const [live, setLive] = useState(null)
  const [totals, setTotals] = useState(null)
  const [paused, setPaused] = useState(null) // { message, resets_at, pending } | null
  // Server-side wait: the worker is riding out a rate limit and resumes by itself.
  const [waiting, setWaiting] = useState(null) // { resume_at, resets_at, message, since } | null
  const [queue, setQueue] = useState({ current: null, kind: 'translate', pending: [] })
  const esRef = useRef(null)
  const jobIdRef = useRef(null)
  const resumeRef = useRef(null)
  const resumeJobRef = useRef(null)
  const submittingRef = useRef(false)  // guards against double-submit races
  const dataRef = useRef(null)         // live `data` for long-lived stream closures
  // The SSE handler is attached once at stream start, so anything it calls has to be
  // reached through a ref rather than captured from this render's scope.
  const showError = useError()
  const showErrorRef = useRef(showError)
  const enqueueRef = useRef(null)
  useEffect(() => { showErrorRef.current = showError }, [showError])

  // Keep a ref in sync with `data` so the SSE onmessage closure (attached once, at
  // stream start) reads the CURRENT project, not the null it closed over at attach.
  useEffect(() => { dataRef.current = data }, [data])

  // `stillWanted` is the [pid] effect's `alive` flag. Without it a slow novel's
  // response landed in whichever novel was open by the time it arrived: novel A's
  // title, chapter table and glossary rendered under novel B, and clicking a row
  // navigated to /novel/B/chapter/<A's index>. Calls from sub-pages pass nothing,
  // because those always concern the novel already on screen.
  async function load(refresh = false, stillWanted = null) {
    const wanted = () => (stillWanted ? stillWanted() : true)
    setLoading(true)
    setError(null)
    try {
      const fetched = await api.chapters(pid, refresh)
      if (!wanted()) return
      setData(fetched)
    } catch (e) {
      if (wanted()) setError(e)
    } finally {
      if (wanted()) setLoading(false)
    }
  }
  async function loadPending(stillWanted = null) {
    try {
      const g = await api.glossary(pid)
      if (stillWanted && !stillWanted()) return
      setPendingCount((g.pending || []).length)
      setGlossary(g.locked || [])
    } catch { /* a missing glossary just means no tips and no badge */ }
  }

  // Reset everything and re-attach when the novel changes (or on first mount). The
  // `alive` flag guards the async reattach: React Router keeps this layout mounted
  // across an in-place :pid change (back/forward between novels, a deep link), so a
  // late activeJob promise from the previous novel must NOT attach its stream or
  // restore its pause banner into the novel now being viewed.
  useEffect(() => {
    let alive = true
    setData(null); setLog([]); setLive(null); setTotals(null); setPaused(null); setWaiting(null); setQueue({ current: null, kind: 'translate', pending: [] }); setRunning(false); setGlossary([])
    const alive_ = () => alive
    load(false, alive_); loadPending(alive_)
    api.activeJob(pid).then((j) => {
      if (!alive) return
      if (j.job_id) {
        setRunning(true)
        setQueue({ current: j.current ?? null, kind: j.kind || 'translate', pending: j.pending || [] })
        setWaiting(j.waiting ?? null)
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
  async function resumeJob() {
    // A live worker waiting out a rate limit just needs a wake-up; the queue is
    // still intact server-side. The re-enqueue path below is only for the case
    // where no job survives (e.g. the server restarted mid-pause).
    if (waiting && jobIdRef.current) {
      try { await api.resumeNow(pid); return } catch { /* fall through to re-enqueue */ }
    }
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
      let e
      try { e = JSON.parse(ev.data) } catch { return }  // ignore a malformed/keep-alive frame
      if ('pending' in e) setQueue({ current: e.current ?? null, kind: e.kind || 'translate', pending: e.pending || [] })
      if ('waiting' in e) setWaiting(e.waiting ?? null)
      if (e.type === 'start') {
        // A page event must never restyle the chapter row that happens to share its
        // number — after a build, page 5 and chapter 5 both exist.
        if (!e.page_id) setRowStatus(e.index, 'translating')
        setLive({
          index: e.index, title: e.title, chars: e.chars, model: e.model, effort: e.effort,
          task: e.kind || 'translate',
          source: [], english: '', committed: '', chunk: [1, 1],
          started_at: e.started_at || Date.now() / 1000,
        })
        setLog((l) => [...l, { kind: 'info', text: `${TASK_LABEL[e.kind] || TASK_LABEL.translate} ${noun(e)} ${e.index}…` }])
      } else if (e.type === 'live') {
        // Catch-up frame for a stream that connected mid-chapter (reload, second tab).
        setLive({
          index: e.index, title: e.title, chars: e.chars,
          source: e.source || [], english: e.english || '', committed: e.committed || '',
          chunk: e.chunk || [1, 1], started_at: e.started_at,
        })
      } else if (e.type === 'source') {
        setLive((v) => (v && v.index === e.index ? { ...v, source: e.source || [] } : v))
      } else if (e.type === 'delta') {
        setLive((v) => (v && v.index === e.index
          ? { ...v, english: v.english + (e.text || ''), chunk: e.chunk || v.chunk }
          : v))
      } else if (e.type === 'chunk') {
        setLive((v) => (v && v.index === e.index
          ? { ...v, chunk: e.chunk || v.chunk, committed: v.english }
          : v))
      } else if (e.type === 'reset') {
        setLive((v) => (v && v.index === e.index ? { ...v, english: e.english || '' } : v))
        setLog((l) => [...l, { kind: 'warn', text: `Restarting (${e.reason})…` }])
      } else if (e.type === 'chapter') {
        if (!e.page_id) setRowStatus(e.index, e.status)
        setLive(null)
        if (e.totals) setTotals(e.totals)
        // `refused` means a repair declined to write anything — the chapter is exactly
        // as it was. That's a warning, not a failure, and the text must say so.
        const what = e.aborted ? 'stopped' : e.skipped ? 'already done'
          : e.refused ? 'left unchanged' : e.status
        setLog((l) => [...l, {
          kind: e.status === 'failed' ? 'error' : e.refused ? 'warn'
            : e.status === 'validated' ? 'good' : 'info',
          text: `${Noun(e)} ${e.index}: ${what}${e.error ? ` — ${e.error}` : ''}`,
        }])
        // A mid-queue failure carries the same explanation the HTTP layer produces, so
        // surface it the same way rather than leaving it as one grey log line.
        if (e.status === 'failed' && e.explain) {
          showErrorRef.current?.(e.explain, {
            context: e.page_id ? `reading page ${e.index}` : `translating chapter ${e.index}`,
            // Retrying is chapter work; a failed page is retried from the Pages tab,
            // where re-reading it is one button next to the photo.
            onRetry: e.page_id ? undefined : () => enqueueRef.current?.([e.index], true),
          })
        }
      } else if (e.type === 'queued') {
        setLog((l) => [...l, { kind: 'info', text: `Queued ${(e.added || []).length} chapter${(e.added || []).length === 1 ? '' : 's'}` }])
      } else if (e.type === 'waiting') {
        // The SERVER is riding this out and will resume by itself — keep the
        // stream open and `running` true; no client timer. localStorage is kept
        // in sync only as a fallback for a server restart mid-wait.
        const when = e.resume_at ? new Date(e.resume_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : 'soon'
        setLive(null)
        setLog((l) => [...l, { kind: 'warn', text: `Rate limit reached — waiting for Claude to refresh (resumes ~${when})` }])
        notify('Waiting for Claude to refresh', 'Translation will resume automatically.')
        setPausedJob(pid, { message: e.message, resets_at: e.resets_at, pending: e.pending || [] })
      } else if (e.type === 'resumed') {
        setLog((l) => [...l, { kind: 'info', text: 'Claude refreshed — resuming…' }])
        clearPausedJob(pid)
      } else if (e.type === 'paused') {
        const pend = e.pending || []
        setPaused({ message: e.message, resets_at: e.resets_at, pending: pend })
        setLive(null)
        setLog((l) => [...l, { kind: 'warn', text: `Paused: ${e.message}` }])
        notify('Translation paused', e.message)
        setPausedJob(pid, { message: e.message, resets_at: e.resets_at, pending: pend })
        scheduleResume(e.resets_at)
        finish(es)
      } else if (e.type === 'done') {
        if (e.totals) setTotals(e.totals)
        setLive(null)
        setLog((l) => [...l, { kind: 'good', text: 'Done.' }])
        clearPausedJob(pid)
        notify('Translation complete', `${dataRef.current?.project?.name || 'Your novel'} — chapters are ready.`)
        finish(es)
      }
    }
    es.onerror = () => { if (es.readyState === EventSource.CLOSED) finish(es) }
  }

  // Every operation on this novel — translate, AI resolve, fix pronouns — goes through
  // one submit path, because they all queue on the SAME server-side worker. That's what
  // puts a repair in the Activity view and on the live console for free, and it's why a
  // repair requested mid-translation queues behind it instead of racing it.
  async function submitTask(call, { context, onRetry, resetLog = true } = {}) {
    // Re-entrancy guard: a rapid second click (or a resume firing mid-start) must not
    // race the first request and attach a second EventSource for the same job.
    if (submittingRef.current) return null
    submittingRef.current = true
    setSubmitting(true)
    const wasIdle = !esRef.current
    if (wasIdle) {
      clearResumeTimer()
      clearPausedJob(pid)
      setPaused(null)
      if (resetLog) setLog([])
      setLive(null)
      setRunning(true)
      ensureNotifyPermission()
    }
    try {
      const res = await call()
      if (!res.job_id) {
        // Nothing was queued — the request resolved on the spot (a pronoun flag that
        // turned out to be stale clears itself without a model call). There is no job
        // to stream, so undo the optimistic "running" or the novel would sit there
        // claiming to be busy forever, and pick up the new state instead.
        setQueue({ current: null, kind: 'translate', pending: [] })
        if (wasIdle) setRunning(false)
        if (res.message) setLog((l) => [...l, { kind: 'good', text: res.message }])
        load()
        return res
      }
      setQueue({ current: res.current ?? null, kind: res.kind || 'translate', pending: res.pending || [] })
      if (jobIdRef.current !== res.job_id) {
        esRef.current?.close()
        attachStream(res.job_id)
        setRunning(true)
      }
      return res
    } catch (e) {
      setError(e)
      showError(e, { context, onRetry })
      if (wasIdle) setRunning(false)
      return null
    } finally {
      submittingRef.current = false
      setSubmitting(false)
    }
  }

  async function enqueue(indices, force = false, { foldBacklog = true } = {}) {
    const wasIdle = !esRef.current
    const backlog = wasIdle && foldBacklog
      ? ((paused?.pending?.length ? paused.pending : getPausedJob(pid)?.pending) || [])
      : []
    const res = await submitTask(
      () => api.translate(pid, { ...(indices ? { indices } : {}), force }),
      { context: 'starting a translation', onRetry: () => enqueue(indices, force) },
    )
    if (res && backlog.length) {
      try {
        const more = await api.translate(pid, { indices: backlog, force: true })
        setQueue({ current: more.current ?? null, kind: more.kind || 'translate', pending: more.pending || [] })
      } catch { /* the main request already started; a failed backlog fold isn't fatal */ }
    }
  }
  enqueueRef.current = enqueue

  // AI resolve: re-translate ONE flagged chapter with a correction aimed at what failed.
  const resolveChapter = (index) => submitTask(
    () => api.resolveChapter(pid, index),
    { context: `AI resolve on chapter ${index}`, onRetry: () => resolveChapter(index), resetLog: false },
  )

  // Fix pronouns: rewrite only the mis-gendered pronouns, keeping the existing prose.
  const fixPronouns = (index) => submitTask(
    () => api.fixPronouns(pid, index),
    { context: `fixing pronouns in chapter ${index}`, onRetry: () => fixPronouns(index), resetLog: false },
  )

  const fixPronounsFlagged = () => submitTask(
    () => api.fixPronounsFlagged(pid),
    { context: 'fixing pronouns across this novel', onRetry: () => fixPronounsFlagged(), resetLog: false },
  )

  function finish(es) {
    es?.close()
    if (es && esRef.current !== es) return
    esRef.current = null
    jobIdRef.current = null
    setRunning(false)
    setWaiting(null)
    setQueue({ current: null, kind: 'translate', pending: [] })
    load()
    loadPending()
  }

  async function cancelQueue({ stopCurrent = false } = {}) {
    // Cancelling must also cancel a pending auto-resume — otherwise the timer fires
    // later and silently re-enqueues the backlog the user just cleared.
    clearResumeTimer()
    clearPausedJob(pid)
    setPaused(null)
    setWaiting(null)
    try {
      const r = await api.cancelQueue(pid, stopCurrent)
      setQueue({ current: r.current ?? null, kind: r.kind || 'translate', pending: r.pending || [] })
      setLog((l) => [...l, {
        kind: 'warn',
        text: stopCurrent
          ? `Stopping${r.stopped != null ? ` chapter ${r.stopped}` : ''}…`
          : 'Queue cleared.',
      }])
    } catch (e) {
      setError(e)
      showError(e, { context: stopCurrent ? 'stopping the translation' : 'clearing the queue' })
    }
  }
  // Stop the chapter mid-flight as well as clearing the backlog. The server can't kill
  // the worker thread, so this asks the translator to bail out cooperatively — it lands
  // within a message or two rather than instantly.
  const stopAll = () => cancelQueue({ stopCurrent: true })

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

  // Empty whenever the queue's numbers are page sequence numbers rather than chapter
  // indices — see chapterScopedQueue. Everything asking "is this CHAPTER busy?" reads
  // this, never `queue` directly.
  const chapterQueue = chapterScopedQueue(queue)
  const lastRead = getLastRead(pid)

  const ctx = {
    pid, status, data, loading, error, reload: load, loadPending, pendingCount, glossary,
    setRowStatus, setProjectMeta, showError,
    chapters, offline, counts, koreanTotal, done, remaining,
    running, submitting, log, live, totals: totals || data?.totals, paused, waiting,
    queue, chapterQueue, totalQueued, enqueue, cancelQueue, stopAll, resumeJob,
    // Pages start OCR work through this, so a page run attaches the SSE stream and
    // flips `running` exactly as a translation does.
    submitTask,
    resolveChapter, fixPronouns, fixPronounsFlagged,
  }

  return (
    <div className="min-h-screen">
      <header className="border-b border-line" style={{ background: 'var(--surface)' }}>
        <div className="mx-auto max-w-6xl px-6 pt-4">
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <h1 className="truncate font-reading text-xl font-medium leading-tight">{data?.project?.name || 'Novel'}</h1>
              <p className="text-xs text-hint">
                {data?.total ?? '—'}{' '}
                {data?.project?.source_type === 'images' ? 'chapters' : 'tabs'}
                {data?.project?.source_type === 'text' ? ' · pasted text' : ''}
                {data?.project?.source_type === 'images' ? ' · from photos' : ''}
              </p>
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
            {data?.project?.source_type === 'images' && (
              <NavLink to={`/novel/${pid}/pages`} className={subClass}>Pages</NavLink>
            )}
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

      {error && (
        <div className="mx-auto mt-4 max-w-6xl px-6">
          <div className="flex flex-wrap items-center justify-between gap-2 rounded-card px-3 py-2 text-sm pill-review">
            <span>{errorTitle(error)}</span>
            <button
              onClick={() => showError(error)}
              className="shrink-0 text-xs underline hover:no-underline"
            >
              Details →
            </button>
          </div>
        </div>
      )}

      <Outlet context={ctx} />
    </div>
  )
}
