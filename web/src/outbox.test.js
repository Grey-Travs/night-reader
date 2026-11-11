// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { HOLD_MS, classify, createOutbox, nextAction } from './outbox'
import { memoryStore } from './outboxStore'

// The phone's outbox. What has to hold, whatever the connection does:
// - a chapter's box is made before its first photo, once, and only after every earlier
//   chapter of the same novel has its box;
// - photos land in the order they were taken, one request at a time;
// - "finished" goes only once every photo is in, and not while "Back to Chapter N" is
//   still on offer;
// - an interrupted request is simply sent again, and the server takes it as the same one;
// - a change made on the phone while a photo was on its way is never undone by its answer.

const BOX_GONE = "That chapter box isn't in this novel any more."
const failure = (status, message, detail = { title: message }) => Object.assign(new Error(message), { status, detail })
const offline = () => Object.assign(new TypeError('Failed to fetch'), {})

function fakeServer() {
  const log = []
  const boxes = new Map()       // origin -> box
  const byId = new Map()
  const pages = new Map()       // page id -> { box, bytes }
  let made = 0
  let pageNo = 0
  const findBytes = (bytes) => [...pages.entries()].find(([, p]) => p.bytes === bytes)
  const server = {
    log, boxes, byId, pages,
    // Errors for the next calls, in order (undefined lets a call through). Queue them
    // before the change that starts the sending: a photo saved starts its upload at once.
    fail: [],
    hold: null,                // a promise the next upload waits on
    next() { const e = server.fail.shift(); if (e) throw e },
    dropBox(id) {
      byId.delete(id)
      for (const [origin, b] of boxes) if (b.id === id) boxes.delete(origin)
      for (const [pageId, p] of pages) if (p.box === id) pages.delete(pageId)
    },
  }
  server.api = {
    createPageBatch: vi.fn(async (pid, { origin, hands_free }) => {
      log.push(['create', origin, hands_free])
      server.next()
      let box = boxes.get(origin)
      if (!box) {
        made += 1
        box = { id: `box${made}`, number: made, captured: false }
        boxes.set(origin, box)
        byId.set(box.id, box)
      }
      return { created: true, batch: { id: box.id, number: box.number }, rev: 1 }
    }),
    uploadPage: vi.fn(async (pid, file, { batch, name }) => {
      const bytes = await file.text()
      log.push(['upload', bytes, batch])
      if (server.hold) await server.hold
      server.next()
      if (!byId.has(batch)) throw failure(404, BOX_GONE)
      const dup = findBytes(bytes)
      if (dup) {
        return { duplicate: true, page: { id: dup[0] }, existing_batch: dup[1].box,
          existing_number: byId.get(dup[1].box)?.number ?? null }
      }
      pageNo += 1
      const id = `pg${pageNo}`
      pages.set(id, { box: batch, bytes, name })
      return { duplicate: false, page: { id }, batch, number: byId.get(batch).number }
    }),
    replacePage: vi.fn(async (pid, pageId, file) => {
      const bytes = await file.text()
      log.push(['replace', pageId, bytes])
      server.next()
      const old = pages.get(pageId)
      if (!old) throw failure(404, "That page isn't in this novel.")
      pages.delete(pageId)
      pageNo += 1
      const id = `pg${pageNo}`
      pages.set(id, { box: old.box, bytes })
      return { page: { id }, replaced: pageId, batch: old.box, number: byId.get(old.box).number }
    }),
    uploadPdf: vi.fn(async (pid, file, { batch }) => {
      const bytes = await file.text()
      log.push(['pdf', bytes, batch])
      server.next()
      if (!byId.has(batch)) throw failure(404, BOX_GONE)
      // A fake split: "a|b|c" is a PDF of three pages.
      const added = []
      let duplicates = 0
      for (const part of bytes.split('|')) {
        if (findBytes(part)) { duplicates += 1; continue }
        pageNo += 1
        const id = `pg${pageNo}`
        pages.set(id, { box: batch, bytes: part })
        added.push(id)
      }
      return { added_ids: added, duplicates, batch, number: byId.get(batch).number }
    }),
    deletePages: vi.fn(async (pid, ids) => {
      log.push(['delete', ...ids])
      server.next()
      for (const id of ids) pages.delete(id)
      return { ok: true }
    }),
    markCaptured: vi.fn(async (pid, batch, { expect_number } = {}) => {
      log.push(['captured', batch, expect_number])
      server.next()
      const box = byId.get(batch)
      if (!box) throw failure(404, BOX_GONE)
      box.captured = true
      return { batch: { id: batch, number: box.number } }
    }),
    deletePageBatch: vi.fn(async (pid, batch, { expect_page_ids }) => {
      log.push(['deleteBox', batch, [...expect_page_ids]])
      server.next()
      const inside = [...pages.entries()].filter(([, p]) => p.box === batch).map(([id]) => id)
      if (inside.length !== expect_page_ids.length || inside.some((id) => !expect_page_ids.includes(id))) {
        throw failure(409, 'The photos in that chapter changed. Look again before deleting it.')
      }
      server.dropBox(batch)
      return { ok: true }
    }),
  }
  return server
}

