import { Link } from 'react-router-dom'
import { SkeletonRows } from '../components/ui'
import { useToast } from '../toast'
import {
  NUMBERING, ago, clock, formatRuns, pickupLine, priceLine, range, runHeadline,
  unnumberedLabel,
} from '../posting'
import { usePostingConsole } from '../usePostingConsole'

// Where a posting run is set up and started.
//
// The extension is what actually posts — it has to be, since it runs inside the logged-in
// browser — but it cannot decide anything: which chapters, what they are called, what they
// cost and what is held back are all worked out in Python. So this page is the control
// panel, and pressing Start leaves a request in a file that an open meiko tab picks up on
// its next poll. That indirection is also what lets a phone start a run: the request waits
// on disk rather than in the tab that asked for it.
//
// It used to be a read-only preview whose largest element was a per-chapter table of
// everything the series contained — which, on a series that has been posted by hand for a
// year, is a hundred rows that will never post again. The counts and one line saying what
// this run will do are what is actually worth reading.
//
// The state and the arithmetic live in usePostingConsole, shared with the phone page. Only
// the layout is here.
export default function PostingPage() {
  const toast = useToast()
  const c = usePostingConsole()

  return (
    <div className="page">
      <div className="mb-5">
        <h1 className="font-reading text-2xl font-medium">Posting</h1>
        <p className="text-sm text-hint">
          Pick a series, set what it costs, give it a range, and start. The browser
          extension does the posting from whichever meiko.studio tab is open.
        </p>
      </div>

      {c.error && (
        <div className="mb-4 rounded-card px-3 py-2 text-sm pill-review">
          {c.error.message || String(c.error)}
        </div>
      )}

      {c.series == null ? <SkeletonRows rows={4} /> : c.series.length === 0 ? (
        <div className="rounded-card border border-dashed border-line p-10 text-center">
          <p className="text-sm text-muted">
            No series has a publishing link yet. Add one from the{' '}
            <Link to="/series" className="text-accent-text hover:underline">Series</Link> page.
          </p>
        </div>
      ) : (
        <>
          <div className="mb-4 flex flex-wrap items-center gap-2">
            <select
              className="rounded border border-line bg-transparent px-2 py-1.5 text-sm"
              value={c.sid}
              onChange={(e) => c.setSid(e.target.value)}
            >
              {c.series.map((s) => (
                <option key={s.id} value={s.id}>{s.name}</option>
              ))}
            </select>
            <button className="btn btn-ghost px-3 py-1.5 text-xs" onClick={c.loadPlan}>
              Refresh
            </button>
            {c.chosen && (
              <Link to={`/series/${c.chosen.id}`} className="text-xs text-hint hover:underline">
                numbering &amp; glossary →
              </Link>
            )}
          </div>

          {c.loading && !c.plan ? <SkeletonRows rows={5} /> : !c.plan ? null : (
            <>
              <section className="grid gap-3 sm:grid-cols-3">
                <Tile label="Ready to post" value={c.ready.length} sub={range(c.ready)} />
                <Tile label="Free" value={c.free.length} sub={range(c.free)} />
                <Tile
                  label={`Paid · ${c.plan.coin_price} coins`}
                  value={c.paid.length}
                  sub={range(c.paid)}
                />
              </section>

              <p className="mt-2 text-xs text-hint">
                {c.site?.fetched_at ? (
                  <>
                    Read {c.site.count} chapters off the site {ago(c.site.fetched_at)}
                    {/* Suppressed when the reading is not believable: a snapshot we do
                        not trust has no business making a claim about what is up there. */}
                    {c.site.trusted !== false && c.site.numbered > 0
                      && <> — it has {formatRuns(c.site.posted)}</>}.
                  </>
                ) : (
                  <>
                    These counts don’t know what’s on the site yet — open the series on
                    meiko.studio once and the extension syncs its chapter list, and reads
                    the coin price off it too.
                  </>
                )}
              </p>

              {c.siteWarning && (
                <p className="mt-1 inline-block rounded-card px-2 py-1 text-xs pill-review">
                  {c.siteWarning}
                </p>
              )}

              {/* A report about the SITE, like the held line below -- not a warning about
                  the reading, which is what the pill above is for. The count goes in
                  brackets because each problem is already a third-person predicate
                  ("costs 5 coins on the site, not 55"), so "58 costs ..." would not read. */}
              {c.audit.length > 0 && (
                <p className="mt-1 text-xs text-hint">
                  Already on the site, but not as set:{' '}
                  {c.audit.map(([problem, n], k) => (
                    <span key={problem}>
                      {k > 0 && ' · '}
                      {problem} ({n})
                    </span>
                  ))}
                </p>
              )}

              {c.site?.fetched_at && c.site.trusted !== false && (
                <p className="mt-1 text-xs text-muted">{pickupLine(c.nextUp)}</p>
              )}

              {c.held.length > 0 && (
                <p className="mt-1 text-xs text-hint">
                  Held back:{' '}
                  {c.held.map(([reason, n], k) => (
                    <span key={reason}>
                      {k > 0 && ' · '}
                      {reason === NUMBERING ? (
                        <Link to={`/series/${c.sid}`} className="hover:underline">
                          {n} {reason} →
                        </Link>
                      ) : `${n} ${reason}`}
                    </span>
                  ))}
                </p>
              )}

              <RunPanel
                run={c.run}
                poll={c.poll}
                onCancel={() => c.cancel(() => toast('Run stopped'))}
                onResume={() => c.resume(() => toast('Run queued again'))}
              />

              {!c.live && (
                <>
                  <section className="mt-5 rounded-card border border-line p-3">
                    <h2 className="text-sm font-medium">Pricing</h2>
                    <div className="mt-2 flex flex-wrap items-end gap-4">
                      <Num label="Free through chapter" value={c.freeThrough} onChange={c.setFreeThrough} />
                      <Num label="Coins after that" value={c.coinPrice} onChange={c.setCoinPrice} />
                      <button
                        className="btn btn-primary px-3 py-1.5 text-xs"
                        disabled={!c.priceDirty || c.savingPrice}
                        onClick={() => c.savePricing(() => toast('Pricing saved'))}
                      >
                        {c.savingPrice ? 'Saving…' : 'Save'}
                      </button>
                      {c.suggestedCoins != null && Number(c.coinPrice) !== c.suggestedCoins && (
                        <button
                          className="btn btn-ghost px-3 py-1.5 text-xs"
                          onClick={() => c.setCoinPrice(String(c.suggestedCoins))}
                        >
                          the site charges {c.suggestedCoins} — use that
                        </button>
                      )}
                    </div>
                    <label className="mt-3 flex items-center gap-2 text-xs text-muted">
                      <input
                        type="checkbox"
                        checked={c.sidePaid}
                        onChange={(e) => c.setSidePaid(e.target.checked)}
                      />
                      Side stories cost coins too
                    </label>
                    <p className="mt-2 text-xs text-hint">
                      Chapter {Number(c.freeThrough) || 0} and below post free; everything
                      after costs {Number(c.coinPrice) || 0} coins. Both are readable off the
                      site’s own chapter list. For a series that is free all the way, set
                      the cutoff past its last chapter rather than leaving these at zero.
                      Side stories have no chapter number for the cutoff to compare, so
                      they follow that box instead.
                    </p>
                  </section>

                  <section className="mt-3 rounded-card border border-line p-3">
                    <h2 className="text-sm font-medium">This run</h2>
                    <div className="mt-2 flex flex-wrap items-end gap-4">
                      <Num label="From chapter" value={c.from} onChange={c.setFrom} placeholder="start" />
                      <Num label="To chapter" value={c.to} onChange={c.setTo} placeholder="end" />
                      <Num label="Stop at chapter" value={c.upTo} onChange={c.setUpTo} placeholder="—" />
                      <Num label="At most" value={c.limit} onChange={c.setLimit} placeholder="all" />
                      {c.suggestionApplies && (
                        <button
                          className="btn btn-ghost px-3 py-1.5 text-xs"
                          onClick={c.applyNextRun}
                        >
                          {c.suggestion}
                        </button>
                      )}
                      <label className="text-xs text-muted">
                        Publish as
                        <select
                          className="mt-1 block rounded border border-line bg-transparent px-2 py-1 text-xs"
                          value={c.postState}
                          onChange={(e) => c.setPostState(e.target.value)}
                        >
                          <option value="public">Public</option>
                          <option value="private">Private</option>
                        </select>
                      </label>
                    </div>
                    <p className="mt-2 text-xs text-hint">
                      Leave these empty to post everything that is ready.
                    </p>
                  </section>

                  <section className="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-card border border-line p-3">
                    <div className="text-sm">
                      {c.willPost.length === 0 ? (
                        <span className="text-hint">Nothing to post.</span>
                      ) : (
                        <>
                          <span className="text-hint">will post </span>
                          <span className="font-medium">
                            {c.willPost[0].title}
                            {c.willPost.length > 1 && ` → ${c.willPost[c.willPost.length - 1].title}`}
                          </span>
                          <span className="text-hint">
                            {' · '}{c.willPost.length} chapter{c.willPost.length === 1 ? '' : 's'}
                            {' · '}{priceLine(c.willPost, c.savedCoins)}
                            {c.postState === 'private' && ' · private'}
                          </span>
                        </>
                      )}
                      {c.blocked && (
                        <div className="mt-1 text-xs pill-review inline-block rounded-card px-2 py-0.5">
                          {c.blocked}
                        </div>
                      )}
                      {c.sideExcluded.length > 0 && (
                        <div className="mt-1 text-xs text-hint">
                          {unnumberedLabel(c.sideExcluded)} not in this run — a chapter
                          range can’t reach them.{' '}
                          <button className="underline" onClick={c.clearRange}>
                            post everything ready instead
                          </button>
                        </div>
                      )}
                    </div>
                    <button
                      className="btn btn-primary px-4 py-2 text-sm"
                      disabled={!!c.blocked || c.starting}
                      onClick={() => c.start((r) => toast(`Queued ${r.progress.total} chapters`))}
                    >
                      {c.starting ? 'Starting…' : 'Start posting'}
                    </button>
                  </section>
                </>
              )}
            </>
          )}
        </>
      )}
    </div>
  )
}

