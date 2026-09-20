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
// A ledger record with no chapter behind it any more. Matched EXACTLY on purpose: the
// server says "already in the ledger as posted, and still on the site" when the chapter
// is also live, and that one must not match -- forgetting it would buy nothing but a
// duplicate on the next run.
export const STALE_LEDGER = 'already in the ledger as posted'

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

// Contiguous numbers collapsed to [first, last] pairs. One algorithm, because the server
// sends runs in this exact shape and they must render identically to ones computed here.
export function runs(numbers) {
  const ns = [...numbers].filter((n) => n != null).sort((a, b) => a - b)
  const out = []
  for (const n of ns) {
    const last = out[out.length - 1]
    if (last && n === last[1] + 1) last[1] = n
    else if (!last || n !== last[1]) out.push([n, n])
  }
  return out
}

export function formatRuns(pairs) {
  if (!pairs?.length) return '—'
  return pairs.map(([a, b]) => (a === b ? `${a}` : `${a}–${b}`)).join(', ')
}

// Contiguous runs read far better than a list of forty numbers.
export function range(rows) {
  return formatRuns(runs(rows.map(chapterNumber)))
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

// The number a chapter RANGE is matched against, or null when a range cannot reach the
// row at all.
//
// Side content is never range-selectable, even when it carries a number of its own. That
// exception is the whole reason this exists: two series number their side stories 1..10 in
// their own sequence, which collides exactly with their chapters 1..10. Reading `i.global`
// directly — as every function below used to — made a side story's ordinal look like a
// chapter number, so a run for chapters 1-10 swept ten side stories in, and the site's own
// posted range 1..125 marked all ten as already-posted duplicates.
//
// Mirrors `is_side` in server/posting.py::plan_run. These two must agree: the server
// decides what a run contains, this decides what the page PROMISES it contains.
export const chapterNumber = (i) => ((i && i.kind !== 'side' && i.global != null)
  ? i.global : null)

// Which of the ready chapters this run would actually post, applying the four controls in
// the same order the server does. A numeric range never sweeps up a side story.
export function eligible(ready, { from, to, upTo, limit }) {
  let rows = ready.filter((i) => {
    const n = chapterNumber(i)
    if (from && (n == null || n < Number(from))) return false
    if (to && (n == null || n > Number(to))) return false
    return true
  })
  const cap = Number(upTo) || 0
  if (cap) rows = rows.filter((i) => chapterNumber(i) != null && chapterNumber(i) <= cap)
  const most = Number(limit) || 0
  if (most) rows = rows.slice(0, most)
  return rows
}

// Why this run must not start, or null. The pricing question is asked of THIS RUN rather
// than of the series, which is the only way to get it right: a series with a free cutoff
// but no coin price would otherwise sail through and post everything past the cutoff as
// paid at zero coins — a locked chapter readers cannot buy, with the revenue simply gone.
export function blockedReason({ willPost, savedCoins, savedFree, priceDirty, dupes = [] }) {
  const paidInRun = willPost.filter((i) => i.paid).length
  if (savedCoins === 0 && paidInRun > 0) {
    return `${paidInRun} of these are past the free cutoff (chapter ${savedFree}), so they `
      + 'would post as paid at 0 coins — a locked chapter readers cannot buy. Set the '
      + 'coins first.'
  }
  // Second, because revenue lost outright is still the more expensive mistake. A duplicate
  // is the next one: readers can buy the same chapter twice, and removing one on meiko
  // means retyping its name to confirm.
  if (dupes.length) {
    return `${range(dupes)} ${dupes.length === 1 ? 'is' : 'are'} already on the site, by `
      + 'number, even though the titles did not match. Posting again makes a second copy. '
      + 'Narrow the range, or open the novel on meiko.studio so its chapter list is read '
      + 'again.'
  }
  if (priceDirty) return 'Save the pricing first, so the run charges what you just typed.'
  if (!willPost.length) return 'Nothing in that range is ready to post.'
  return null
}

// Staleness is a WARNING and must never reach blockedReason. The threshold that blocks a
// two-day-old snapshot also blocks every legitimate run on a machine that has not opened
// the meiko tab today -- and the cure needs that tab open anyway, which is when the push
// happens by itself. A blocking staleness check is the one that gets switched off.
export const SNAPSHOT_STALE_HOURS = 24

export function snapshotWarning(site) {
  if (!site) return null
  if (site.trusted === false) {
    return `The last reading of this novel cannot be used ${site.why ? `(${site.why})` : ''}`
      + ' — so nothing here is posting until it is read again. Open the novel on'
      + ' meiko.studio and let its chapter list finish loading.'
  }
  if (!site.fetched_at) {
    return 'Nobody has read this novel’s chapter list off the site yet, so nothing here'
      + ' knows what is already posted.'
  }
  // count without numbered is the corruption tell: 94 titles, none of them a chapter.
  if (site.count > 0 && site.numbered === 0) {
    return `The last reading got ${site.count} titles with no chapter numbers among them,`
      + ' which looks like the studio’s series list rather than this novel’s'
      + ' chapters. Open the novel itself on meiko.studio.'
  }
  const hours = (site.age_seconds || 0) / 3600
  if (hours >= SNAPSHOT_STALE_HOURS) {
    return `This list was read ${ago(site.fetched_at)} — anything posted since is not`
      + ' counted here.'
  }
  return null
}

// What this run would pick up next, given what is NOT yet on the site.
//
// `from` is min(ready), not max_posted + 1. `ready` already IS "mine that is not posted",
// with both the ledger and the site subtracted by the server; asking max_posted + 1
// consults a second, weaker oracle for the same thing. On real data it also gives the
// wrong answer: one series' site ends at 196 while its first ready chapter is 198, so
// max_posted + 1 names 197, which is not finished and cannot post.
//
// `to` ends the FIRST CONTIGUOUS run rather than at the highest ready number, because the
// site orders chapters by number: posting 223 while 201-222 are missing publishes a novel
// with a hole the reader can see.
export function nextRun(ready) {
  const numbered = ready.map(chapterNumber).filter((n) => n != null)
  const sides = ready.filter((i) => chapterNumber(i) == null).length
  const pairs = runs(numbered)
  if (!pairs.length) {
    return { from: null, to: null, count: 0, apart: [], sides, covers: true }
  }
  const [from, to] = pairs[0]
  const apart = [...numbered].sort((a, b) => a - b).filter((n) => n > to)
  return { from, to, count: to - from + 1, apart, sides, covers: apart.length === 0 }
}

const inRuns = (n, pairs) => (pairs || []).some(([a, b]) => n >= a && n <= b)

// Chapters this run would post whose NUMBER the site already carries.
//
// A real contradiction, not a guess: the server said this chapter is not posted (its title
// matched nothing) while the site's own list says that number is there. That is what title
// drift looks like — "Chapter 118: The Duel" up there against "Chapter 118" here — and it
// is exactly how the same chapter gets posted twice.
export function duplicates(willPost, site) {
  return willPost.filter((i) => {
    const n = chapterNumber(i)
    return n != null && inRuns(n, site?.posted)
  })
}

// Chapters below the site's highest number that it does not have: a hole. Could be a
// chapter deliberately deleted and due to be re-posted, so this warns rather than refuses.
export function belowSite(willPost, site) {
  const max = site?.max_posted
  if (max == null) return []
  return willPost.filter((i) => {
    const n = chapterNumber(i)
    return n != null && n < max && !inRuns(n, site?.posted)
  })
}

// The one-tap offer. Names the evidence, then the effect — the same shape as the coin
// price suggestion, which says "the site charges 55 — use that".
export function suggestionLabel(next, site) {
  if (next?.from == null) return ''
  const span = next.from === next.to
    ? `${next.from}` : `${next.from}${'–'}${next.to}`
  const ending = site?.max_posted != null ? `the site ends at ${site.max_posted} — ` : ''
  return `${ending}post ${span}`
}

// Where what you have meets what is up there — the direct answer to "will I post twice?".
//
// Deliberately not a repeat of range(ready): the Ready tile already shows that. The tile
// is "what I have", the site line is "what is up there", and this is where they meet.
export function pickupLine(next) {
  if (next?.from == null) return 'Nothing of yours is missing from it.'
  const span = next.count === 1 ? '1 chapter' : `${next.count} chapters`
  if (next.apart.length) {
    return `Picks up at ${next.from} — ${span}, then a gap before ${next.apart[0]}.`
  }
  return `Picks up at ${next.from} — ${span}.`
}

// "1 side story is" / "2 chapters without a number are" — `kind` is on every plan item.
export function unnumberedLabel(rows) {
  const loose = rows.filter((i) => chapterNumber(i) == null)
  if (!loose.length) return ''
  const allSide = loose.every((i) => i.kind === 'side')
  const noun = allSide
    ? `side stor${loose.length === 1 ? 'y' : 'ies'}`
    : `chapter${loose.length === 1 ? '' : 's'} without a number`
  return `${loose.length} ${noun} ${loose.length === 1 ? 'is' : 'are'}`
}