const photo = (bytes) => ({ blob: new Blob([bytes], { type: 'image/jpeg' }), name: `${bytes}.jpg` })

let server
let store
let box
const make = () => createOutbox({ store, api: server.api, locks: null })

beforeEach(async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] })
  server = fakeServer()
  store = memoryStore()
  box = make()
  await box.resume()
})
afterEach(() => vi.useRealTimers())

const kinds = () => server.log.map((entry) => entry[0])

async function chapterOf(bytesList, { pid = 'p1', close = true, hold = false } = {}) {
  const ch = await box.startChapter({ pid, shown: 1 })
  for (const bytes of bytesList) await box.addPhoto(ch.id, photo(bytes))
  if (close) await box.closeChapter(ch.id, { hold })
  await box.pump()
  return ch
}

describe('the order things are sent in', () => {
  it('makes the box first, then each photo in order, then says it is finished', async () => {
    const ch = await chapterOf(['a', 'b', 'c'])
    expect(server.log).toEqual([
      ['create', ch.origin, true],
      ['upload', 'a', 'box1'], ['upload', 'b', 'box1'], ['upload', 'c', 'box1'],
      // Boxed while it was being shot: the number on screen from then on is the one
      // the computer is told to expect.
      ['captured', 'box1', 1],
    ])
    const [state] = box.getSnapshot().chapters
    expect(state.state).toBe('captured')
    expect(state.photos.every((p) => p.state === 'sent' && !p.saved)).toBe(true)
  })

  it('never finishes a chapter still being shot', async () => {
    const ch = await chapterOf(['a'], { close: false })
    expect(kinds()).toEqual(['create', 'upload'])
    await box.addPhoto(ch.id, photo('b'))
    await box.pump()
    expect(kinds()).toEqual(['create', 'upload', 'upload'])
  })

  it('makes a later chapter’s box only after the earlier one has its own', async () => {
    server.fail.push(offline())
    const first = await box.startChapter({ pid: 'p1' })
    await box.addPhoto(first.id, photo('a'))
    const second = await box.startChapter({ pid: 'p1' })
    await box.addPhoto(second.id, photo('b'))
    await box.pump()
    expect(server.log).toEqual([['create', first.origin, true]])
    expect(box.getSnapshot().status.state).toBe('offline')

    await vi.advanceTimersByTimeAsync(5000)
    await box.pump()
    const creates = server.log.filter((e) => e[0] === 'create').map((e) => e[1])
    expect(creates).toEqual([first.origin, first.origin, second.origin])
    expect(server.byId.get('box1').number).toBe(1)
  })

  it('lets another novel’s chapter go ahead of one that is stuck', async () => {
    const stuck = await box.startChapter({ pid: 'p1' })
    server.fail.push(failure(400, 'That novel doesn’t use page images.'))
    await box.addPhoto(stuck.id, photo('a'))
    await box.pump()
    expect(box.getSnapshot().chapters[0].stop.kind).toBe('refused')
    await chapterOf(['z'], { pid: 'p2' })
    expect(server.log.at(-1)).toEqual(['captured', 'box1', undefined])
  })
})

describe('the number the computer is told to expect', () => {
  it('is the one the phone showed when the box was made only after shooting', async () => {
    const ch = await box.startChapter({ pid: 'p1', shown: 12, expect: 12 })
    server.fail.push(offline())   // no box while shooting: the photos wait on the phone
    await box.addPhoto(ch.id, photo('a'))
    await box.closeChapter(ch.id)
    await box.pump()
    await vi.advanceTimersByTimeAsync(5000)
    await box.pump()
    expect(server.log.at(-1)).toEqual(['captured', 'box1', 12])
  })
})

