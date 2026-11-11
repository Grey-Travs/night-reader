/* Night Reader's phone service worker. Registered by the phone app with scope /phone.
 *
 * It keeps a copy of the phone app on the phone, so the app opens — and the camera works
 * and photos are saved — while the computer is asleep or out of reach. The photos then
 * wait in the phone's outbox until it can be reached again.
 *
 * - A share from Gallery or Google Photos (POST /phone/share): its files are kept here, on
 *   the phone, and the share screen opens to say where they go.
 * - /phone pages: the computer first, given 4 seconds; then the copy kept here.
 * - /assets/: kept copies first. Their names carry a hash of their contents, so a kept
 *   one is never out of date.
 * - /api/ and everything else: never touched.
 *
 * Each build's page is kept with the files it names, and the two newest builds are kept,
 * so a copy never points at a file that was thrown away.
 *
 * Plain script, no imports, no build step: it is served as written, from the root, so
 * that its scope can be /phone.
 */

const SHELL = 'nr-phone-shell-'
const META = 'nr-phone-meta'
const SHARES = 'nr-phone-share-v1'
// The form field a share's files arrive in: the manifest's share_target names the same.
const SHARE_FIELD = 'photos'
const KEEP = 2
const NAV_TIMEOUT_MS = 4000

const OFFLINE_PAGE = `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Night Reader</title><body style="font-family:system-ui,sans-serif;padding:2rem;background:#181520;color:#eee">
<h1 style="font-size:1.2rem">Night Reader can’t open yet</h1>
<p>The computer can’t be reached, and this phone hasn’t kept a copy of the app yet. Open it once
while the computer is on and connected, and after that it opens without it.</p></body>`

self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()))

self.addEventListener('fetch', (event) => {
  const request = event.request
  const url = new URL(request.url)
  if (url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return
  if (request.method === 'POST' && url.pathname === '/phone/share') {
    event.respondWith(receiveShare(request, url))
  } else if (request.method === 'GET' && request.mode === 'navigate' && inScope(url)) {
    event.respondWith(page(request))
  } else if (request.method === 'GET' && url.pathname.startsWith('/assets/')) {
    event.respondWith(asset(request))
  }
})

// A share: keep its files on the phone, under an id, and open the share screen on them.
// Kept before anything else happens, so a share is never lost to the computer being away.
async function receiveShare(request, url) {
  const key = url.searchParams.get('k')
  const go = (query) => Response.redirect(new URL(
    `/phone/share?${query}${key ? `&k=${encodeURIComponent(key)}` : ''}`, self.location.origin).href, 303)
  let form
  try {
    form = await request.formData()
  } catch {
    return go('error=storage')
  }
  const files = form.getAll(SHARE_FIELD).filter((f) => f && typeof f !== 'string' && f.size)
  const text = ['title', 'text', 'url'].map((k) => form.get(k)).filter((v) => typeof v === 'string' && v).join('\n')
  if (!files.length && !text) return go('empty=1')
  const id = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`
  try {
    const cache = await caches.open(SHARES)
    for (const [i, file] of files.entries()) {
      await cache.put(`/phone/__share/${id}/${i}`,
        new Response(file, { headers: { 'Content-Type': file.type || 'application/octet-stream' } }))
    }
    const items = files.map((f) => ({ name: f.name || 'shared', type: f.type || '', size: f.size, lastModified: f.lastModified || 0 }))
    await cache.put(`/phone/__share/${id}`, new Response(JSON.stringify({ id, items, text, at: Date.now() }),
      { headers: { 'Content-Type': 'application/json' } }))
  } catch {
    return go('error=storage')
  }
  return go(`id=${id}`)
}

function inScope(url) {
  return url.pathname === '/phone' || url.pathname.startsWith('/phone/')
}

function withTimeout(promise, ms) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('timed out')), ms)
    promise.then((value) => { clearTimeout(timer); resolve(value) },
      (error) => { clearTimeout(timer); reject(error) })
  })
}

async function page(request) {
  try {
    const response = await withTimeout(fetch(request), NAV_TIMEOUT_MS)
    if (response.ok) {
      keep(response.clone()).catch(() => {})
      return response
    }
    // A refusal (no access key) is the computer's own answer, and the right one to show.
    if (response.status < 500) return response
  } catch {
    // Unreachable, or too slow to wait for: the kept copy.
  }
  const kept = await newestShell()
  return kept || new Response(OFFLINE_PAGE, { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } })
}

async function hex(text) {
  const bytes = new TextEncoder().encode(text)
  const hash = await crypto.subtle.digest('SHA-256', bytes)
  return Array.from(new Uint8Array(hash).slice(0, 8), (b) => b.toString(16).padStart(2, '0')).join('')
}

async function builds() {
  const meta = await caches.open(META)
  const found = await meta.match('/builds')
  return found ? found.json() : []
}

async function keep(response) {
  const html = await response.text()
  const name = SHELL + await hex(html)
  const cache = await caches.open(name)
  await cache.put('/phone', new Response(html, { headers: { 'Content-Type': 'text/html; charset=utf-8' } }))
  const files = [...html.matchAll(/(?:src|href)="(\/assets\/[^"]+)"/g)].map((m) => m[1])
  await Promise.all(files.map(async (path) => {
    if (await cache.match(path)) return
    try {
      const got = await fetch(path, { credentials: 'same-origin' })
      if (got.ok) await cache.put(path, got)
    } catch { /* fetched again next time */ }
  }))
  const order = (await builds()).filter((b) => b !== name).concat(name)
  const meta = await caches.open(META)
  await meta.put('/builds', new Response(JSON.stringify(order.slice(-KEEP)), { headers: { 'Content-Type': 'application/json' } }))
  for (const old of order.slice(0, -KEEP)) await caches.delete(old)
}

async function newestShell() {
  const order = await builds()
  for (const name of [...order].reverse()) {
    const kept = await (await caches.open(name)).match('/phone')
    if (kept) return kept
  }
  return null
}

async function asset(request) {
  for (const name of [...(await builds())].reverse()) {
    const kept = await (await caches.open(name)).match(request)
    if (kept) return kept
  }
  const response = await fetch(request)
  if (response.ok) {
    // A page's own chunks, loaded when it is first opened: kept with the newest build.
    const order = await builds()
    if (order.length) await (await caches.open(order[order.length - 1])).put(request, response.clone())
  }
  return response
}
