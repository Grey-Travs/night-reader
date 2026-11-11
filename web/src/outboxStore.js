// Where the phone keeps photos until the computer has them: IndexedDB, so a photo taken
// with the computer asleep, the signal gone or the app killed is still there to send.
//
// Three stores in one database, `nr-phone-outbox`:
//   chapters  keyPath id          one record per chapter being sent
//   photos    keyPath id, index `chapter` on chapterId
//   meta      key -> value        small things worth keeping: the novel list, counters
//
// This is the only module that touches IndexedDB. The outbox (outbox.js) talks to a
// store with eight methods, so its tests run on memoryStore() and never need a browser.

const DB_NAME = 'nr-phone-outbox'
const DB_VERSION = 1

/** A store in memory: what tests use, and the fallback when IndexedDB won't open. */
export function memoryStore() {
  const chapters = new Map()
  const photos = new Map()
  const meta = new Map()
  const copy = (value) => (value == null ? value : { ...value })
  return {
    durable: false,
    async listChapters() { return [...chapters.values()].map(copy) },
    async putChapter(chapter) { chapters.set(chapter.id, copy(chapter)) },
    async deleteChapter(id) { chapters.delete(id) },
    async listPhotos(chapterId) {
      return [...photos.values()].filter((p) => p.chapterId === chapterId).map(copy)
    },
    async putPhoto(photo) { photos.set(photo.id, copy(photo)) },
    async deletePhoto(id) { photos.delete(id) },
    async getMeta(key) { return meta.has(key) ? meta.get(key) : null },
    async putMeta(key, value) { meta.set(key, value) },
  }
}

function request(req) {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error)
  })
}

/** One transaction per call, with nothing awaited inside it: a transaction left idle
 *  across an await commits early, and the next request on it throws. */
function idbStore(db) {
  const run = (names, mode, fn) => new Promise((resolve, reject) => {
    const tx = db.transaction(names, mode)
    let result
    tx.oncomplete = () => resolve(result)
    tx.onerror = () => reject(tx.error)
    tx.onabort = () => reject(tx.error)
    const req = fn(tx)
    if (req) req.onsuccess = () => { result = req.result }
  })
  return {
    durable: true,
    listChapters: () => run(['chapters'], 'readonly', (tx) => tx.objectStore('chapters').getAll()),
    putChapter: (chapter) => run(['chapters'], 'readwrite', (tx) => tx.objectStore('chapters').put(chapter)),
    deleteChapter: (id) => run(['chapters'], 'readwrite', (tx) => tx.objectStore('chapters').delete(id)),
    listPhotos: (chapterId) => run(['photos'], 'readonly',
      (tx) => tx.objectStore('photos').index('chapter').getAll(chapterId)),
    putPhoto: (photo) => run(['photos'], 'readwrite', (tx) => tx.objectStore('photos').put(photo)),
    deletePhoto: (id) => run(['photos'], 'readwrite', (tx) => tx.objectStore('photos').delete(id)),
    getMeta: async (key) => {
      const row = await run(['meta'], 'readonly', (tx) => tx.objectStore('meta').get(key))
      return row ? row.value : null
    },
    putMeta: (key, value) => run(['meta'], 'readwrite', (tx) => tx.objectStore('meta').put({ key, value })),
  }
}

/**
 * The phone's store: IndexedDB when it opens, memory when it doesn't (a private window,
 * storage blocked). A memory store says so with `durable: false`, and the hub warns that
 * photos aren't kept if the app closes.
 */
export async function openStore(idb = globalThis.indexedDB) {
  if (!idb) return memoryStore()
  try {
    const open = idb.open(DB_NAME, DB_VERSION)
    open.onupgradeneeded = () => {
      const db = open.result
      if (!db.objectStoreNames.contains('chapters')) db.createObjectStore('chapters', { keyPath: 'id' })
      if (!db.objectStoreNames.contains('photos')) {
        db.createObjectStore('photos', { keyPath: 'id' }).createIndex('chapter', 'chapterId')
      }
      if (!db.objectStoreNames.contains('meta')) db.createObjectStore('meta', { keyPath: 'key' })
    }
    return idbStore(await request(open))
  } catch {
    return memoryStore()
  }
}