describe('holding "finished" back', () => {
  it('waits while Back to Chapter N is on offer, then finishes', async () => {
    const ch = await chapterOf(['a'], { hold: true })
    expect(kinds()).toEqual(['create', 'upload'])
    expect(box.getSnapshot().chapters[0].held).toBe(true)
    await vi.advanceTimersByTimeAsync(HOLD_MS)
    await box.pump()
    expect(kinds()).toEqual(['create', 'upload', 'captured'])
    expect(ch.id).toBeTruthy()
  })

  it('can go back to the chapter without a word to the computer', async () => {
    const ch = await chapterOf(['a'], { hold: true })
    expect(await box.reopen(ch.id)).toBe(true)
    await box.addPhoto(ch.id, photo('b'))
    await box.pump()
    await vi.advanceTimersByTimeAsync(HOLD_MS)
    expect(kinds()).toEqual(['create', 'upload', 'upload'])
  })

  it('holds nothing after the app restarts', async () => {
    await chapterOf(['a'], { hold: true })
    box = make()
    await box.resume()
    await box.pump()
    expect(kinds()).toEqual(['create', 'upload', 'captured'])
  })
})

describe('when the connection fails', () => {
  it('waits and sends the same bytes again', async () => {
    const ch = await box.startChapter({ pid: 'p1' })
    server.fail.push(undefined, offline())   // the box, then the photo's first try
    await box.addPhoto(ch.id, photo('a'))
    await box.pump()
    expect(box.getSnapshot().status.state).toBe('offline')
    await vi.advanceTimersByTimeAsync(5000)
    await box.pump()
    expect(server.log.filter((e) => e[0] === 'upload').map((e) => e[1])).toEqual(['a', 'a'])
    expect(box.getSnapshot().chapters[0].photos[0].state).toBe('sent')
  })

  it('carries on after a restart without repeating what was done', async () => {
    const ch = await box.startChapter({ pid: 'p1' })
    server.fail.push(undefined, undefined, offline())
    await box.addPhoto(ch.id, photo('a'))
    await box.addPhoto(ch.id, photo('b'))
    await box.pump()
    expect(kinds()).toEqual(['create', 'upload', 'upload'])
    box = make()
    await box.resume()
    await box.pump()
    expect(kinds()).toEqual(['create', 'upload', 'upload', 'upload'])
    expect(server.log.at(-1)).toEqual(['upload', 'b', 'box1'])
  })

  it('says it is blocked when the computer refuses the phone', async () => {
    server.fail.push(failure(403, 'This needs the access key.'))
    await chapterOf(['a'])
    expect(box.getSnapshot().status.state).toBe('blocked')
    server.fail.push(failure(400, 'Bad Request', 'Bad Request'))
    box.retryNow()
    await box.pump()
    expect(box.getSnapshot().status.state).toBe('blocked')
  })
})

describe('duplicates and refusals', () => {
  it('counts a photo already in its chapter as sent, and one elsewhere as elsewhere', async () => {
    await chapterOf(['a'])
    const ch = await chapterOf(['b', 'a'], { close: false })
    const photos = box.getSnapshot().chapters.find((c) => c.id === ch.id).photos
    expect(photos.map((p) => p.state)).toEqual(['sent', 'elsewhere'])
    expect(photos[1].elsewhere.number).toBe(1)
  })

  it('deletes the box of a chapter whose every photo was elsewhere', async () => {
    await chapterOf(['a'])
    await chapterOf(['a'])
    expect(server.log.at(-1)).toEqual(['deleteBox', 'box2', []])
    expect(box.getSnapshot().chapters).toHaveLength(1)
  })

  it('stops at a refused photo, and goes on once it is skipped', async () => {
    const ch = await box.startChapter({ pid: 'p1' })
    server.fail.push(undefined, undefined, failure(400, 'That image is bigger than 25 MB.'))
    await box.addPhoto(ch.id, photo('a'))
    const bad = await box.addPhoto(ch.id, photo('b'))
    await box.addPhoto(ch.id, photo('c'))
    await box.closeChapter(ch.id)
    await box.pump()
    const stopped = box.getSnapshot().chapters[0]
    expect(stopped.stop).toEqual({ kind: 'refused', message: 'That image is bigger than 25 MB.', photoId: bad.id })
    await box.skipPhoto(bad.id)
    await box.pump()
    expect(server.log.slice(-2)).toEqual([['upload', 'c', 'box1'], ['captured', 'box1', 1]])
  })

  it('stops when the chapter was deleted on the computer, and can send it again as new', async () => {
    const ch = await chapterOf(['a'], { close: false })
    server.dropBox('box1')
    await box.addPhoto(ch.id, photo('b'))
    await box.pump()
    expect(box.getSnapshot().chapters[0].stop.kind).toBe('box-gone')
    await box.resendAsNew(ch.id)
    await box.closeChapter(ch.id)
    await box.pump()
    expect(server.log.slice(-4)).toEqual([
      ['create', ch.origin, true], ['upload', 'a', 'box2'], ['upload', 'b', 'box2'],
      ['captured', 'box2', undefined],   // sent on as new, so no number is expected
    ])
  })
})

