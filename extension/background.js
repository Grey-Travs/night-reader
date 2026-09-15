// The only part of the extension that talks to Night Reader.
//
// This lives in the service worker rather than the content script on purpose: MV3 grants
// the worker cross-origin access through host_permissions, so the app's API needs no CORS
// change at all. The same fetch from a content script would be a cross-origin request
// from https://meiko.studio and would have required opening the app's CORS list up.
//
// It also keeps the split clean — the content script only ever touches the DOM, and every
// decision about what to post and what happened is Night Reader's.

const BASES = ['http://localhost:8000', 'http://127.0.0.1:8000']

let base = null

async function pickBase() {
  if (base) return base
  for (const candidate of BASES) {
    try {
      const r = await fetch(`${candidate}/api/status`, { method: 'GET' })
      if (r.ok) { base = candidate; return base }
    } catch { /* try the next one */ }
  }
  throw new Error('Night Reader is not running — start it and try again.')
}

async function call(path, init) {
  const root = await pickBase()
  const r = await fetch(root + path, init)
  if (!r.ok) {
    let detail
    try { detail = (await r.json()).detail } catch { detail = r.statusText }
    const message = typeof detail === 'string' ? detail
      : detail && detail.title ? detail.title
      : `Request failed (${r.status})`
    throw new Error(message)
  }
  return r.status === 204 ? null : r.json()
}

const post = (path, body) => call(path, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body || {}),
})

const HANDLERS = {
  ping: () => pickBase().then((b) => ({ base: b })),
  adapter: ({ site = 'meiko' }) => call(`/api/posting/adapter?site=${encodeURIComponent(site)}`),
  // "Which series am I looking at?" — the extension knows its own URL and nothing else.
  match: ({ url }) => call(`/api/posting/match?${new URLSearchParams({ url })}`),
  plan: ({ sid, targetId, start, end }) => {
    const q = new URLSearchParams({ sid })
    if (targetId) q.set('target_id', targetId)
    if (start != null) q.set('start', String(start))
    if (end != null) q.set('end', String(end))
    return call(`/api/posting/plan?${q}`)
  },
  payload: ({ sid, projectId, index }) => call(
    `/api/posting/payload?${new URLSearchParams({ sid, project_id: projectId, index: String(index) })}`,
  ),
  // The durable record of what has been posted. Worth reading back into the panel: posting
  // through the API means the site's own list cannot be trusted to show a new chapter until
  // the page reloads, so the ledger is the honest answer to "what actually went out".
  ledger: ({ sid, targetId = 'default' }) => call(
    `/api/posting/ledger?${new URLSearchParams({ sid, target_id: targetId })}`,
  ),
  result: (body) => post('/api/posting/result', body),

  // --- runs the app asked for ---------------------------------------------
  //
  // Before these, the panel on this tab was the ONLY way to start posting: the app could
  // preview a run but never begin one. Now Night Reader writes a request to a file and the
  // content script asks here for it on a timer, so a run can be started from the Posting
  // page — or from a phone, since the request waits on disk rather than in the tab that
  // asked for it.
  //
  // These belong here, in the worker, for the same reason everything else does: MV3 gives
  // the worker cross-origin access through host_permissions, so meiko.studio can stay out
  // of the app's CORS list. Calling any of them from the content script would force that
  // list open.
  claim: ({ url, token }) => post('/api/posting/claim', { url, token }),
  progress: ({ sid, targetId = 'default', token, event }) => post('/api/posting/progress', {
    sid, target_id: targetId, token, event,
  }),
  // The chapter names read off the site. Python has no session there and no business
  // having one, so this push is the only way the plan can know what is already published.
  site: ({ sid, targetId = 'default', titles, prices }) => post('/api/posting/site', {
    sid, target_id: targetId, titles, prices,
  }),

  // --- reading a published novel back into the library --------------------
  //
  // Same reason these live here rather than in the content script: the worker has
  // cross-origin access through host_permissions, so the app's CORS list stays shut.
  catalogue: ({ studioUid, series }) => post('/api/import/catalogue', {
    studio_uid: studioUid, series,
  }),
  importPreview: ({ name, seriesUid, chapters }) => post('/api/import/preview', {
    name, series_uid: seriesUid, chapters,
  }),
  importSite: ({ name, seriesUid, seriesUrl, chapters }) => post('/api/import/site', {
    name, series_uid: seriesUid, series_url: seriesUrl, chapters,
  }),
}

chrome.runtime.onMessage.addListener((msg, _sender, respond) => {
  const handler = HANDLERS[msg?.kind]
  if (!handler) { respond({ ok: false, error: `Unknown request: ${msg?.kind}` }); return false }
  Promise.resolve(handler(msg.payload || {}))
    .then((data) => respond({ ok: true, data }))
    .catch((e) => respond({ ok: false, error: e.message || String(e) }))
  return true // keep the channel open for the async reply
})
