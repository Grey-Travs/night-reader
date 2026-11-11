// @vitest-environment node
import { readFileSync } from 'node:fs'
import { webcrypto } from 'node:crypto'
import { describe, expect, it } from 'vitest'

import { deleteShare, isDriveLink, listShares, readShare } from './shareInbox'

// A share from Gallery, end to end on the phone: the service worker takes the POST and
// keeps the files, and the share screen reads them back, in order. Run in Node's own
// environment, whose FormData and File are the real ones a share arrives as.

const SOURCE = readFileSync(new URL('../public/phone-sw.js', import.meta.url), 'utf8')
const ORIGIN = 'https://pc.example.ts.net:8443'

function fakeCaches({ failPut = false } = {}) {
  const stores = new Map()
  const keyOf = (req) => (typeof req === 'string' ? req : new URL(req.url).pathname)
  return {
    stores,
    open: async (name) => {
      if (!stores.has(name)) stores.set(name, new Map())
      const store = stores.get(name)
      return {
        match: async (req) => { const r = store.get(keyOf(req)); return r ? r.clone() : undefined },
        put: async (req, res) => {
          if (failPut) throw new Error('QuotaExceededError')
          store.set(keyOf(req), res.clone())
        },
        delete: async (req) => store.delete(keyOf(req)),
        keys: async () => [...store.keys()].map((path) => ({ url: `${ORIGIN}${path}` })),
      }
    },
  }
}

function worker(caches) {
  const listeners = {}
  const self = {
    location: { origin: ORIGIN },
    addEventListener: (type, fn) => { listeners[type] = fn },
    skipWaiting: () => {},
    clients: { claim: async () => {} },
  }
  new Function('self', 'caches', 'fetch', 'crypto', 'setTimeout', 'clearTimeout', SOURCE)(
    self, caches, async () => { throw new Error('no network in this test') }, webcrypto, setTimeout, clearTimeout)
  return async (form, query = '') => {
    const request = new Request(`${ORIGIN}/phone/share${query}`, { method: 'POST', body: form })
    let answer = null
    listeners.fetch({ request, respondWith: (p) => { answer = p } })
    return answer
  }
}

const photo = (name, text = name) => new File([text], name, { type: 'image/jpeg' })

describe('a share', () => {
  it('is kept on the phone in the order it came, and the share screen opens on it', async () => {
    const caches = fakeCaches()
    const share = worker(caches)
    const form = new FormData()
    form.append('photos', photo('IMG_2.jpg', 'two'))
    form.append('photos', photo('IMG_1.jpg', 'one'))
    form.append('photos', new File(['%PDF-1.7'], 'scan.pdf', { type: 'application/pdf' }))
    const res = await share(form, '?k=secret')
    expect(res.status).toBe(303)
    const where = new URL(res.headers.get('location'))
    expect(where.pathname).toBe('/phone/share')
    expect(where.searchParams.get('k')).toBe('secret')
    const id = where.searchParams.get('id')

    const kept = await readShare(id, caches)
    expect(kept.files.map((f) => f.name)).toEqual(['IMG_2.jpg', 'IMG_1.jpg', 'scan.pdf'])
    expect(await kept.files[0].text()).toBe('two')
    expect(kept.files[2].type).toBe('application/pdf')
    expect(await listShares(caches)).toEqual([id])

    await deleteShare(id, caches)
    expect(await readShare(id, caches)).toBeNull()
    expect(await listShares(caches)).toEqual([])
  })

  it('keeps a link that came with no file, for the screen to explain', async () => {
    const caches = fakeCaches()
    const form = new FormData()
    form.append('url', 'https://drive.google.com/file/d/abc/view')
    const res = await worker(caches)(form)
    const id = new URL(res.headers.get('location')).searchParams.get('id')
    const kept = await readShare(id, caches)
    expect(kept.files).toEqual([])
    expect(isDriveLink(kept.text)).toBe(true)
  })

  it('says when nothing came with it', async () => {
    const res = await worker(fakeCaches())(new FormData())
    expect(new URL(res.headers.get('location')).search).toBe('?empty=1')
  })

  it('says when the phone couldn’t keep it', async () => {
    const form = new FormData()
    form.append('photos', photo('a.jpg'))
    const res = await worker(fakeCaches({ failPut: true }))(form)
    expect(new URL(res.headers.get('location')).search).toBe('?error=storage')
  })
})
