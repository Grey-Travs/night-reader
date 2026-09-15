// Posts chapters to meiko.studio from inside your own logged-in browser.
//
// Posting goes through the site's own HTTP API (see "posting through the site's own API"
// below), not by clicking. That decision was earned: nine versions of DOM automation kept
// failing because the page has two buttons labelled "Create" whose dialogs look identical,
// and the sidebar one is studio-scoped and answers 400. The API has no such ambiguity, and
// no Vue state, disabled buttons, focus traps or timing either.
//
// What to post, what it is called and what HTML to send are all Night Reader's business —
// see background.js for the one channel to the app.
//
// The diagnostic modes create NOTHING: deleting a chapter here means retyping its name, so
// a debugging session that left an orphan per attempt would be miserable. Probe reads the
// page; Test paste uses an editor you already have open and never submits.

const VERSION = (() => {
  try {
    return chrome.runtime.getManifest().version
  } catch {
    return '?'
  }
})()

// Every report opens with the version that produced it.
//
// Reloading an unpacked extension does NOT re-inject a content script into an already-open
// tab — the tab has to be refreshed as well. Without that, an old build keeps running and
// its reports look current, which is exactly how a fix that had shipped was read as a fix
// that had failed. Stamping the log settles it at a glance.
// The report is kept in sessionStorage as well as memory, so it survives the page reload
// that follows a post. Posting through the API leaves the site's own chapter list stale —
// Vue fetched it before the chapter existed — so the page has to be refreshed to show the
// new chapter, and a report that vanished when it did would be worse than the staleness.
const LOG_KEY = 'nr.log'
const LOG = (() => {
  try {
    const saved = JSON.parse(sessionStorage.getItem(LOG_KEY) || '[]')
    return Array.isArray(saved) ? saved : []
  } catch {
    return []
  }
})()

function persistLog() {
  try {
    sessionStorage.setItem(LOG_KEY, JSON.stringify(LOG.slice(-400)))
  } catch { /* blocked storage only costs us the restore */ }
}

function say(...parts) {
  const line = parts.join(' ')
  LOG.push(line)
  persistLog()
  render()
  return line
}

function startLog(title) {
  LOG.length = 0
  say(`${title}   [extension v${VERSION}]`)
}

// --- what the page sent ----------------------------------------------------
//
// netlog.js runs in the page's own context and forwards a summary of every fetch and XHR.
// This is the instrument that settles the question DOM inspection could not: whether a
// click reached the site's handler at all. No request means the handler never ran; a
// request with a 4xx means it ran and the server refused, and says why.

// Kept in sessionStorage as well as memory, because creating a chapter navigates — and the
// first attempt to capture a manual Create lost it to exactly that: the page reloaded, the
// buffer went with it, and the log showed only the reload's own telemetry.
const NET_KEY = 'nr.netlog'
const NET = (() => {
  try {
    const saved = JSON.parse(sessionStorage.getItem(NET_KEY) || '[]')
    return Array.isArray(saved) ? saved : []
  } catch {
    return []
  }
})()

function persistNet() {
  try {
    sessionStorage.setItem(NET_KEY, JSON.stringify(NET.slice(-60)))
  } catch { /* storage full or blocked; the in-memory copy still works */ }
}

window.addEventListener('message', (e) => {
  if (e.source !== window || e.data?.source !== 'nr-net' || !e.data.entry) return
  NET.push({ ...e.data.entry, at: Date.now() })
  if (NET.length > 200) NET.shift()
  persistNet()
  showWriteCount()
})

// A live count of writes, in the header — so it is visible even when the panel is folded.
//
// This replaces an arm-then-act button. Requiring two presses in the right order was a bad
// design: each one cleared the other's output, and a report taken at the wrong moment looks
// exactly like one where nothing was sent. Now nothing needs arming — the number simply
// goes up when the site writes something, and you can watch it happen.
function showWriteCount() {
  const el = document.querySelector('.nr-panel .nr-writes')
  if (!el) return
  const n = NET.filter((r) => r.method !== 'GET').length
  el.textContent = n ? `${n} write${n === 1 ? '' : 's'}` : 'no writes yet'
  el.className = `nr-writes ${n ? 'nr-ok' : ''}`
}

function netSince(since) {
  return NET.filter((r) => r.at >= since)
}

// The site's own chapter records, learned from the list it fetches for itself. Keyed by
// display name, because that is the only handle shared between Night Reader's ledger and
// the site — the ids are the site's alone.
//
// Needed because a chapter can only be changed by PUTting its whole record back, so editing
// one created in an earlier session means knowing its `id`, `uid`, `createdAt` and the rest.
const CHAPTERS = new Map()
window.addEventListener('message', (e) => {
  if (e.source !== window || e.data?.source !== 'nr-chapters') return
  for (const ch of e.data.chapters || []) {
    if (ch?.displayName) CHAPTERS.set(ch.displayName.trim().toLowerCase(), ch)
  }
})

// GETs are the page refreshing itself; a write is what proves a submit happened.
// Tokens are already redacted in netlog.js before they reach here.
function describeNet(since, { limit = 6 } = {}) {
  const all = netSince(since)
  if (!all.length) return 'no requests at all — the click never reached the site'
  const writes = all.filter((r) => r.method !== 'GET')
  const show = writes.length ? writes : all
  const lines = [`${all.length} request(s), ${writes.length} of them writes:`]
  for (const r of show.slice(-limit)) {
    lines.push(`  ${r.method} ${r.status} ${r.url}`)
    // What was SENT matters as much as what came back: an operation the site puts in the
    // request body would be missing from ours, and that is the difference between 200
    // and 400.
    if (r.sent) lines.push(`    sent: ${r.sent.replace(/\s+/g, ' ').slice(0, 400)}`)
    if (r.body) lines.push(`    got:  ${r.body.replace(/\s+/g, ' ').slice(0, 300)}`)
  }
  return lines.join('\n')
}

/**
 * The writes, broken out parameter by parameter.
 *
 * This site's API takes everything in the query string, which means a successful request is
 * a TEMPLATE: swap the chapter name and it can be re-issued. That is a far better layer to
 * work at than the DOM — no Vue state to update, no disabled buttons, no dialog focus trap,
 * no timing. But building it needs the real thing, param by param, rather than a clipped URL.
 *
 * base64 values are decoded inline, because the chapter name travels that way and reading
 * it is the difference between "the name arrived" and "the name arrived as something else".
 */
function showApi() {
  startLog('# API shape of every write (secrets removed)')
  const writes = NET.filter((r) => r.method !== 'GET')
  if (!writes.length) {
    say('\nNo writes recorded yet. Post a chapter by hand and press this again.')
    return
  }
  for (const r of writes.slice(-10)) {
    say(`\n${r.method} ${r.status}${r.ok ? ' OK' : ' FAILED'}`)
    let q = ''
    try {
      const u = new URL(r.url, location.origin)
      say(`  host: ${u.origin === location.origin ? '(this site)' : u.origin}`)
      say(`  path: ${u.pathname}`)
      q = u.search
      for (const [k, v] of u.searchParams.entries()) {
        let shown = v
        // Decode base64 so the name is readable; leave anything that is not base64 alone.
        if (/^[A-Za-z0-9+/]+={0,2}$/.test(v) && v.length % 4 === 0 && v.length > 3) {
          try {
            const decoded = atob(v)
            if (/^[\x20-\x7E]+$/.test(decoded)) shown = `${v}   (= "${decoded}")`
          } catch { /* not base64 after all */ }
        }
        say(`    ${k} = ${shown.length > 120 ? `${shown.slice(0, 120)}…` : shown}`)
      }
    } catch {
      say(`  url: ${r.url.slice(0, 300)}`)
    }
    if (!q) say('  (no query parameters)')
    if (r.sent) say(`  body sent: ${r.sent.replace(/\s+/g, ' ').slice(0, 400)}`)
    if (r.body) say(`  response:  ${r.body.replace(/\s+/g, ' ').slice(0, 200)}`)
  }
  say('\nCopy this back to Night Reader.')
}

function showRequests() {
  startLog('# Recent requests from this page (tokens removed)')
  const writes = NET.filter((r) => r.method !== 'GET')
  say(`\n${NET.length} recorded, ${writes.length} of them writes.`)
  if (!writes.length) {
    // Listing the GETs here just fills the panel with icon JSON. The absence of a write is
    // the whole message: nothing has been created or saved since this page loaded.
    say('\nNo writes yet — nothing has been created or saved since this page loaded.')
    say('Create the chapter by hand: the counter in this panel\'s header goes up the moment')
    say('the site writes anything, so you can see it land. Then press this again.')
    return
  }
  say('')
  for (const r of writes.slice(-8)) {
    say(`  ${r.method} ${r.status} ${r.url}`)
    if (r.sent) say(`    sent: ${r.sent.replace(/\s+/g, ' ').slice(0, 400)}`)
    if (r.body) say(`    got:  ${r.body.replace(/\s+/g, ' ').slice(0, 300)}`)
  }
  say('\nCopy this back to Night Reader.')
}

/**
 * Which of the site's records is which.
 *
 * `reportChapters` forwards anything carrying `id` and `displayName`, matched by shape
 * rather than by path, which is what makes it catch chapters from any endpoint. The cost is
 * that the studio record and the series record match too — the studio is the one with a
 * `plan` and a `storage` quota, the series is the one with a `slug`. The first version of
 * this diagnostic reported the studio as its sample chapter, which is the sort of thing
 * that sends you looking for a text field on the wrong object.
 */
function recordKind(r) {
  if (!r || typeof r !== 'object') return 'other'
  if ('plan' in r || 'storage' in r) return 'studio'
  if ('slug' in r) return 'series'
  if ('series_uid' in r && 'type' in r) return 'chapter'
  return 'other'
}

/** The most recent value the page itself sent for a given query parameter. */
function observedParam(name) {
  for (const r of [...NET].reverse()) {
    try {
      const v = new URL(r.url, location.origin).searchParams.get(name)
      if (v) return v
    } catch { /* not a URL we can read */ }
  }
  return ''
}

/**
 * The READ side of the site's API — the one thing never yet looked at.
 *
 * Every other diagnostic here filters GETs out on purpose, because when the question was
 * "did my click send anything", the GETs were the page refreshing itself and they buried
 * the one request that mattered. That filter is now the only reason the read API was
 * unknown: `keepBody` has been keeping `/app/` GET bodies all along, and `reportChapters`
 * has been filling CHAPTERS with the site's own records, unclipped.
 *
 * Writes nothing and changes nothing.
 */
