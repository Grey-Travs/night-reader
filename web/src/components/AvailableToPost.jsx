import { useEffect, useState } from 'react'
import { api } from '../api'
import { ago } from '../posting'
import { chapterRuns } from './NewChaptersNote'

// The top of the Posting page: every novel with a publishing link, and which of them
// have translated chapters waiting, busiest first. Picking one loads its plan below.
//
// The counts come from GET /api/posting/available, a quick count per novel rather than
// the full plan, and they are only as good as the last reading of each novel on
// meiko.studio. So freshness is shown on every row rather than buried: a novel never
// read from the site has no believable count at all (everything Night Reader didn't post
// itself looks unposted), and one read weeks ago may count chapters posted since.

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`

function Reading({ site }) {
  if (!site.fetched_at) return null
  return site.fresh
    ? <span className="text-hint">site read {ago(site.fetched_at)}</span>
    : <span style={{ color: 'var(--b-queued-tx)' }}>site read {ago(site.fetched_at)} — may be out of date</span>
}

// "ch 179–200 come first": translated chapters that would not continue where the site
// stops, because the ones in between are not ready yet.
function holeBefore(row) {
  const first = row.ready_numbers[0]
  const top = row.site.max_posted
  if (first == null || top == null || first <= top + 1) return null
  return first - 1 === top + 1 ? `ch ${top + 1}` : `ch ${top + 1}–${first - 1}`
}

export default function AvailableToPost({ currentSid, onPick, reloadKey = 0 }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let alive = true
    api.postingAvailable()
      .then((d) => { if (alive) { setData(d); setError(null) } })
      .catch((e) => { if (alive) setError(e) })
    return () => { alive = false }
  }, [reloadKey])

  if (error) {
    return <p className="mb-5 text-xs text-hint">The overview couldn’t be loaded: {error.message || String(error)}</p>
  }
  if (!data) return <p className="mb-5 text-xs text-hint">Counting what’s ready to post…</p>

  const rows = data.series || []
  const read = rows.filter((r) => r.site.fetched_at && r.site.trusted)
  const ready = read.filter((r) => r.ready > 0)
  const upToDate = read.filter((r) => r.ready === 0)
  const notRead = rows.filter((r) => !r.site.fetched_at)
  const unusable = rows.filter((r) => r.site.fetched_at && !r.site.trusted)
  const total = ready.reduce((n, r) => n + r.ready, 0)

  return (
    <section className="mb-6" aria-labelledby="available-heading">
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="available-heading" className="text-sm font-medium">Available to post</h2>
        <span className="text-xs text-hint">
          {ready.length ? `${plural(total, 'chapter')} across ${plural(ready.length, 'novel')}` : 'Nothing waiting'}
        </span>
      </div>

      {ready.length > 0 && (
        <ul className="card divide-y divide-[var(--border)] overflow-hidden">
          {ready.map((r) => {
            const hole = holeBefore(r)
            const current = r.sid === currentSid
            return (
              <li key={`${r.sid}:${r.target_id}`}>
                <button
                  type="button"
                  onClick={() => onPick(r.sid)}
                  aria-current={current ? 'true' : undefined}
                  className="flex w-full flex-wrap items-baseline gap-x-3 gap-y-0.5 px-3 py-2 text-left text-sm hover:bg-[var(--b-muted-bg)]"
                  style={current ? { background: 'var(--b-muted-bg)' } : undefined}
                >
                  <span className="min-w-0 flex-1 font-medium">{r.name}</span>
                  <span className="shrink-0 tabular-nums">
                    <strong>{r.ready}</strong> ready · ch {chapterRuns(r.ready_numbers)}
                  </span>
                  <span className="basis-full text-xs">
                    <Reading site={r.site} />
                    {hole && <span className="text-hint"> · {hole} aren’t ready yet, so this would leave a gap</span>}
                    {r.unfinished > 0 && <span className="text-hint"> · {plural(r.unfinished, 'more chapter')} not translated yet</span>}
                    {r.unnumbered?.length > 0 && <span className="text-hint"> · new chapters need numbering</span>}
                  </span>
                </button>
              </li>
            )
          })}
        </ul>
      )}

      <div className="mt-2 space-y-1 text-xs text-hint">
        {notRead.length > 0 && (
          <details>
            <summary className="cursor-pointer">
              {plural(notRead.length, 'novel')} never read from meiko.studio, so their counts can’t be trusted yet
            </summary>
            <ul className="mt-1 flex flex-wrap gap-x-3 gap-y-1">
              {notRead.map((r) => (
                <li key={r.sid}><button type="button" className="hover:underline" onClick={() => onPick(r.sid)}>{r.name}</button></li>
              ))}
            </ul>
          </details>
        )}
        {unusable.length > 0 && (
          <p>
            {plural(unusable.length, 'novel')} with a site reading that looks wrong:{' '}
            {unusable.map((r, i) => (
              <span key={r.sid}>{i > 0 && ', '}<button type="button" className="hover:underline" onClick={() => onPick(r.sid)}>{r.name}</button></span>
            ))}.
          </p>
        )}
        {upToDate.length > 0 && (
          <details>
            <summary className="cursor-pointer">{plural(upToDate.length, 'novel')} with nothing left to post</summary>
            <ul className="mt-1 flex flex-wrap gap-x-3 gap-y-1">
              {upToDate.map((r) => (
                <li key={r.sid}><button type="button" className="hover:underline" onClick={() => onPick(r.sid)}>{r.name}</button></li>
              ))}
            </ul>
          </details>
        )}
        <p>
          Each count is only as fresh as the last time that novel was read on meiko.studio. To
          read them all at once, open meiko.studio and press <strong>Read every novel</strong> in
          the Night Reader panel.
        </p>
      </div>
    </section>
  )
}
