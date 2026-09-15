import { useEffect, useState } from 'react'
import { Link, useNavigate, useOutletContext } from 'react-router-dom'
import { api } from '../api'
import { Badge, STATUS_LABEL, SkeletonRows } from '../components/ui'
import Hint from '../components/Hint'
import { useConfirm } from '../confirm'
import { errorTitle } from '../errors'
import { fmtCost, fmtTokens } from '../format'
import { getReadChapters } from '../prefs'
import { describeTask, taskNoun } from '../tasks'

const SELECTABLE = ['pending', 'needs-review', 'failed', 'validated']
const FILTER_ORDER = ['pending', 'validated', 'needs-review', 'failed', 'english-source', 'empty']
const isSelectable = (ch) => ch.language === 'korean' && SELECTABLE.includes(ch.status)
// Mis-gendered chapters are flagged separately from everything else, because they have
// their own repair — rewriting the pronouns instead of re-translating the chapter.
const isPronoun = (ch) => (ch.flags || []).includes('pronoun')

// Tokens actually billed for a chapter: what was sent plus what came back. Cache reads
// are excluded here since they're the cheap part — they get their own readout on Activity.
const chapterTokens = (ch) => {
  const u = ch?.usage || {}
  return (Number(u.input_tokens) || 0) + (Number(u.output_tokens) || 0)
}
const usageTitle = (ch) => {
  const u = ch?.usage || {}
  return [
    `in ${Number(u.input_tokens) || 0}`,
    `out ${Number(u.output_tokens) || 0}`,
    `cached ${Number(u.cache_read_input_tokens) || 0}`,
  ].join(' · ')
}