function showReads() {
  startLog('# What this page READS from the site (secrets removed)')
  const reads = NET.filter((r) => r.method === 'GET')
  const appReads = reads.filter((r) => {
    try {
      return new URL(r.url, location.origin).pathname.startsWith('/app/')
    } catch {
      return false
    }
  })

  say(`\n${NET.length} requests recorded · ${reads.length} GETs · ${appReads.length} of`
    + ` those against the site's own /app/ API`)

  if (!appReads.length) {
    say('\nNothing yet. Open the series page so the site fetches its own chapter list,')
    say('then press this again. If it stays empty, the list may have been fetched before')
    say('this tab was loaded — reload the page and try once more.')
  }

  // One line per distinct endpoint rather than per request: the page refetches the same
  // three on every navigation, and six near-identical blocks hid the shape rather than
  // showing it.
  const byPath = new Map()
  for (const r of appReads) {
    try {
      const u = new URL(r.url, location.origin)
      const params = [...u.searchParams.keys()].filter((k) => k !== 'token').sort()
      const key = `${u.pathname}?${params.join('&')}`
      if (!byPath.has(key)) {
        byPath.set(key, { origin: u.origin, path: u.pathname, params, n: 0, sample: r,
          values: Object.fromEntries([...u.searchParams.entries()]
            .filter(([k]) => k !== 'token')) })
      }
      byPath.get(key).n += 1
    } catch { /* skip */ }
  }
  for (const e of byPath.values()) {
    say(`\n${e.origin}${e.path}   (seen ${e.n}x)`)
    for (const k of e.params) {
      const v = String(e.values[k] ?? '')
      say(`    ${k} = ${v.length > 80 ? `${v.slice(0, 80)}…` : v}`)
    }
    if (e.sample.body) {
      say(`  response begins: ${e.sample.body.replace(/\s+/g, ' ').slice(0, 200)}`)
    }
  }

  // --- the decisive part ---------------------------------------------------
  //
  // CHAPTERS holds whatever the site returned, unclipped and unredacted, because
  // reportChapters forwards it on its own channel for exactly that reason. So the question
  // "does the list include the text" is answered by looking at a real CHAPTER record --
  // not at the studio record, which also matches the shape filter.
  say(`\n${'-'.repeat(46)}`)
  const all = [...CHAPTERS.values()]
  const kinds = {}
  for (const r of all) kinds[recordKind(r)] = (kinds[recordKind(r)] || 0) + 1
  say(`Records this tab has seen: ${all.length}  (`
    + Object.entries(kinds).map(([k, n]) => `${n} ${k}`).join(', ') + ')')

  const chapters = all.filter((r) => recordKind(r) === 'chapter')
  if (!chapters.length) {
    say('\nNo CHAPTER records yet — only the studio and series objects, which match the')
    say('same shape filter. Open the series page so the site fetches its chapter list.')
    say('\nCopy this back to Night Reader.')
    return
  }

  const withText = chapters.filter((r) => chapterHtml(r).length > 0)
  say(`Chapter records carrying their text: ${withText.length} of ${chapters.length}`)
  // Deliberately NOT concluding anything about the list from this. A record carries its
  // text only if it was fetched with `pages=true`, so one record having text may simply
  // mean an editor was opened. The first version of this drew the opposite conclusion and
  // stated it confidently, which is worse than saying nothing. Use Fetch one chapter.
  say('A record only carries text when it was fetched with pages=true — so this counts')
  say('what this tab happens to have seen, not what the list returns. Press Fetch one')
  say('chapter to settle whether a whole novel can be read in one request.')

  const sample = withText[0] || chapters[0]
  say(`\nEvery field on one real chapter ("${String(sample.displayName || '?').slice(0, 40)}"):`)
  say(`  ${Object.keys(sample).sort().join(', ')}`)
  for (const field of ['id', 'uid', 'studio_uid', 'series_uid', 'displayName', 'state',
    'status', 'type', 'paid', 'coins', 'position', 'position_number', 'view_count',
    'slug', 'shortlink']) {
    if (field in sample) say(`  ${field} = ${JSON.stringify(sample[field])}`)
  }
  // Any field big enough to be prose, whatever it is called. If the text is not in `pages`
  // it is somewhere, and a long string is how it will show itself.
  const longish = Object.entries(sample)
    .filter(([, v]) => typeof v === 'string' && v.length > 120)
    .map(([k, v]) => `${k} (${v.length} chars)`)
  say(`  fields long enough to be prose: ${longish.length ? longish.join(', ') : 'none'}`)

  reportHtml(chapterHtml(sample))
  say('\nCopy this back to Night Reader.')
}

