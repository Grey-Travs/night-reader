import { useEffect, useState } from 'react'
import { api } from '../api'
import { SkeletonCards, StatCard } from '../components/ui'
import { cacheHitRate, fmtCost, fmtPercent, fmtTokens } from '../format'

// Personal statistics: what Night Reader has done since the first novel went in. Every
// number comes from /api/stats, which only adds up what the app already records.

const fmtInt = (n) => (n == null ? '—' : Number(n).toLocaleString())
const fmtDate = (iso) => {
  if (!iso) return null
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? null : d.toLocaleDateString(undefined, { day: 'numeric', month: 'long', year: 'numeric' })
}

// The four kinds of token, in the order they happen in one call. Colours come from the
// app's badge palette so they hold up in both themes.
const TOKEN_PARTS = [
  { key: 'input_tokens', label: 'Sent fresh', color: 'var(--accent)' },
  { key: 'cache_creation_input_tokens', label: 'Written to cache', color: 'var(--b-queued-tx)' },
  { key: 'cache_read_input_tokens', label: 'Read from cache', color: 'var(--b-muted-tx)' },
  { key: 'output_tokens', label: 'Written by Claude', color: 'var(--b-translated-tx)' },
]

// What the app does, one line each. Kept to things that exist in the code today.
const FEATURES = [
  { group: 'Getting novels in', items: [
    ['Google Docs', 'Reads a novel from a Google Doc, one chapter per tab.'],
    ['Pasted text', 'Starts a novel from pasted text or a .txt file.'],
    ['Page photos', 'Claude reads the Korean off photos of the pages.'],
    ['Phone capture', 'Photograph a chapter on your phone; the PC takes it from there.'],
    ['Quick look', 'Flags a blurry or cut-off photo while you can still retake it.'],
    ['Hands-free', 'Phone chapters get read, built and translated with no button presses.'],
    ['Scanner PDFs', 'Splits a scanner-app PDF into pages.'],
    ['Telegram bot', 'Send page photos to your own Telegram bot.'],
    ['Import from meiko', 'Pulls a published novel back into the library.'],
  ] },
  { group: 'Translating', items: [
    ['Whole-chapter translation', 'Translates each chapter in one go to keep voice and honorifics.'],
    ['Glossary', 'Locks names and terms so spellings never drift.'],
    ['Name approval', 'New names wait for your OK before they are used.'],
    ['Quality checks', 'Suspect chapters are flagged for review, never silently accepted.'],
    ['English tabs skipped', 'Tabs already in English are left alone.'],
    ['AI resolve', 'Re-translates a flagged chapter, aimed at what failed.'],
    ['Pronoun fix', 'Fixes he/she slips using the glossary.'],
    ['Paragraph rewrite', 'Redo one paragraph and keep every version to compare.'],
    ['Consistency check', 'Finds names spelled more than one way.'],
    ['Auto-resume', 'Pauses at your plan limit and picks up when it resets.'],
  ] },
  { group: 'Posting', items: [
    ['Series', 'Joins several documents into one novel with one numbering.'],
    ['Post to meiko', 'Posts chapters at the right price, free or paid.'],
    ['Duplicate guard', 'Checks the site before each post so nothing goes up twice.'],
    ['Hide or delete a range', 'Takes a run of chapters down in one go.'],
    ['E-book export', 'Saves a novel as an EPUB.'],
  ] },
  { group: 'Around the app', items: [
    ['Use from your phone', 'Open the app on your phone, away from the PC.'],
    ['Notifications', 'Phone alerts through ntfy when work finishes.'],
    ['Tray icon', 'Open, restart or quit from the icon by the clock.'],
    ['Start with Windows', 'Starts with the PC and keeps it awake while there is work.'],
    ['Command palette', 'Ctrl+K jumps anywhere.'],
    ['Archive', 'Puts finished novels away without deleting them.'],
    ['Light and dark', 'Switch themes from the sidebar.'],
  ] },
]

function TokenHero({ tokens }) {
  const total = Number(tokens?.total) || 0
  const hit = cacheHitRate(tokens)
  return (
    <section className="card p-5 sm:p-6">
      <div className="text-sm text-muted">Tokens used</div>
      <div className="mt-1 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="font-reading text-5xl font-medium tabular-nums">{fmtTokens(total)}</span>
        <span className="text-sm tabular-nums text-hint">{fmtInt(total)} tokens</span>
      </div>
      {total > 0 && (
        <>
          <div className="mt-5 flex h-3 overflow-hidden rounded-full" style={{ background: 'var(--b-muted-bg)' }} aria-hidden>
            {TOKEN_PARTS.map((p) => {
              const v = Number(tokens[p.key]) || 0
              return v > 0 ? <span key={p.key} style={{ width: `${(v / total) * 100}%`, background: p.color }} /> : null
            })}
          </div>
          <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-4">
            {TOKEN_PARTS.map((p) => (
              <div key={p.key} className="min-w-0">
                <dt className="flex items-center gap-2 text-xs text-muted">
                  <span className="inline-block h-2.5 w-2.5 shrink-0 rounded-sm" style={{ background: p.color }} />
                  {p.label}
                </dt>
                <dd className="mt-0.5 text-lg font-semibold tabular-nums">{fmtTokens(tokens[p.key])}</dd>
              </div>
            ))}
          </dl>
          <p className="mt-4 text-xs text-hint">
            {hit != null && <>{fmtPercent(hit)} of what was sent came from the cache. </>}
            Counts translation, pronoun fixes, AI resolves, page reading and paragraph rewrites.
          </p>
        </>
      )}
    </section>
  )
}

