import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api'
import { SkeletonRows } from '../components/ui'
import { useError } from '../components/ErrorDialog'
import { useToast } from '../toast'
import { useConfirm } from '../confirm'

// Cross-novel upkeep: approve glossary suggestions and fix name inconsistencies for the
// whole library from one screen, instead of opening each novel's Glossary and Consistency
// tabs by hand. Same assembly pattern as the Review inbox — the server walks every
// project, isolating each so one unreadable novel can't blank the page.

const TYPES = ['name', 'place', 'skill', 'term', 'other']
const PRONOUNS = ['', 'he', 'she', 'they']

export default function UpkeepPage() {
  const navigate = useNavigate()
  const toast = useToast()
  const confirm = useConfirm()
  const showError = useError()

  const [terms, setTerms] = useState(null)      // { novels: [{pid,name,pending:[]}], total }
  const [cons, setCons] = useState(null)        // { novels: [...], scanning, total_variants, ... }
  const [busy, setBusy] = useState(new Set())
  const [open, setOpen] = useState(new Set())   // expanded novel ids (terms section)
  const [edits, setEdits] = useState({})        // `${pid}:${korean}` -> patch
  const [dropped, setDropped] = useState(new Set()) // locally rejected, pending a save

  const working = (k) => busy.has(k)
  const start = (k) => setBusy((b) => new Set(b).add(k))
  const stop = (k) => setBusy((b) => { const n = new Set(b); n.delete(k); return n })

  async function loadTerms() {
    try { setTerms(await api.allPendingTerms()) }
    catch (e) { showError(e, { context: 'loading pending glossary terms', onRetry: loadTerms }) }
  }
  // The summary answers immediately with whatever the server has already scanned and
  // reports how many novels are still outstanding. A first sweep of a big library reads
  // thousands of files, so rather than hanging on one long request we poll and let rows
  // appear as they land.
  async function loadCons(refresh = false) {
    try {
      const d = await api.consistencySummary(refresh)
      setCons(d)
      return d
    } catch (e) {
      showError(e, { context: 'scanning for name inconsistencies' })
      return null
    }
  }

  useEffect(() => { loadTerms() }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Poll while the server works through the novels it hasn't scanned yet.
  const [pollKey, setPollKey] = useState(0)
  const rescan = () => setPollKey((k) => k + 1)

  useEffect(() => {
    let alive = true
    let timer = null
    const tick = async (refresh) => {
      if (!alive) return
      const d = await loadCons(refresh)
      if (alive && d?.scanning) timer = setTimeout(() => tick(false), 1500)
    }
    tick(pollKey > 0)
    return () => { alive = false; if (timer) clearTimeout(timer) }
  }, [pollKey]) // eslint-disable-line react-hooks/exhaustive-deps

  const keyOf = (pid, t) => `${pid}:${t.korean}`
  const merged = (pid, t) => ({ ...t, ...(edits[keyOf(pid, t)] || {}) })
  const patch = (pid, t, p) =>
    setEdits((e) => ({ ...e, [keyOf(pid, t)]: { ...(e[keyOf(pid, t)] || {}), ...p } }))

  // Build the {approve, reject} payload for one novel from whatever is on screen.
  function payloadFor(novel, { only = null } = {}) {
    const approve = []
    const reject = []
    for (const t of novel.pending) {
      const k = keyOf(novel.pid, t)
      if (only && k !== only) continue
      if (dropped.has(k)) reject.push(t.korean)
      else approve.push(merged(novel.pid, t))
    }
    return { approve, reject }
  }

  async function saveNovel(novel, opts) {
    const k = `save:${novel.pid}`
    start(k)
    try {
      const body = payloadFor(novel, opts)
      if (!body.approve.length && !body.reject.length) return
      await api.reviewGlossaryBulk({ [novel.pid]: body })
      toast(`${novel.name}: ${body.approve.length} added${body.reject.length ? `, ${body.reject.length} dropped` : ''}`)
      await loadTerms()
      setEdits({})
      setDropped(new Set())
    } catch (e) {
      showError(e, { context: `saving glossary terms for ${novel.name}`, onRetry: () => saveNovel(novel, opts) })
    } finally { stop(k) }
  }

  async function acceptEverything() {
    const total = terms?.total || 0
    const ok = await confirm({
      title: 'Accept every suggested term?',
      body:
        `This adds ${total} term${total === 1 ? '' : 's'} across ${terms.novels.length} ` +
        `novel${terms.novels.length === 1 ? '' : 's'} to their glossaries, exactly as Claude suggested them.\n\n` +
        `These are suggestions, not verified facts — a wrong spelling or pronoun locked in ` +
        `here gets injected into every future chapter of that novel. Spot-checking a novel ` +
        `and using "Add all" per novel is safer.\n\n` +
        `You can still edit or delete any term afterwards from a novel's Glossary tab.`,
      confirmLabel: `Accept all ${total}`,
    })
    if (!ok) return
    const k = 'save:all'
    start(k)
    try {
      const by = {}
      for (const n of terms.novels) by[n.pid] = payloadFor(n)
      const res = await api.reviewGlossaryBulk(by)
      toast(`Added ${res.approved} terms across ${res.novels.length} novels`)
      if (res.failed?.length) {
        showError({ code: 'partial', title: `${res.failed.length} novel(s) couldn't be saved`,
          what: 'Everything else was saved. These novels reported a problem:',
          fixes: res.failed.map((f) => `${f.pid}: ${f.error}`) })
      }
      await loadTerms()
      setEdits({}); setDropped(new Set())
    } catch (e) {
      showError(e, { context: 'accepting all glossary terms' })
    } finally { stop(k) }
  }

  async function autoFixConsistency() {
    const n = cons?.total_auto_fixable || 0
    const ok = await confirm({
      title: 'Fix the unambiguous spellings?',
      body:
        `This corrects ${n} name${n === 1 ? '' : 's'} that are spelled differently in some ` +
        `chapters than the spelling their glossary already specifies.\n\n` +
        `Only those are touched — where no glossary entry exists there's no way to know ` +
        `which spelling is right, so those are left for you.\n\n` +
        `Each edited chapter is snapshotted first, so any single chapter stays revertible ` +
        `from the reader.`,
      confirmLabel: 'Fix them',
    })
    if (!ok) return
    const k = 'unify:all'
    start(k)
    try {
      const res = await api.unifyBulk([])
      toast(res.replaced ? `Fixed ${res.replaced} occurrences in ${res.novels.length} novels` : 'Nothing needed fixing')
      await loadCons(true)
    } catch (e) {
      showError(e, { context: 'unifying spellings across novels' })
    } finally { stop(k) }
  }

  const toggle = (pid) => setOpen((s) => {
    const n = new Set(s); n.has(pid) ? n.delete(pid) : n.add(pid); return n
  })

  const termNovels = terms?.novels || []
  const consNovels = (cons?.novels || []).filter((n) => n.variants > 0 || n.missing > 0)

  return (
    <div className="page">
      <div className="mb-6">
        <h1 className="font-reading text-2xl font-medium">Upkeep</h1>
        <p className="text-sm text-hint">
          Glossary suggestions and name consistency for every novel, in one place — so you
          don't have to open each one to keep them tidy.
        </p>
      </div>

      {/* ---------------- pending glossary terms ---------------- */}
      <section className="mb-10">
        <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
          <div>
            <h2 className="font-reading text-lg font-medium">Suggested glossary terms</h2>
            <p className="text-xs text-hint">
              {terms == null ? 'Loading…'
                : terms.total === 0 ? 'Nothing waiting — every suggestion has been decided.'
                : `${terms.total} waiting across ${termNovels.length} novel${termNovels.length === 1 ? '' : 's'}.`}
            </p>
          </div>
          {terms?.total > 0 && (
            <button onClick={acceptEverything} disabled={working('save:all')} className="btn btn-ghost px-3 py-1.5 text-xs">
              {working('save:all') ? 'Adding…' : `Accept everything (${terms.total})`}
            </button>
          )}
        </div>

        {terms == null && <div className="card"><SkeletonRows rows={5} /></div>}

        {terms?.total === 0 && (
          <div className="rounded-card border border-dashed border-line-strong p-8 text-center text-sm text-muted">
            All clear. New terms appear here as chapters are translated.
          </div>
        )}

        <div className="space-y-3">
          {termNovels.map((n) => {
            const expanded = open.has(n.pid)
            const live = n.pending.filter((t) => !dropped.has(keyOf(n.pid, t)))
            return (
              <div key={n.pid} className="card">
                <div className="flex flex-wrap items-center justify-between gap-2 p-3">
                  <button onClick={() => toggle(n.pid)} className="flex items-center gap-2 text-left">
                    <span className="text-xs text-hint">{expanded ? '▾' : '▸'}</span>
                    <span className="font-reading font-medium hover:text-accent-text">{n.name}</span>
                    <span className="pill pill-review !px-1.5 !py-0 text-[11px]">{live.length}</span>
                  </button>
                  <div className="flex items-center gap-2">
                    <button onClick={() => navigate(`/novel/${n.pid}/glossary`)} className="btn btn-ghost px-3 py-1.5 text-xs">Open novel</button>
                    <button onClick={() => saveNovel(n)} disabled={working(`save:${n.pid}`)} className="btn btn-primary px-3 py-1.5 text-xs">
                      {working(`save:${n.pid}`) ? 'Saving…' : `Add all ${live.length}`}
                    </button>
                  </div>
                </div>

                {expanded && (
                  <div className="divide-y divide-line border-t border-line">
                    {n.pending.map((t) => {
                      const k = keyOf(n.pid, t)
                      const v = merged(n.pid, t)
                      const isDropped = dropped.has(k)
                      return (
                        <div key={k} className={`flex flex-wrap items-center gap-2 p-3 text-sm ${isDropped ? 'opacity-40' : ''}`}>
                          <span className="w-28 shrink-0 truncate text-muted" title={t.korean}>{t.korean}</span>
                          <input
                            value={v.english}
                            onChange={(e) => patch(n.pid, t, { english: e.target.value })}
                            disabled={isDropped}
                            className="input !py-1 w-40 text-xs"
                            aria-label={`English for ${t.korean}`}
                          />
                          <select
                            value={TYPES.includes(v.type) ? v.type : 'other'}
                            onChange={(e) => patch(n.pid, t, { type: e.target.value })}
                            disabled={isDropped}
                            className="input !py-1 text-xs"
                            aria-label="Type"
                          >
                            {TYPES.map((x) => <option key={x} value={x}>{x}</option>)}
                          </select>
                          {v.type === 'name' && (
                            <select
                              value={v.pronoun || ''}
                              onChange={(e) => patch(n.pid, t, { pronoun: e.target.value })}
                              disabled={isDropped}
                              className="input !py-1 text-xs"
                              aria-label="Pronoun"
                            >
                              {PRONOUNS.map((x) => <option key={x} value={x}>{x || 'pronoun?'}</option>)}
                            </select>
                          )}
                          {v.note && (
                            <span className="min-w-0 flex-1 truncate text-xs text-hint" title={v.note}>{v.note}</span>
                          )}
                          <span className="ml-auto shrink-0 text-[11px] text-hint">ch {t.chapter}</span>
                          <button
                            onClick={() => setDropped((s) => {
                              const nn = new Set(s); nn.has(k) ? nn.delete(k) : nn.add(k); return nn
                            })}
                            className="tap shrink-0 px-2 py-1 text-xs text-hint hover:text-danger"
                            title={isDropped ? 'Keep this term after all' : "Don't add this term"}
                          >
                            {isDropped ? '↩' : '✕'}
                          </button>
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </section>

      {/* ---------------- consistency ---------------- */}
      <section>
        <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
          <div>
            <h2 className="font-reading text-lg font-medium">Name consistency</h2>
            <p className="text-xs text-hint">
              {cons == null ? 'Scanning…'
                : cons.scanning
                  ? `Scanning… ${cons.total - cons.remaining} of ${cons.total} novels done`
                  : consNovels.length === 0 ? 'Every novel is consistent.'
                  : `${cons.total_variants} inconsistent name${cons.total_variants === 1 ? '' : 's'} · ` +
                    `${cons.total_missing} frequent name${cons.total_missing === 1 ? '' : 's'} not in a glossary.`}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={() => rescan()} disabled={cons?.scanning} className="btn btn-ghost px-3 py-1.5 text-xs">
              {cons?.scanning ? 'Scanning…' : 'Re-scan'}
            </button>
            {cons?.total_auto_fixable > 0 && (
              <button onClick={autoFixConsistency} disabled={working('unify:all')} className="btn btn-primary px-3 py-1.5 text-xs">
                {working('unify:all') ? 'Fixing…' : `Fix ${cons.total_auto_fixable} unambiguous`}
              </button>
            )}
          </div>
        </div>

        {cons == null && <div className="card"><SkeletonRows rows={4} /></div>}

        {cons != null && !cons.scanning && consNovels.length === 0 && (
          <div className="rounded-card border border-dashed border-line-strong p-8 text-center text-sm text-muted">
            No inconsistencies anywhere. <Link to="/" className="text-accent-text hover:underline">Back to library</Link>
          </div>
        )}

        {consNovels.length > 0 && (
          <div className="card divide-y divide-line">
            {consNovels.map((n) => (
              <div key={n.pid} className="flex flex-wrap items-center justify-between gap-2 p-3 text-sm">
                <button onClick={() => navigate(`/novel/${n.pid}/consistency`)} className="font-reading font-medium hover:text-accent-text hover:underline">
                  {n.name}
                </button>
                <div className="flex items-center gap-3 text-xs text-hint">
                  {n.variants > 0 && <span className="pill pill-review !px-1.5 !py-0">{n.variants} inconsistent</span>}
                  {n.auto_fixable > 0 && <span className="text-accent-text">{n.auto_fixable} auto-fixable</span>}
                  {n.missing > 0 && <span>{n.missing} not in glossary</span>}
                  <span className="tabular-nums">{n.scanned} ch scanned</span>
                  <button onClick={() => navigate(`/novel/${n.pid}/consistency`)} className="btn btn-ghost px-3 py-1 text-xs">Review</button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
