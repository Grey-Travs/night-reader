import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'
import { SkeletonRows } from '../components/ui'

// Every photographed novel in one place — which ones still have pages to read, and
// which have pages the model wasn't sure about. Mirrors the Review inbox: a list of
// what wants attention, each row landing you where the work happens.
export default function ScansPage() {
  const navigate = useNavigate()
  const [novels, setNovels] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let alive = true
    api.scans()
      .then((d) => { if (alive) setNovels(d.novels || []) })
      .catch((e) => { if (alive) setError(e) })
    return () => { alive = false }
  }, [])

  const totals = (novels || []).reduce((acc, n) => ({
    unread: acc.unread + n.counts.new + n.counts.failed,
    check: acc.check + n.counts['needs-check'],
    pages: acc.pages + n.counts.total,
  }), { unread: 0, check: 0, pages: 0 })

  return (
    <div className="page page-narrow">
      <div className="mb-6">
        <h1 className="font-reading text-2xl font-medium">Scans</h1>
        <p className="text-sm text-hint">
          {novels == null ? 'Loading…'
            : novels.length === 0
              ? 'No novels from photos yet. Add one from the library with “Photos”.'
              : `${novels.length} novel${novels.length === 1 ? '' : 's'} · ${totals.pages} pages · `
                + `${totals.unread} not read yet · ${totals.check} to check`}
        </p>
      </div>

      {error && (
        <div className="mb-4 rounded-card px-3 py-2 text-sm pill-review">
          {error.message || String(error)}
        </div>
      )}

      {novels == null ? (
        <SkeletonRows rows={4} />
      ) : novels.length === 0 ? (
        <div className="rounded-card border border-dashed border-line p-10 text-center">
          <p className="text-sm text-muted">
            Photos of printed pages, screenshots and scans all work.
          </p>
          <button className="btn btn-primary mt-3 px-4 py-2 text-sm" onClick={() => navigate('/')}>
            Add one from the library
          </button>
        </div>
      ) : (
        <section className="card divide-y divide-line overflow-hidden">
          {novels.map((n) => {
            const wants = n.counts.new + n.counts.failed + n.counts['needs-check']
            return (
              <button
                key={n.id}
                onClick={() => navigate(`/novel/${n.id}/pages`)}
                className="rowhover flex w-full items-center justify-between gap-3 px-4 py-3 text-left"
              >
                <div className="min-w-0">
                  <div className="truncate font-reading">{n.name}</div>
                  <div className="text-xs text-hint">
                    {n.counts.total} page{n.counts.total === 1 ? '' : 's'}
                    {n.built ? ` · ${n.chapter_count} chapter${n.chapter_count === 1 ? '' : 's'} built`
                      : ' · not built into chapters yet'}
                  </div>
                </div>
                <div className="flex shrink-0 items-center gap-1.5">
                  {n.counts.new + n.counts.failed > 0 && (
                    <span className="pill pill-queued">{n.counts.new + n.counts.failed} to read</span>
                  )}
                  {n.counts['needs-check'] > 0 && (
                    <span className="pill pill-review">{n.counts['needs-check']} to check</span>
                  )}
                  {wants === 0 && n.counts.total > 0 && (
                    <span className="pill pill-translated">All read</span>
                  )}
                </div>
              </button>
            )
          })}
        </section>
      )}
    </div>
  )
}
