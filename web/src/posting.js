// The rules a posting run is judged by, in one place.
//
// There are two screens that start a run now — the desktop console and the phone page —
// and the expensive mistake in this whole feature is starting a mispriced one. If the two
// screens computed "is this safe" separately they would eventually disagree, and the way
// you would find out is a hundred chapters posted as paid at zero coins. So both import
// these, and the server re-derives the same answer in posting.eligible_items and remains
// the authority.

export const NUMBERING = 'waiting on a chapter number'

// Blockers are written as sentences for a person reading one chapter. Grouped into a count
// they need a short label instead, and the numbering one is the only kind that is one
// click from being releasable — so it is the only one worth linking.
export function heldReason(blocker) {
  const b = blocker || ''
  if (b.includes('not confirmed')) return NUMBERING
  if (b.startsWith('already on the site')) return 'already on the site'
  if (b.startsWith('already in the ledger')) return 'already posted'
  if (b.startsWith('not finished')) return 'not finished'
  if (b.startsWith('no translation')) return 'not translated yet'
  if (b.startsWith('too long')) return 'too long for the editor'
  return b
}

export function priceLine(rows, coins) {
  const paid = rows.filter((i) => i.paid).length
  if (!paid) return 'all free'
  if (paid === rows.length) return `${coins} coins each`
  return `${rows.length - paid} free, ${paid} at ${coins} coins`
}

// Contiguous runs read far better than a list of forty numbers.
export function range(rows) {
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

export function ago(iso) {
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000
  if (!Number.isFinite(seconds)) return 'recently'
  if (seconds < 90) return 'just now'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  return hours < 36 ? `${hours}h ago` : `${Math.round(hours / 24)}d ago`
}

export function clock(iso) {
  try {
    return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  } catch { return '' }
}

// A run nobody has to act on yet: still going, or still waiting to be picked up. A stalled
// run is deliberately NOT live — it is the one state that wants a person.
export function isLive(run) {
  return run?.state === 'queued' || (run?.state === 'running' && !run?.stalled)
}

export function runHeadline(run) {
  const posted = run?.progress?.posted || []
  const total = run?.progress?.total || 0
  if (run?.state === 'queued') return 'Waiting for the browser'
  if (run?.stalled) return 'Stalled'
  if (run?.state === 'running') return `Running · ${posted.length} of ${total}`
  if (run?.state === 'done') return `Finished · ${posted.length} posted`
  if (run?.state === 'failed') return 'Stopped on an error'
  return `Cancelled · ${posted.length} posted`
}

// Which of the ready chapters this run would actually post, applying the four controls in
// the same order the server does. A numeric range never sweeps up a side story, because a
// side story has no global number to compare.
export function eligible(ready, { from, to, upTo, limit }) {
  let rows = ready.filter((i) => {
    if (from && (i.global == null || i.global < Number(from))) return false
    if (to && (i.global == null || i.global > Number(to))) return false
    return true
  })
  const cap = Number(upTo) || 0
  if (cap) rows = rows.filter((i) => i.global != null && i.global <= cap)
  const most = Number(limit) || 0
  if (most) rows = rows.slice(0, most)
  return rows
}

// Why this run must not start, or null. The pricing question is asked of THIS RUN rather
// than of the series, which is the only way to get it right: a series with a free cutoff
// but no coin price would otherwise sail through and post everything past the cutoff as
// paid at zero coins — a locked chapter readers cannot buy, with the revenue simply gone.
export function blockedReason({ willPost, savedCoins, savedFree, priceDirty }) {
  const paidInRun = willPost.filter((i) => i.paid).length
  if (savedCoins === 0 && paidInRun > 0) {
    return `${paidInRun} of these are past the free cutoff (chapter ${savedFree}), so they `
      + 'would post as paid at 0 coins — a locked chapter readers cannot buy. Set the '
      + 'coins first.'
  }
  if (priceDirty) return 'Save the pricing first, so the run charges what you just typed.'
  if (!willPost.length) return 'Nothing in that range is ready to post.'
  return null
}
