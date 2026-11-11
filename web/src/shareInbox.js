// Shares kept on the phone by its service worker (public/phone-sw.js) until the share
// screen says where they go. Cache Storage rather than IndexedDB, so that the worker and
// the page need nothing in common but a few names.

const SHARES = 'nr-phone-share-v1'
const PREFIX = '/phone/__share/'

/** One share: its files in the order they were shared, and any text or link with them. */
export async function readShare(id, store = globalThis.caches) {
  if (!store || !id) return null
  const cache = await store.open(SHARES)
  const meta = await cache.match(`${PREFIX}${id}`)
  if (!meta) return null
  const info = await meta.json()
  const files = []
  for (const [i, item] of (info.items || []).entries()) {
    const kept = await cache.match(`${PREFIX}${id}/${i}`)
    if (!kept) continue
    const blob = await kept.blob()
    files.push(new File([blob], item.name || `shared-${i + 1}`, { type: item.type || blob.type, lastModified: item.lastModified || 0 }))
  }
  return { id, text: info.text || '', at: info.at || 0, files }
}

/** Forget a share once its files are in the outbox. */
export async function deleteShare(id, store = globalThis.caches) {
  if (!store || !id) return
  const cache = await store.open(SHARES)
  const meta = await cache.match(`${PREFIX}${id}`)
  const count = meta ? ((await meta.json()).items || []).length : 0
  for (let i = 0; i < count; i += 1) await cache.delete(`${PREFIX}${id}/${i}`)
  await cache.delete(`${PREFIX}${id}`)
}

/** The ids of every share still kept, oldest first. */
export async function listShares(store = globalThis.caches) {
  if (!store) return []
  const cache = await store.open(SHARES)
  const keys = await cache.keys()
  return keys.map((req) => new URL(req.url, 'https://x').pathname)
    .filter((path) => path.startsWith(PREFIX) && !path.slice(PREFIX.length).includes('/'))
    .map((path) => path.slice(PREFIX.length))
    .sort()
}

/** A share that is only a link to a file in Google Drive, not the file. */
export function isDriveLink(text) {
  return /https?:\/\/(drive|docs)\.google\.com\//i.test(text || '')
}
