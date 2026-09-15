import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api'
import { SkeletonRows } from '../components/ui'
import SeriesGlossaryMerge from '../components/SeriesGlossaryMerge'
import SeriesPublishTarget from '../components/SeriesPublishTarget'
import { useToast } from '../toast'

// Confirming a series' chapter numbering.
//
// The global number is read out of each chapter's own export header, which is the only
// signal that actually works here — tab titles are all "Tab N", where N is creation order
// rather than the chapter. That gets it right for most chapters, but not all: some
// documents restart their numbering partway into side stories, some contain a tab that is
// byte-identical to an earlier one, and some novels genuinely skip a chapter. So anything
// the resolver was unsure about is shown here and waits for a person.
//
// `index` is never editable. It is the key tying a chapter to state.json,
// chapter-NNN.md, previous/, audit/ and variants/ — renumbering it would orphan finished
// translations. Only the GLOBAL number and the kind can be changed.
const KINDS = ['chapter', 'prologue', 'epilogue', 'side', 'extra', 'duplicate']

const KIND_HINT = {
  chapter: 'A numbered chapter, part of the reading sequence.',
  prologue: 'Reads before chapter 1 but keeps a number.',
  epilogue: 'Reads after the last chapter but keeps a number.',
  side: 'A side story (외전). Out of the sequence — no chapter number.',
  extra: 'An author note, glossary or cover tab. Out of the sequence.',
  duplicate: 'The same text as an earlier tab. Out of the sequence.',
}

