import { useState } from 'react'
import { api } from '../api'
import { useToast } from '../toast'

// Set every series' publishing link at once, from the site's own series export.
//
// Titles drift between the two systems — stray spaces, case, the odd reworded word — so
// exact matches are applied in bulk and near-matches are only ever proposed. A rule should
// not be trusted to decide whether "The Loathsome Scapegoat" and "That Loathsome
// Scapegoat" are the same novel.
//
// Only the URL is written. Each series' free/paid cutoff is left alone, because the export
// knows nothing about pricing and resetting it to zero would quietly turn paid chapters
// free on the next run.
export default function ImportPublishLinks({ onDone }) {
  const toast = useToast()
  const [open, setOpen] = useState(false)
  const [csv, setCsv] = useState('')
  const [result, setResult] = useState(null)
  const [picked, setPicked] = useState(() => new Set())
  const [busy, setBusy] = useState('')
  const [error, setError] = useState(null)

  async function check() {
    setBusy('check')
    setError(null)
    try {
      const r = await api.matchPublishLinks(csv)
      setResult(r)
      setPicked(new Set(r.exact.map((e) => e.sid)))
    } catch (e) {
      setError(e)
    } finally {
      setBusy('')
    }
  }

  async function apply() {
    const all = [...(result.exact || []), ...(result.fuzzy || [])]
    const assignments = all.filter((e) => picked.has(e.sid))
      .map((e) => ({ sid: e.sid, url: e.url }))
    if (!assignments.length) return
    setBusy('apply')
    try {
      const r = await api.applyPublishLinks(assignments)
      toast(`Publishing links set on ${r.applied.length} series`)
      setResult(null)
      setCsv('')
      setOpen(false)
      onDone?.()
    } catch (e) {
      setError(e)
    } finally {
      setBusy('')
    }
  }

  function toggle(sid) {
    setPicked((p) => {
      const next = new Set(p)
      next.has(sid) ? next.delete(sid) : next.add(sid)
      return next
    })
  }

  if (!open) {
    return (
      <button className="btn btn-ghost px-3 py-1.5 text-xs" onClick={() => setOpen(true)}>
        Set publishing links from a CSV
      </button>
    )
  }

  const rows = result ? [...result.exact, ...result.fuzzy] : []

  return (
    <section className="mt-3 rounded-card border border-line p-3">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="text-sm font-medium">Publishing links</h2>
        <button className="btn btn-ghost px-2 py-1 text-xs" onClick={() => setOpen(false)}>
          Close
        </button>
      </div>
      <p className="mt-0.5 text-xs text-hint">
        Paste the site’s series export (Title, Type, Status, Views, Admin URL). Exact title
        matches are ticked for you; anything reworded is left for you to confirm. Pricing is
        never touched.
      </p>

      {error && (
        <div className="mt-3 rounded-card px-3 py-2 text-sm pill-review">
          {error.message || String(error)}
        </div>
      )}

      {!result ? (
        <>
          <textarea
            className="mt-2 h-28 w-full rounded border border-line bg-transparent px-2 py-1 font-mono text-xs"
            placeholder={'Title,Type,Status,Views,Admin URL\n"My Novel","novel","ongoing",100,"https://…"'}
            value={csv}
            onChange={(e) => setCsv(e.target.value)}
          />
          <button
            className="btn btn-primary mt-2 px-3 py-1.5 text-xs"
            disabled={!csv.trim() || !!busy}
            onClick={check}
          >
            {busy === 'check' ? 'Matching…' : 'Match against my series'}
          </button>
        </>
      ) : (
        <>
          <p className="mt-2 text-xs text-muted">
            {result.site_rows} novels in the export · {result.exact.length} matched exactly
            {result.fuzzy.length ? ` · ${result.fuzzy.length} reworded` : ''}
            {result.unmatched.length ? ` · ${result.unmatched.length} with no match` : ''}
          </p>
          <div className="mt-2 space-y-1">
            {rows.map((e) => (
              <label key={e.sid} className="flex items-baseline gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={picked.has(e.sid)}
                  onChange={() => toggle(e.sid)}
                />
                <span className="min-w-0 flex-1 truncate">{e.name}</span>
                {e.score != null && (
                  <span className="shrink-0 pill-review px-1">
                    site calls it “{e.site_title.trim()}”
                  </span>
                )}
                {e.current && <span className="shrink-0 text-hint">replaces a link</span>}
              </label>
            ))}
          </div>
          {result.unmatched.length > 0 && (
            <p className="mt-2 text-xs text-hint">
              No match for: {result.unmatched.map((u) => u.name).join(', ')}
            </p>
          )}
          <div className="mt-3 flex items-center gap-2">
            <button
              className="btn btn-primary px-3 py-1.5 text-xs"
              disabled={!picked.size || !!busy}
              onClick={apply}
            >
              {busy === 'apply' ? 'Saving…' : `Set ${picked.size} link${picked.size === 1 ? '' : 's'}`}
            </button>
            <button
              className="btn btn-ghost px-3 py-1.5 text-xs"
              onClick={() => setResult(null)}
            >
              Back
            </button>
          </div>
        </>
      )}
    </section>
  )
}
