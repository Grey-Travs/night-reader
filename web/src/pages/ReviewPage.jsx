import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api'
import { Badge, SkeletonRows } from '../components/ui'
import { TASK_LABEL_BARE as TASK_LABEL, isPageTask } from '../tasks'
import { useToast } from '../toast'

// A mis-gendered chapter gets its own repair, so it needs telling apart from the rest.
const isPronoun = (it) => (it.flags || []).includes('pronoun')

const keyOf = (pid, index) => `${pid}:${index}`

// Cross-novel "needs review" inbox: every chapter flagged needs-review or failed,
// across all novels, in one place. Assembled from saved state on the server.
export default function ReviewPage() {
  const navigate = useNavigate()
  const toast = useToast()
  const [items, setItems] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(() => new Set()) // requests still in flight
  // What the novels' workers are doing right now, keyed "pid:index". Repairs are
  // queued rather than run inline, so without this the page could only ever say
  // "off you go" and then look untouched while the work actually happened.
  const [work, setWork] = useState(() => new Map())
  const start = (k) => setBusy((b) => new Set(b).add(k))
  const done = (k) => setBusy((b) => { const n = new Set(b); n.delete(k); return n })

  async function load() {
    try { setItems((await api.reviewInbox()).items); setError(null) }
    catch (e) { setError(String(e.message || e)) }
  }
  useEffect(() => { load() }, [])

  // Poll the queue so every row shows its own live state, and refresh the list as soon
  // as a chapter finishes — a repaired chapter should leave this page by itself.
  const watched = useRef(new Set())
  useEffect(() => {
    let alive = true
    const tick = async () => {
      let jobs = []
      try { jobs = (await api.queueOverview()).jobs || [] } catch { return }
      if (!alive) return
      const next = new Map()
      for (const j of jobs) {
        // A scanned-page job's current/pending are PAGE sequence numbers. This inbox
        // is keyed by chapter index, so folding them in locked whichever flagged
        // chapter happened to share a number with a page being read: its row showed
        // "Reading a page now…" and every repair button went disabled, on a chapter
        // no worker was touching.
        if (isPageTask(j.kind)) continue
        if (j.current != null) next.set(keyOf(j.pid, j.current), { kind: j.kind, active: true, waiting: !!j.waiting })
        for (const i of j.pending || []) next.set(keyOf(j.pid, i), { kind: j.kind, active: false, waiting: false })
      }
      // Anything we were watching that has left the queue has finished: its verdict is
      // saved now, so re-read the inbox (fixed chapters drop off, stuck ones stay).
      let finished = false
      for (const k of watched.current) if (!next.has(k)) { finished = true; break }
      watched.current = new Set(next.keys())
      setWork(next)
      if (finished) load()
    }
    tick()
    const id = setInterval(tick, 2500)
    return () => { alive = false; clearInterval(id) }
  }, [])

  // A queued repair the poll hasn't caught up with yet still has to look queued, or the
  // row would flick back to idle for a couple of seconds right after being clicked.
  const [optimistic, setOptimistic] = useState(() => new Set())
  const markQueued = (keys) => {
    if (!keys.length) return
    setOptimistic((o) => { const n = new Set(o); keys.forEach((k) => n.add(k)); return n })
    setTimeout(() => setOptimistic((o) => {
      const n = new Set(o); keys.forEach((k) => n.delete(k)); return n
    }), 12000)
  }
  const workFor = (pid, index) => {
    const k = keyOf(pid, index)
    return work.get(k) || (optimistic.has(k) ? { kind: 'pronouns', active: false, waiting: false } : null)
  }

  // Every repair returns which chapters it queued and which it cleared outright, so the
  // page can tell "started, watch it happen" apart from "there was nothing to fix".
  function report(res, queuedLabel) {
    const cleared = res?.cleared || []
    const queued = res?.queued || []
    if (queued.length) {
      markQueued(queued.map((i) => keyOf(res.pid, i)))
      toast(queuedLabel(queued.length))
    }
    if (cleared.length) {
      toast(res?.message || `${cleared.length} chapter${cleared.length === 1 ? '' : 's'} cleared — nothing needed fixing.`)
      load()
    }
    // Neither queued nor cleared means it was already on the worker. Saying so beats
    // the click looking like it did nothing at all.
    if (!queued.length && !cleared.length) toast('Already in the queue for this novel')
  }

  async function retranslate(it) {
    const key = keyOf(it.project_id, it.index)
    start(key); setError(null)
    try {
      await api.translate(it.project_id, { indices: [it.index], force: true })
      markQueued([key]); toast('Queued — watch it below')
    } catch (e) { setError(String(e.message || e)) }
    finally { done(key) }
  }
  async function resolve(it) {
    const key = keyOf(it.project_id, it.index)
    start(key); setError(null)
    try {
      await api.resolveChapter(it.project_id, it.index)
      markQueued([key]); toast('AI resolve started')
    } catch (e) { setError(String(e.message || e)) }
    finally { done(key) }
  }
  async function fixPronouns(it) {
    const key = keyOf(it.project_id, it.index)
    start(key); setError(null)
    try {
      const res = await api.fixPronouns(it.project_id, it.index)
      report({ ...res, pid: it.project_id }, () => 'Fixing pronouns — progress shows below')
    } catch (e) { setError(String(e.message || e)) }
    finally { done(key) }
  }
  async function fixPronounsGroup(g) {
    const key = `grp:${g.pid}`
    start(key); setError(null)
    try {
      const res = await api.fixPronounsFlagged(g.pid)
      report({ ...res, pid: g.pid }, (n) => `Fixing pronouns in ${n} chapter${n === 1 ? '' : 's'}`)
    } catch (e) { setError(String(e.message || e)) }
    finally { done(key) }
  }
  async function accept(it) {
    const key = keyOf(it.project_id, it.index)
    start(key); setError(null)
    try { await api.acceptChapter(it.project_id, it.index); toast('Marked as fine'); await load() }
    catch (e) { setError(String(e.message || e)) }
    finally { done(key) }
  }
  async function retranslateGroup(g) {
    const key = `grp:${g.pid}`
    start(key); setError(null)
    try {
      await api.translate(g.pid, { indices: g.rows.map((r) => r.index), force: true })
      markQueued(g.rows.map((r) => keyOf(g.pid, r.index)))
      toast(`Queued ${g.rows.length} chapter${g.rows.length === 1 ? '' : 's'}`)
    } catch (e) { setError(String(e.message || e)) }
    finally { done(key) }
  }

  // Group by novel, preserving the server's sort order.
  const groups = []
  for (const it of items || []) {
    let g = groups.find((x) => x.pid === it.project_id)
    if (!g) { g = { pid: it.project_id, name: it.project_name, rows: [] }; groups.push(g) }
    g.rows.push(it)
  }
  const running = (items || []).filter((it) => workFor(it.project_id, it.index)).length

  return (
    <div className="page">
      <div className="mb-6">
        <h1 className="font-reading text-2xl font-medium">Needs review</h1>
        <p className="text-sm text-hint">
          {items == null ? 'Loading…'
            : items.length === 0 ? 'Nothing flagged — every translated chapter passed its checks.'
            : `${items.length} chapter${items.length === 1 ? '' : 's'} flagged across ${groups.length} novel${groups.length === 1 ? '' : 's'}.`}
          {running > 0 && ` ${running} being repaired now — this list updates itself.`}
        </p>
      </div>
      {error && <div className="mb-4 rounded-btn px-3 py-2 text-sm pill-review">{error}</div>}

      {items == null && <div className="card"><SkeletonRows rows={6} /></div>}

      {items != null && items.length === 0 && (
        <div className="rounded-card border border-dashed border-line-strong p-10 text-center text-muted">
          All clear. Flagged chapters (length/structure mismatches, leftover stray text) show up here when translations need a look.
          <div className="mt-4"><Link to="/" className="btn btn-primary px-4 py-2">Back to library</Link></div>
        </div>
      )}

      <div className="space-y-6">
        {groups.map((g) => {
          const groupBusy = busy.has(`grp:${g.pid}`)
          const groupWorking = g.rows.some((r) => workFor(g.pid, r.index))
          return (
            <section key={g.pid}>
              <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                <button onClick={() => navigate(`/novel/${g.pid}`)} className="font-reading text-lg font-medium hover:text-accent-text hover:underline">{g.name}</button>
                <div className="flex items-center gap-2">
                  <span className="text-xs text-hint">{g.rows.length} flagged</span>
                  {g.rows.some(isPronoun) && (
                    <button onClick={() => fixPronounsGroup(g)} disabled={groupBusy || groupWorking} className="btn btn-ghost px-3 py-1.5 text-xs" title="Rewrite the pronouns in every mis-gendered chapter of this novel">
                      {groupBusy ? 'Starting…' : groupWorking ? 'Working…' : `Fix pronouns (${g.rows.filter(isPronoun).length})`}
                    </button>
                  )}
                  <button onClick={() => retranslateGroup(g)} disabled={groupBusy || groupWorking} className="btn btn-ghost px-3 py-1.5 text-xs">{groupBusy ? 'Starting…' : `Re-translate all (${g.rows.length})`}</button>
                </div>
              </div>
              <div className="card divide-y divide-line">
                {g.rows.map((it) => {
                  const key = keyOf(it.project_id, it.index)
                  const w = workFor(it.project_id, it.index)
                  const locked = busy.has(key) || !!w
                  return (
                    <div key={key} className="flex flex-wrap items-start justify-between gap-3 p-4">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2">
                          <button onClick={() => navigate(`/novel/${it.project_id}/chapter/${it.index}`)} className="font-medium hover:text-accent-text hover:underline">
                            {it.title || `Chapter ${it.index}`}
                          </button>
                          <span className="text-xs tabular-nums text-hint">#{it.index}</span>
                          <Badge status={it.status} />
                          {isPronoun(it) && <span className="pill pill-review !px-1.5 !py-0 text-[11px]">wrong gender</span>}
                        </div>
                        {w ? (
                          // The one thing this page could never say before: it is happening,
                          // right now, and you don't have to go to Activity to believe it.
                          <div className="mt-1 flex items-center gap-2 text-xs text-muted">
                            <span className="inline-block h-2 w-2 shrink-0 rounded-full animate-pulse" style={{ background: 'var(--accent)' }} />
                            {w.waiting ? `${TASK_LABEL[w.kind] || 'Working'} — waiting out a rate limit, it will resume on its own`
                              : w.active ? `${TASK_LABEL[w.kind] || 'Working'} now…`
                              : `Queued — ${(TASK_LABEL[w.kind] || 'work').toLowerCase()} starts when the chapter ahead finishes`}
                          </div>
                        ) : (it.diagnosis?.length || it.failures?.length) > 0 && (
                          <ul className="mt-1 list-disc pl-5 text-xs text-muted">
                            {(it.diagnosis?.length ? it.diagnosis.map((d) => d.message) : it.failures).slice(0, 3).map((m, i) => <li key={i}>{m}</li>)}
                          </ul>
                        )}
                      </div>
                      <div className="flex shrink-0 flex-wrap items-center justify-end gap-2">
                        <button onClick={() => navigate(`/novel/${it.project_id}/chapter/${it.index}`)} className="btn btn-ghost px-3 py-1.5 text-xs">Open</button>
                        {isPronoun(it) && (
                          <button onClick={() => fixPronouns(it)} disabled={locked} className="btn btn-primary px-3 py-1.5 text-xs" title="Rewrite only the pronouns, keeping the rest of the chapter as it is">{busy.has(key) ? 'Starting…' : '⚥ Fix pronouns'}</button>
                        )}
                        <button onClick={() => resolve(it)} disabled={locked} className={`btn ${isPronoun(it) ? 'btn-ghost' : 'btn-primary'} px-3 py-1.5 text-xs`} title="Re-translate targeting the problem">{busy.has(key) ? 'Starting…' : '✨ AI resolve'}</button>
                        <button onClick={() => retranslate(it)} disabled={locked} className="btn btn-ghost px-3 py-1.5 text-xs">Re-translate</button>
                        <button onClick={() => accept(it)} disabled={locked} className="btn btn-ghost px-3 py-1.5 text-xs" title="It's actually fine">Mark fine</button>
                      </div>
                    </div>
                  )
                })}
              </div>
            </section>
          )
        })}
      </div>
    </div>
  )
}