describe('changes on the phone', () => {
  it('a photo removed before it was sent is never sent', async () => {
    const ch = await box.startChapter({ pid: 'p1' })
    server.fail.push(offline())
    await box.pump()
    const p = await box.addPhoto(ch.id, photo('a'))
    await box.removePhoto(p.id)
    await vi.advanceTimersByTimeAsync(5000)
    await box.pump()
    expect(kinds()).not.toContain('upload')
  })

  it('a photo removed after it was sent is deleted on the computer', async () => {
    const ch = await chapterOf(['a', 'b'], { close: false })
    const sent = box.getSnapshot().chapters[0].photos[0]
    await box.removePhoto(sent.id)
    await box.pump()
    expect(server.log.at(-1)).toEqual(['delete', sent.pageId])
    expect(box.getSnapshot().chapters[0].photos.map((p) => p.order)).toEqual([2])
    expect(ch.id).toBeTruthy()
  })

  it('a photo removed while on its way is deleted once it lands', async () => {
    const ch = await box.startChapter({ pid: 'p1' })
    await box.pump()
    let release
    server.hold = new Promise((done) => { release = done })
    const p = await box.addPhoto(ch.id, photo('a'))
    await vi.advanceTimersByTimeAsync(0)
    await box.removePhoto(p.id)
    server.hold = null
    release()
    await box.pump()
    expect(kinds()).toEqual(['create', 'upload', 'delete'])
    expect(server.pages.size).toBe(0)
  })

  it('a retake of a sent photo replaces it in place', async () => {
    const ch = await chapterOf(['a', 'b'], { close: false })
    const first = box.getSnapshot().chapters[0].photos[0]
    await box.retakePhoto(first.id, photo('a2'))
    await box.pump()
    expect(server.log.at(-1)).toEqual(['replace', first.pageId, 'a2'])
    const after = box.getSnapshot().chapters[0].photos
    expect(after.map((p) => [p.order, p.state])).toEqual([[1, 'sent'], [2, 'sent']])
    expect(after[0].pageId).not.toBe(first.pageId)
    expect(ch.id).toBeTruthy()
  })

  it('a retake of a photo not sent yet sends only the new one', async () => {
    const ch = await box.startChapter({ pid: 'p1' })
    server.fail.push(offline())
    await box.pump()
    const p = await box.addPhoto(ch.id, photo('a'))
    await box.retakePhoto(p.id, photo('a2'))
    await vi.advanceTimersByTimeAsync(5000)
    await box.pump()
    expect(server.log.filter((e) => e[0] === 'upload').map((e) => e[1])).toEqual(['a2'])
  })

  it('a retake of a page this phone didn’t send goes on its own, then is forgotten', async () => {
    server.pages.set('pgX', { box: 'boxZ', bytes: 'old' })
    server.byId.set('boxZ', { id: 'boxZ', number: 9 })
    await box.retakePage('p1', 'pgX', photo('new'))
    await box.pump()
    expect(server.log).toEqual([['replace', 'pgX', 'new']])
    expect(box.getSnapshot().chapters).toEqual([])
  })

  it('a retake of a page this phone sent goes into its chapter', async () => {
    await chapterOf(['a'])
    const sent = box.getSnapshot().chapters[0].photos[0]
    await box.retakePage('p1', sent.pageId, photo('a2'))
    await box.pump()
    expect(server.log.slice(-2)).toEqual([['replace', sent.pageId, 'a2'], ['captured', 'box1', undefined]])
    expect(box.getSnapshot().chapters).toHaveLength(1)
  })

  it('a retake after Done is sent and the chapter finished again', async () => {
    await chapterOf(['a'])
    const sent = box.getSnapshot().chapters[0].photos[0]
    await box.retakePhoto(sent.id, photo('a2'))
    await box.pump()
    expect(server.log.slice(-2)).toEqual([['replace', sent.pageId, 'a2'], ['captured', 'box1', undefined]])
  })

  it('discarding a sent chapter deletes its box with the photos it holds', async () => {
    const ch = await chapterOf(['a', 'b'], { close: false })
    const ids = box.getSnapshot().chapters[0].photos.map((p) => p.pageId)
    await box.discard(ch.id)
    await box.pump()
    expect(server.log.at(-1)).toEqual(['deleteBox', 'box1', ids])
    expect(box.getSnapshot().chapters).toEqual([])
  })

  it('a chapter finished with no photos is simply forgotten', async () => {
    const ch = await box.startChapter({ pid: 'p1' })
    await box.closeChapter(ch.id)
    await box.pump()
    expect(server.log).toEqual([])
    expect(box.getSnapshot().chapters).toEqual([])
  })
})

