import { useEffect, useState, useSyncExternalStore } from 'react'
import { api as realApi } from './api'
import { openStore } from './outboxStore'

// The phone's outbox: every photo is saved on the phone the moment it is taken, and sent
// from here, in order, one request at a time, whenever the computer can be reached. A
// lost signal, a sleeping computer or a killed app only delays a photo; nothing is lost
// and nothing arrives out of order.
//
// The rules are the desktop upload queue's (uploadQueue.js), made to survive the app
// closing:
// - a chapter's box is made before its first photo, and only once every earlier chapter
//   of the same novel has its box, so box N is always chapter N;
// - photo k+1 goes only once the computer has confirmed photo k, so the photos of a
//   chapter land in the order they were taken;
// - "this chapter is finished" (captured) goes only once every photo is in;
// - every request is safe to repeat: a box is found again by its origin, a photo by its
//   bytes, and finishing twice is finishing once. So after a crash the sender just
//   repeats the one request it was in the middle of.
//
// Never put a label on a box: a label becomes the chapter's title, ahead of the heading
// Claude reads off its first photo.

const BACKOFF_MS = [5000, 15000, 30000, 60000, 120000, 300000]
export const HOLD_MS = 2 * 60 * 1000
const KEEP_FINISHED_MS = 14 * 24 * 3600 * 1000
const JSON_TIMEOUT_MS = 20000

const BOX_GONE = "That chapter box isn't in this novel any more."
const isPdf = (photo) => photo.type === 'application/pdf'
const sameText = (a, b) => String(a || '').replace(/’/g, "'") === String(b || '').replace(/’/g, "'")

export function newId() {
  const bytes = new Uint8Array(12)
  globalThis.crypto.getRandomValues(bytes)
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
}

/** How a failed request is treated: wait and resend, wait for the person, or stop. */
export function classify(error) {
  const status = error?.status
  if (!status || error?.name === 'AbortError' || status >= 500) return 'offline'
  // 403 is the key gate. A 400 with no detail of the app's own is the host check: the app
  // was started before Tailscale was up, and nothing sent now will get through.
  if (status === 403 || (status === 400 && (error.detail == null || typeof error.detail !== 'object'))) {
    return 'blocked'
  }
  if (status === 404 && sameText(error.message, BOX_GONE)) return 'box-gone'
  return 'refused'
}

// 'retake' is a one-photo record for a retake of a page this phone didn't send: it holds
// the replacement until it has gone, then is forgotten.
const SENDING = new Set(['open', 'closed', 'discarding', 'retake'])

/**
 * The next request to make, or null. Pure. `chapters` in the order they were started;
 * `photos` maps a chapter id to its photos in the order they were taken.
 */
export function nextAction(chapters, photos, held = new Set()) {
  const waitingForBox = new Set()   // novels with an earlier chapter still to get its box
  for (const ch of chapters) {
    if (!SENDING.has(ch.state)) continue
    const list = photos.get(ch.id) || []
    const waiting = list.filter((p) => p.state === 'waiting')
    if (ch.state === 'retake') {
      if (ch.stop) continue
      return waiting.length ? { kind: 'replace', chapter: ch, photo: waiting[0] } : { kind: 'forget', chapter: ch }
    }
    if (ch.state === 'discarding') {
      if (ch.box && !ch.stop) return { kind: 'discardBox', chapter: ch }
      if (!ch.box) return { kind: 'forget', chapter: ch }
      continue
    }
    if (!ch.box) {
      if (!waiting.length) {
        // Nothing to send and never boxed: finished with nothing, or every photo deleted.
        if (ch.state === 'closed' && !held.has(ch.id) && !ch.stop) return { kind: 'forget', chapter: ch }
        continue
      }
      if (waitingForBox.has(ch.pid)) continue
      waitingForBox.add(ch.pid)
      if (ch.stop) continue
      return { kind: 'create', chapter: ch }
    }
    if (ch.stop) continue
    const removing = list.find((p) => p.state === 'remove')
    if (removing) return { kind: 'delete', chapter: ch, photo: removing }
    if (waiting.length) {
      const photo = waiting[0]
      return { kind: photo.replaces ? 'replace' : 'upload', chapter: ch, photo }
    }
    if (ch.state === 'closed' && !held.has(ch.id)) {
      const landed = list.some((p) => p.state === 'sent')
      return { kind: landed ? 'captured' : 'deleteBox', chapter: ch }
    }
  }
  return null
}