export default function SeriesDetailPage() {
  const { sid } = useParams()
  const toast = useToast()
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [edits, setEdits] = useState({})
  const [onlyFlagged, setOnlyFlagged] = useState(true)
  const [busy, setBusy] = useState('')

  const load = useCallback(async (refresh = false) => {
    try {
      const d = await api.seriesMapping(sid, refresh)
      setData(d)
      setEdits({})
      setError(null)
    } catch (e) {
      setError(e)
    }
  }, [sid])

  useEffect(() => { load() }, [load])

  // One flat list in READING order — member order, then index within the member. Not
  // sorted by global number: a side story sits where it actually lives in the document,
  // which is what makes excluding it from the sequence harmless.
  const rows = useMemo(() => {
    if (!data) return []
    const byPid = data.members || {}
    const out = []
    for (const member of data.series?.members || []) {
      const pid = member.project_id
      const rs = (byPid[pid]?.rows || []).slice().sort((a, b) => a.index - b.index)
      for (const r of rs) out.push({ ...r, pid, memberName: member.name })
    }
    return out
  }, [data])

  const key = (r) => `${r.pid}:${r.index}`
  const edited = (r) => edits[key(r)] || {}
  const shown = (r) => ({ ...r, ...edited(r) })
  const isFlagged = (r) => shown(r).confidence === 'low' || r.kind === 'duplicate'

  function edit(r, patch) {
    setEdits((prev) => ({ ...prev, [key(r)]: { ...prev[key(r)], ...patch } }))
  }

  async function save() {
    const overrides = Object.entries(edits).map(([k, patch]) => {
      const [pid, index] = [k.slice(0, k.lastIndexOf(':')), Number(k.slice(k.lastIndexOf(':') + 1))]
      const out = { project_id: pid, index }
      if ('global' in patch) out.global = patch.global
      if ('kind' in patch) out.kind = patch.kind
      return out
    })
    if (!overrides.length) return
    setBusy('save')
    try {
      const d = await api.confirmSeriesMapping(sid, overrides)
      setData(d)
      setEdits({})
      toast(`Confirmed ${overrides.length} chapter${overrides.length === 1 ? '' : 's'}`)
    } catch (e) {
      setError(e)
    } finally {
      setBusy('')
    }
  }

  async function refresh() {
    setBusy('refresh')
    await load(true)
    setBusy('')
  }

  const series = data?.series
  const flaggedCount = rows.filter(isFlagged).length
  const visible = onlyFlagged ? rows.filter(isFlagged) : rows
  const dirty = Object.keys(edits).length

  return (
    <div className="page">
      <div className="mb-4">
        <Link to="/series" className="text-xs text-hint hover:underline">← Series</Link>
        <h1 className="font-reading text-2xl font-medium">{series?.name || 'Series'}</h1>
        <p className="text-sm text-hint">
          {!data ? 'Loading…'
            : `${series.members.length} documents · chapters ${data.first}–${data.last}`
              + ` · ${data.total} in sequence`}
        </p>
      </div>

      {error && (
        <div className="mb-4 rounded-card px-3 py-2 text-sm pill-review">
          {error.message || String(error)}
        </div>
      )}

      {!data ? <SkeletonRows rows={6} /> : (
        <>
          <section className="mb-5 rounded-card border border-line p-3">
            <h2 className="mb-2 text-sm font-medium">Documents, in reading order</h2>
            <ol className="space-y-1">
              {series.members.map((m, i) => (
                <li key={m.project_id} className="flex items-baseline gap-2 text-xs">
                  <span className="w-4 shrink-0 text-right text-hint">{i + 1}.</span>
                  <Link
                    to={`/novel/${m.project_id}`}
                    className="min-w-0 flex-1 truncate text-muted hover:underline"
                  >
                    {m.name || m.project_id}
                  </Link>
                  <span className="shrink-0 text-hint">{m.chapter_count} tabs</span>
                  {m.sealed && <span className="shrink-0 text-hint">· sealed</span>}
                  {m.offline && <span className="shrink-0 text-hint">· not readable</span>}
                  {m.missing && <span className="shrink-0 pill-review px-1">missing</span>}
                  {m.needs_review > 0 && (
                    <span className="shrink-0 text-hint">· {m.needs_review} to check</span>
                  )}
                </li>
              ))}
            </ol>
            {(data.gaps?.length > 0 || data.duplicates?.length > 0) && (
              <p className="mt-2 text-xs text-hint">
                {data.gaps?.length > 0 && (
                  <>Missing chapter{data.gaps.length === 1 ? '' : 's'} {data.gaps.slice(0, 12).join(', ')}
                    {data.gaps.length > 12 ? ` and ${data.gaps.length - 12} more` : ''}. </>
                )}
                {data.duplicates?.length > 0 && (
                  <>Repeated number{data.duplicates.length === 1 ? '' : 's'} {data.duplicates.slice(0, 12).join(', ')}. </>
                )}
                A gap is not always a mistake — some novels really do skip a number.
              </p>
            )}
          </section>

          <div className="mb-3 flex flex-wrap items-center gap-2">
            <button
              className={`btn px-3 py-1.5 text-xs ${onlyFlagged ? 'btn-primary' : 'btn-ghost'}`}
              onClick={() => setOnlyFlagged(true)}
            >
              Needs checking ({flaggedCount})
            </button>
            <button
              className={`btn px-3 py-1.5 text-xs ${onlyFlagged ? 'btn-ghost' : 'btn-primary'}`}
              onClick={() => setOnlyFlagged(false)}
            >
              All {rows.length}
            </button>
            <div className="flex-1" />
            <button
              className="btn btn-ghost px-3 py-1.5 text-xs"
              disabled={!!busy}
              onClick={refresh}
            >
              {busy === 'refresh' ? 'Re-reading…' : 'Re-read documents'}
            </button>
            <button
              className="btn btn-primary px-3 py-1.5 text-xs"
              disabled={!dirty || !!busy}
              onClick={save}
            >
              {busy === 'save' ? 'Saving…' : dirty ? `Confirm ${dirty}` : 'Confirm'}
            </button>
          </div>

          {visible.length === 0 ? (
            <div className="rounded-card border border-dashed border-line p-10 text-center">
              <p className="text-sm text-muted">
                {onlyFlagged
                  ? 'Nothing needs checking — every chapter stated its own number.'
                  : 'No chapters resolved yet. Try “Re-read documents”.'}
              </p>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="text-hint">
                  <tr className="border-b border-line">
                    <th className="px-2 py-1.5 text-left font-normal">Document</th>
                    <th className="px-2 py-1.5 text-right font-normal">Tab</th>
                    <th className="px-2 py-1.5 text-left font-normal">Stated</th>
                    <th className="px-2 py-1.5 text-left font-normal">Chapter</th>
                    <th className="px-2 py-1.5 text-left font-normal">Kind</th>
                    <th className="px-2 py-1.5 text-left font-normal">Where from</th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((r) => {
                    const v = shown(r)
                    const changed = !!edits[key(r)]
                    return (
                      <tr
                        key={key(r)}
                        className={`border-b border-line/50 ${changed ? 'bg-elevated' : ''}`}
                      >
                        <td className="max-w-[14rem] truncate px-2 py-1 text-muted">
                          {r.memberName || r.pid}
                        </td>
                        <td className="px-2 py-1 text-right text-hint">{r.index}</td>
                        <td className="px-2 py-1 text-hint">
                          {r.header_number ?? '—'}
                        </td>
                        <td className="px-2 py-1">
                          <input
                            type="number"
                            className="w-20 rounded border border-line bg-transparent px-1 py-0.5"
                            value={v.global ?? ''}
                            placeholder="—"
                            onChange={(e) => edit(r, {
                              global: e.target.value === '' ? null : Number(e.target.value),
                            })}
                          />
                        </td>
                        <td className="px-2 py-1">
                          <select
                            className="rounded border border-line bg-transparent px-1 py-0.5"
                            value={v.kind || 'chapter'}
                            title={KIND_HINT[v.kind || 'chapter']}
                            onChange={(e) => edit(r, { kind: e.target.value })}
                          >
                            {KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
                          </select>
                        </td>
                        <td className="px-2 py-1 text-hint">
                          {v.source === 'header' ? 'its own header'
                            : v.source === 'inferred' ? 'guessed from neighbours'
                            : v.source === 'auto-dupe' ? `same text as tab ${r.duplicate_of}`
                            : v.source === 'manual' ? 'you confirmed it'
                            : 'nothing stated'}
                          {v.confidence === 'low' && v.source !== 'manual' && (
                            <span className="ml-1 pill-review px-1">check</span>
                          )}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}

          <p className="mt-3 text-xs text-hint">
            Clear a chapter number to take that tab out of the reading sequence — its
            translation, glossary and files are never touched, it simply stops taking up a
            chapter number.
          </p>

          <SeriesGlossaryMerge
            sid={sid}
            merged={!!series.glossary_merged_at}
            onMerged={() => load()}
          />

          <SeriesPublishTarget
            sid={sid}
            targets={series.publish_targets}
            onSaved={() => load()}
          />
        </>
      )}
    </div>
  )
}
