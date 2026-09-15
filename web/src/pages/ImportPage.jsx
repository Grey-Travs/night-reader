import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { SkeletonRows } from '../components/ui'

// Novels that are published but not in the library.
//
// About 50 of them, and picking one up used to mean copying every posted chapter into a
// Google Doc by hand. The site answers a whole novel — text included — in one request, so
// the Doc step disappears rather than being automated.
//
// This page is the list and the instructions; the extension does the reading, because only
// it is inside the logged-in browser. Nothing here writes anything.
export default function ImportPage() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [query, setQuery] = useState('')
  const [showAll, setShowAll] = useState(false)

  const load = useCallback(async () => {
    setError(null)
    try {
      setData(await api.importCandidates())
    } catch (e) {
      setError(e)
    }
  }, [])

  useEffect(() => { load() }, [load])

  // The list refreshes itself while you are away doing the import in the other tab, so
  // coming back here shows the novel arrived rather than a stale list.
  useEffect(() => {
    const timer = setInterval(load, 8000)
    return () => clearInterval(timer)
  }, [load])

  const rows = useMemo(() => {
    const all = data?.series || []
    const needle = query.trim().toLowerCase()
    const matching = needle
      ? all.filter((r) => r.name.toLowerCase().includes(needle))
      : all
    return showAll ? matching : matching.filter((r) => !r.in_library)
  }, [data, query, showAll])

  const total = data?.series?.length || 0
  const importable = data?.importable || 0

  return (
    <div className="page">
      <div className="mb-5">
        <h1 className="font-reading text-2xl font-medium">Import</h1>
        <p className="text-sm text-hint">
          Novels you have already published that aren’t in your library yet. The extension
          reads them straight off the site — no copying chapters into a document.
        </p>
      </div>

      {error && (
        <div className="mb-4 rounded-card px-3 py-2 text-sm pill-review">
          {error.message || String(error)}
        </div>
      )}

      {data == null ? <SkeletonRows rows={5} /> : total === 0 ? (
        <div className="rounded-card border border-dashed border-line p-8">
          <p className="text-sm font-medium">No list of your novels yet.</p>
          <p className="mt-2 text-sm text-muted">
            Open meiko.studio in the browser with the extension loaded. It sends the list
            on its own within half a minute — or press <b>Send novel list</b> in its panel
            to do it now. Then come back here.
          </p>
          <p className="mt-2 text-xs text-hint">
            Night Reader can’t read the site itself: it has no account there, which is the
            whole reason the extension exists.
          </p>
        </div>
      ) : (
        <>
          <div className="mb-4 flex flex-wrap items-center gap-2">
            <input
              type="text"
              className="input min-w-0 flex-1 text-sm"
              placeholder="Find a novel…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
            <button
              className="btn btn-ghost px-3 py-1.5 text-xs"
              onClick={() => setShowAll((v) => !v)}
            >
              {showAll ? 'Only ones to import' : `Show all ${total}`}
            </button>
            <button className="btn btn-ghost px-3 py-1.5 text-xs" onClick={load}>
              Refresh
            </button>
          </div>

          <p className="mb-4 text-sm text-muted">
            <b>{importable}</b> of {total} aren’t in your library yet.
            {data.fetched_at && (
              <span className="text-hint"> · list read {ago(data.fetched_at)}</span>
            )}
          </p>

          <ol className="mb-6 ml-4 list-decimal space-y-1 text-xs text-hint">
            <li>Open one below on the site.</li>
            <li>In the extension panel, press <b>Check this novel</b> — it reports what
              would come in without changing anything.</li>
            <li>If it looks right, press <b>Import it</b>.</li>
          </ol>

          {rows.length === 0 ? (
            <div className="rounded-card border border-dashed border-line p-8 text-center">
              <p className="text-sm text-muted">
                {query.trim()
                  ? 'Nothing matches that.'
                  : 'Every novel on the site is already in your library.'}
              </p>
            </div>
          ) : (
            <ul className="space-y-2">
              {rows.map((r) => (
                <li
                  key={r.uid}
                  className="flex flex-wrap items-center gap-3 rounded-card border border-line px-3 py-2.5"
                >
                  <span className="min-w-0 flex-1 truncate text-sm font-medium">
                    {r.name}
                  </span>
                  {r.views > 0 && (
                    <span className="shrink-0 text-xs text-hint">
                      {r.views.toLocaleString()} views
                    </span>
                  )}
                  {r.in_library ? (
                    <span className="pill pill-muted shrink-0" title={r.why}>
                      in your library
                    </span>
                  ) : r.series_url ? (
                    <a
                      href={r.series_url}
                      target="_blank"
                      rel="noreferrer"
                      className="btn btn-ghost shrink-0 px-3 py-1.5 text-xs"
                    >
                      Open on the site →
                    </a>
                  ) : (
                    <span className="shrink-0 text-xs text-hint">no link</span>
                  )}
                </li>
              ))}
            </ul>
          )}

          <p className="mt-6 text-xs text-hint">
            An imported novel arrives as finished English chapters, already marked as
            published, so the{' '}
            <Link to="/posting" className="hover:underline">Posting</Link> page won’t offer
            to publish them again. Carry on translating from the next chapter by adding its
            Google Doc as a new novel and linking the two on the{' '}
            <Link to="/series" className="hover:underline">Series</Link> page.
          </p>
        </>
      )}
    </div>
  )
}

function ago(iso) {
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000
  if (!Number.isFinite(seconds)) return 'recently'
  if (seconds < 90) return 'just now'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  return hours < 36 ? `${hours}h ago` : `${Math.round(hours / 24)}d ago`
}