/** What the markup of a real chapter actually contains, and how far it is from ours. */
function reportHtml(html) {
  if (!html) return
  say(`\nIts text decoded to ${html.length} characters of HTML. The opening:`)
  say(`  ${html.slice(0, 500)}`)
  const tags = [...new Set([...html.matchAll(/<\s*([a-zA-Z][a-zA-Z0-9]*)/g)]
    .map((m) => m[1].toLowerCase()))].sort()
  say(`\n  tags present: ${tags.join(', ')}`)
  // Night Reader only ever emits these eight, with no attributes at all. A chapter typed
  // into the site's own editor can carry much more, and that gap is the converter's work.
  const known = new Set(['p', 'br', 'hr', 'blockquote', 'strong', 'em',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
  const extra = tags.filter((t) => !known.has(t))
  say(extra.length
    ? `  tags Night Reader does not produce: ${extra.join(', ')}`
    : '  all of them are tags Night Reader already produces.')
  const attrs = [...new Set([...html.matchAll(/<[a-zA-Z][^>]*?\s([a-zA-Z-]+)\s*=/g)]
    .map((m) => m[1].toLowerCase()))].sort()
  say(`  attributes present: ${attrs.length ? attrs.join(', ') : 'none'}`)
  if (/&[a-zA-Z#][a-zA-Z0-9]*;/.test(html)) {
    const ents = [...new Set([...html.matchAll(/&([a-zA-Z#][a-zA-Z0-9]*);/g)]
      .map((m) => m[1]))].slice(0, 12)
    say(`  HTML entities present: ${ents.join(', ')}`)
  }
}

/**
 * Ask the site for chapter text, now that the parameter that carries it is known.
 *
 * `pages=true` is the whole trick, and no amount of guessing found it — four attempts at
 * uid/id/chapter_uid all came back with the chapter's settings and no prose. It turned up
 * in the log the moment the editor was opened by hand, which is the same lesson as the
 * Create button in Part B: the page knows how to ask, so watch it ask.
 *
 * The open question now is worth one request: whether `pages=true` works on the LIST as
 * well as on a single chapter. If it does, a whole novel arrives in one request instead of
 * one per chapter, which is the difference between a few seconds and several minutes for a
 * 150-chapter novel — and between one failure point and a hundred and fifty.
 */
async function probeChapterFetch() {
  startLog('# Can a whole novel be read in one request?')
  const chapters = [...CHAPTERS.values()].filter((r) => recordKind(r) === 'chapter')
  if (!chapters.length) {
    say('\nNo chapter records yet — open the series page first so the list is fetched.')
    return
  }
  const target = chapters[0]
  const userid = observedParam('userid')
  const { studio_uid, series_uid } = target
  say(`\nseries ${series_uid} · ${chapters.length} chapters known to this tab`)

  const attempts = [
    // The prize: the list, with the text included.
    ['THE LIST with pages=true', { studio_uid, series_uid, userid, pages: true }],
    // The known-good single read, as a control - if this one fails too, something else
    // is wrong and the answer above means nothing.
    ['one chapter with pages=true (control)',
      { studio_uid, series_uid, uid: target.uid, pages: true }],
  ]

  for (const [label, params] of attempts) {
    say(`\n--- ${label}`)
    say(`    GET /app/chapter?${Object.entries(params).filter(([, v]) => v !== '')
      .map(([k, v]) => `${k}=${v}`).join('&')}`)
    let res
    try {
      res = await api('GET', '/app/chapter', params)
    } catch (e) {
      say(`    refused: ${e.message}`)
      if (isAuthFailure(e.message)) {
        say('    That is the site session, not the request. Refresh the page and retry.')
      }
      continue
    }
    const rows = Array.isArray(res?.data) ? res.data
      : res?.data && typeof res.data === 'object' ? [res.data] : []
    const withText = rows.filter((r) => chapterHtml(r).length > 0)
    say(`    ${rows.length} record${rows.length === 1 ? '' : 's'},`
      + ` ${withText.length} carrying text`)
    for (const r of rows.slice(0, 3)) {
      const html = chapterHtml(r)
      say(`      "${String(r.displayName || '?').slice(0, 32)}"`
        + ` · paid ${JSON.stringify(r.paid)} · coins ${JSON.stringify(r.coins)}`
        + ` · ${html.length} chars`)
    }
    if (rows.length > 3) say(`      … and ${rows.length - 3} more`)

    if (rows.length > 1 && withText.length === rows.length) {
      say(`\nYES. All ${rows.length} chapters came back with their text in ONE request.`)
      reportHtml(chapterHtml(withText[0]))
      surveyMarkup(withText)
      say('\nCopy this back to Night Reader.')
      return
    }
    await sleep(800)
  }
  say('\nThe list does not carry text even with pages=true, so it is one request per')
  say('chapter — which is fine, just paced. Night Reader has what it needs either way.')
  say('\nCopy this back to Night Reader.')
}

/**
 * Every tag, attribute and entity across a whole novel, not just its first chapter.
 *
 * This decides how much work the HTML-to-Markdown converter actually has. Night Reader
 * emits eight tags and no attributes at all; a chapter typed into the site's own editor can
 * carry lists, links, underline, colour spans and non-breaking spaces. Sampling one chapter
 * would miss the one chapter somebody formatted by hand.
 */
function surveyMarkup(records) {
  const tags = new Set()
  const attrs = new Set()
  const ents = new Set()
  let total = 0
  for (const r of records) {
    const html = chapterHtml(r)
    total += html.length
    for (const m of html.matchAll(/<\s*([a-zA-Z][a-zA-Z0-9]*)/g)) tags.add(m[1].toLowerCase())
    for (const m of html.matchAll(/<[a-zA-Z][^>]*?\s([a-zA-Z-]+)\s*=/g)) {
      attrs.add(m[1].toLowerCase())
    }
    for (const m of html.matchAll(/&([a-zA-Z#][a-zA-Z0-9]*);/g)) ents.add(m[1])
  }
  const known = new Set(['p', 'br', 'hr', 'blockquote', 'strong', 'em',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
  const extra = [...tags].filter((t) => !known.has(t)).sort()
  say(`\nAcross all ${records.length} chapters (${total} characters of HTML):`)
  say(`  tags:       ${[...tags].sort().join(', ')}`)
  say(`  attributes: ${attrs.size ? [...attrs].sort().join(', ') : 'none'}`)
  say(`  entities:   ${ents.size ? [...ents].sort().slice(0, 15).join(', ') : 'none'}`)
  say(extra.length
    ? `  NEEDS CONVERSION: ${extra.join(', ')}`
    : '  Nothing outside what Night Reader already produces.')
}

// --- posting through the site's own API ------------------------------------
//
// The site's chapter API takes everything in the query string, so posting is two requests:
//
//   POST /app/chapter?displayName=<base64>&state=private&studio_uid&series_uid
//     -> the new chapter object, including its id and uid
//   PUT  /app/chapter?<those fields, with state/type/paid/coins set>
//     body {"pages": base64(JSON.stringify([{displayName:"", note: html}]))}
//
// This replaces eight DOM steps, and it is why the DOM approach kept failing: the page has
// TWO buttons labelled "Create" and the sidebar one is studio-scoped, posting to
// /app/studio, which answers 400. Both open an identical Name dialog, so nothing visible
// distinguished them. Working at the API removes that ambiguity along with Vue state,
// disabled buttons, focus traps and timing.
//
// The token never passes through here: netlog.js holds it in the page's context and
// supplies it per call, so it is never logged or reported.

let apiSeq = 0
function api(method, path, params, body) {
  return new Promise((resolve, reject) => {
    const id = `nr-${++apiSeq}`
    const onReply = (e) => {
      if (e.source !== window || e.data?.source !== 'nr-api-reply' || e.data.id !== id) return
      window.removeEventListener('message', onReply)
      e.data.ok ? resolve(e.data.data) : reject(new Error(e.data.error
        || `the site refused it (HTTP ${e.data.status}) ${JSON.stringify(e.data.data || {}).slice(0, 160)}`))
    }
    window.addEventListener('message', onReply)
    window.postMessage({ source: 'nr-api-call', id, method, path, params, body }, '*')
    setTimeout(() => {
      window.removeEventListener('message', onReply)
      reject(new Error(`the site did not answer the ${method} in time`))
    }, 30000)
  })
}

/**
 * base64, UTF-8 safe.
 *
 * `btoa` throws on any character above U+00FF, and these translations are full of curly
 * quotes and dashes — so the naive version would fail on most real chapters, and on the
 * Korean source outright.
 */
function b64(text) {
  const bytes = new TextEncoder().encode(String(text))
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary)
}

/**
 * The inverse of b64. Needed to READ a chapter back off the site.
 *
 * Bare `atob` is not enough for the same reason bare `btoa` was not: it returns bytes, and
 * these chapters are full of curly quotes and dashes, so the result has to go back through
 * TextDecoder or every one of them arrives as mojibake. Returns '' rather than throwing,
 * because this runs over whatever the site happens to have stored.
 */
function unb64(value) {
  try {
    const binary = atob(String(value || ''))
    const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0))
    return new TextDecoder().decode(bytes)
  } catch {
    return ''
  }
}

/**
 * The chapter text out of a chapter record, or ''.
 *
 * The site keeps it at `pages`, which is base64 of a JSON array of page objects, each with
 * the HTML at `note` — the exact envelope apiSaveChapter builds on the way out. Nothing has
 * ever read it back, so every layer here is defensive: the field may be absent, may be
 * plain JSON rather than base64, and may hold several pages for one chapter.
 */
function chapterHtml(record) {
  const raw = record?.pages
  if (!raw) return ''
  let pages
  try {
    pages = JSON.parse(typeof raw === 'string' ? (unb64(raw) || raw) : JSON.stringify(raw))
  } catch {
    return ''
  }
  const list = Array.isArray(pages) ? pages : [pages]
  return list.map((p) => String(p?.note || '')).filter(Boolean).join('\n')
}

function uidsFromUrl() {
  // .../page/<studio_uid>/series/<series_uid>[/...]
  const m = location.pathname.match(/\/page\/([^/]+)\/series\/([^/?]+)/)
  if (!m) throw new Error('this does not look like a series page')
  return { studio_uid: m[1], series_uid: m[2] }
}

async function apiCreateChapter(title) {
  const { studio_uid, series_uid } = uidsFromUrl()
  const res = await api('POST', '/app/chapter', {
    displayName: b64(title),
    // Created private, always. The chapter is only made public once its text is in, so a
    // failure part-way cannot leave an empty chapter visible to readers.
    state: 'private',
    studio_uid,
    series_uid,
  })
  const data = res?.data
  if (!data?.id) throw new Error('the site created nothing it could name')
  return data
}

async function apiSaveChapter(data, { state, coins, html }) {
  const now = Date.now()
  const pages = html === undefined ? undefined
    : [{ displayName: '', note: html }]
  return api('PUT', '/app/chapter', {
    version: data.version ?? 2,
    id: data.id,
    studio_uid: data.studio_uid,
    series_uid: data.series_uid,
    photoURL: data.photoURL || '',
    bannerURL: data.bannerURL || '',
    uid: data.uid,
    view_count: data.view_count ?? 0,
    displayName: b64(data.displayName),
    description: data.description || '',
    status: 'live',
    // Lowercase, because that is what the site's own requests send and the server stores
    // whatever it is given verbatim — a chapter saved as "Private" carries a value the site
    // itself never produces, which is asking for trouble in whatever reads it back.
    state: String(state).toLowerCase(),
    // "notes" is the text type; the default "pages" is for image chapters.
    type: 'notes',
    createdAt: data.createdAt ?? now,
    createdBy: data.createdBy || '',
    lastUpdatedAt: now,
    lastUpdatedBy: data.createdBy || '',
    position: data.position ?? 0,
    position_number: data.position_number ?? 0,
    preview: JSON.stringify({ cover: { number: 1, type: 1 } }),
    paid: Number(coins) > 0,
    coins: Number(coins) || 0,
    // toggle 0 = not scheduled. Chapters go out live, as the walkthrough does by hand.
    schedule: JSON.stringify({ date: new Date().toISOString(), toggle: 0 }),
    pin: 0,
    shortlink: '',
    tier: 0,
    tags: false,
    synonyms: false,
    socials: false,
    disqus: false,
    discord: false,
    custom_date: false,
    emails: false,
    cache: JSON.stringify(new Date().toISOString()),
  }, pages ? { pages: b64(JSON.stringify(pages)) } : undefined)
}

/**
 * Post one chapter: create it, then save its fields and text together.
 *
 * `state` stays a parameter so a trial run can leave the chapter Private and invisible to
 * readers. The text goes in the SAME call that publishes it, so there is no window in which
 * an empty chapter is public.
 */
// A chapter that was created but not finished, remembered so a retry continues into it.
//
// Without this, a failure between "created" and "saved" leaves a chapter on the site and the
// next run makes a SECOND one with the same name — and removing either means retyping its
// name to confirm. The record is keyed by title and cleared the moment the save succeeds.
const PENDING_KEY = 'nr.pendingChapter'

function readPending() {
  try {
    return JSON.parse(sessionStorage.getItem(PENDING_KEY) || 'null')
  } catch {
    return null
  }
}

function writePending(value) {
  try {
    if (value) sessionStorage.setItem(PENDING_KEY, JSON.stringify(value))
    else sessionStorage.removeItem(PENDING_KEY)
  } catch { /* blocked storage just means no resume */ }
}

async function postViaApi({ payload, coins, state }) {
  say(`\n## ${payload.title}  (${state}, ${coins ? `${coins} coins` : 'free'})`)

  const pending = readPending()
  let created
  if (pending?.title === payload.title && pending.data?.id) {
    say(`1/3 resuming the chapter created earlier (id ${pending.data.id})`)
    created = pending.data
  } else {
    say('1/3 creating the chapter')
    created = await apiCreateChapter(payload.title)
    say(`    created id ${created.id} · uid ${created.uid}`)
    // Written BEFORE the save is attempted: the point is to survive a failure in the save.
    writePending({ title: payload.title, data: created })
  }

  say('2/3 saving its settings and text')
  const saved = await apiSaveChapter(created, { state, coins, html: payload.html })
  const after = saved?.data || {}
  say(`    ${after.displayName || payload.title}: state=${after.state} type=${after.type}`
    + ` paid=${after.paid} coins=${after.coins}`)

  say('3/3 checking it back')
  // Read the server's own answer rather than trusting the request: these are the three
  // fields that cost something real if they are silently wrong — the wrong type, invisible
  // to readers, or paid content given away free.
  // Compared loosely on purpose: the server normalises what it is given, returning `paid`
  // as 1/0 where `true` was sent. Comparing strictly turned a correct save into a failure.
  const same = (a, b) => String(a).toLowerCase() === String(b).toLowerCase()
  const wrong = []
  if (!same(after.type, 'notes')) wrong.push(`type is "${after.type}"`)
  if (!same(after.state, state)) wrong.push(`state is "${after.state}"`)
  if (Number(after.coins || 0) !== Number(coins || 0)) wrong.push(`coins is ${after.coins}`)
  const wantPaid = Number(coins) > 0
  const gotPaid = after.paid === true || Number(after.paid) === 1
  if (gotPaid !== wantPaid) wrong.push(`paid is ${after.paid}`)
  if (wrong.length) throw new Error(`saved wrongly — ${wrong.join('; ')}`)
  say('    settings confirmed by the site')
  // Only now: the chapter is complete, so there is nothing left to resume into.
  writePending(null)
  return { title: payload.title, id: created.id, chars: payload.html.length }
}

// --- finding things --------------------------------------------------------
//
// Matched by visible TEXT first, because text is what survives a redesign: a class like
// `.btn-x7f` changes whenever the site rebuilds its CSS, while a button labelled "Submit"
// stays labelled "Submit".

const CLICKABLE = 'button, a, [role="button"], [type="submit"]'

function visible(el) {
  if (!el) return false
  const r = el.getBoundingClientRect()
  if (!r.width && !r.height) return false
  const s = getComputedStyle(el)
  return s.visibility !== 'hidden' && s.display !== 'none'
}

function label(el) {
  return (el.innerText || el.textContent || el.value || el.getAttribute?.('aria-label') || '')
    .replace(/\s+/g, ' ').trim()
}

function byText(text, selector = CLICKABLE) {
  const want = String(text || '').trim().toLowerCase()
  if (!want) return []
  return [...document.querySelectorAll(selector)]
    .filter(visible)
    .filter((el) => label(el).toLowerCase().includes(want))
    // Prefer the tightest match: a wrapper often contains the button's text too.
    .sort((a, b) => label(a).length - label(b).length)
}

function cssPath(el) {
  const bits = []
  for (let node = el; node && node.nodeType === 1 && bits.length < 5; node = node.parentElement) {
    let bit = node.tagName.toLowerCase()
    const cls = [...node.classList].slice(0, 2).map((c) => `.${c}`).join('')
    if (cls) bit += cls
    const siblings = node.parentElement
      ? [...node.parentElement.children].filter((s) => s.tagName === node.tagName)
      : []
    if (siblings.length > 1) bit += `:nth-of-type(${siblings.indexOf(node) + 1})`
    bits.unshift(bit)
    // An id anchors the path — but the descendant chain below it has to be KEPT, or two
    // different elements under the same id-bearing wrapper report the identical path and
    // the report silently conflates them.
    if (node.id) { bits[0] = `#${node.id}`; break }
  }
  return bits.join(' > ')
}

function findEditor() {
  const candidates = [...document.querySelectorAll('[contenteditable="true"], [contenteditable=""]')]
    .filter(visible)
  if (candidates.length) return candidates.sort((a, b) => b.clientHeight - a.clientHeight)[0]
  // Some editors keep a hidden textarea alongside; a plain textarea is also worth trying.
  return [...document.querySelectorAll('textarea')].filter(visible)
    .sort((a, b) => b.clientHeight - a.clientHeight)[0] || null
}

function findCounter() {
  // "0 / 100000". The single most useful signal this site gives us: if it does not move
  // off zero after a paste, the paste failed and the chapter would save empty.
  const re = /(\d[\d,]*)\s*\/\s*(\d[\d,]{3,})/
  for (const el of document.querySelectorAll('div, span, p, small')) {
    if (el.children.length) continue
    const m = re.exec(label(el))
    if (m) return { el, used: Number(m[1].replace(/,/g, '')), max: Number(m[2].replace(/,/g, '')) }
  }
  return null
}

// --- mode 1: probe. Reads the page, changes nothing. -----------------------

function probe() {
  startLog(`# Probe — ${location.href}`)

  const buttons = [...document.querySelectorAll(CLICKABLE)].filter(visible)
  say(`\n## Clickable (${buttons.length})`)
  for (const b of buttons.slice(0, 40)) {
    const text = label(b)
    if (text) say(`  "${text}"   ${cssPath(b)}`)
  }

  const selects = [...document.querySelectorAll('select')].filter(visible)
  say(`\n## Selects (${selects.length})`)
  for (const s of selects) {
    const opts = [...s.options].map((o) => o.text.trim()).join(' | ')
    say(`  value="${s.value}"  options: ${opts}   ${cssPath(s)}`)
  }

  // This site has NO <select> elements — it is built with Headless UI, whose Listbox is a
  // button that opens a [role="listbox"] of [role="option"] items. Report those, plus
  // anything else that behaves like a dropdown, so the Details form can be mapped.
  const dropdowns = [...document.querySelectorAll(
    '[role="combobox"], [aria-haspopup], [data-headlessui-state], [role="listbox"]',
  )].filter(visible)
  say(`\n## Dropdown-ish (${dropdowns.length})`)
  for (const s of dropdowns.slice(0, 30)) {
    const state = s.getAttribute('data-headlessui-state') || ''
    const pop = s.getAttribute('aria-haspopup') || ''
    const role = s.getAttribute('role') || s.tagName.toLowerCase()
    say(`  role=${role} haspopup=${pop} state="${state}" "${label(s).slice(0, 40)}"   ${cssPath(s)}`)
  }

  const options = [...document.querySelectorAll('[role="option"]')].filter(visible)
  if (options.length) {
    say(`\n## Open options (${options.length})`)
    for (const o of options.slice(0, 20)) say(`  "${label(o)}"   ${cssPath(o)}`)
  }

  // The structure inside a row matters: the chapter name and its coin badge share one
  // cell, so knowing how they are nested is what makes the name parseable.
  const firstRow = [...document.querySelectorAll('tbody tr')].filter(visible)[0]
  if (firstRow) {
    say('\n## First row, inner HTML (trimmed)')
    say(`  ${firstRow.innerHTML.replace(/\s+/g, ' ').slice(0, 700)}`)
  }

  const inputs = [...document.querySelectorAll('input')].filter(visible)
  say(`\n## Inputs (${inputs.length})`)
  for (const i of inputs.slice(0, 30)) {
    say(`  type=${i.type} placeholder="${i.placeholder || ''}" value="${String(i.value).slice(0, 24)}"   ${cssPath(i)}`)
  }

  const toggles = [...document.querySelectorAll('[role="switch"], input[type="checkbox"]')].filter(visible)
  say(`\n## Toggles (${toggles.length})`)
  for (const t of toggles) {
    const on = t.getAttribute('aria-checked') ?? t.checked
    say(`  on=${on} "${label(t.parentElement || t)}"   ${cssPath(t)}`)
  }

  const rows = [...document.querySelectorAll('tbody tr, [role="row"]')].filter(visible)
  say(`\n## Table rows (${rows.length}) — the existing chapter list`)
  for (const r of rows.slice(0, 5)) {
    const cells = [...r.children].map((c) => label(c)).filter(Boolean)
    say(`  ${JSON.stringify(cells)}`)
  }

  const editor = findEditor()
  say(`\n## Editor: ${editor ? cssPath(editor) : 'not on this page (open Write Page first)'}`)
  const counter = findCounter()
  say(`## Counter: ${counter ? `${counter.used} / ${counter.max}  ${cssPath(counter.el)}` : 'not found'}`)
  say('\nNothing was changed. Copy this and hand it back to Night Reader to fix the adapter.')
}

// --- mode 2: test the paste. Uses an editor you already have open. ---------

const SAMPLE = '<p>Night Reader paste test — paragraph one.</p>\n'
  + '<p>Paragraph two, with <strong>bold</strong> and <em>italic</em>.</p>'

function pasteHtml(el, html, text) {
  // A rich-text editor of the ProseMirror/TipTap family will not take `el.value` (it is
  // not a textarea) and setting innerHTML corrupts its internal document. A synthetic
  // `paste` event carrying text/html is the reliable route, and it is exactly what
  // happens when a person pastes by hand.
  el.focus()
  el.click?.()
  const dt = new DataTransfer()
  dt.setData('text/html', html)
  dt.setData('text/plain', text)
  const ev = new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true })
  el.dispatchEvent(ev)
  // dispatchEvent returns FALSE when a listener called preventDefault() — which for a
  // paste means the editor took it. So "handled" is the success signal, and reading the
  // boolean the other way round makes a working paste look like a failure.
  return { handled: ev.defaultPrevented }
}

async function testPaste() {
  startLog('# Paste test — creates nothing, submits nothing')
  const editor = findEditor()
  if (!editor) {
    say('\nNo editor on this page. Open any EXISTING chapter → Pages → Write Page, then')
    say('press this again. Using a chapter that already exists means nothing has to be')
    say('created or deleted afterwards.')
    return
  }
  const before = findCounter()
  say(`editor:  ${cssPath(editor)}`)
  say(`counter: ${before ? `${before.used} / ${before.max}` : 'not found'}`)

  const r = pasteHtml(editor, SAMPLE, 'Night Reader paste test — paragraph one.\n\nParagraph two.')
  say(`\npaste handled by the editor: ${r.handled}`)

  await new Promise((res) => setTimeout(res, 600))
  const after = findCounter()
  const text = (editor.innerText || editor.value || '').trim()
  say(`counter now: ${after ? `${after.used} / ${after.max}` : 'not found'}`)
  say(`editor holds ${text.length} characters`)
  say(`html: ${(editor.innerHTML || '').slice(0, 160)}`)

  const moved = before && after && after.used > before.used
  if (moved || text.includes('paste test')) {
    say('\nRESULT: the synthetic paste WORKS. Close the editor without submitting.')
  } else {
    say('\nRESULT: the synthetic paste did NOT land. The fallback is to put the HTML on')
    say('the real clipboard and send a genuine Ctrl+V — tell Night Reader and it will')
    say('switch to that. Close the editor without submitting.')
  }
}

// --- the new-chapter dialog ------------------------------------------------

// Containers a modal is likely to be: an explicit dialog role, a Headless UI panel, or a
// fixed-position overlay.
const DIALOG_CSS = '[role="dialog"], dialog, [data-headlessui-state*="open"], .fixed'

function dialogs() {
  return [...document.querySelectorAll(DIALOG_CSS)]
    .filter(visible)
    .filter((d) => d.querySelector('input, textarea'))
    // Innermost first: the overlay usually wraps the panel, and the panel is what holds
    // the field we want.
    .sort((a, b) => b.compareDocumentPosition(a) & 2 ? 1 : -1)
}

function textBoxes(root = document) {
  return [...root.querySelectorAll('input, textarea')]
    .filter(visible)
    .filter((i) => i.tagName === 'TEXTAREA' || (i.type || 'text') === 'text')
    .filter((i) => !/search/i.test(i.placeholder || ''))
}

function findNameBox(before) {
  // Inside a modal first — that is unambiguous. Only then fall back to anything new on
  // the page, and last of all to any text box at all.
  for (const d of dialogs()) {
    const inside = textBoxes(d)
    if (inside.length) return inside.find((i) => !before?.has(i)) || inside[0]
  }
  const all = textBoxes()
  return all.find((i) => !before?.has(i)) || null
}

/**
 * Open the new-chapter dialog and return its name box.
 *
 * Tries every button with the Create label rather than assuming which one it is: there are
 * two on the series page and DOM order is not a promise. Shared by the probe and the real
 * run, so the probe exercises exactly the code that posts.
 *
 * Worth knowing: the working button reports `disabled=true` just after the page settles,
 * and a synthetic event dispatched straight at an element still reaches its listeners even
 * when disabled — which is why it opens anyway. That is luck, not design, so wait for it to
 * become enabled and only fall back to clicking a disabled one as a last resort.
 */
async function openCreateDialog(adapter, log = () => {}) {
  const candidates = byText(adapter.create.text)
  if (!candidates.length) throw new Error(`no button labelled "${adapter.create.text}"`)
  for (const [i, button] of candidates.entries()) {
    const before = new Set([...document.querySelectorAll('input, textarea')])
    if (button.disabled) {
      // Give it a moment: it is usually disabled only while the page finishes loading.
      try {
        await waitFor(() => !button.disabled, { ms: 3000, what: 'the button to enable' })
      } catch {
        log(`    [${i + 1}] still disabled — clicking it anyway as a last resort`)
      }
    }
    log(`    [${i + 1}] ${cssPath(button)}`)
    button.scrollIntoView({ block: 'center' })
    realClick(button)
    await sleep(1200)
    const box = findNameBox(before)
    if (box) {
      // Return the PANEL as well as the box. Everything else in this dialog has to be
      // found inside it: a Headless UI dialog closes on a pointerdown anywhere outside its
      // panel, and `realClick` fires one — so clicking a same-named button elsewhere on the
      // page dismisses the dialog instead of submitting it, which is exactly what happened
      // to the first attempt at chapter 62.
      const panel = box.closest('[role="dialog"], dialog, .fixed') || box.parentElement
      log(`    [${i + 1}] opened the dialog · name box ${cssPath(box)}`)
      return { box, panel }
    }
    // Nothing appeared. Put the page back before trying the next one.
    document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await sleep(300)
  }
  throw new Error('no Create button opened a text box')
}

function describeDialog() {
  const lines = ['--- what is on the page right now ---']
  const ds = [...document.querySelectorAll(DIALOG_CSS)].filter(visible)
  lines.push(`dialog-ish containers: ${ds.length}`)
  for (const d of ds.slice(0, 4)) {
    lines.push(`  ${cssPath(d)}`)
    lines.push(`    text: "${label(d).slice(0, 90)}"`)
  }
  const boxes = [...document.querySelectorAll('input, textarea')]
  lines.push(`inputs + textareas: ${boxes.length}`)
  for (const i of boxes.slice(0, 12)) {
    lines.push(`  <${i.tagName.toLowerCase()}> type=${i.type || '-'}`
      + ` placeholder="${i.placeholder || ''}" visible=${visible(i)}`
      + `  ${cssPath(i)}`)
  }
  const ce = [...document.querySelectorAll('[contenteditable]')].filter(visible)
  if (ce.length) {
    lines.push(`contenteditable: ${ce.length}`)
    for (const e of ce.slice(0, 3)) lines.push(`  ${cssPath(e)}`)
  }
  lines.push('Copy this back to Night Reader — nothing was submitted, so close the dialog.')
  return lines.join('\n')
}

// --- what is already on the site -------------------------------------------

/**
 * Every chapter the site already lists, read straight off the page.
 *
 * This is a better duplicate guard than our own ledger, because it reflects reality —
 * including everything posted by hand, long before this extension existed. Without it the
 * first run picked "Chapter 61", which had been live on the site for ages, and would have
 * created a second copy of it.
 *
 * Cell 1 runs the chapter's name and its coin badge together as "Chapter 61 55", so the
 * price is read from the row's coins input and only then trimmed off the end of the name.
 * Splitting off a trailing number unconditionally would turn "Side Stories 1" into
 * "Side Stories" priced at 1.
 */
function scrapeChapterList(adapter) {
  const cl = adapter.chapter_list || {}
  const rows = [...document.querySelectorAll(cl.row_css || 'tbody tr')].filter(visible)
  return rows.map((row) => {
    const nameCell = row.querySelector(cl.name_css || 'td:nth-child(1)')
    let title = nameCell ? label(nameCell) : ''
    const coinsInput = row.querySelector(cl.coins_input_css || 'input[placeholder="Coins"]')
    const coins = coinsInput ? Number(coinsInput.value) || 0 : 0
    if (coins) {
      const suffix = new RegExp(`\\s*${coins}\\s*$`)
      if (suffix.test(title)) title = title.replace(suffix, '')
    }
    const stateCell = row.querySelector(cl.state_css || 'td:nth-child(3)')
    return { title: title.trim(), coins, state: stateCell ? label(stateCell) : '' }
  }).filter((r) => r.title)
}

// --- driving the form ------------------------------------------------------

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function waitFor(fn, { ms = 15000, every = 150, what = 'something' } = {}) {
  const until = Date.now() + ms
  for (;;) {
    const got = fn()
    if (got) return got
    if (Date.now() > until) throw new Error(`gave up waiting for ${what}`)
    await sleep(every)
  }
}

/**
 * Click something the way a person does.
 *
 * `el.click()` alone dispatches ONE `click` event and nothing else, which is why the
 * Create button did nothing: plenty of components bind their handler to `pointerdown` or
 * `mousedown` instead, and those never fired. Sending the whole sequence covers every
 * case, and costs nothing when the component really did only want `click`.
 */
function realClick(el) {
  const r = el.getBoundingClientRect()
  const at = {
    bubbles: true,
    cancelable: true,
    composed: true,
    view: window,
    clientX: Math.round(r.left + r.width / 2),
    clientY: Math.round(r.top + r.height / 2),
  }
  try { el.focus({ preventScroll: true }) } catch { /* not focusable, fine */ }
  el.dispatchEvent(new PointerEvent('pointerover', at))
  el.dispatchEvent(new PointerEvent('pointerenter', at))
  el.dispatchEvent(new MouseEvent('mouseover', at))
  el.dispatchEvent(new PointerEvent('pointerdown', { ...at, button: 0, isPrimary: true }))
  el.dispatchEvent(new MouseEvent('mousedown', { ...at, button: 0 }))
  el.dispatchEvent(new PointerEvent('pointerup', { ...at, button: 0, isPrimary: true }))
  el.dispatchEvent(new MouseEvent('mouseup', { ...at, button: 0 }))
  // Exactly ONE click. This used to dispatch a synthetic click event AND call el.click(),
  // which fired the site's handler twice — two identical create requests a millisecond
  // apart. They both failed so it went unnoticed, but a success would have produced two
  // chapters, and removing one here means retyping its name to confirm. el.click() alone
  // runs the element's activation behaviour, which is what a real click does.
  try {
    el.click()
  } catch {
    el.dispatchEvent(new MouseEvent('click', { ...at, button: 0 }))
  }
}

/**
 * @param {object} [opts]
 * @param {boolean} [opts.allowDisabled] Click even if the control is disabled.
 *   Off by default, and that default matters: a synthetic event dispatched straight at an
 *   element reaches its listeners even when the element is disabled, so clicking blindly
 *   drives a control the site is deliberately holding shut. That is what broke the first
 *   real post — a Submit disabled because the name had not registered was clicked anyway,
 *   and the server rejected a chapter with no name.
 */
async function click(el, what, { allowDisabled = false } = {}) {
  if (!el) throw new Error(`could not find ${what}`)
  if (el.disabled && !allowDisabled) {
    // Briefly disabled is normal — a Save button often is while a field settles. Still
    // disabled after a few seconds means the site is refusing, and pressing on would drive
    // a control it is deliberately holding shut.
    try {
      await waitFor(() => !el.disabled, { ms: 4000, what: `${what} to become usable` })
    } catch {
      throw new Error(`${what} stayed disabled — the site is not ready for it`)
    }
  }
  el.scrollIntoView({ block: 'center' })
  realClick(el)
  await sleep(150)
}

/**
 * Put text into a field so the page's own framework notices.
 *
 * Setting `.value` alone shows the text but leaves Vue's bound state untouched, so the
 * site still believes the field is empty — the text is visibly there and Submit stays
 * disabled. The key events are what a real keyboard produces, and `blur` is what triggers
 * validation on most forms.
 */
async function typeInto(el, text) {
  el.focus()
  el.select?.()

  // `execCommand('insertText')` first. It is the one programmatic way to enter text that
  // produces a real `beforeinput`/`input` pair from the browser itself, so every framework
  // honours it — whereas assigning `.value` and dispatching a synthetic `input` puts the
  // text on screen without necessarily reaching the component's state. That distinction is
  // what cost chapter 62 twice: the box visibly read "Chapter 62" while the site submitted
  // an empty name, and it could not be caught by checking `.value` or whether Submit was
  // enabled, because both looked right.
  let inserted = false
  try {
    inserted = document.execCommand('insertText', false, text)
  } catch {
    inserted = false
  }
  if (!inserted || el.value !== text) {
    setInputValue(el, text)
  }

  // No synthetic `blur` here. It was added on the theory that blurring triggers
  // validation, but a Headless UI dialog traps focus and can treat focus leaving as a
  // dismissal — which would close the dialog before Submit is even clicked, leaving the
  // click to land on the page underneath. `insertText` already updates the component's
  // state, so the blur bought nothing and risked exactly this.
  await sleep(250)
  if (el.value !== text) {
    throw new Error(`the name box holds "${el.value}" instead of "${text}"`)
  }
  return inserted ? 'typed' : 'assigned'
}

// Is the dialog still there? Checked immediately before submitting, because a dialog that
// has already closed turns the Submit click into a click on whatever is behind it — which
// looks exactly like a handler that silently did nothing.
function dialogOpen(panel, nameInput) {
  return document.contains(nameInput) && visible(nameInput)
    && (!panel || document.contains(panel))
}

function isBlocked(el) {
  // Vue components often mark a button unusable with aria-disabled and a no-op handler
  // rather than the disabled attribute, so checking only `.disabled` misses it.
  return el.disabled || el.getAttribute('aria-disabled') === 'true'
}

/**
 * Submit the new-chapter dialog, escalating until something actually happens.
 *
 * A plain click is what a person does, so it is tried first. If the dialog is still
 * sitting there afterwards the click did not take, and `requestSubmit` (the correct
 * programmatic way to submit a form, validation included) and then Enter are worth trying
 * before giving up. Each step says what it did, so the log shows which one the site wants.
 */
async function submitDialog(panel, nameInput, adapter, log) {
  const wanted = adapter.create_dialog.submit_text.toLowerCase()
  const find = () => [...panel.querySelectorAll(CLICKABLE)]
    .filter(visible)
    .find((b) => label(b).toLowerCase().includes(wanted))

  if (!dialogOpen(panel, nameInput)) {
    throw new Error('the dialog closed before it could be submitted')
  }

  const button = find()
  if (button) {
    log(`    submitting via "${label(button)}" inside the dialog`
      + (isBlocked(button) ? ' (marked unusable — trying anyway)' : ''))
    button.scrollIntoView({ block: 'center' })
    realClick(button)
    await sleep(900)
    if (!dialogOpen(panel, nameInput)) return 'clicked'

    const form = button.closest('form')
    if (form) {
      log('    still open — submitting the form directly')
      try {
        form.requestSubmit(button.type === 'submit' ? button : undefined)
      } catch {
        form.requestSubmit()
      }
      await sleep(900)
      if (!dialogOpen(panel, nameInput)) return 'requestSubmit'
    }
  } else {
    log('    no Submit inside the dialog')
  }

  log('    still open — pressing Enter in the name box')
  nameInput.focus()
  for (const type of ['keydown', 'keypress', 'keyup']) {
    nameInput.dispatchEvent(new KeyboardEvent(type, {
      key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true,
    }))
  }
  await sleep(900)
  return dialogOpen(panel, nameInput) ? 'nothing worked' : 'enter'
}

// Every short piece of visible text on the page, for spotting what a toast said. The site
// answers both success and failure with a notification, and until now the run read neither
// — so a rejected submission looked identical to a silent no-op.
function textSnapshot() {
  const out = new Set()
  for (const el of document.querySelectorAll('div, span, p, small, li, strong')) {
    if (el.children.length) continue
    const t = label(el)
    if (t && t.length < 160) out.add(t)
  }
  return out
}

function newTextSince(before) {
  return [...textSnapshot()].filter((t) => !before.has(t))
}

function clickable(text, what) {
  const hit = byText(text)[0]
  if (!hit) throw new Error(`could not find ${what || `a control labelled "${text}"`}`)
  return hit
}

// A field is found by its PRINTED LABEL, then the control inside that label's container.
// It cannot be found by the control's own text, because a Headless UI listbox button
// shows its current VALUE ("Notes"), not what it is for ("Type") — and it cannot be found
// by id, because every id here carries a per-render random prefix.
const FIELD_CONTROL = '[id*="listbox-button"], [role="switch"], input, [aria-haspopup]'

// An element's OWN text, ignoring text inside its children. A field label is sometimes
// written as `<label>Type<span>Notes</span></label>`, whose full text reads "TypeNotes" and
// which is not a leaf — so matching only leaf elements on their whole text misses it.
function ownText(el) {
  return [...el.childNodes]
    .filter((n) => n.nodeType === 3)
    .map((n) => n.textContent)
    .join('')
    .replace(/\s+/g, ' ')
    .trim()
}

function fieldContainer(labelText) {
  const want = String(labelText).trim().toLowerCase()
  const labels = [...document.querySelectorAll('div, span, label, p, h3, h4, dt, dd')]
    .filter((e) => ownText(e).toLowerCase() === want)
  for (const el of labels) {
    for (let n = el.parentElement, i = 0; n && i < 5; n = n.parentElement, i++) {
      if (n.querySelector(FIELD_CONTROL)) return n
    }
  }
  return null
}

// What the chapter form actually looks like, for when a field cannot be found. Reporting
// this beats guessing at the structure a third time.
function describeDetails(adapter) {
  const d = adapter.details || {}
  const lines = ['--- the chapter form as it stands ---', `url: ${location.href}`]
  for (const name of [d.type_label, d.state_label, d.status_label, d.paid_label]) {
    if (!name) continue
    const box = fieldContainer(name)
    const control = box && box.querySelector(FIELD_CONTROL)
    lines.push(`label "${name}": ${box ? 'container found' : 'NOT FOUND'}`
      + (control ? ` · control ${cssPath(control)} reads "${label(control).slice(0, 30)}"` : ''))
  }
  const boxes = [...document.querySelectorAll('[id*="listbox-button"], [aria-haspopup]')]
    .filter(visible)
  lines.push(`dropdowns on the page: ${boxes.length}`)
  for (const b of boxes.slice(0, 8)) lines.push(`  reads "${label(b).slice(0, 30)}"  ${cssPath(b)}`)
  const switches = [...document.querySelectorAll('[role="switch"]')].filter(visible)
  lines.push(`switches: ${switches.length}`)
  lines.push('Copy this back to Night Reader.')
  return lines.join('\n')
}

function listboxFor(labelText) {
  const box = fieldContainer(labelText)
  return box ? box.querySelector('[id*="listbox-button"], [aria-haspopup]') : null
}

function valueOf(labelText) {
  const b = listboxFor(labelText)
  return b ? label(b).trim() : null
}

async function setListbox(labelText, want) {
  const button = listboxFor(labelText)
  if (!button) throw new Error(`no dropdown found for "${labelText}"`)
  if (label(button).trim().toLowerCase() === String(want).toLowerCase()) return 'already'
  await click(button, `the "${labelText}" dropdown`)
  const options = await waitFor(
    () => {
      const opts = [...document.querySelectorAll('[role="option"]')].filter(visible)
      return opts.length ? opts : null
    },
    { what: `the "${labelText}" options to open` },
  )
  const option = options.find((o) => label(o).trim().toLowerCase() === String(want).toLowerCase())
  if (!option) {
    const seen = options.map((o) => label(o).trim()).join(', ')
    await click(button, 'to close the dropdown')  // leave the form as we found it
    throw new Error(`"${labelText}" has no option "${want}" — it offers: ${seen}`)
  }
  await click(option, `the "${want}" option`)
  await waitFor(() => label(button).trim().toLowerCase() === String(want).toLowerCase(),
                { ms: 3000, what: `"${labelText}" to read "${want}"` })
  return 'set'
}

function setInputValue(el, value) {
  // The site is Nuxt/Vue (the page root is #__nuxt), so `v-model` listens for `input` and
  // reads el.value back — meaning the value must be set BEFORE the event is fired, and the
  // event must bubble. Going through the prototype's setter rather than assigning directly
  // costs nothing here and is also what a React-controlled input would require, so this
  // works either way if the site is ever rebuilt.
  const setter = Object.getOwnPropertyDescriptor(el.constructor.prototype, 'value')?.set
  if (setter) setter.call(el, String(value))
  else el.value = String(value)
  el.dispatchEvent(new Event('input', { bubbles: true }))
  el.dispatchEvent(new Event('change', { bubbles: true }))
}

async function setPaid(coins, adapter) {
  const labelText = adapter?.details?.paid_label || 'Paid Chapter'
  const box = fieldContainer(labelText)
  if (!box) throw new Error(`no "${labelText}" field found`)
  const sw = box.querySelector('[role="switch"], input[type="checkbox"]')
  if (!sw) throw new Error(`no toggle inside "${labelText}"`)
  const isOn = () => {
    const a = sw.getAttribute('aria-checked')
    if (a != null) return a === 'true' || a === '1'
    return !!sw.checked
  }
  const wantOn = Number(coins) > 0
  if (isOn() !== wantOn) await click(sw, `the "${labelText}" toggle`)
  if (!wantOn) return { paid: false, coins: 0 }
  const input = await waitFor(() => box.querySelector('input[type="number"]'),
                              { ms: 4000, what: 'the coins box to appear' })
  setInputValue(input, coins)
  await sleep(150)
  return { paid: true, coins: Number(input.value) }
}

// Read the three fields back rather than trusting that setting them worked. Each is wrong
// by default and each costs something real: Type left as "Pages" is the wrong kind of
// chapter, State left as "Private" is invisible to readers, and a paid chapter posted free
// is revenue gone and awkward to claw back once people have read it.
function readBackDetails(adapter, want) {
  const d = adapter.details
  const got = {
    type: valueOf(d.type_label),
    state: valueOf(d.state_label),
    coins: (() => {
      const box = fieldContainer(d.paid_label)
      const input = box && box.querySelector('input[type="number"]')
      return input ? Number(input.value) : 0
    })(),
  }
  const wrong = []
  if (got.type !== want.type) wrong.push(`Type is "${got.type}", wanted "${want.type}"`)
  if (got.state !== want.state) wrong.push(`State is "${got.state}", wanted "${want.state}"`)
  if (Number(got.coins) !== Number(want.coins)) {
    wrong.push(`Coins is ${got.coins}, wanted ${want.coins}`)
  }
  return { got, wrong }
}

async function saveAndConfirm(adapter, what) {
  const savedText = (adapter.details?.saved_text || 'saved').toLowerCase()
  await click(clickable(adapter.details?.save_text || 'Save', `the ${what} Save button`),
              `the ${what} Save button`)
  await waitFor(
    () => [...document.querySelectorAll('div, span, p')]
      .some((e) => !e.children.length && label(e).toLowerCase().includes(savedText)),
    { ms: 20000, what: 'the "saved" confirmation' },
  )
  await sleep(250)
}

/**
 * NOT USED for posting any more — kept as a fallback if the API ever changes shape, and
 * because its field-by-field read-back is the only other way to verify a save. The live
 * path is postViaApi.
 *
 * Post one chapter by driving the form, end to end.
 *
 * `state` is deliberately a parameter: the first real run uses "Private" so the chapter is
 * invisible to readers while the form mapping is still being confirmed. Deleting a chapter
 * here means retyping its name, so nothing is created until the payload is in hand, and a
 * failure reports the chapter's title so a retry can continue into the shell that already
 * exists rather than making a second one.
 */
async function postOne({ payload, coins, adapter, state }) {
  const want = { type: adapter.details.type_value, state, coins: Number(coins) || 0 }
  say(`\n## ${payload.title}  (${want.state}, ${want.coins ? want.coins + ' coins' : 'free'})`)

  say('1/8 opening the new-chapter dialog')
  let nameInput, panel
  try {
    ({ box: nameInput, panel } = await openCreateDialog(adapter, say))
  } catch (e) {
    // Guessing at this twice was enough. Report what the page actually holds, so the log
    // says what to target rather than inviting a third guess.
    say(`\n${describeDialog()}`)
    throw e
  }

  say(`2/8 naming it "${payload.title}"`)
  const how = await typeInto(nameInput, payload.title)
  say(`    entered by ${how === 'typed' ? 'real key input' : 'value assignment (fallback)'}`)

  const urlBefore = location.href
  const textBefore = textSnapshot()
  const netFrom = Date.now()
  const how2 = await submitDialog(panel, nameInput, adapter, say)
  say(`    submitted by: ${how2}`)
  if (how2 === 'nothing worked') {
    say('    the dialog is still open, so nothing was submitted and nothing created')
    const inside = [...panel.querySelectorAll(CLICKABLE)].filter(visible)
    say(`    buttons in the dialog (${inside.length}):`)
    for (const b of inside) {
      say(`      "${label(b)}" tag=${b.tagName.toLowerCase()} type=${b.type || '-'}`
        + ` disabled=${b.disabled} aria-disabled=${b.getAttribute('aria-disabled')}`)
    }
    throw new Error('the dialog would not submit')
  }

  say('3/8 waiting for the chapter form')
  // Submitting creates the chapter on the server and then moves to its own page, so this
  // is a round trip, not a render. Waiting for the URL to change first separates "the
  // chapter was never created" from "it was created but the form looks different than
  // expected" — two very different problems that both used to surface as one timeout.
  try {
    await waitFor(() => location.href !== urlBefore && /\/chapter\//.test(location.href),
                  { ms: 25000, every: 200, what: 'the new chapter to open' })
  } catch {
    say('    the page never moved to a chapter — the chapter may not have been created')
    // The site answers with a notification either way, so whatever text appeared after the
    // click is the site's own account of what went wrong. Reading it beats inferring.
    const said = newTextSince(textBefore)
    say(said.length
      ? `    the site then said: ${said.slice(0, 8).map((t) => `"${t}"`).join(' · ')}`
      : '    the site said nothing at all — no notification appeared')
    // The decisive line. Whether a request was made separates "the click never reached
    // the handler" from "it ran and the server refused", which the DOM cannot tell us.
    say(`\n--- network since Submit ---\n${describeNet(netFrom)}`)
    say(`\n${describeDetails(adapter)}`)
    throw new Error('Submit did not open a chapter page')
  }
  say(`    on ${location.pathname}`)
  try {
    await waitFor(() => listboxFor(adapter.details.type_label),
                  { ms: 25000, every: 200, what: 'the Details form' })
  } catch (e) {
    say(`\n${describeDetails(adapter)}`)
    throw e
  }
  // The form mounts its fields progressively; a moment here costs nothing and avoids
  // setting a dropdown that is about to be re-rendered underneath us.
  await sleep(600)

  say(`4/8 Type -> ${want.type}, State -> ${want.state}`)
  await setListbox(adapter.details.type_label, want.type)
  await setListbox(adapter.details.state_label, want.state)
  const paid = await setPaid(want.coins, adapter)
  say(`    paid: ${paid.paid ? `${paid.coins} coins` : 'free'}`)

  const before = readBackDetails(adapter, want)
  if (before.wrong.length) throw new Error(`before saving — ${before.wrong.join('; ')}`)

  say('5/8 saving the form')
  await saveAndConfirm(adapter, 'chapter')
  const after = readBackDetails(adapter, want)
  if (after.wrong.length) throw new Error(`after saving — ${after.wrong.join('; ')}`)
  say('    fields read back correctly')

  say('6/8 opening the page editor')
  await click(clickable(adapter.pages.nav_text, 'the Pages tab'), 'Pages')
  await sleep(500)
  await waitFor(() => byText(adapter.pages.write_text).length,
                { what: 'the Write Page button' })
  await click(clickable(adapter.pages.write_text, 'the Write Page button'), 'Write Page')
  const editor = await waitFor(() => document.querySelector(adapter.pages.editor_css),
                               { what: 'the editor' })
  await sleep(400)

  say('7/8 pasting the chapter')
  const counterBefore = findCounter()
  pasteHtml(editor, payload.html, payload.text)
  await sleep(500)
  const counterAfter = findCounter()
  const moved = counterBefore && counterAfter && counterAfter.used > counterBefore.used
  if (!moved && !(editor.innerText || '').trim()) {
    throw new Error('the paste did not land — the chapter would have saved empty')
  }
  say(`    editor now holds ${counterAfter ? counterAfter.used : '?'} characters`)
  await click(clickable(adapter.pages.submit_text, 'the editor Submit button'), 'Submit')

  say('8/8 saving the page')
  await saveAndConfirm(adapter, 'page')
  say(`\nDONE — ${payload.title} is on the site as ${want.state}.`)
  return { title: payload.title, chars: counterAfter ? counterAfter.used : null }
}

// Opens the new-chapter dialog and reports what is in it, without submitting. Creating a
// chapter here costs a manual delete (the site makes you retype its name), so this exists
// to get the dialog's real structure without paying that.
async function probeDialog() {
  startLog('# Dialog probe — opens the dialog, submits nothing')
  try {
    const adapter = await ask('adapter', { site: 'meiko' })
    // Exercises the same openCreateDialog the real run uses, so a pass here means step 1
    // of a post will behave identically.
    const { box, panel } = await openCreateDialog(adapter, say)
    say(`\nname box: ${cssPath(box)}`)
    say(`placeholder="${box.placeholder || ''}" tag=${box.tagName.toLowerCase()}`)
    // The buttons INSIDE the panel are the only ones safe to click: pressing one outside
    // it counts as a click-away and closes the dialog without submitting.
    say(`\npanel: ${cssPath(panel)}`)
    const inside = [...panel.querySelectorAll(CLICKABLE)].filter(visible)
    say(`buttons inside the panel (${inside.length}):`)
    for (const b of inside) {
      say(`  "${label(b)}" disabled=${b.disabled}  ${cssPath(b)}`)
    }
    say('\nThat worked. Close the dialog — nothing was submitted.')
  } catch (e) {
    say(`\nfailed: ${e.message}`)
    say(`\n${describeDialog()}`)
  }
}

// --- the panel -------------------------------------------------------------

let panel, out, busy = false
let ctx = { found: false }

function render() {
  if (out) out.textContent = LOG.join('\n')
}

function ask(kind, payload) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage({ kind, payload }, (res) => {
      if (!res) return reject(new Error('the extension lost contact with Night Reader'))
      res.ok ? resolve(res.data) : reject(new Error(res.error))
    })
  })
}

// One chapter, live, left Private so no reader sees it while the form mapping is still
// being confirmed. Whatever happens is reported back so the ledger knows — including a
// failure, whose title is what lets a retry continue into the shell already on the site
// instead of creating a second one to delete by hand.
// An expired session reads as an ordinary refusal, which would send someone hunting for a
// bug in the payload. The token is learned from the page's own traffic, so a refresh gets a
// new one.
function isAuthFailure(message) {
  return /\b(401|403|unauthor|forbidden|expired|invalid token)\b/i.test(message || '')
}

// --- runs Night Reader asked for -------------------------------------------
//
// Until now the buttons in this panel were the only way to start posting, which meant the
// app could show you what a run would do but never begin one. The app now writes the
// request to a file and this asks for it on a timer, so a run can be started from the
// Posting page — and from a phone, since the request waits on disk rather than in the tab
// that asked for it.
//
// The claim is what stops two tabs posting the same chapter twice: the server hands a
// queued run out ONCE, and this tab proves on every later report that the run is still the
// one it took.

// Per tab, and surviving a reload of it — sessionStorage does both. Shared across tabs it
// would let a second tab report progress into a run it never claimed.
const TOKEN_KEY = 'nr.browserToken'
const TOKEN = (() => {
  try {
    let existing = sessionStorage.getItem(TOKEN_KEY)
    if (!existing) {
      existing = `tab-${Math.random().toString(36).slice(2, 10)}`
      sessionStorage.setItem(TOKEN_KEY, existing)
    }
    return existing
  } catch {
    // Blocked storage costs the reload-resume, not the run.
    return `tab-${Math.random().toString(36).slice(2, 10)}`
  }
})()

// How often to ask for work. Matched to what the app tells the user it is, so the two
// cannot drift into the page promising a 30-second wait that is really two minutes.
const CLAIM_EVERY_MS = 30000

let adapterCache = null
async function adapterOnce() {
  if (!adapterCache) adapterCache = await ask('adapter', { site: 'meiko' })
  return adapterCache
}

function setWatch(text, ok) {
  const el = panel?.querySelector('.nr-watch')
  if (!el) return
  el.textContent = text || ''
  el.className = `nr-watch${ok ? ' nr-ok' : ''}`
}

/**
 * Tell the app which chapters the site already has.
 *
 * Python cannot read the site, so without this the plan knows only what this app itself
 * posted — and a series being published by hand long before Night Reader existed reads as
 * entirely unposted. One real series showed 105 chapters "ready" when every one was live.
 *
 * Only sent when it has changed, because this is called on a timer and rewriting the same
 * list every thirty seconds for every open tab is pure churn.
 */
let lastPushed = ''

async function pushSiteList(rows) {
  if (!ctx.found || !rows?.length) return 0
  const titles = rows.map((r) => r.title)
  // The coin value of each PAID chapter, which the site export carries nowhere. It is only
  // visible here, so sending it is how the app comes to know what a series charges without
  // the number being typed in once per series.
  //
  // Taken from the site's own chapter records rather than from the coins box in the row,
  // because the box does not draw the distinction that matters: a FREE chapter still
  // carries a coin value (paid 0 alongside coins 1, observed on a real series), so reading
  // the box alone had a mostly-free series priced at 1 coin. A row whose record this tab
  // has not seen contributes nothing rather than a guess — write_site keeps whatever an
  // earlier reading learned, so silence is safe and a wrong number is not.
  const prices = rows.map((r) => {
    const rec = CHAPTERS.get(String(r.title || '').trim().toLowerCase())
    if (!rec) return 0
    const isPaid = rec.paid === true || Number(rec.paid) === 1
    return isPaid ? Number(rec.coins) || 0 : 0
  }).filter((n) => n > 0)
  const signature = JSON.stringify([titles, prices])
  if (signature === lastPushed) return titles.length
  try {
    await ask('site', { sid: ctx.sid, targetId: ctx.target_id, titles, prices })
    lastPushed = signature
    return titles.length
  } catch {
    // Syncing the list is a convenience for the app; never let it interrupt a run or
    // shout at the user from a page they were only passing through.
    return 0
  }
}

/**
 * Which series this page belongs to, re-asked whenever the route changes.
 *
 * meiko is a Nuxt app, so clicking from the studio list into a novel changes the URL
 * WITHOUT reloading the page. The content script is not re-run, and the one startup call
 * to `match` never happened again — so entering anywhere but a series page left the panel
 * stuck on "no series for this page" however many novels you clicked through, and the
 * claim poll, gated on that first answer, never started at all.
 *
 * Polled rather than hooked: `popstate` only fires for back and forward, and patching
 * `history.pushState` has to happen in the page's own context, which this script is not
 * in. Comparing a string once a second costs nothing and catches every kind of
 * navigation.
 */
let identifiedUrl = ''

async function identify() {
  const url = location.href
  identifiedUrl = url
  const status = panel?.querySelector('.nr-status')
  try {
    const m = await ask('match', { url })
    // Navigated again while that was in flight: the answer describes the previous page.
    if (identifiedUrl !== url) return
    ctx = m
    // A different series is a different chapter list, so nothing about the last one should
    // suppress the next push.
    lastPushed = ''
    if (status) {
      status.textContent = m.found
        ? `${m.name}${m.resolved ? '' : ' (numbering not resolved)'}`
        : 'no series publishes to this page'
      // The URL it actually tried, so a mismatch is self-diagnosing instead of being a
      // red label with nothing behind it.
      status.title = m.found ? url : `Night Reader matched no series against ${url}`
      status.className = `nr-status ${m.found && m.resolved ? 'nr-ok' : 'nr-bad'}`
    }
    if (m.found && m.resolved) checkForWork()
  } catch (e) {
    ctx = { found: false }
    if (status) {
      status.textContent = e.message
      status.title = url
      status.className = 'nr-status nr-bad'
    }
  }
}

function watchLocation() {
  setInterval(() => {
    // Never mid-run: the run owns ctx, and it pins its own series anyway.
    if (busy || location.href === identifiedUrl) return
    identify()
  }, 1000)
}

/**
 * Ask whether Night Reader has queued a run for this series, and if so, do it.
 *
 * Gated on this page actually having the chapter list. The server hands a queued run out
 * ONCE, so a tab sitting on a single chapter would take the run and then fail for want of
 * the list it needs to avoid double-posting — leaving the app waiting on a browser that
 * had already given up. Better to leave the run for whichever tab can do it.
 */
async function checkForWork() {
  if (busy || !ctx.found || !ctx.resolved) return
  try {
    const rows = scrapeChapterList(await adapterOnce())
    pushSiteList(rows)
    const titles = rows.map((r) => r.title)
    if (!titles.length) {
      setWatch('open the chapter list to post')
      return
    }
    const { run } = await ask('claim', { url: location.href, token: TOKEN })
    if (!run) {
      setWatch(`watching · ${ctx.name}`, true)
      return
    }
    setWatch(`starting ${run.progress?.total || 0}`, true)
    await postBatch({ run })
  } catch (e) {
    // Includes the app being down, which is ordinary: it is a desktop app the user closes.
    setWatch(isAuthFailure(e.message) ? 'site session expired' : 'app unreachable')
  }
}

function watchForRuns() {
  // Unconditional, and started once. It used to be started only if the page happened to
  // match a series at load, which on a client-routed site meant a tab entering on the
  // studio list never watched for work no matter where it went next. checkForWork bails
  // on its own when there is no series here, and identify() kicks an immediate check the
  // moment a navigation lands on one.
  setInterval(checkForWork, CLAIM_EVERY_MS)
}

/**
 * Work out which chapters to post, in order.
 *
 * Three things decide it: the plan says what is finished, the site's live chapter list says
 * what is already up (including anything posted by hand, which no local record knows), and
 * a pending record says whether a chapter was created but never completed. The pending one
 * goes first — it is already on the site, so the duplicate check would skip it forever and
 * leave it half-done.
 */
function buildQueue(plan, adapter, count, upTo) {
  const onSite = scrapeChapterList(adapter)
  if (!onSite.length) {
    throw new Error('No chapter list on this page. Open the series\' own page (the one '
      + 'listing its chapters) rather than a single chapter, so the already-posted '
      + 'chapters can be read off it.')
  }
  const already = new Set(onSite.map((r) => r.title.toLowerCase()))
  const held = plan.items.filter((i) => i.blockers.length)
  const ready = plan.items.filter((i) => !i.blockers.length)
  let fresh = ready.filter((i) => !already.has(i.title.toLowerCase()))
  // "up to chapter N" is a truer way to say what to post than a count: it names the
  // finishing line, so a run can be repeated without working out how many are left.
  // Rows with no global number are side stories, which no numeric range should sweep up.
  if (upTo) fresh = fresh.filter((i) => i.global != null && i.global <= upTo)

  const pending = readPending()
  const resumable = pending && ready.find((i) => i.title === pending.title)
  const queue = resumable && !fresh.some((i) => i.title === resumable.title)
    ? [resumable, ...fresh]
    : fresh

  return {
    onSite, held, ready, resumable,
    queue: queue.slice(0, Math.max(1, count)),
    skipped: ready.length - fresh.length,
    waiting: Math.max(0, queue.length - Math.max(1, count)),
  }
}

/**
 * Post a batch of chapters, one after another.
 *
 * Two ways in. The panel buttons pass a count and a state directly; a run claimed from
 * Night Reader passes the whole request, and then the app is where progress shows and
 * where Cancel comes from. Everything after the options are resolved is identical, so
 * there is one posting path rather than two to keep in step.
 *
 * Serial on purpose. These are real paid chapters on a live site, so the run stops at the
 * first thing that does not go exactly right rather than pressing on — whatever is wrong
 * with one chapter is likely wrong with the next thirty, and a batch that ploughs through
 * leaves a mess that has to be undone by retyping chapter names.
 */
async function postBatch({ count, state, upTo, run = null }) {
  if (busy) return
  busy = true
  startLog(run ? '# posting run (started from Night Reader)' : '# posting run')
  const posted = []
  let stopped = null
  let current = null
  let cancelled = false

  // A run names its own terms. "At most" absent means everything that is ready, which is
  // what "post continuously" has to mean for a backlog nobody wants to count.
  const opts = run?.options || {}
  const want = run
    ? {
      count: opts.limit || 9999,
      upTo: opts.up_to || 0,
      state: opts.post_state === 'private' ? 'Private' : 'Public',
      start: opts.start ?? null,
      end: opts.end ?? null,
    }
    : { count, upTo, state, start: null, end: null }

  // Every report doubles as the heartbeat the app watches, and its answer is how a Cancel
  // pressed in the app reaches this tab — the same cooperative stop a translation uses,
  // polled instead of pushed. A report that cannot be delivered never stops a run: the
  // chapter is already on the site by then, and the ledger is the durable record.
  const report = async (event) => {
    if (!run) return false
    try {
      const res = await ask('progress', {
        sid: ctx.sid, targetId: ctx.target_id, token: TOKEN, event,
      })
      return !!res.cancelled
    } catch {
      return false
    }
  }

  try {
    if (!ctx.found) throw new Error('No Night Reader series publishes to this page. Set '
      + 'its publish target in the app first.')
    if (!ctx.resolved) throw new Error(`"${ctx.name}" has no resolved chapter numbering `
      + 'yet — open it in Night Reader once.')
    say(`# ${ctx.name}`)
    // Pin the series this run belongs to. uidsFromUrl reads the CURRENT url every time a
    // chapter is created, so a tab that navigated mid-run would carry on making chapters
    // under whatever novel it had moved to - on a live site, under the wrong series.
    const pinned = uidsFromUrl()

    const [adapter, plan] = await Promise.all([
      adapterOnce(),
      ask('plan', {
        sid: ctx.sid, targetId: ctx.target_id, start: want.start, end: want.end,
      }),
    ])
    const q = buildQueue(plan, adapter, want.count, want.upTo)
    // Send the app the list this run just read, so its counts stop guessing. Through the
    // same path as the idle push, so one reading does not get sent twice.
    pushSiteList(q.onSite)

    say(`${q.onSite.length} already on the site, newest "${q.onSite[0].title}"`)
    // The plan now knows what is on the site too, via the snapshot this pushes, so the
    // already-posted ones arrive as blocked rather than as ready-but-skipped. Counting
    // them in both places would report them twice and inflate "not finished".
    const done = q.held.filter((i) => i.blockers.some((b) => b.startsWith('already')))
    const unfinished = q.held.filter((i) => !done.includes(i))
    say(`${plan.items.length} in the series · ${done.length + q.skipped} already posted`
      + ` · ${unfinished.length} not finished · ${q.queue.length + q.waiting} to go`)
    for (const i of unfinished.slice(0, 3)) {
      say(`  held: ${i.title} — ${i.blockers.join('; ')}`)
    }
    if (q.resumable) say(`resuming "${q.resumable.title}", created earlier but not finished`)

    if (!q.queue.length) {
      // For a run this is an answer, not a failure: the app planned from what it knew, and
      // the site says there is nothing left. The site wins, so the run is simply done.
      if (!run) throw new Error('every finished chapter is already on the site')
      say('\nevery finished chapter is already on the site — nothing to do')
    } else {
      say(`\nposting ${q.queue.length} as ${want.state}: `
        + q.queue.map((i) => i.title).join(', '))
      if (q.waiting) say(`(${q.waiting} more are ready and will wait for the next run)`)
      // Correct the app count before the first chapter. It planned from the snapshot,
      // which can be behind the list just read off the page, and a run that finishes at
      // "3 of 5" reads like it gave up when it had in fact done everything there was.
      await report({
        kind: 'queue',
        total: q.queue.length,
        first: q.queue[0].title,
        last: q.queue[q.queue.length - 1].title,
      })

      for (const [n, item] of q.queue.entries()) {
        if (await report({ kind: 'current', title: item.title })) {
          cancelled = true
          say('\nStopped from Night Reader. Nothing after this was attempted.')
          break
        }
        const here = uidsFromUrl()
        if (here.series_uid !== pinned.series_uid) {
          throw new Error('the tab moved to a different series part-way through, so this '
            + 'stopped rather than create a chapter under the wrong novel')
        }
        current = item
        setWatch(`posting ${n + 1}/${q.queue.length}`, true)
        say(`\n———— ${n + 1} of ${q.queue.length} ————`)
        const payload = await ask('payload', {
          sid: ctx.sid, projectId: item.project_id, index: item.index,
        })
        const done = await postViaApi({ payload, coins: item.coins, state: want.state })
        await ask('result', {
          sid: ctx.sid, target_id: ctx.target_id, title: payload.title,
          project_id: item.project_id, index: item.index, global: item.global,
          status: 'posted', price_coins: item.coins, remote_url: location.href,
          // The site's own id for it. Without this, changing a chapter later depends on
          // matching it by name against a list the page happens to have fetched.
          remote_id: done.id || '',
        })
        posted.push({ title: payload.title, coins: item.coins, chars: done.chars })
        current = null
        await report({
          kind: 'posted', title: payload.title, global: item.global, coins: item.coins,
        })
        // A short gap between chapters. The API is quick enough to hammer, and a burst of
        // writes is the kind of thing that attracts rate limiting on someone's live account.
        if (n < q.queue.length - 1) await sleep(1500)
      }
    }
    if (!cancelled) await report({ kind: 'finished' })
  } catch (e) {
    stopped = e.message
    if (current) {
      try {
        await ask('result', {
          sid: ctx.sid, target_id: ctx.target_id, title: current.title,
          project_id: current.project_id, index: current.index, global: current.global,
          status: 'needs-attention', price_coins: current.coins, error: e.message,
        })
      } catch { /* the app may be down; the report below is still the record */ }
    }
    await report({ kind: 'failed', title: current?.title || '', error: e.message })
  } finally {
    busy = false
    setWatch(ctx.found ? `watching · ${ctx.name}` : '', ctx.found)
  }

  // --- what actually went out ---------------------------------------------
  //
  // The site's own list cannot show these until the page reloads, so this summary IS the
  // confirmation. It is the last thing written, so it survives at the bottom of the report.
  say(`\n${'='.repeat(46)}`)
  if (posted.length) {
    say(`POSTED ${posted.length} chapter${posted.length === 1 ? '' : 's'} as ${want.state}:`)
    for (const p of posted) {
      say(`  ✓ ${p.title}  ${p.coins ? `${p.coins} coins` : 'free'}  ${p.chars} chars`)
    }
  } else {
    say('POSTED nothing.')
  }
  if (stopped) {
    say(`\nSTOPPED${current ? ` on ${current.title}` : ''}: ${stopped}`)
    if (isAuthFailure(stopped)) {
      say('That looks like the site session expiring. Refresh the page and run it again —')
      say('the token comes from the page\'s own traffic, so a refresh renews it.')
    } else if (current) {
      say('It was created but not finished, and is recorded so the next run continues into')
      say('it rather than making a second one.')
    }
    say('Nothing after it was attempted.')
    if (run) say('Night Reader has it as stopped, and can queue it again from the Posting page.')
  }
  if (posted.length && want.state !== 'Public') {
    say('\nThese are PRIVATE, so no reader can see them yet.')
  }
  if (posted.length) {
    // A run reloads nothing. The claim is handed out once, so a reload part-way would drop
    // this tab out of the run and leave the app waiting on a browser that had moved on —
    // and the app is already showing what went out, which is what the reload was for.
    if (run) {
      say('\nNight Reader has the full list. Reload the page when you want to see them here.')
    } else {
      say('\nRefreshing so the list shows them…')
      setTimeout(() => location.reload(), 2000)
    }
  }
}

/**
 * Make the chapters we posted privately visible to readers.
 *
 * The content is re-sent along with the state change, rather than sending the state alone.
 * A chapter is updated by PUTting its whole record back, and the site's own save always
 * carries a `pages` body — so a PUT without one might well be read as "no pages", which on
 * ten already-published chapters would blank them. Re-sending the same HTML that Night
 * Reader generated is both safe and idempotent: the worst case is writing identical content.
 */
async function publishPosted() {
  if (busy) return
  busy = true
  startLog('# Making the private chapters public')
  const done = []
  let stopped = null
  try {
    if (!ctx.found) throw new Error('no Night Reader series publishes to this page')
    const doc = await ask('ledger', { sid: ctx.sid, targetId: ctx.target_id })
    const mine = (doc.entries || []).filter((e) => e.status === 'posted')
    if (!mine.length) throw new Error('the ledger has nothing recorded as posted')

    // Only the ones the site still has as private. Anything already public is left alone,
    // so running this twice costs nothing.
    const todo = []
    const unknown = []
    for (const e of mine) {
      const ch = CHAPTERS.get(String(e.title).trim().toLowerCase())
      if (!ch) unknown.push(e.title)
      else if (String(ch.state).toLowerCase() !== 'public') todo.push({ entry: e, ch })
    }
    say(`\n${mine.length} posted by Night Reader · ${todo.length} still private`)
    if (unknown.length) {
      say(`\n${unknown.length} could not be found in the site's chapter list:`)
      say(`  ${unknown.slice(0, 8).join(', ')}`)
      say('Open the series page so the site fetches its list, then try again.')
    }
    if (!todo.length) throw new Error('nothing private left to publish')

    say(`\npublishing: ${todo.map((t) => t.entry.title).join(', ')}`)
    for (const [n, { entry, ch }] of todo.entries()) {
      say(`\n———— ${n + 1} of ${todo.length}: ${entry.title} ————`)
      if (!entry.project_id || entry.index == null) {
        throw new Error(`"${entry.title}" has no local chapter recorded, so its text cannot `
          + 'be re-sent safely — publish that one on the site by hand')
      }
      const payload = await ask('payload', {
        sid: ctx.sid, projectId: entry.project_id, index: entry.index,
      })
      const saved = await apiSaveChapter(ch, {
        state: 'public',
        coins: entry.price_coins || 0,
        html: payload.html,
      })
      const after = saved?.data || {}
      if (String(after.state).toLowerCase() !== 'public') {
        throw new Error(`it saved as "${after.state}" instead of public`)
      }
      say(`    now public · ${after.coins ? `${after.coins} coins` : 'free'}`)
      done.push(entry.title)
      if (n < todo.length - 1) await sleep(1200)
    }
  } catch (e) {
    stopped = e.message
  } finally {
    busy = false
  }

  say(`\n${'='.repeat(46)}`)
  if (done.length) {
    say(`NOW PUBLIC (${done.length}):`)
    for (const t of done) say(`  ✓ ${t}`)
  } else {
    say('Nothing was changed.')
  }
  if (stopped) say(`\nSTOPPED: ${stopped}`)
  if (done.length) {
    say('\nRefreshing so the list shows them…')
    setTimeout(() => location.reload(), 2000)
  }
}

/** Everything this series has ever posted, from Night Reader's own ledger. */
async function showPosted() {
  startLog('# Posted chapters (from Night Reader\'s ledger)')
  try {
    if (!ctx.found) throw new Error('no Night Reader series publishes to this page')
    const doc = await ask('ledger', { sid: ctx.sid, targetId: ctx.target_id })
    const entries = doc.entries || []
    if (!entries.length) {
      say('\nNothing recorded yet.')
      return
    }
    const ok = entries.filter((e) => e.status === 'posted')
    const bad = entries.filter((e) => e.status !== 'posted')
    say(`\n${ok.length} posted${bad.length ? `, ${bad.length} needing attention` : ''}:`)
    for (const e of ok) {
      say(`  ✓ ${e.title}  ${e.price_coins ? `${e.price_coins} coins` : 'free'}`
        + `  ${(e.at || '').slice(0, 16).replace('T', ' ')}`)
    }
    for (const e of bad) {
      say(`  ! ${e.title}  ${e.status}${e.error ? ` — ${e.error}` : ''}`)
    }
  } catch (e) {
    say(`\nCould not read it: ${e.message}`)
  }
}

function build() {
  if (panel) return
  panel = document.createElement('div')
  panel.className = 'nr-panel'
  panel.innerHTML = `
    <div class="nr-head">
      <strong>Night Reader</strong>
      <span class="nr-ver"></span>
      <span class="nr-writes">no writes yet</span>
      <span class="nr-watch"></span>
      <span class="nr-status">checking…</span>
      <button class="nr-fold" title="Collapse / expand">–</button>
      <button class="nr-x" title="Hide">×</button>
    </div>
    <div class="nr-row">
      <label class="nr-lbl">post
        <input class="nr-count" type="number" min="1" max="200" value="10">
      </label>
      <label class="nr-lbl">up to ch
        <input class="nr-upto" type="number" min="1" max="9999" placeholder="—">
      </label>
      <button class="nr-btn nr-go" data-act="private">as Private</button>
      <button class="nr-btn nr-go" data-act="public">as Public</button>
      <button class="nr-btn" data-act="posted">Posted so far</button>
      <button class="nr-btn nr-go" data-act="publish">Publish private</button>
    </div>
    <div class="nr-row nr-diag">
      <button class="nr-btn" data-act="probe">Probe page</button>
      <button class="nr-btn" data-act="paste">Test paste</button>
      <button class="nr-btn" data-act="dialog">Probe dialog</button>
      <button class="nr-btn" data-act="net">Show requests</button>
      <button class="nr-btn" data-act="api">API shape</button>
      <button class="nr-btn" data-act="reads">Show reads</button>
      <button class="nr-btn" data-act="fetch1">Fetch one chapter</button>
      <button class="nr-btn" data-act="copy">Copy report</button>
    </div>
    <pre class="nr-out"></pre>`
  document.body.appendChild(panel)
  out = panel.querySelector('.nr-out')

  panel.querySelector('.nr-x').onclick = () => panel.remove()

  // --- staying out of the way ---------------------------------------------
  //
  // The site's Submit and Save sit in the same corners a floating panel wants, so this has
  // to be movable and foldable. Both are remembered, because having to reposition it on
  // every page load would be its own annoyance.
  const PLACE_KEY = 'nr.panel.place'
  const place = (() => {
    try {
      return JSON.parse(localStorage.getItem(PLACE_KEY) || '{}')
    } catch {
      return {}
    }
  })()
  const savePlace = () => {
    try {
      localStorage.setItem(PLACE_KEY, JSON.stringify(place))
    } catch { /* blocked storage is not worth failing over */ }
  }

  function applyPlace() {
    if (place.left != null && place.top != null) {
      // Clamp back on-screen: a panel dragged off the edge, or a window since resized
      // smaller, would otherwise be unreachable.
      const w = panel.offsetWidth || 420
      const h = panel.offsetHeight || 80
      const left = Math.min(Math.max(place.left, 0), Math.max(window.innerWidth - w, 0))
      const top = Math.min(Math.max(place.top, 0), Math.max(window.innerHeight - h, 0))
      Object.assign(panel.style, {
        left: `${left}px`, top: `${top}px`, right: 'auto', bottom: 'auto',
      })
    }
    panel.classList.toggle('nr-collapsed', !!place.collapsed)
    const fold = panel.querySelector('.nr-fold')
    if (fold) fold.textContent = place.collapsed ? '+' : '–'
  }

  panel.querySelector('.nr-fold').onclick = (e) => {
    e.stopPropagation()
    place.collapsed = !place.collapsed
    savePlace()
    applyPlace()
  }

  const head = panel.querySelector('.nr-head')
  head.addEventListener('pointerdown', (e) => {
    // Only the header drags, and not its buttons — otherwise Collapse and Hide would be
    // impossible to press without moving the panel.
    if (e.target.closest('button')) return
    const box = panel.getBoundingClientRect()
    const grabX = e.clientX - box.left
    const grabY = e.clientY - box.top
    const move = (ev) => {
      place.left = ev.clientX - grabX
      place.top = ev.clientY - grabY
      applyPlace()
    }
    const done = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', done)
      savePlace()
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', done)
    e.preventDefault()
  })

  window.addEventListener('resize', applyPlace)
  applyPlace()
  panel.onclick = (e) => {
    const act = e.target?.dataset?.act
    if (act === 'probe') probe()
    if (act === 'paste') testPaste()
    if (act === 'dialog') probeDialog()
    if (act === 'net') showRequests()
    if (act === 'api') showApi()
    if (act === 'reads') showReads()
    if (act === 'fetch1') probeChapterFetch()
    if (act === 'private' || act === 'public') {
      const box = panel.querySelector('.nr-count')
      const count = Math.max(1, Math.min(200, Number(box?.value) || 1))
      const upTo = Number(panel.querySelector('.nr-upto')?.value) || 0
      postBatch({ count, upTo, state: act === 'public' ? 'Public' : 'Private' })
    }
    if (act === 'publish') publishPosted()
    if (act === 'posted') showPosted()
    if (act === 'copy') {
      navigator.clipboard.writeText(LOG.join('\n'))
        .then(() => say('\n(report copied)'))
        .catch(() => say('\n(could not copy — select the text instead)'))
    }
  }

  // The version, shown right in the panel. An unpacked extension does not reload on its
  // own, so a change can look like it did nothing when really the old code is still
  // running — reading the number here settles that without opening chrome://extensions.
  panel.querySelector('.nr-ver').textContent = `v${chrome.runtime.getManifest().version}`

  showWriteCount()
  render()   // bring back the report that survived the reload

  identify()
  watchLocation()
  watchForRuns()
}

build()