export default function ChaptersPage() {
  const {
    pid, data, loading, chapters, offline, counts, done, remaining,
    running, submitting, waiting, queue, chapterQueue, totalQueued, totals, enqueue,
    cancelQueue, stopAll, showError, fixPronounsFlagged,
  } = useOutletContext()
  const navigate = useNavigate()
  const confirm = useConfirm()
  const openReader = (index) => navigate(`/novel/${pid}/chapter/${index}`)

  const pronounFlagged = chapters.filter(isPronoun).length

  // Bulk pronoun repair for the whole novel. Confirmed because it rewrites saved
  // chapters, and worth saying out loud that each one stays revertible.
  async function runFixPronouns() {
    const ok = await confirm({
      title: `Fix pronouns in ${pronounFlagged} chapter${pronounFlagged === 1 ? '' : 's'}?`,
      body: `Each chapter is sent back to Claude to have ONLY its pronouns corrected to match `
        + `your glossary — the prose itself is left alone, and the result is rejected outright `
        + `if anything else changed.

`
        + `The version before the fix is kept, so any chapter can be compared and reverted from `
        + `the reader. This uses your Claude plan, and runs on the Activity tab where you can `
        + `watch it or stop it.`,
      confirmLabel: 'Fix them',
    })
    if (!ok) return
    await fixPronounsFlagged?.()
  }

  async function stop() {
    if (queue.current != null) {
      // The noun follows the kind: during an OCR run this is a page, not a chapter.
      const noun = taskNoun(queue.kind)
      const ok = await confirm({
        title: `Stop ${noun} ${queue.current}?`,
        body: `It stops as soon as Claude sends its next update, usually within a second or two.\n\n`
          + `The ${noun} won't be marked failed and nothing already saved is overwritten — but the `
          + `work done so far is discarded, and re-running it spends your plan allowance again.`
          + (queue.pending.length ? `\n\nThe ${queue.pending.length} ${noun}(s) still queued are dropped too.` : ''),
        confirmLabel: 'Stop it',
      })
      if (!ok) return
    }
    stopAll()
  }

  const [readSet] = useState(() => getReadChapters(pid))
  const [selected, setSelected] = useState(() => new Set())
  const [lastClicked, setLastClicked] = useState(null) // anchor for shift-click ranges
  const [filter, setFilter] = useState('all')
  const [searchQ, setSearchQ] = useState('')
  const [searchResults, setSearchResults] = useState(null)
  const [searching, setSearching] = useState(false)
  const [error, setError] = useState(null)

  // Keep the selection in sync with what's actually selectable as statuses change.
  useEffect(() => {
    if (!data) return
    const selectable = new Set(chapters.filter(isSelectable).map((c) => c.index))
    setSelected((s) => {
      const next = new Set([...s].filter((i) => selectable.has(i)))
      return next.size === s.size ? s : next
    })
  }, [data, chapters])

  // chapterQueue, not queue: while the worker reads scanned pages these numbers are
  // page sequence numbers, and badging chapter 7 as "Queued" because PAGE 7 is
  // waiting is simply a different novel's worth of wrong.
  const queuedSet = new Set(chapterQueue.pending)
  const visible = filter === 'all'
    ? chapters
    : chapters.filter((c) => c.status === filter || c.status === 'translating')
  const selectableVisible = offline ? [] : visible.filter(isSelectable)
  const allVisibleSelected = selectableVisible.length > 0 && selectableVisible.every((c) => selected.has(c.index))
  const availableFilters = FILTER_ORDER.filter((s) => (counts[s] || 0) > 0)

  function toggleSelect(index) {
    setSelected((s) => { const n = new Set(s); n.has(index) ? n.delete(index) : n.add(index); return n })
  }
  // Click a row's checkbox; shift-click selects the range of selectable rows between
  // the previous click and this one (anchored in visible order).
  function onRowCheck(e, index) {
    if (e.shiftKey && lastClicked != null) {
      const ids = selectableVisible.map((c) => c.index)
      const a = ids.indexOf(lastClicked), b = ids.indexOf(index)
      if (a !== -1 && b !== -1) {
        const [lo, hi] = a < b ? [a, b] : [b, a]
        const range = ids.slice(lo, hi + 1)
        setSelected((s) => { const n = new Set(s); range.forEach((i) => n.add(i)); return n })
        setLastClicked(index)
        return
      }
    }
    toggleSelect(index)
    setLastClicked(index)
  }
  // Add every selectable chapter of a given status to the selection.
  function selectByStatus(status) {
    const ids = chapters.filter((c) => c.status === status && isSelectable(c)).map((c) => c.index)
    setSelected((s) => { const n = new Set(s); ids.forEach((i) => n.add(i)); return n })
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
    enqueue(indices, true)
  }

  async function doSearch(e) {
    e?.preventDefault()
    const q = searchQ.trim()
    if (!q) { setSearchResults(null); return }
    setSearching(true)
    try {
      setSearchResults((await api.searchChapters(pid, q)).results)
    } catch (err) {
      setError(err)
    } finally {
      setSearching(false)
    }
  }

  const libTokens = totals?.tokens || {}
  const STATS = [
    ['Translated', done],
    ['To translate', counts.pending || 0],
    ['Needs review', counts['needs-review'] || 0],
    ['Already English', counts['english-source'] || 0],
    ['Plan usage', fmtCost(totals?.cost_usd ?? 0)],
    ...(chapterTokens({ usage: libTokens }) > 0
      ? [['Tokens', fmtTokens(chapterTokens({ usage: libTokens }))]]
      : []),
  ]

  return (
    <div className="page">
      {error && (
        <div className="mb-4 flex flex-wrap items-center justify-between gap-2 rounded-card px-3 py-2 text-sm pill-review">
          <span>{errorTitle(error)}</span>
          <button onClick={() => showError(error)} className="shrink-0 text-xs underline hover:no-underline">Details →</button>
        </div>
      )}

      {offline && (
        <div className="mb-6 rounded-card border border-line p-4 text-sm" style={{ background: 'var(--b-queued-bg)', color: 'var(--b-queued-tx)' }}>
          <div className="font-medium">📖 Read-only saved copy</div>
          <div className="mt-1 opacity-90">
            The original Google Doc isn’t reachable on this device, so this shows the copy saved on this computer.
            You can read, copy, and export your chapters. Translating new chapters isn’t available here — open this
            novel on the device that has access to the document for that.
          </div>
        </div>
      )}

      {/* slim stats strip */}
      <div className="mb-5 flex flex-wrap gap-x-6 gap-y-2 rounded-card border border-line px-4 py-3 text-sm" style={{ background: 'var(--surface)' }}>
        <span className="text-muted">Total <strong className="text-ink tabular-nums">{data?.total ?? '—'}</strong></span>
        {STATS.map(([label, value]) => (
          <span key={label} className="text-muted">{label} <strong className="text-ink tabular-nums">{value}</strong></span>
        ))}
      </div>

      {/* Translate control */}
      {!offline && (
        <section className="card mb-5 flex flex-col gap-3 p-5 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <div className="flex items-center gap-1.5"><span className="font-medium">Translate</span>
              <Hint text="Turns the Korean chapters into English using your Claude plan. Translate all of them, or tick a few below and use “Translate selected” — they queue up." />
            </div>
            <div className="text-sm text-muted">
              {remaining > 0 ? `${remaining} Korean chapter${remaining === 1 ? '' : 's'} still to translate` : 'All Korean chapters translated'}
              {' · tick chapters to (re)translate just those'}
            </div>
          </div>
          <button onClick={() => enqueue(null, false)} disabled={remaining === 0 || submitting} className="btn btn-primary px-5 py-2.5">
            {running ? `Queue all remaining (${remaining})` : `Translate all remaining (${remaining})`}
          </button>
        </section>
      )}

      {/* live pointer to the Activity tab while a job runs */}
      {totalQueued > 0 && (
        <div className="mb-5 flex flex-wrap items-center justify-between gap-2 rounded-card border border-line p-3 text-sm" style={{ background: 'var(--b-translating-bg)', color: 'var(--b-translating-tx)' }}>
          <span className="flex items-center gap-2">
            <span className="inline-block h-2 w-2 rounded-full animate-pulse" style={{ background: 'var(--accent)' }} />
            {/* describeTask, not a hardcoded "Translating chapter": the same worker
                reads scanned pages, and this banner announced "Translating chapter 7"
                while it was reading PAGE 7. */}
            {waiting ? 'Waiting for Claude to refresh'
              : queue.current != null ? <strong>{describeTask(queue.kind, queue.current)}</strong>
              : 'Queued'}
            {queue.pending.length > 0 && ` · ${queue.pending.length} waiting`}
          </span>
          <span className="flex items-center gap-2">
            <Link to={`/novel/${pid}/activity`} className="font-medium hover:underline">View console →</Link>
            {queue.pending.length > 0 && (
              <button onClick={() => cancelQueue()} className="btn btn-ghost shrink-0 px-3 py-1 text-xs">Clear queue</button>
            )}
            {/* Reachable while a single chapter runs, which is exactly when the old
                pending-only condition hid every control. */}
            {(queue.current != null || queue.pending.length > 0) && (
              <button onClick={stop} className="btn btn-ghost shrink-0 px-3 py-1 text-xs" title="Stop the chapter being translated right now">■ Stop</button>
            )}
          </span>
        </div>
      )}

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
                <button onClick={translateSelected} disabled={submitting} className="btn btn-primary px-3 py-1.5 text-xs">Translate selected</button>
                <button onClick={clearSelection} className="btn btn-ghost px-2.5 py-1 text-xs">Clear</button>
              </div>
            ) : (
              <div className="flex flex-wrap items-center gap-1.5">
                {!offline && (counts['needs-review'] || 0) > 0 && (
                  <button onClick={() => selectByStatus('needs-review')} className="btn btn-ghost px-2.5 py-1 text-xs">Select needs-review ({counts['needs-review']})</button>
                )}
                {!offline && (counts.failed || 0) > 0 && (
                  <button onClick={() => selectByStatus('failed')} className="btn btn-ghost px-2.5 py-1 text-xs">Select failed ({counts.failed})</button>
                )}
                {!offline && pronounFlagged > 0 && (
                  <button onClick={runFixPronouns} disabled={submitting} className="btn btn-ghost px-2.5 py-1 text-xs" title="Rewrite the pronouns in every chapter that refers to a character by the wrong gender">
                    Fix pronouns in {pronounFlagged} flagged
                  </button>
                )}
                {done > 0 && (
                  <form onSubmit={doSearch} className="flex items-center gap-1.5">
                    <input value={searchQ} onChange={(e) => setSearchQ(e.target.value)} placeholder="Search translations…" className="input !py-1 text-xs" />
                    <button type="submit" disabled={searching} className="btn btn-ghost px-2.5 py-1 text-xs">{searching ? '…' : 'Search'}</button>
                    {searchResults != null && <button type="button" onClick={() => { setSearchResults(null); setSearchQ('') }} className="btn btn-ghost px-2.5 py-1 text-xs">Clear</button>}
                  </form>
                )}
              </div>
            )}
          </div>
        </div>

        {searchResults != null ? (
          <div className="max-h-[64vh] overflow-auto p-3">
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
          <SkeletonRows rows={8} />
        ) : (
          <div className="max-h-[64vh] overflow-auto">
            <table className="w-full min-w-[600px] text-sm">
              <thead className="sticky top-0 text-left text-xs uppercase tracking-wide text-hint" style={{ background: 'var(--surface)' }}>
                <tr>
                  <th className="w-9 px-3 py-2">
                    <input type="checkbox" checked={allVisibleSelected} disabled={selectableVisible.length === 0} onChange={toggleSelectAll} aria-label="Select all" title="Select all · tip: shift-click a row to select a range" style={{ accentColor: 'var(--accent)' }} />
                  </th>
                  <th className="px-4 py-2 font-medium">#</th>
                  <th className="px-4 py-2 font-medium">Title</th>
                  <th className="px-4 py-2 font-medium">Lang</th>
                  <th className="px-4 py-2 font-medium">Status</th>
                  <th className="hidden px-4 py-2 text-right font-medium lg:table-cell">Usage</th>
                  <th className="px-4 py-2 text-right font-medium">Action</th>
                </tr>
              </thead>
              <tbody>
                {visible.length === 0 && (
                  <tr><td colSpan={7} className="px-4 py-8 text-center text-hint">No chapters match this filter.</td></tr>
                )}
                {visible.map((ch) => {
                  const selectable = !offline && isSelectable(ch)
                  const isTranslatable = !offline && ch.language === 'korean' && ['pending', 'needs-review', 'failed'].includes(ch.status)
                  const translated = ch.has_output && ch.language === 'korean'
                  const canRead = ch.has_output || ch.language === 'english'
                  const inQueue = queuedSet.has(ch.index)
                  const isCurrent = chapterQueue.current === ch.index
                  return (
                    <tr key={ch.index} className="rowhover border-t border-line">
                      <td className="px-3 py-2">
                        {selectable ? (
                          <input type="checkbox" checked={selected.has(ch.index)} readOnly onClick={(e) => onRowCheck(e, ch.index)} aria-label={`Select chapter ${ch.index}`} style={{ accentColor: 'var(--accent)' }} />
                        ) : null}
                      </td>
                      <td className="px-4 py-2 tabular-nums text-hint">{ch.index}</td>
                      <td className="px-4 py-2 font-medium">
                        <button onClick={() => openReader(ch.index)} className="text-left hover:text-accent-text hover:underline">{(ch.global ?? ch.number) ? `Chapter ${ch.global ?? ch.number}` : ch.title}</button>
                        {ch.has_output && !readSet.has(ch.index) && (
                          <span title="Unread" className="ml-2 inline-block h-1.5 w-1.5 rounded-full align-middle" style={{ background: 'var(--accent)' }} />
                        )}
                      </td>
                      <td className="px-4 py-2 text-xs text-muted">
                        {ch.language === 'korean' ? '🇰🇷' : ch.language === 'english' ? '🇬🇧' : '—'}
                      </td>
                      <td className="px-4 py-2">
                        <Badge status={ch.status} />
                        {isPronoun(ch) && <span className="ml-1.5 pill pill-review !px-1.5 !py-0 text-[11px]" title="A character is referred to by the wrong gender">wrong gender</span>}
                      </td>
                      {/* Recorded per chapter all along in state.json, never shown until now. */}
                      <td className="hidden px-4 py-2 text-right text-xs tabular-nums text-hint lg:table-cell">
                        {ch.cost_usd > 0 || chapterTokens(ch) > 0 ? (
                          <span title={usageTitle(ch)}>
                            {chapterTokens(ch) > 0 && `${fmtTokens(chapterTokens(ch))} tok`}
                            {ch.cost_usd > 0 && chapterTokens(ch) > 0 && ' · '}
                            {ch.cost_usd > 0 && fmtCost(ch.cost_usd)}
                          </span>
                        ) : '—'}
                      </td>
                      <td className="px-4 py-2 text-right whitespace-nowrap">
                        {isCurrent ? (
                          <span className="text-xs text-muted">Translating…</span>
                        ) : inQueue ? (
                          <span className="text-xs text-hint">Queued</span>
                        ) : isTranslatable ? (
                          <button onClick={() => enqueue([ch.index], false)} disabled={submitting} className="btn btn-ghost px-2.5 py-1 text-xs">Translate</button>
                        ) : (
                          <>
                            {canRead && <button onClick={() => openReader(ch.index)} className="btn btn-ghost px-2.5 py-1 text-xs">Read</button>}
                            {translated && !offline && <button onClick={() => enqueue([ch.index], true)} disabled={submitting} className="btn btn-ghost ml-1.5 px-2.5 py-1 text-xs">Re-translate</button>}
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
    </div>
  )
}
