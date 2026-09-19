import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api'
import {
  blockedReason, duplicates, eligible, heldReason, isLive, nextRun, snapshotWarning,
  suggestionLabel,
} from './posting'

// Everything a screen needs to configure and start a posting run, minus the markup.
//
// Two screens do this — the desktop console and the phone page — and they differ only in
// how they lay it out. Sharing the state and the arithmetic means the phone cannot end up
// with a stale copy of the pricing guard, which is the one rule in this feature worth
// being obstructive about: undoing a price after readers have paid is the expensive
// mistake. The server checks the same thing again and remains the authority.
export function usePostingConsole() {
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

  const onlyLinked = (list) => (list || []).filter(
    (s) => (s.publish_targets || []).some((t) => t.series_url))

  useEffect(() => {
    api.listSeries()
      .then((d) => {
        const withTargets = onlyLinked(d.series)
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
  const live = isLive(run)
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
        const nowLive = isLive(d.run)
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
  const ready = useMemo(() => items.filter((i) => !i.blockers.length), [items])
  const free = ready.filter((i) => !i.paid)
  const paid = ready.filter((i) => i.paid)

  const willPost = useMemo(
    () => eligible(ready, { from, to, upTo, limit }),
    [ready, from, to, upTo, limit])

  const held = useMemo(() => {
    const groups = new Map()
    for (const i of items) {
      if (!i.blockers.length) continue
      const reason = heldReason(i.blockers[0])
      groups.set(reason, (groups.get(reason) || 0) + 1)
    }
    return [...groups.entries()].sort((a, b) => b[1] - a[1])
  }, [items])

  const site = plan?.site || null

  // What this run would pick up next, and whether offering it would change anything.
  const nextUp = useMemo(() => nextRun(ready), [ready])
  const suggestion = useMemo(() => suggestionLabel(nextUp, site), [nextUp, site])
  // Shown only when tapping it would change what posts — the same discipline as the coin
  // button, which disappears once the value matches.
  //
  // The second clause is the important one: when the unposted chapters are contiguous
  // with nothing apart, blank boxes ALREADY post exactly the suggestion plus the side
  // stories, so the button would be a no-op that quietly drops them.
  const untouched = !from && !to && !upTo && !limit
  const suggestionApplies = nextUp.from != null
    && (String(nextUp.from) !== from || String(nextUp.to) !== to)
    && !(nextUp.covers && untouched)

  const applyNextRun = useCallback(() => {
    setFrom(String(nextUp.from)); setTo(String(nextUp.to)); setUpTo(''); setLimit('')
  }, [nextUp])
  const clearRange = useCallback(() => { setFrom(''); setTo(''); setUpTo('') }, [])

  // A numeric bound cannot reach a side story, in eligible() or on the server. Said where
  // it happens rather than warned about in the abstract.
  const sideExcluded = useMemo(
    () => ((from || to || upTo) ? ready.filter((i) => i.global == null) : []),
    [ready, from, to, upTo])

  const siteWarning = useMemo(() => snapshotWarning(site), [site])
  const dupes = useMemo(() => duplicates(willPost, site), [willPost, site])
  const rangeLabel = from && to ? `${from}–${to}`
    : from ? `from ${from}` : to ? `to ${to}`
    : upTo ? `up to ${upTo}` : limit ? `at most ${limit}` : ''

  const savedFree = Number(target?.free_through ?? 0)
  const savedCoins = Number(target?.coin_price ?? 0)
  const savedSidePaid = target?.side_paid ?? true
  const priceDirty = Number(freeThrough || 0) !== savedFree
    || Number(coinPrice || 0) !== savedCoins
    || sidePaid !== savedSidePaid
  // What the site itself charges, read off its own chapter rows by the extension. Offered
  // as one click rather than written on automatically: it is the site's own number and so
  // almost certainly right, but a price is painful to undo, so it gets a person's nod.
  const suggestedCoins = plan?.site?.observed_coins ?? null

  const savePricing = useCallback(async (onSaved) => {
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
      setSeries(onlyLinked(d.series))
      onSaved?.()
      loadPlan()
    } catch (e) {
      setError(e)
    } finally {
      setSavingPrice(false)
    }
  }, [sid, target, freeThrough, coinPrice, sidePaid, loadPlan])

  const start = useCallback(async (onStarted) => {
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
      onStarted?.(started)
    } catch (e) {
      setError(e)
    } finally {
      setStarting(false)
    }
  }, [sid, targetId, from, to, upTo, limit, postState])

  const cancel = useCallback(async (onDone) => {
    try {
      const d = await api.cancelPostingRun(sid, targetId)
      setRun(d.run)
      onDone?.()
    } catch (e) { setError(e) }
  }, [sid, targetId])

  const resume = useCallback(async (onDone) => {
    try {
      const d = await api.resumePostingRun(sid, targetId)
      setRun(d.run)
      onDone?.()
    } catch (e) { setError(e) }
  }, [sid, targetId])

  return {
    series, sid, setSid, chosen, target, targetId,
    plan, loading, error, setError, loadPlan,
    run, poll, live,
    ready, free, paid, willPost, held,
    freeThrough, setFreeThrough, coinPrice, setCoinPrice, sidePaid, setSidePaid,
    savedFree, savedCoins, priceDirty, suggestedCoins, savingPrice, savePricing,
    from, setFrom, to, setTo, upTo, setUpTo, limit, setLimit,
    postState, setPostState, starting, start, cancel, resume,
    site, nextUp, suggestion, suggestionApplies, applyNextRun, clearRange,
    sideExcluded, siteWarning, dupes, rangeLabel,
    blocked: blockedReason({ willPost, savedCoins, savedFree, priceDirty, dupes }),
  }
}
