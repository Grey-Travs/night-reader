import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { SkeletonRows } from '../components/ui'
import { useToast } from '../toast'

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
export default function PostingPage() {
  const toast = useToast()
  const [series, setSeries] = useState(null)
  const [sid, setSid] = useState('')
  const [plan, setPlan] = useState(null)
  const [run, setRun] = useState(null)
  const [poll, setPoll] = useState({ stale_after: 90, poll_seconds: 30 })
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  // Pricing lives on the series, but it is edited here because it is what changes between
  // runs — the site's coin price is not a fact about the translation.
  const [freeThrough, setFreeThrough] = useState('0')
  const [coinPrice, setCoinPrice] = useState('0')
  // A side story has no chapter number, so the cutoff cannot classify it. Paid by default,
  // because the site's own lists show side content as the earliest paid entry and it is
  // the newest thing on those series.
  const [sidePaid, setSidePaid] = useState(true)
  const [savingPrice, setSavingPrice] = useState(false)

  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [upTo, setUpTo] = useState('')
  const [limit, setLimit] = useState('')
  const [postState, setPostState] = useState('public')
  const [starting, setStarting] = useState(false)

  const chosen = (series || []).find((s) => s.id === sid) || null
  const target = (chosen?.publish_targets || [])[0] || null
  const targetId = target?.id || 'default'

  useEffect(() => {
    api.listSeries()
      .then((d) => {
        const withTargets = (d.series || []).filter(
          (s) => (s.publish_targets || []).some((t) => t.series_url),
        )
        setSeries(withTargets)
        if (withTargets.length) setSid(withTargets[0].id)
      })
      .catch(setError)
  }, [])

  // The pricing form follows whatever series is showing, so switching novels never leaves
  // the previous one's coin price sitting in the box.
  useEffect(() => {
    setFreeThrough(String(target?.free_through ?? 0))
    setCoinPrice(String(target?.coin_price ?? 0))
    setSidePaid(target?.side_paid ?? true)
    setFrom(''); setTo(''); setUpTo(''); setLimit(''); setPostState('public')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sid])

  const loadPlan = useCallback(async () => {
    if (!sid) return
    setLoading(true)
    setError(null)
    try {
      setPlan(await api.postingPlan(sid))
    } catch (e) {
      setError(e)
      setPlan(null)
    } finally {
      setLoading(false)
    }
  }, [sid])

  useEffect(() => { loadPlan() }, [loadPlan])

  // One small JSON read, so this is cheap enough to keep running: 2 seconds while a run is
  // live, and a slow tick otherwise so a run started from another device still shows up.
  const live = run?.state === 'queued' || (run?.state === 'running' && !run?.stalled)
  const wasLive = useRef(false)
  useEffect(() => {
    if (!sid) return
    let alive = true
    let timer = null
    const tick = async () => {
      try {
        const d = await api.postingRun(sid, targetId)
        if (!alive) return
        setRun(d.run)
        setPoll({ stale_after: d.stale_after, poll_seconds: d.poll_seconds })
        // A run that has just finished changed what is already on the site, so the counts
        // above are out of date until the plan is read again.
        const nowLive = d.run?.state === 'queued'
          || (d.run?.state === 'running' && !d.run?.stalled)
        if (wasLive.current && !nowLive) loadPlan()
        wasLive.current = nowLive
      } catch { /* a status poll is not worth a banner */ }
      if (alive) timer = setTimeout(tick, live ? 2000 : 10000)
    }
    tick()
    return () => { alive = false; if (timer) clearTimeout(timer) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sid, targetId, live, loadPlan])

  const items = plan?.items || []
  const inRange = useCallback((i) => {
    // Mirrors the server: a numeric range never sweeps up a side story, which has no
    // global number of its own.
    if (from && (i.global == null || i.global < Number(from))) return false
    if (to && (i.global == null || i.global > Number(to))) return false
    return true
  }, [from, to])

  const ready = useMemo(() => items.filter((i) => !i.blockers.length), [items])
  const free = ready.filter((i) => !i.paid)
  const paid = ready.filter((i) => i.paid)

  // What this run would post. The server re-derives this and is the authority — see
  // posting.eligible_items — so keep the three rules here in step with it.
  const willPost = useMemo(() => {
    let rows = ready.filter(inRange)
    const cap = Number(upTo) || 0
    if (cap) rows = rows.filter((i) => i.global != null && i.global <= cap)
    const most = Number(limit) || 0
    if (most) rows = rows.slice(0, most)
    return rows
  }, [ready, inRange, upTo, limit])

  const held = useMemo(() => {
    const groups = new Map()
    for (const i of items) {
      if (!i.blockers.length) continue
      const reason = heldReason(i.blockers[0])
      groups.set(reason, (groups.get(reason) || 0) + 1)
    }
    return [...groups.entries()].sort((a, b) => b[1] - a[1])
  }, [items])

  const savedFree = Number(target?.free_through ?? 0)
  const savedCoins = Number(target?.coin_price ?? 0)
  const savedSidePaid = target?.side_paid ?? true
  const priceDirty = Number(freeThrough || 0) !== savedFree
    || Number(coinPrice || 0) !== savedCoins
    || sidePaid !== savedSidePaid
  // Asked of THIS run, not of the series, which is the only way to get it right: a series
  // with a free cutoff but no coin price would otherwise sail through and post everything
  // past the cutoff as paid at zero coins. The server checks the same thing the same way.
  const paidInRun = willPost.filter((i) => i.paid).length
  const needsPrice = savedCoins === 0 && paidInRun > 0
  // What the site itself charges, read off its own chapter rows by the extension. Offered
  // as one click rather than written on automatically: it is the site's own number and so
  // almost certainly right, but a price is painful to undo, so it gets a person's nod.
  const suggestedCoins = plan?.site?.observed_coins ?? null

  async function savePricing() {
    setSavingPrice(true)
    try {
      // publish_targets is a whole-list replace on the server, and a target can carry keys
      // this page knows nothing about — so spread the existing one rather than rebuilding
      // it from the fields shown here.
      await api.updateSeries(sid, {
        publish_targets: [{
          ...(target || { id: 't1', site: 'meiko' }),
          free_through: Number(freeThrough) || 0,
          coin_price: Number(coinPrice) || 0,
          side_paid: sidePaid,
        }],
      })
      const d = await api.listSeries()
      setSeries((d.series || []).filter(
        (s) => (s.publish_targets || []).some((t) => t.series_url)))
      toast('Pricing saved')
      loadPlan()
    } catch (e) {
      setError(e)
    } finally {
      setSavingPrice(false)
    }
  }

  async function start() {
    setStarting(true)
    setError(null)
    try {
      const started = await api.startPostingRun(sid, targetId, {
        start: Number(from) || null,
        end: Number(to) || null,
        up_to: Number(upTo) || null,
        limit: Number(limit) || null,
        post_state: postState,
      })
      setRun(started)
      wasLive.current = true
      toast(`Queued ${started.progress.total} chapters`)
    } catch (e) {
      setError(e)
    } finally {
      setStarting(false)
    }
  }

  async function act(fn, label) {
    try {
      const d = await fn()
      setRun(d.run)
      toast(label)
    } catch (e) { setError(e) }
  }

  const blockedReason = needsPrice
    ? `${paidInRun} of these are past the free cutoff (chapter ${savedFree}), so they would post as paid at 0 coins — a locked chapter readers cannot buy. Set the coins first.`
    : priceDirty ? 'Save the pricing first, so the run charges what you just typed.'
    : !willPost.length ? 'Nothing in that range is ready to post.'
    : null

  return (
    <div className="page">
      <div className="mb-5">
        <h1 className="font-reading text-2xl font-medium">Posting</h1>
        <p className="text-sm text-hint">
          Pick a series, set what it costs, give it a range, and start. The browser
          extension does the posting from whichever meiko.studio tab is open.
        </p>
      </div>

      {error && (
        <div className="mb-4 rounded-card px-3 py-2 text-sm pill-review">
          {error.message || String(error)}
        </div>
      )}

      {series == null ? <SkeletonRows rows={4} /> : series.length === 0 ? (
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
              value={sid}
              onChange={(e) => setSid(e.target.value)}
            >
              {series.map((s) => (
                <option key={s.id} value={s.id}>{s.name}</option>
              ))}
            </select>
            <button className="btn btn-ghost px-3 py-1.5 text-xs" onClick={loadPlan}>
              Refresh
            </button>
            {chosen && (
              <Link to={`/series/${chosen.id}`} className="text-xs text-hint hover:underline">
                numbering &amp; glossary →
              </Link>
            )}
          </div>

          {loading && !plan ? <SkeletonRows rows={5} /> : !plan ? null : (
            <>
              <section className="grid gap-3 sm:grid-cols-3">
                <Tile label="Ready to post" value={ready.length} sub={range(ready)} />
                <Tile label="Free" value={free.length} sub={range(free)} />
                <Tile
                  label={`Paid · ${plan.coin_price} coins`}
                  value={paid.length}
                  sub={range(paid)}
                />
              </section>

              <p className="mt-2 text-xs text-hint">
                {plan.site?.fetched_at ? (
                  <>Read {plan.site.count} chapters off the site {ago(plan.site.fetched_at)}.</>
                ) : (
                  <>
                    These counts don’t know what’s on the site yet — open the series on
                    meiko.studio once and the extension syncs its chapter list, and reads
                    the coin price off it too.
                  </>
                )}
              </p>

              {held.length > 0 && (
                <p className="mt-1 text-xs text-hint">
                  Held back:{' '}
                  {held.map(([reason, n], k) => (
                    <span key={reason}>
                      {k > 0 && ' · '}
                      {reason === NUMBERING ? (
                        <Link to={`/series/${sid}`} className="hover:underline">
                          {n} {reason} →
                        </Link>
                      ) : `${n} ${reason}`}
                    </span>
                  ))}
                </p>
              )}

              <RunPanel
                run={run}
                poll={poll}
                onCancel={() => act(() => api.cancelPostingRun(sid, targetId), 'Run stopped')}
                onResume={() => act(() => api.resumePostingRun(sid, targetId), 'Run queued again')}
              />

              {!live && (
                <>
                  <section className="mt-5 rounded-card border border-line p-3">
                    <h2 className="text-sm font-medium">Pricing</h2>
                    <div className="mt-2 flex flex-wrap items-end gap-4">
                      <Num label="Free through chapter" value={freeThrough} onChange={setFreeThrough} />
                      <Num label="Coins after that" value={coinPrice} onChange={setCoinPrice} />
                      <button
                        className="btn btn-primary px-3 py-1.5 text-xs"
                        disabled={!priceDirty || savingPrice}
                        onClick={savePricing}
                      >
                        {savingPrice ? 'Saving…' : 'Save'}
                      </button>
                      {suggestedCoins != null && Number(coinPrice) !== suggestedCoins && (
                        <button
                          className="btn btn-ghost px-3 py-1.5 text-xs"
                          onClick={() => setCoinPrice(String(suggestedCoins))}
                        >
                          the site charges {suggestedCoins} — use that
                        </button>
                      )}
                    </div>
                    <label className="mt-3 flex items-center gap-2 text-xs text-muted">
                      <input
                        type="checkbox"
                        checked={sidePaid}
                        onChange={(e) => setSidePaid(e.target.checked)}
                      />
                      Side stories cost coins too
                    </label>
                    <p className="mt-2 text-xs text-hint">
                      Chapter {Number(freeThrough) || 0} and below post free; everything
                      after costs {Number(coinPrice) || 0} coins. Both are readable off the
                      site’s own chapter list. For a series that is free all the way, set
                      the cutoff past its last chapter rather than leaving these at zero.
                      Side stories have no chapter number for the cutoff to compare, so
                      they follow that box instead.
                    </p>
                  </section>

                  <section className="mt-3 rounded-card border border-line p-3">
                    <h2 className="text-sm font-medium">This run</h2>
                    <div className="mt-2 flex flex-wrap items-end gap-4">
                      <Num label="From chapter" value={from} onChange={setFrom} placeholder="start" />
                      <Num label="To chapter" value={to} onChange={setTo} placeholder="end" />
                      <Num label="Stop at chapter" value={upTo} onChange={setUpTo} placeholder="—" />
                      <Num label="At most" value={limit} onChange={setLimit} placeholder="all" />
                      <label className="text-xs text-muted">
                        Publish as
                        <select
                          className="mt-1 block rounded border border-line bg-transparent px-2 py-1 text-xs"
                          value={postState}
                          onChange={(e) => setPostState(e.target.value)}
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
                      {willPost.length === 0 ? (
                        <span className="text-hint">Nothing to post.</span>
                      ) : (
                        <>
                          <span className="text-hint">will post </span>
                          <span className="font-medium">
                            {willPost[0].title}
                            {willPost.length > 1 && ` → ${willPost[willPost.length - 1].title}`}
                          </span>
                          <span className="text-hint">
                            {' · '}{willPost.length} chapter{willPost.length === 1 ? '' : 's'}
                            {' · '}{priceLine(willPost, savedCoins)}
                            {postState === 'private' && ' · private'}
                          </span>
                        </>
                      )}
                      {blockedReason && (
                        <div className="mt-1 text-xs pill-review inline-block rounded-card px-2 py-0.5">
                          {blockedReason}
                        </div>
                      )}
                    </div>
                    <button
                      className="btn btn-primary px-4 py-2 text-sm"
                      disabled={!!blockedReason || starting}
                      onClick={start}
                    >
                      {starting ? 'Starting…' : 'Start posting'}
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

const NUMBERING = 'waiting on a chapter number'

// Blockers are written as sentences for a person reading one chapter. Grouped into a count
// they need a short label instead, and the numbering one is the only kind that is one
// click from being releasable — so it is the only one worth linking.
function heldReason(blocker) {
  const b = blocker || ''
  if (b.includes('not confirmed')) return NUMBERING
  if (b.startsWith('already on the site')) return 'already on the site'
  if (b.startsWith('already in the ledger')) return 'already posted'
  if (b.startsWith('not finished')) return 'not finished'
  if (b.startsWith('no translation')) return 'not translated yet'
  if (b.startsWith('too long')) return 'too long for the editor'
  return b
}

function priceLine(rows, coins) {
  const paid = rows.filter((i) => i.paid).length
  if (!paid) return 'all free'
  if (paid === rows.length) return `${coins} coins each`
  return `${rows.length - paid} free, ${paid} at ${coins} coins`
}

// Contiguous runs read far better than a list of forty numbers.
function range(rows) {
  const ns = rows.map((i) => i.global).filter((n) => n != null).sort((a, b) => a - b)
  if (!ns.length) return '—'
  const runs = []
  for (const n of ns) {
    const last = runs[runs.length - 1]
    if (last && n === last[1] + 1) last[1] = n
    else runs.push([n, n])
  }
  return runs.map(([a, b]) => (a === b ? `${a}` : `${a}–${b}`)).join(', ')
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

function clock(iso) {
  try {
    return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  } catch { return '' }
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

  const headline =
    run.state === 'queued' ? 'Waiting for the browser'
    : stalled ? 'Stalled'
    : run.state === 'running' ? `Running · ${posted.length} of ${total}`
    : run.state === 'done' ? `Finished · ${posted.length} posted`
    : run.state === 'failed' ? 'Stopped on an error'
    : `Cancelled · ${posted.length} posted`

  return (
    <section className="mt-5 rounded-card border border-line p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-sm font-medium">
          {headline}
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
      {run.state === 'running' && !stalled && total > 0 && (
        <div className="mt-2 h-1.5 overflow-hidden rounded-full" style={{ background: 'var(--line)' }}>
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