// "2026-09" -> a date in that month, for the browser to name in the reader's language.
const monthDate = (m) => new Date(Number(m.slice(0, 4)), Number(m.slice(5, 7)) - 1, 1)
const monthLong = (m) => monthDate(m).toLocaleDateString(undefined, { month: 'long', year: 'numeric' })
const monthShort = (m) => monthDate(m).toLocaleDateString(undefined, { month: 'short' })
const plural = (n, word) => `${fmtInt(n)} ${word}${n === 1 ? '' : 's'}`

// Chapters translated per month: one bar each, oldest on the left, this month last.
// Only the best month and this month carry a number on the bar; every month's figures
// show in the line above the bars on hover or focus (a line rather than a floating
// tooltip, which the scrolling box would clip at either end), and in the table below.
function MonthlyChapters({ monthly }) {
  const [hover, setHover] = useState(null)
  const months = monthly?.months || []
  const undated = monthly?.undated || 0
  if (!months.length) return null

  const current = months[months.length - 1].month
  const max = Math.max(1, ...months.map((r) => r.chapters))
  const best = months.reduce((a, r) => (r.chapters > a.chapters ? r : a), months[0])
  const full = months.filter((r) => r.month !== current)
  const average = full.length ? Math.round(full.reduce((n, r) => n + r.chapters, 0) / full.length) : null
  const describe = (r) => `${monthLong(r.month)}${r.month === current ? ' (so far)' : ''}: ${plural(r.chapters, 'chapter')}`
    + (r.novels ? ` across ${plural(r.novels, 'novel')}` : '')

  return (
    <section className="card p-5 sm:p-6">
      <h2 className="font-reading text-lg font-medium">Chapters translated per month</h2>
      <p className="mt-0.5 text-sm text-hint">
        Best: {monthLong(best.month)}, {plural(best.chapters, 'chapter')}
        {average != null && <> · about {fmtInt(average)} a month</>}
      </p>

      <p className="mt-4 text-sm text-ink" style={{ minHeight: '1.25rem' }} aria-live="polite">
        {hover ? describe(months.find((r) => r.month === hover)) : <span className="text-hint">Point at a month for its numbers.</span>}
      </p>

      <div className="mt-2 overflow-x-auto pb-1">
        <ol className="flex min-w-full items-end gap-2" style={{ height: '11rem' }} aria-label="Chapters translated per month">
          {months.map((r) => {
            const labelled = r.month === best.month || r.month === current
            return (
              <li
                key={r.month}
                tabIndex={0}
                aria-label={describe(r)}
                className="flex h-full min-w-[2.5rem] flex-1 flex-col items-center justify-end rounded-sm outline-none focus-visible:ring-2"
                onMouseEnter={() => setHover(r.month)}
                onMouseLeave={() => setHover(null)}
                onFocus={() => setHover(r.month)}
                onBlur={() => setHover(null)}
              >
                {labelled && r.chapters > 0 && (
                  <span className="mb-1 text-xs font-medium tabular-nums text-ink" aria-hidden>{fmtInt(r.chapters)}</span>
                )}
                <span
                  aria-hidden
                  className="block w-full max-w-[3rem]"
                  style={{
                    // Leaves room above the tallest bar for its number.
                    height: `calc((100% - 1.25rem) * ${r.chapters / max})`,
                    minHeight: r.chapters ? 2 : 0,
                    background: 'var(--accent)',
                    opacity: hover && hover !== r.month ? 0.55 : 1,
                    borderRadius: '4px 4px 0 0',
                  }}
                />
              </li>
            )
          })}
        </ol>
        <div className="flex min-w-full gap-2 border-t border-line pt-1.5" aria-hidden>
          {months.map((r, i) => (
            <div key={r.month} className="min-w-[2.5rem] flex-1 text-center text-xs leading-tight text-muted">
              {monthShort(r.month)}
              {(i === 0 || r.month.endsWith('-01')) && <div className="text-[10px] text-hint">{r.month.slice(0, 4)}</div>}
            </div>
          ))}
        </div>
      </div>

      <details className="mt-4">
        <summary className="cursor-pointer text-xs text-hint hover:text-accent-text">Show as a table</summary>
        <div className="mt-2 overflow-x-auto">
          <table className="w-full text-sm tabular-nums">
            <thead>
              <tr className="text-left text-xs text-muted">
                <th className="py-1 pr-4 font-medium">Month</th>
                <th className="py-1 pr-4 text-right font-medium">Chapters</th>
                <th className="py-1 text-right font-medium">Novels</th>
              </tr>
            </thead>
            <tbody>
              {[...months].reverse().map((r) => (
                <tr key={r.month} className="border-t border-line">
                  <td className="py-1 pr-4">{monthLong(r.month)}{r.month === current ? ' (so far)' : ''}</td>
                  <td className="py-1 pr-4 text-right">{fmtInt(r.chapters)}</td>
                  <td className="py-1 text-right">{fmtInt(r.novels)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>

      {undated > 0 && (
        <p className="mt-3 text-xs text-hint">{plural(undated, 'older chapter')} {undated === 1 ? 'has' : 'have'} no date saved, so {undated === 1 ? 'it isn’t' : 'they aren’t'} in any month.</p>
      )}
    </section>
  )
}

function Feature({ name, text }) {
  const [open, setOpen] = useState(false)
  const id = `feat-${name.replace(/\W+/g, '-').toLowerCase()}`
  return (
    <span className="relative inline-flex">
      <button
        type="button"
        className="rounded-full border border-line px-3 py-1 text-sm text-ink transition-colors hover:border-line-strong"
        style={{ background: open ? 'var(--b-muted-bg)' : 'var(--surface)' }}
        aria-describedby={open ? id : undefined}
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onClick={() => setOpen((v) => !v)}
      >{name}</button>
      {open && (
        <span
          id={id}
          role="tooltip"
          className="absolute left-0 top-full z-40 mt-1.5 w-56 rounded-card border border-line p-2 text-xs leading-snug shadow-lg"
          style={{ background: 'var(--elevated)', color: 'var(--ink)' }}
        >{text}</span>
      )}
    </span>
  )
}

export default function StatsPage() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let alive = true
    api.stats()
      .then((d) => { if (alive) setData(d) })
      .catch((e) => { if (alive) setError(e?.message || 'The numbers could not be loaded.') })
    return () => { alive = false }
  }, [])

  const since = fmtDate(data?.since)
  const updatesSince = fmtDate(data?.app_updates?.since)
  const lastPost = fmtDate(data?.posted?.last_at)

  return (
    <div className="page">
      <div className="mb-6">
        <h1 className="font-reading text-2xl font-medium">Stats</h1>
        <p className="text-sm text-hint">
          {error ? error : data == null ? 'Adding it all up…' : `Everything Night Reader has done${since ? ` since ${since}` : ''}.`}
        </p>
      </div>

      {data == null && !error && <SkeletonCards count={4} />}

      {data && (
        <div className="flex flex-col gap-6">
          <TokenHero tokens={data.tokens} />

          <section className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
            <StatCard label="Chapters translated" value={fmtInt(data.chapters?.translated)}
              sub={data.chapters?.needs_review ? `${data.chapters.needs_review} waiting for review` : 'all checked'} />
            <StatCard label="Novels" value={fmtInt(data.novels?.total)}
              sub={`${fmtInt(data.novels?.active)} active · ${fmtInt(data.novels?.archived)} archived`} />
            <StatCard label="Chapters posted to meiko" value={fmtInt(data.posted?.chapters)}
              sub={data.posted?.novels ? `across ${data.posted.novels} novels${lastPost ? ` · last ${lastPost}` : ''}` : 'none yet'} />
            <StatCard label="Night Reader updates" value={fmtInt(data.app_updates?.count)}
              sub={updatesSince ? `changes since ${updatesSince}` : 'history not available'} />
            <StatCard label="Plan-equivalent value" value={fmtCost(data.cost_usd)} sub="covered by your subscription" />
            <StatCard label="Glossary names" value={fmtInt(data.glossary_names)} sub="names and terms locked in" />
            <StatCard label="Pages read from photos" value={fmtInt(data.pages_read)} sub="transcribed by Claude" />
            <StatCard label="Paragraph rewrites" value={fmtInt(data.rewrites)} sub="single paragraphs redone" />
          </section>

          <MonthlyChapters monthly={data.monthly} />

          {data.unreadable > 0 && (
            <p className="text-xs text-hint">{data.unreadable} file{data.unreadable === 1 ? '' : 's'} could not be read and {data.unreadable === 1 ? 'is' : 'are'} left out of these totals.</p>
          )}

          <section>
            <h2 className="mb-1 font-reading text-lg font-medium">Features</h2>
            <p className="mb-4 text-sm text-hint">Hover over one, or tap it, for what it does.</p>
            <div className="flex flex-col gap-5">
              {FEATURES.map((g) => (
                <div key={g.group}>
                  <h3 className="mb-2 text-xs font-medium uppercase tracking-wider text-muted">{g.group}</h3>
                  <div className="flex flex-wrap gap-2">
                    {g.items.map(([name, text]) => <Feature key={name} name={name} text={text} />)}
                  </div>
                </div>
              ))}
            </div>
          </section>
        </div>
      )}
    </div>
  )
}
