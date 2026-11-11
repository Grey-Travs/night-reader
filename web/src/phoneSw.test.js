// @vitest-environment node
import { readFileSync } from 'node:fs'
import { webcrypto } from 'node:crypto'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// The phone app's service worker (public/phone-sw.js), run in a fake worker: a Map-based
// Cache Storage and a fetch the test controls. What has to hold:
// - with the computer up, pages come from it, and a copy is kept with the files it names;
// - with the computer down or slow, the kept copy opens instead;
// - the API is never touched, and a refusal from the computer is shown as it is;
// - only the two newest builds are kept.

const SOURCE = readFileSync(new URL('../public/phone-sw.js', import.meta.url), 'utf8')
const ORIGIN = 'https://pc.example.ts.net:8443'

function fakeCaches() {
  const stores = new Map()
  const keyOf = (req) => (typeof req === 'string' ? new URL(req, ORIGIN).pathname : new URL(req.url).pathname)
  const open = async (name) => {
    if (!stores.has(name)) stores.set(name, new Map())
    const store = stores.get(name)
    return {
      match: async (req) => { const r = store.get(keyOf(req)); return r ? r.clone() : undefined },
      put: async (req, res) => { store.set(keyOf(req), res.clone()) },
    }
  }
  return {
    stores,
    open,
    delete: async (name) => stores.delete(name),
    keys: async () => [...stores.keys()],
  }
}

function worker({ fetch }) {
  const listeners = {}
  const self = {
    location: { origin: ORIGIN },
    addEventListener: (type, fn) => { listeners[type] = fn },
    skipWaiting: () => {},
    clients: { claim: async () => {} },
  }
  const caches = fakeCaches()
  new Function('self', 'caches', 'fetch', 'crypto', 'setTimeout', 'clearTimeout', SOURCE)(
    self, caches, fetch, webcrypto, setTimeout, clearTimeout)
  async function request(path, { mode = 'navigate', method = 'GET' } = {}) {
    let answered = null
    const event = {
      request: { url: `${ORIGIN}${path}`, method, mode },
      respondWith: (p) => { answered = p },
    }
    listeners.fetch(event)
    return answered ? await answered : null
  }
  return { request, caches }
}

const page = (build) => `<!doctype html><script type="module" src="/assets/index-${build}.js"></script>`
  + `<link rel="stylesheet" href="/assets/index-${build}.css">`
const html = (body, status = 200) => new Response(body, { status, headers: { 'Content-Type': 'text/html' } })
// The copy is kept in the background; wait until it is recorded.
async function kept(sw, count = 1) {
  for (let i = 0; i < 200; i += 1) {
    const meta = sw.caches.stores.get('nr-phone-meta')
    const builds = meta?.get('/builds')
    if (builds && (await builds.clone().json()).length >= count) return
    await new Promise((done) => setTimeout(done, 10))
  }
  throw new Error('the copy was never kept')
}

let net
beforeEach(() => {
  net = { up: true, build: 'a1', calls: [] }
})
const fetch = vi.fn(async (req) => {
  const path = typeof req === 'string' ? req : new URL(req.url).pathname
  net.calls.push(path)
  if (!net.up) throw new TypeError('Failed to fetch')
  if (path.startsWith('/assets/')) return new Response(`file ${path}`)
  return html(page(net.build))
})

describe('the phone app’s offline copy', () => {
  it('opens from the computer, and keeps a copy with its files', async () => {
    const sw = worker({ fetch })
    const res = await sw.request('/phone?k=secret')
    expect(await res.text()).toBe(page('a1'))
    await kept(sw)
    net.up = false
    const offline = await sw.request('/phone/capture')
    expect(await offline.text()).toBe(page('a1'))
    const script = await sw.request('/assets/index-a1.js', { mode: 'cors' })
    expect(await script.text()).toBe('file /assets/index-a1.js')
  })

  it('says plainly when there is no copy yet and the computer can’t be reached', async () => {
    net.up = false
    const sw = worker({ fetch })
    const res = await sw.request('/phone')
    expect(res.status).toBe(503)
    expect(await res.text()).toMatch(/can’t open yet/)
  })

  it('shows the computer’s refusal as it is', async () => {
    const refusing = vi.fn(async () => html('needs the key', 403))
    const sw = worker({ fetch: refusing })
    const res = await sw.request('/phone')
    expect(res.status).toBe(403)
  })

  it('never touches the API, other pages or other methods', async () => {
    const sw = worker({ fetch })
    expect(await sw.request('/api/phone/progress', { mode: 'cors' })).toBeNull()
    expect(await sw.request('/novel/abc')).toBeNull()
    expect(await sw.request('/phone/capture', { method: 'POST' })).toBeNull()   // only a share's POST
  })

  it('keeps only the two newest builds', async () => {
    const sw = worker({ fetch })
    for (const [i, build] of ['a1', 'b2', 'c3'].entries()) {
      net.build = build
      await sw.request('/phone')
      await kept(sw, Math.min(i + 1, 2))
      await new Promise((done) => setTimeout(done, 30))
    }
    const shells = [...sw.caches.stores.keys()].filter((name) => name.startsWith('nr-phone-shell-'))
    expect(shells).toHaveLength(2)
    net.up = false
    expect(await (await sw.request('/phone')).text()).toBe(page('c3'))
  })
})