function Tile({ label, value, sub }) {
  return (
    <div className="rounded-card border border-line p-3">
      <div className="text-xs text-hint">{label}</div>
      <div className="font-reading text-2xl">{value}</div>
      <div className="mt-1 text-xs text-muted">{sub}</div>
    </div>
  )
}

function Num({ label, value, onChange, placeholder }) {
  return (
    <label className="text-xs text-muted">
      {label}
      <input
        type="number"
        min="1"
        placeholder={placeholder}
        className="mt-1 block w-32 rounded border border-line bg-transparent px-2 py-1 text-xs"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  )
}

// What a run is doing, without having to switch to the meiko tab. The extension panel
// keeps its own detailed log — that is the right place to be when something breaks — but
// knowing how far along a run is should not cost a tab switch.
function RunPanel({ run, poll, onCancel, onResume }) {
  if (!run) return null
  const posted = run.progress?.posted || []
  const failed = run.progress?.failed || []
  const total = run.progress?.total || 0
  const last = posted[posted.length - 1]
  const stalled = run.stalled
  const live = run.state === 'queued' || (run.state === 'running' && !stalled)

  return (
    <section className="mt-5 rounded-card border border-line p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-sm font-medium">
          {runHeadline(run)}
          {run.progress?.current && (
            <span className="text-hint"> · {run.progress.current}</span>
          )}
        </div>
        <div className="flex gap-2">
          {!live && run.state !== 'done' && (
            <button className="btn btn-ghost px-3 py-1.5 text-xs" onClick={onResume}>
              Resume
            </button>
          )}
          {live && (
            <button className="btn btn-ghost px-3 py-1.5 text-xs" onClick={onCancel}>
              Cancel
            </button>
          )}
        </div>
      </div>

      {run.state === 'queued' && (
        <p className="mt-1 text-xs text-hint">
          Open {run.series_name} on meiko.studio in a tab — the extension checks for work
          every {poll.poll_seconds} seconds and will start on its own.
        </p>
      )}
      {stalled && (
        <p className="mt-1 text-xs text-hint">
          Nothing heard from the browser for over {Math.round(poll.stale_after / 60)} minute
          {poll.stale_after >= 120 ? 's' : ''} — the tab was probably closed. Resume puts it
          back in the queue; anything already posted stays posted.
        </p>
      )}
      {last && (
        <p className="mt-1 text-xs text-muted">
          last posted {last.title}
          {last.coins ? ` · ${last.coins} coins` : ' · free'}
          {last.at ? ` · ${clock(last.at)}` : ''}
        </p>
      )}
      {failed.length > 0 && (
        <p className="mt-1 text-xs pill-review inline-block rounded-card px-2 py-0.5">
          {failed[failed.length - 1].title}: {failed[failed.length - 1].error}
        </p>
      )}
      {/* --border, not --line: the latter is a Tailwind theme name (--color-line) and is
          not a real custom property, so the track was silently transparent. */}
      {run.state === 'running' && !stalled && total > 0 && (
        <div className="mt-2 h-1.5 overflow-hidden rounded-full" style={{ background: 'var(--border)' }}>
          <div
            className="h-full rounded-full"
            style={{
              width: `${Math.round((posted.length / total) * 100)}%`,
              background: 'var(--accent)',
            }}
          />
        </div>
      )}
    </section>
  )
}
