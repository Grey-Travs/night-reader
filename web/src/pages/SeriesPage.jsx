import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'
import { SkeletonRows } from '../components/ui'
import ImportPublishLinks from '../components/ImportPublishLinks'
import { useToast } from '../toast'
import { useConfirm } from '../confirm'

// A Google Doc holds about 100 tabs, so a long novel arrives as several documents and the
// library shows them as unrelated novels — reading stops dead at chapter 100, and each
// document keeps its own glossary, so a character's locked spelling silently resets at 101.
//
// This page links them back together. The suggestions come from the documents' names, which
// use at least six different conventions, so they are only ever PROPOSED: nothing is written
// until you press Link.
export default function SeriesPage() {
  const navigate = useNavigate()
  const toast = useToast()
  const confirm = useConfirm()
  const [series, setSeries] = useState(null)
  const [groups, setGroups] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState('')
  // Groups the user has dismissed this session. Kept in memory only: a wrong suggestion
  // is a naming coincidence, not a decision worth persisting.
  const [skipped, setSkipped] = useState(() => new Set())

  const load = useCallback(async () => {
    try {
      const [s, g] = await Promise.all([api.listSeries(), api.suggestSeries()])
      setSeries(s.series || [])
      setGroups(g.groups || [])
      setError(null)
    } catch (e) {
      setError(e)
    }
  }, [])

  useEffect(() => { load() }, [load])

  async function link(group) {
    setBusy(group.key)
    try {
      const created = await api.createSeries(group.name, group.members.map((m) => m.project_id))
      toast(`Linked ${group.members.length} documents as “${created.name}”`)
      await load()
    } catch (e) {
      setError(e)
    } finally {
      setBusy('')
    }
  }

  async function linkAll() {
    const pending = (groups || []).filter((g) => !skipped.has(g.key))
    const ok = await confirm({
      title: `Link ${pending.length} series?`,
      body: 'Each set of documents becomes one series with continuous chapter numbering. '
        + 'Nothing is moved or merged on disk, and you can unlink any of them afterwards. '
        + 'Glossaries are NOT merged yet — that is a separate, deliberate step per series.',
      confirmLabel: `Link ${pending.length}`,
    })
    if (!ok) return
    setBusy('all')
    let done = 0
    for (const group of pending) {
      try {
        await api.createSeries(group.name, group.members.map((m) => m.project_id))
        done += 1
      } catch (e) {
        // One bad group must not abandon the rest — report at the end.
        setError(e)
      }
    }
    setBusy('')
    toast(`Linked ${done} series`)
    await load()
  }

  async function unlink(s) {
    const ok = await confirm({
      title: `Unlink “${s.name}”?`,
      body: 'The documents go back to being separate novels. Nothing is deleted — every '
        + 'translation, glossary and chapter file stays exactly where it is.',
      confirmLabel: 'Unlink', danger: true,
    })
    if (!ok) return
    try {
      await api.deleteSeries(s.id)
      toast(`Unlinked “${s.name}”`)
      await load()
    } catch (e) {
      setError(e)
    }
  }

  const pending = (groups || []).filter((g) => !skipped.has(g.key))
  const loading = series == null || groups == null

  return (
    <div className="page page-narrow">
      <div className="mb-6">
        <h1 className="font-reading text-2xl font-medium">Series</h1>
        <p className="text-sm text-hint">
          {loading ? 'Loading…'
            : series.length === 0 && pending.length === 0
              ? 'Nothing to link — every novel here is a single document.'
              : `${series.length} series · ${pending.length} suggested`}
        </p>
      </div>

      {error && (
        <div className="mb-4 rounded-card px-3 py-2 text-sm pill-review">
          {error.message || String(error)}
        </div>
      )}

      {loading ? <SkeletonRows rows={4} /> : (
        <>
          {pending.length > 0 && (
            <section className="mb-8">
              <div className="mb-2 flex items-baseline justify-between gap-3">
                <h2 className="text-sm font-medium">Suggested</h2>
                <button
                  className="btn btn-primary px-3 py-1.5 text-xs"
                  disabled={!!busy}
                  onClick={linkAll}
                >
                  {busy === 'all' ? 'Linking…' : `Link all ${pending.length}`}
                </button>
              </div>
              <p className="mb-3 text-xs text-hint">
                Matched on the documents’ names. Check the parts are in the right order before
                linking — the chapter numbering follows it.
              </p>
              <div className="space-y-2">
                {pending.map((g) => (
                  <div key={g.key} className="rounded-card border border-line p-3">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="truncate text-sm font-medium">{g.name}</div>
                        <div className="text-xs text-hint">
                          {g.members.length} documents · {g.total_chapters} chapters
                        </div>
                      </div>
                      <div className="flex shrink-0 gap-2">
                        <button
                          className="btn btn-ghost px-2 py-1 text-xs"
                          onClick={() => setSkipped((s) => new Set(s).add(g.key))}
                        >
                          Not a series
                        </button>
                        <button
                          className="btn btn-primary px-3 py-1 text-xs"
                          disabled={!!busy}
                          onClick={() => link(g)}
                        >
                          {busy === g.key ? 'Linking…' : 'Link'}
                        </button>
                      </div>
                    </div>
                    <ol className="mt-2 space-y-1">
                      {g.members.map((m, i) => (
                        <li key={m.project_id} className="flex items-baseline gap-2 text-xs">
                          <span className="w-4 shrink-0 text-right text-hint">{i + 1}.</span>
                          <span className="min-w-0 flex-1 truncate text-muted">{m.name}</span>
                          <span className="shrink-0 text-hint">
                            ch {m.start_chapter}–{m.start_chapter + m.chapter_count - 1}
                          </span>
                        </li>
                      ))}
                    </ol>
                  </div>
                ))}
              </div>
            </section>
          )}

          {series.length > 0 && (
            <section>
              <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
                <h2 className="text-sm font-medium">Linked</h2>
                <ImportPublishLinks onDone={load} />
              </div>
              <div className="space-y-2">
                {series.map((s) => {
                  const flags = (s.gaps?.length || 0) + (s.duplicates?.length || 0)
                  const review = s.members.reduce((n, m) => n + (m.needs_review || 0), 0)
                  return (
                    <div
                      key={s.id}
                      className="cursor-pointer rounded-card border border-line p-3 hover:border-accent"
                      onClick={() => navigate(`/series/${s.id}`)}
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <div className="truncate text-sm font-medium">{s.name}</div>
                          <div className="text-xs text-hint">
                            {s.members.length} documents
                            {s.resolved
                              ? ` · chapters ${s.first}–${s.last} · ${s.total} in sequence`
                              : ' · numbering not resolved yet'}
                          </div>
                        </div>
                        <button
                          className="btn btn-ghost shrink-0 px-2 py-1 text-xs"
                          onClick={(e) => { e.stopPropagation(); unlink(s) }}
                        >
                          Unlink
                        </button>
                      </div>
                      {(flags > 0 || review > 0 || s.members.some((m) => m.missing)) && (
                        <div className="mt-2 flex flex-wrap gap-2 text-xs">
                          {review > 0 && (
                            <span className="rounded-card px-2 py-0.5 pill-review">
                              {review} to check
                            </span>
                          )}
                          {s.gaps?.length > 0 && (
                            <span className="text-hint">
                              missing {s.gaps.slice(0, 6).join(', ')}
                              {s.gaps.length > 6 ? ` +${s.gaps.length - 6}` : ''}
                            </span>
                          )}
                          {s.duplicates?.length > 0 && (
                            <span className="text-hint">
                              repeated {s.duplicates.slice(0, 6).join(', ')}
                            </span>
                          )}
                          {s.members.some((m) => m.missing) && (
                            <span className="text-hint">a document is missing</span>
                          )}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            </section>
          )}

          {series.length === 0 && pending.length === 0 && (
            <div className="rounded-card border border-dashed border-line p-10 text-center">
              <p className="text-sm text-muted">
                When a novel outgrows one Google Doc, add the next document as its own novel
                and it will be suggested here.
              </p>
            </div>
          )}
        </>
      )}
    </div>
  )
}