export function createOutbox({ store, api = realApi, now = () => Date.now(), timers = globalThis,
  locks = globalThis.navigator?.locks } = {}) {
  const chapters = new Map()
  const photos = new Map()          // chapter id -> photos, in order
  const held = new Map()            // chapter id -> its hold's timer
  const listeners = new Set()
  let ready = false
  let loading = null
  let pumping = null
  let again = false
  let failures = 0
  let retryTimer = null
  let status = { state: 'idle', retryAt: null, message: '' }
  let snapshot = null

  // ---- what the screens read ----
  function build() {
    const list = [...chapters.values()].sort((a, b) => a.seq - b.seq).map((ch) => {
      const own = photos.get(ch.id) || []
      return {
        ...ch, held: held.has(ch.id),
        photos: own.filter((p) => p.state !== 'remove').map(({ blob, ...rest }) => ({ ...rest, saved: !!blob })),
        waiting: own.filter((p) => p.state === 'waiting').length,
      }
    })
    return {
      ready, durable: store.durable !== false, status, chapters: list,
      unsent: list.reduce((n, ch) => n + (SENDING.has(ch.state) ? ch.waiting : 0), 0),
    }
  }
  function emit() {
    snapshot = build()
    for (const fn of [...listeners]) fn()
  }
  snapshot = build()

  // ---- keeping records ----
  const sorted = (list) => list.sort((a, b) => a.order - b.order)
  const photosOf = (id) => photos.get(id) || []
  const findPhoto = (id) => {
    for (const list of photos.values()) {
      const found = list.find((p) => p.id === id)
      if (found) return found
    }
    return null
  }
  async function saveChapter(ch) {
    chapters.set(ch.id, ch)
    await store.putChapter(ch)
  }
  async function savePhoto(photo) {
    const list = photosOf(photo.chapterId).filter((p) => p.id !== photo.id)
    photos.set(photo.chapterId, sorted([...list, photo]))
    await store.putPhoto(photo)
  }
  async function dropPhoto(photo) {
    photos.set(photo.chapterId, photosOf(photo.chapterId).filter((p) => p.id !== photo.id))
    await store.deletePhoto(photo.id)
  }
  async function dropChapter(ch) {
    for (const photo of photosOf(ch.id)) await store.deletePhoto(photo.id)
    photos.delete(ch.id)
    chapters.delete(ch.id)
    unhold(ch.id)
    await store.deleteChapter(ch.id)
  }

  // ---- starting ----
  async function resume() {
    if (!loading) {
      loading = (async () => {
        for (const ch of await store.listChapters()) {
          if (ch.state === 'captured' && now() - (ch.capturedAt || 0) > KEEP_FINISHED_MS) {
            for (const photo of await store.listPhotos(ch.id)) await store.deletePhoto(photo.id)
            await store.deleteChapter(ch.id)
            continue
          }
          chapters.set(ch.id, ch)
          photos.set(ch.id, sorted(await store.listPhotos(ch.id)))
        }
        ready = true
        emit()
      })()
    }
    await loading
    pump()
  }

  // ---- what the screens do ----
  /**
   * A new chapter. `origin` names where its photos came from (the camera's own by
   * default); `box` sends them to the end of a chapter already on the computer instead
   * of a new one.
   */
  async function startChapter({ pid, novel = '', shown = null, expect = null, origin = null, box = null }) {
    await resume()
    const seq = Math.max(0, Number(await store.getMeta('seq')) || 0,
      ...[...chapters.values()].map((c) => c.seq)) + 1
    await store.putMeta('seq', seq)
    const id = newId()
    const ch = { id, pid, novel, seq, origin: origin || `phone:${id}`, state: 'open', box, number: null,
      shown, expect, stop: null, createdAt: now(), closedAt: null, capturedAt: null }
    await saveChapter(ch)
    emit()
    return ch
  }

  async function addPhoto(chapterId, { blob, name, type = 'image/jpeg', check = null }) {
    const ch = chapters.get(chapterId)
    if (!ch) throw new Error('That chapter isn’t on this phone any more.')
    const order = Math.max(0, ...photosOf(chapterId).map((p) => p.order)) + 1
    const photo = { id: newId(), chapterId, pid: ch.pid, order, blob, name, type, state: 'waiting',
      pageId: null, replaces: null, elsewhere: null, check, version: 1 }
    await savePhoto(photo)
    emit()
    pump()
    return photo
  }

  /** A new shot for a photo: in its place, whether or not the old one was sent. */
  async function retakePhoto(photoId, { blob, name, type = 'image/jpeg', check = null }) {
    const photo = findPhoto(photoId)
    if (!photo) return null
    const next = { ...photo, blob, name: name || photo.name, type, check, version: photo.version + 1,
      elsewhere: null, state: 'waiting',
      replaces: photo.state === 'sent' ? photo.pageId : photo.replaces,
      pageId: photo.state === 'sent' ? null : photo.pageId }
    await savePhoto(next)
    const ch = chapters.get(photo.chapterId)
    // Retaken after Done: the replacement is sent, then the chapter is finished again.
    if (ch && ch.state === 'captured') await saveChapter({ ...ch, state: 'closed' })
    emit()
    pump()
    return next
  }

  /** A new shot for any page on the computer: one this phone sent is retaken in its
   *  chapter; any other gets a record of its own that holds it until it has gone. */
  async function retakePage(pid, pageId, { blob, name, type = 'image/jpeg', check = null }) {
    await resume()
    for (const list of photos.values()) {
      const own = list.find((p) => p.pageId === pageId && p.state === 'sent')
      if (own) return retakePhoto(own.id, { blob, name, type, check })
    }
    const seq = Math.max(0, Number(await store.getMeta('seq')) || 0,
      ...[...chapters.values()].map((c) => c.seq)) + 1
    await store.putMeta('seq', seq)
    const id = newId()
    await saveChapter({ id, pid, novel: '', seq, origin: null, state: 'retake', box: null, number: null,
      shown: null, expect: null, stop: null, createdAt: now(), closedAt: null, capturedAt: null })
    const photo = { id: newId(), chapterId: id, pid, order: 1, blob, name, type, state: 'waiting',
      pageId: null, replaces: pageId, elsewhere: null, check, version: 1 }
    await savePhoto(photo)
    emit()
    pump()
    return photo
  }

  async function removePhoto(photoId) {
    const photo = findPhoto(photoId)
    if (!photo) return
    if (photo.state === 'sent') await savePhoto({ ...photo, state: 'remove', version: photo.version + 1 })
    else if (photo.state === 'waiting' && photo.replaces) {
      // The retake goes, and so does the photo it was replacing.
      await savePhoto({ ...photo, state: 'remove', pageId: photo.replaces, replaces: null,
        blob: null, version: photo.version + 1 })
    } else await dropPhoto(photo)
    emit()
    pump()
  }

  function unhold(id) {
    const timer = held.get(id)
    if (timer != null) timers.clearTimeout(timer)
    held.delete(id)
  }

  /** Finished on the phone. `hold` keeps "Back to Chapter N" possible for a while
   *  before the computer is told it may build the chapter. */
  async function closeChapter(id, { hold = false } = {}) {
    const ch = chapters.get(id)
    if (!ch || ch.state !== 'open') return
    await saveChapter({ ...ch, state: 'closed', closedAt: now() })
    unhold(id)
    if (hold) held.set(id, timers.setTimeout(() => release(id), HOLD_MS))
    emit()
    pump()
  }

  function release(id) {
    if (!held.has(id)) return
    unhold(id)
    emit()
    pump()
  }

  /** Back to a chapter closed a moment ago, before the computer was told. */
  async function reopen(id) {
    const ch = chapters.get(id)
    if (!ch || ch.state !== 'closed' || !held.has(id)) return false
    unhold(id)
    await saveChapter({ ...ch, state: 'open', closedAt: null })
    emit()
    return true
  }

  /** Give up on a chapter: its photos leave the phone, and its box leaves the novel. */
  async function discard(id) {
    const ch = chapters.get(id)
    if (!ch) return
    unhold(id)
    if (!ch.box) await dropChapter(ch)
    else await saveChapter({ ...ch, state: 'discarding', stop: null })
    emit()
    pump()
  }

  async function skipPhoto(photoId) {
    const photo = findPhoto(photoId)
    if (!photo) return
    await savePhoto({ ...photo, state: 'skipped', version: photo.version + 1 })
    const ch = chapters.get(photo.chapterId)
    if (ch?.stop) await saveChapter({ ...ch, stop: null })
    emit()
    pump()
  }

  async function retryChapter(id) {
    const ch = chapters.get(id)
    if (ch?.stop) await saveChapter({ ...ch, stop: null })
    emit()
    pump()
  }

  /** Its box was deleted on the computer: send what the phone still has as a new
   *  chapter at the end. */
  async function resendAsNew(id) {
    const ch = chapters.get(id)
    if (!ch) return
    for (const photo of photosOf(id)) {
      if (photo.blob && photo.state !== 'skipped') {
        await savePhoto({ ...photo, state: 'waiting', pageId: null, replaces: null, elsewhere: null,
          version: photo.version + 1 })
      } else await dropPhoto(photo)
    }
    // At the end now, by the person's choice, so no earlier number is expected of it.
    await saveChapter({ ...ch, box: null, number: null, stop: null, expect: null,
      state: ch.state === 'captured' ? 'closed' : ch.state })
    emit()
    pump()
  }

  function retryNow() {
    failures = 0
    if (retryTimer != null) timers.clearTimeout(retryTimer)
    retryTimer = null
    status = { state: 'idle', retryAt: null, message: '' }
    emit()
    pump()
  }

  // ---- sending ----
  function backOff(kind, error) {
    const wait = BACKOFF_MS[Math.min(failures, BACKOFF_MS.length - 1)]
    failures += 1
    const retryAt = now() + wait
    status = kind === 'blocked'
      ? { state: 'blocked', retryAt, message: error?.status === 403
        ? 'The computer refused the phone: open the phone link from Settings on the computer again.'
        : 'The computer turned the phone away. Restart Night Reader on the computer.' }
      : { state: 'offline', retryAt, message: 'The computer can’t be reached. Photos are saved on this phone.' }
    if (retryTimer != null) timers.clearTimeout(retryTimer)
    retryTimer = timers.setTimeout(() => { retryTimer = null; pump() }, wait)
    emit()
  }

  async function withTimeout(fn, ms) {
    const ctrl = typeof AbortController === 'function' ? new AbortController() : null
    const timer = timers.setTimeout(() => ctrl?.abort(), ms)
    try {
      return await fn(ctrl?.signal)
    } finally {
      timers.clearTimeout(timer)
    }
  }

  const fileOf = (photo) => (typeof File === 'function'
    ? new File([photo.blob], photo.name, { type: photo.type })
    : Object.assign(photo.blob, { name: photo.name }))

  async function stopChapter(ch, kind, message, photoId = null) {
    await saveChapter({ ...chapters.get(ch.id), stop: { kind, message, photoId } })
  }

  async function perform(action) {
    const ch = action.chapter
    const sentPhoto = action.photo
    try {
      switch (action.kind) {
        case 'create': {
          const res = await withTimeout((signal) => api.createPageBatch(ch.pid,
            { origin: ch.origin, hands_free: true }, { slim: true, signal }), JSON_TIMEOUT_MS)
          const now_ = chapters.get(ch.id)
          const number = res.batch.number ?? null
          // Boxed while it is still being shot, its number is on the screen from now on,
          // so that is the number the person expects. Boxed later — the photos waited on
          // the phone — it keeps the number they saw, so a chapter that slipped behind
          // another is held for them to check rather than built under the wrong number.
          await saveChapter({ ...now_, box: res.batch.id, number,
            expect: now_.state === 'open' && number != null ? number : now_.expect })
          break
        }
        case 'upload':
        case 'replace': {
          const size = sentPhoto.blob?.size || 0
          const pdf = isPdf(sentPhoto) && action.kind === 'upload'
          const res = await withTimeout((signal) => (pdf
            ? api.uploadPdf(ch.pid, fileOf(sentPhoto), { batch: ch.box, name: sentPhoto.name, slim: true, signal })
            : action.kind === 'upload'
              ? api.uploadPage(ch.pid, fileOf(sentPhoto), { batch: ch.box, name: sentPhoto.name, slim: true, signal })
              : api.replacePage(ch.pid, sentPhoto.replaces, fileOf(sentPhoto), { name: sentPhoto.name, slim: true, signal })),
          // A PDF is split on the computer before it answers.
          (pdf ? 4 : 1) * JSON_TIMEOUT_MS + Math.ceil(size / 100000) * 1000)
          await (pdf ? landedPdf(ch, sentPhoto, res) : landed(ch, sentPhoto, res, action.kind))
          break
        }
        case 'delete':
          try {
            await withTimeout((signal) => api.deletePages(ch.pid,
              sentPhoto.pageIds?.length ? sentPhoto.pageIds : [sentPhoto.pageId], { signal }), JSON_TIMEOUT_MS)
          } catch (e) {
            if (e?.status !== 404) throw e
          }
          await dropPhoto(sentPhoto)
          break
        case 'captured': {
          const res = await withTimeout((signal) => api.markCaptured(ch.pid, ch.box,
            { expect_number: ch.expect ?? undefined }, { slim: true, signal }), JSON_TIMEOUT_MS)
          // The photos are on the computer now; only the record stays, for the hub.
          for (const photo of photosOf(ch.id)) if (photo.blob) await savePhoto({ ...photo, blob: null })
          await saveChapter({ ...chapters.get(ch.id), state: 'captured', capturedAt: now(),
            number: res?.batch?.number ?? ch.number })
          break
        }
        case 'deleteBox':
        case 'discardBox': {
          const expect = action.kind === 'deleteBox' ? []
            : photosOf(ch.id).filter((p) => p.pageId && p.state !== 'remove')
              .flatMap((p) => (p.pageIds?.length ? p.pageIds : [p.pageId]))
          try {
            await withTimeout((signal) => api.deletePageBatch(ch.pid, ch.box,
              { expect_page_ids: expect }, { slim: true, signal }), JSON_TIMEOUT_MS)
          } catch (e) {
            if (e?.status === 409 && action.kind === 'deleteBox') {
              // A photo got in after all, its answer lost: the chapter is real, finish it.
              await saveChapter({ ...chapters.get(ch.id), box: ch.box })
              const res = await withTimeout((signal) => api.markCaptured(ch.pid, ch.box, {},
                { slim: true, signal }), JSON_TIMEOUT_MS)
              await saveChapter({ ...chapters.get(ch.id), state: 'captured', capturedAt: now(),
                number: res?.batch?.number ?? ch.number })
              break
            }
            if (e?.status !== 404) throw e
          }
          await dropChapter(ch)
          break
        }
        case 'forget':
          await dropChapter(ch)
          break
        default:
          break
      }
      failures = 0
      if (status.state !== 'idle') status = { state: 'idle', retryAt: null, message: '' }
      emit()
      return true
    } catch (error) {
      const kind = classify(error)
      if (kind === 'offline' || kind === 'blocked') {
        backOff(kind, error)
        return false
      }
      if (kind === 'box-gone') {
        await stopChapter(ch, 'box-gone', 'This chapter was deleted on the computer.')
      } else {
        await stopChapter(ch, 'refused', error?.message || 'The computer refused this.',
          sentPhoto?.id || null)
      }
      emit()
      return true
    }
  }

  /** A PDF's answer: its pages, in order. All of them already in the novel is like a
   *  photo already in: said, and the chapter goes on without it. */
  async function landedPdf(ch, photo, res) {
    const now_ = findPhoto(photo.id)
    const ids = res?.added_ids || []
    if (res?.number != null) await saveChapter({ ...chapters.get(ch.id), number: res.number })
    if (!now_) {
      if (ids.length) {
        await savePhoto({ ...photo, state: 'remove', pageId: ids[0], pageIds: ids, blob: null,
          version: photo.version + 1 })
      }
      return
    }
    if (!ids.length) {
      await savePhoto({ ...now_, state: 'elsewhere', elsewhere: { number: null, pages: res?.duplicates || 0 } })
      return
    }
    await savePhoto({ ...now_, state: 'sent', pageId: ids[0], pageIds: ids })
  }

  /** A photo's answer. Never undoes a change made on the phone while it was on its way. */
  async function landed(ch, photo, res, kind) {
    const now_ = findPhoto(photo.id)
    const pageId = res?.page?.id || null
    if (res?.duplicate && kind === 'upload' && res.existing_batch !== ch.box) {
      // Already in another chapter: said, never followed — the rest of this chapter
      // must not go there.
      if (now_ && now_.version === photo.version) {
        await savePhoto({ ...now_, state: 'elsewhere', elsewhere: { number: res.existing_number ?? null, pageId } })
      }
      return
    }
    if (res?.number != null) await saveChapter({ ...chapters.get(ch.id), number: res.number })
    if (!now_) {
      // Deleted on the phone while it was being sent: it got there, so it goes again.
      if (pageId) {
        await savePhoto({ ...photo, state: 'remove', pageId, replaces: null, blob: null,
          version: photo.version + 1 })
      }
      return
    }
    if (now_.version !== photo.version) {
      // Retaken or removed while this was on its way: what landed is what the change
      // now replaces or removes.
      if (now_.state === 'waiting') await savePhoto({ ...now_, replaces: pageId })
      else if (now_.state === 'remove') await savePhoto({ ...now_, pageId })
      return
    }
    await savePhoto({ ...now_, state: 'sent', pageId, replaces: null })
  }

  async function drain() {
    for (;;) {
      if (retryTimer != null) return
      const action = nextAction([...chapters.values()].sort((a, b) => a.seq - b.seq), photos,
        new Set(held.keys()))
      if (!action) return
      if (status.state === 'idle' && action.kind !== 'forget') {
        status = { state: 'sending', retryAt: null, message: '' }
        emit()
      }
      if (!(await perform(action))) return
    }
  }

  // Asked to send while already sending, it goes round again once the current pass ends:
  // a photo saved just as the last pass found nothing left would otherwise wait for the
  // next photo. Answers when there is nothing more it can send now.
  function pump() {
    if (!ready) return Promise.resolve()
    again = true
    if (!pumping) {
      pumping = (async () => {
        try {
          while (again) {
            again = false
            // One sender per phone, even with the app open twice.
            if (locks?.request) {
              await locks.request('nr-phone-outbox', { ifAvailable: true }, (lock) => (lock ? drain() : null))
            } else await drain()
          }
        } finally {
          pumping = null
          if (status.state === 'sending') {
            status = { state: 'idle', retryAt: null, message: '' }
            emit()
          }
        }
      })()
    }
    return pumping
  }

  return {
    subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn) },
    getSnapshot: () => snapshot,
    resume, pump, retryNow,
    startChapter, addPhoto, retakePhoto, retakePage, removePhoto, closeChapter, release, reopen, discard,
    skipPhoto, retryChapter, resendAsNew,
    blobOf: (photoId) => findPhoto(photoId)?.blob || null,
    cacheNovels: (list) => store.putMeta('novels', list),
    cachedNovels: () => store.getMeta('novels'),
  }
}

// ---- the phone's one outbox ----
let shared = null
let opening = null

/** The app's outbox, opened on first use. */
export function getOutbox() {
  if (shared) return shared
  if (!opening) {
    opening = openStore().then((store) => {
      shared = createOutbox({ store })
      return shared
    })
  }
  return null
}

export async function outboxReady() {
  getOutbox()
  const box = shared || await opening
  await box.resume()
  return box
}

const EMPTY = { ready: false, durable: true, status: { state: 'idle', retryAt: null, message: '' }, chapters: [], unsent: 0 }
const noop = () => () => {}

/** What the outbox holds, for a screen. */
export function useOutbox(box = shared) {
  return useSyncExternalStore(box ? box.subscribe : noop, box ? box.getSnapshot : () => EMPTY)
}

/** The phone's outbox and what it holds, opening it if this is the first screen. */
export function usePhoneOutbox() {
  const [box, setBox] = useState(() => shared)
  useEffect(() => {
    if (box) return undefined
    let alive = true
    outboxReady().then((opened) => { if (alive) setBox(opened) }).catch(() => {})
    return () => { alive = false }
  }, [box])
  return [box, useOutbox(box)]
}
