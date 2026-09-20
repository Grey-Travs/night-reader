import { useToast } from '../toast'
import {
  ago, formatRuns, pickupLine, priceLine, range, runHeadline, unnumberedLabel,
} from '../posting'
import { usePostingConsole } from '../usePostingConsole'

// Night Reader on a phone: one page, no app around it.
//
// This is deliberately NOT the desktop site at a narrow width. The sidebar, the library,
// the reader and the glossary are all things you do sitting down; the only thing worth
// doing from a phone is starting a posting run and seeing how one is going. So this route
// sits outside AppShell entirely — there is no nav to get lost in and nothing to tap by
// mistake.
//
// It is also what the home-screen icon opens (see the manifest endpoint in server/app.py),
// which is why it has to stand alone: launched from the icon there is no browser chrome,
// no back button and no address bar, so every way out of a mistake has to be on the page.
//
// The arithmetic is shared with the desktop console through usePostingConsole. That is the
// point: the pricing guard that stops a run posting at zero coins is the same code here as
// there, so the phone cannot be the lenient one.
export default function PhonePage() {
  const toast = useToast()
  const c = usePostingConsole()

  return (
    <div className="phone-shell">
      <header className="phone-bar">
        <span className="font-reading text-base font-medium">Night Reader</span>
        <button
          className="btn btn-ghost px-3 py-2 text-xs"
          onClick={c.loadPlan}
          aria-label="Refresh"
        >
          ↻
        </button>
      </header>

      <main className="phone-body">
        {c.error && (
          <div className="rounded-card px-3 py-2 text-sm pill-review">
            {c.error.message || String(c.error)}
          </div>
        )}

        {c.series == null ? (
          <p className="py-10 text-center text-sm text-hint">Loading…</p>
        ) : c.series.length === 0 ? (
          <p className="rounded-card border border-dashed border-line p-8 text-center text-sm text-muted">
            No novel has a publishing link yet. Link one on the computer first.
          </p>
        ) : (
          <>
            <label className="block">
              <span className="text-xs text-hint">Novel</span>
              <select
                className="input mt-1 w-full py-3 text-base"
                value={c.sid}
                onChange={(e) => c.setSid(e.target.value)}
              >
                {c.series.map((s) => (
                  <option key={s.id} value={s.id}>{s.name}</option>
                ))}
              </select>
            </label>

            {!c.plan ? (
              <p className="py-8 text-center text-sm text-hint">Reading the plan…</p>
            ) : (
              <>
                <RunCard
                  run={c.run}
                  poll={c.poll}
                  onCancel={() => c.cancel(() => toast('Run stopped'))}
                  onResume={() => c.resume(() => toast('Run queued again'))}
                />

                <section className="grid grid-cols-3 gap-2">
                  <Tile label="Ready" value={c.ready.length} sub={range(c.ready)} />
                  <Tile label="Free" value={c.free.length} sub={range(c.free)} />
                  <Tile
                    label={`${c.plan.coin_price} coins`}
                    value={c.paid.length}
                    sub={range(c.paid)}
                  />
                </section>

                <p className="text-xs text-hint">
                  {c.site?.fetched_at
                    ? `Read ${c.site.count} chapters off the site ${ago(c.site.fetched_at)}`
                      + `${c.site.trusted !== false && c.site.numbered > 0
                        ? ` — it has ${formatRuns(c.site.posted)}` : ''}.`
                    : 'These counts don’t know what’s on the site yet — open the novel on meiko.studio once on the computer.'}
                </p>

                {c.siteWarning && (
                  <p className="rounded-card px-2 py-1 text-xs pill-review">
                    {c.siteWarning}
                  </p>
                )}

                {c.audit.length > 0 && (
                  <p className="text-xs text-hint">
                    Already on the site, but not as set: {c.audit
                      .map(([problem, n]) => `${problem} (${n})`)
                      .join(' · ')}
                  </p>
                )}

                {c.site?.fetched_at && c.site.trusted !== false && (
                  <p className="text-xs text-muted">{pickupLine(c.nextUp)}</p>
                )}

                {c.held.length > 0 && (
                  <p className="text-xs text-hint">
                    Held back: {c.held.map(([reason, n]) => `${n} ${reason}`).join(' · ')}
                  </p>
                )}

                {!c.live && (
                  <>
                    <section className="card space-y-3 p-4">
                      <h2 className="text-sm font-medium">Price</h2>
                      <div className="flex gap-3">
                        <Num label="Free through" value={c.freeThrough} onChange={c.setFreeThrough} />
                        <Num label="Coins after" value={c.coinPrice} onChange={c.setCoinPrice} />
                      </div>
                      <label className="flex items-center gap-2 text-xs text-muted">
                        <input
                          type="checkbox"
                          checked={c.sidePaid}
                          onChange={(e) => c.setSidePaid(e.target.checked)}
                        />
                        Side stories cost coins too
                      </label>
                      {c.suggestedCoins != null && Number(c.coinPrice) !== c.suggestedCoins && (
                        <button
                          className="btn btn-ghost w-full py-2.5 text-xs"
                          onClick={() => c.setCoinPrice(String(c.suggestedCoins))}
                        >
                          the site charges {c.suggestedCoins} — use that
                        </button>
                      )}
                      <button
                        className="btn btn-primary w-full py-3 text-sm"
                        disabled={!c.priceDirty || c.savingPrice}
                        onClick={() => c.savePricing(() => toast('Pricing saved'))}
                      >
                        {c.savingPrice ? 'Saving…' : 'Save price'}
                      </button>
                      <p className="text-xs text-hint">
                        Chapter {Number(c.freeThrough) || 0} and below post free; after that
                        {' '}{Number(c.coinPrice) || 0} coins each.
                      </p>
                    </section>

                    {c.suggestionApplies && (
                      <button
                        className="btn btn-ghost w-full py-2.5 text-xs"
                        onClick={c.applyNextRun}
                      >
                        {c.suggestion}
                      </button>
                    )}

                    {/* Collapsed by default: these four are normally left empty, and an
                        empty form is the common case worth keeping off the screen. The
                        suggestion sits ABOVE it on purpose — inside, nobody would see it,
                        and the summary carries the applied range so a tap is visible
                        without opening the panel. */}
                    <details className="card p-4">
                      <summary className="cursor-pointer text-sm font-medium">
                        Range &amp; visibility
                        {c.rangeLabel && (
                          <span className="text-hint"> · {c.rangeLabel}</span>
                        )}
                      </summary>
                      <div className="mt-3 space-y-3">
                        <div className="flex gap-3">
                          <Num label="From" value={c.from} onChange={c.setFrom} placeholder="start" />
                          <Num label="To" value={c.to} onChange={c.setTo} placeholder="end" />
                        </div>
                        <div className="flex gap-3">
                          <Num label="Stop at" value={c.upTo} onChange={c.setUpTo} placeholder="—" />
                          <Num label="At most" value={c.limit} onChange={c.setLimit} placeholder="all" />
                        </div>
                        <label className="block">
                          <span className="text-xs text-muted">Publish as</span>
                          <select
                            className="input mt-1 w-full py-2.5 text-sm"
                            value={c.postState}
                            onChange={(e) => c.setPostState(e.target.value)}
                          >
                            <option value="public">Public</option>
                            <option value="private">Private</option>
                          </select>
                        </label>
                        <p className="text-xs text-hint">
                          Leave these empty to post everything that is ready.
                        </p>
                      </div>
                    </details>
                  </>
                )}
              </>
            )}
          </>
        )}
      </main>

      {/* The action stays reachable with a thumb however far the page has scrolled. Only
          while there is no run — during one, the card above is the thing to look at. */}
      {c.plan && !c.live && (
        <div className="phone-foot">
          <div className="text-xs">
            {c.willPost.length === 0 ? (
              <span className="text-hint">Nothing to post.</span>
            ) : (
              <>
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
          </div>
          {c.blocked && (
            <div className="mt-1 rounded-card px-2 py-1 text-xs pill-review">{c.blocked}</div>
          )}
          {c.sideExcluded.length > 0 && (
            <div className="mt-1 text-xs text-hint">
              {unnumberedLabel(c.sideExcluded)} not in this run — a chapter range can’t
              reach them.{' '}
              <button className="underline" onClick={c.clearRange}>
                post everything ready instead
              </button>
            </div>
          )}
          <button
            className="btn btn-primary mt-2 w-full py-3.5 text-base"
            disabled={!!c.blocked || c.starting}
            onClick={() => c.start((r) => toast(`Queued ${r.progress.total} chapters`))}
          >
            {c.starting ? 'Starting…' : 'Start posting'}
          </button>
        </div>
      )}
    </div>
  )
}

function Tile({ label, value, sub }) {
  return (
    <div className="rounded-card border border-line p-2 text-center">
      <div className="text-xs text-hint">{label}</div>
      <div className="font-reading text-xl">{value}</div>
      <div className="mt-0.5 truncate text-[11px] text-muted" title={sub}>{sub}</div>
    </div>
  )
}

function Num({ label, value, onChange, placeholder }) {
  return (
    <label className="min-w-0 flex-1">
      <span className="text-xs text-muted">{label}</span>
      <input
        // inputMode numeric brings up the number pad rather than the full keyboard, which
        // on a phone is the difference between one tap and four.
        type="number"
        inputMode="numeric"
        min="1"
        placeholder={placeholder}
        className="input mt-1 w-full py-2.5 text-base"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  )
}

// A live run is the reason to have opened this at all, so it gets the top of the page and
// the biggest type on it.
function RunCard({ run, poll, onCancel, onResume }) {
  if (!run) return null
  const posted = run.progress?.posted || []
  const failed = run.progress?.failed || []
  const total = run.progress?.total || 0
  const stalled = run.stalled
  const live = run.state === 'queued' || (run.state === 'running' && !stalled)

  return (
    <section className="card space-y-2 p-4">
      <div className="font-reading text-lg">{runHeadline(run)}</div>
      {run.progress?.current && (
        <div className="text-sm text-muted">{run.progress.current}</div>
      )}

      {run.state === 'running' && !stalled && total > 0 && (
        <div className="h-2 overflow-hidden rounded-full" style={{ background: 'var(--border)' }}>
          <div
            className="h-full rounded-full"
            style={{
              width: `${Math.round((posted.length / total) * 100)}%`,
              background: 'var(--accent)',
            }}
          />
        </div>
      )}

      {run.state === 'queued' && (
        <p className="text-xs text-hint">
          Waiting for the computer. Open {run.series_name} on meiko.studio in Chrome there —
          it checks for work every {poll.poll_seconds} seconds and starts on its own.
        </p>
      )}
      {stalled && (
        <p className="text-xs text-hint">
          Nothing heard from the browser for over {Math.round(poll.stale_after / 60)} minute
          {poll.stale_after >= 120 ? 's' : ''} — the tab was probably closed. Resume puts it
          back in the queue; anything already posted stays posted.
        </p>
      )}
      {failed.length > 0 && (
        <p className="rounded-card px-2 py-1 text-xs pill-review">
          {failed[failed.length - 1].title}: {failed[failed.length - 1].error}
        </p>
      )}

      <div className="flex gap-2">
        {live && (
          <button className="btn btn-ghost flex-1 py-3 text-sm" onClick={onCancel}>
            Cancel
          </button>
        )}
        {!live && run.state !== 'done' && (
          <button className="btn btn-ghost flex-1 py-3 text-sm" onClick={onResume}>
            Resume
          </button>
        )}
      </div>
    </section>
  )
}