describe('PDFs and chapters already on the computer', () => {
  const pdf = (pages) => ({ blob: new Blob([pages], { type: 'application/pdf' }), name: 'scan.pdf', type: 'application/pdf' })

  it('sends a PDF whole, its pages landing in order in the chapter', async () => {
    const ch = await box.startChapter({ pid: 'p1', origin: 'share:abc' })
    await box.addPhoto(ch.id, photo('cover'))
    await box.addPhoto(ch.id, pdf('a|b|c'))
    await box.closeChapter(ch.id)
    await box.pump()
    expect(server.log).toEqual([
      ['create', 'share:abc', true], ['upload', 'cover', 'box1'], ['pdf', 'a|b|c', 'box1'],
      ['captured', 'box1', 1],
    ])
    const sent = box.getSnapshot().chapters[0].photos[1]
    expect(sent.pageIds).toEqual(['pg2', 'pg3', 'pg4'])
  })

  it('counts a PDF whose every page is already in as elsewhere', async () => {
    await chapterOf(['a', 'b'])
    const ch = await box.startChapter({ pid: 'p1' })
    await box.addPhoto(ch.id, pdf('a|b'))
    await box.closeChapter(ch.id)
    await box.pump()
    expect(server.log.at(-1)).toEqual(['deleteBox', 'box2', []])
  })

  it('deletes all of a sent PDF’s pages when it is removed', async () => {
    const ch = await box.startChapter({ pid: 'p1' })
    const sent = await box.addPhoto(ch.id, pdf('x|y'))
    await box.pump()
    await box.removePhoto(sent.id)
    await box.pump()
    expect(server.log.at(-1)).toEqual(['delete', 'pg1', 'pg2'])
  })

  it('adds to the end of a chapter already on the computer, without making a box', async () => {
    server.byId.set('desk1', { id: 'desk1', number: 4 })
    const ch = await box.startChapter({ pid: 'p1', box: 'desk1' })
    await box.addPhoto(ch.id, photo('late'))
    await box.closeChapter(ch.id)
    await box.pump()
    expect(server.log).toEqual([['upload', 'late', 'desk1'], ['captured', 'desk1', undefined]])
  })
})

describe('the rules on their own', () => {
  it('classifies failures', () => {
    expect(classify(offline())).toBe('offline')
    expect(classify(failure(502, 'Bad gateway'))).toBe('offline')
    expect(classify(failure(403, 'key'))).toBe('blocked')
    expect(classify(failure(400, 'Invalid host header', 'Bad Request'))).toBe('blocked')
    expect(classify(failure(404, "That chapter box isn’t in this novel any more."))).toBe('box-gone')
    expect(classify(failure(400, 'That image is bigger than 25 MB.'))).toBe('refused')
  })

  it('waits for an earlier chapter’s box even when it is stopped', () => {
    const chapters = [
      { id: 'c1', pid: 'p1', seq: 1, state: 'closed', box: null, stop: { kind: 'refused' } },
      { id: 'c2', pid: 'p1', seq: 2, state: 'open', box: null, stop: null },
    ]
    const photos = new Map([['c1', [{ id: 'x', state: 'waiting', order: 1 }]], ['c2', [{ id: 'y', state: 'waiting', order: 1 }]]])
    expect(nextAction(chapters, photos)).toBeNull()
  })
})
