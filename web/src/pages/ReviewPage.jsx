import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api'
import { Badge, SkeletonRows } from '../components/ui'
import { useToast } from '../toast'

// Cross-novel "needs review" inbox: every chapter flagged needs-review or failed,
// across all novels, in one place. Assembled from saved state on the server.
export default function ReviewPage() {
  const navigate = useNavigate()
  const toast = useToast()
  const [items, setItems] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(() => new Set()) // in-flight action keys
  const start = (k) => setBusy((b) => new Set(b).add(k))
  const done = (k) => setBusy((b) => { const n = new Set(b); n.delete(k); return n })

  async function load() {
    try { setItems((await api.reviewInbox()).items) }
    catch (e) { setError(String(e.message || e)) }
  }
  useEffect(() => { load() }, [])

  async function retranslate(it) {
    const key = `${it.project_id}:${it.index}`
    start(key); setError(null)
    try { await api.translate(it.project_id, { indices: [it.index], force: true }); toast('Queued — track it in Activity') }
    catch (e) { setError(String(e.message || e)) }
    finally { done(key) }
  }
  async function resolve(it) {
    const key = `${it.project_id}:${it.index}`
    start(key); setError(null)
    try { await api.resolveChapter(it.project_id, it.index); toast('AI resolve done'); await load() }
    catch (e) { setError(String(e.message || e)) }
    finally { done(key) }
  }
  async function accept(it) {
    const key = `${it.project_id}:${it.index}`
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
      toast(`Queued ${g.rows.length} chapter${g.rows.length === 1 ? '' : 's'} — track in Activity`)
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

  return (
    <div className="page">
      <div className="mb-6">
        <h1 className="font-reading text-2xl font-medium">Needs review</h1>
        <p className="text-sm text-hint">
          {items == null ? 'Loading…'
            : items.length === 0 ? 'Nothing flagged — every translated chapter passed its checks.'
            : `${items.length} chapter${items.length === 1 ? '' : 's'} flagged across ${groups.length} novel${groups.length === 1 ? '' : 's'}.`}
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
        {groups.map((g) => (
          <section key={g.pid}>
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <button onClick={() => navigate(`/novel/${g.pid}`)} className="font-reading text-lg font-medium hover:text-accent-text hover:underline">{g.name}</button>
              <div className="flex items-center gap-2">
                <span className="text-xs text-hint">{g.rows.length} flagged</span>
                <button onClick={() => retranslateGroup(g)} disabled={busy.has(`grp:${g.pid}`)} className="btn btn-ghost px-3 py-1.5 text-xs">{busy.has(`grp:${g.pid}`) ? 'Queuing…' : `Re-translate all (${g.rows.length})`}</button>
              </div>
            </div>
            <div className="card divide-y divide-line">
              {g.rows.map((it) => {
                const key = `${it.project_id}:${it.index}`
                return (
                  <div key={key} className="flex flex-wrap items-start justify-between gap-3 p-4">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <button onClick={() => navigate(`/novel/${it.project_id}/chapter/${it.index}`)} className="font-medium hover:text-accent-text hover:underline">
                          {it.title || `Chapter ${it.index}`}
                        </button>
                        <span className="text-xs tabular-nums text-hint">#{it.index}</span>
                        <Badge status={it.status} />
                      </div>
                      {(it.diagnosis?.length || it.failures?.length) > 0 && (
                        <ul className="mt-1 list-disc pl-5 text-xs text-muted">
                          {(it.diagnosis?.length ? it.diagnosis.map((d) => d.message) : it.failures).slice(0, 3).map((m, i) => <li key={i}>{m}</li>)}
                        </ul>
                      )}
                    </div>
                    <div className="flex shrink-0 flex-wrap items-center justify-end gap-2">
                      <button onClick={() => navigate(`/novel/${it.project_id}/chapter/${it.index}`)} className="btn btn-ghost px-3 py-1.5 text-xs">Open</button>
                      <button onClick={() => resolve(it)} disabled={busy.has(key)} className="btn btn-primary px-3 py-1.5 text-xs" title="Re-translate targeting the problem">{busy.has(key) ? '…' : '✨ AI resolve'}</button>
                      <button onClick={() => retranslate(it)} disabled={busy.has(key)} className="btn btn-ghost px-3 py-1.5 text-xs">Re-translate</button>
                      <button onClick={() => accept(it)} disabled={busy.has(key)} className="btn btn-ghost px-3 py-1.5 text-xs" title="It's actually fine">Mark fine</button>
                    </div>
                  </div>
                )
              })}
            </div>
          </section>
        ))}
      </div>
    </div>
  )
}
