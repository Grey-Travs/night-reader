import { act, createEvent, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import PagesPage from './PagesPage'
import { api } from '../api'
import { TILE_MIME } from '../pageBoxes'
import { resetUploads } from '../uploadQueue'

// The scanned-page workbench shipped unusable. "Read N pages" called api.readPages
// directly and threw away the job_id it returns, so nothing ever attached the SSE
// stream and `running` stayed false. The rail's 2.5s poll is gated on `running`, so
// every row sat on "Queued" forever, the Activity console stayed empty, and the
// transcribed Korean never appeared however long you waited. The button stayed
// enabled, so it looked like the click had simply done nothing.
//
// `npm run build` passed throughout — the code was valid, it just never called the
// one function that starts a job. Only driving the component catches that, which is
// what these do.
//
// Chapter boxes brought their own ways to scramble a novel, and each of them looked
// fine on screen: a duplicate photo's chapter adopted for the rest of a drop, two drops
// interleaving page by page, a stale poll putting back the order from before a drag,
// quick taps reaching the server out of order. These drive the page through each one.

const outletContext = vi.fn()
vi.mock('react-router-dom', async (actual) => ({
  ...(await actual()),
  useOutletContext: () => outletContext(),
}))

vi.mock('../api', () => ({
  api: {
    pages: vi.fn(),
    page: vi.fn(),
    readPages: vi.fn(),
    verifyPages: vi.fn(),
    stitchPages: vi.fn(),
    savePage: vi.fn(),
    deletePages: vi.fn(),
    movePages: vi.fn(),
    createPageBatch: vi.fn(),
    reorderPageBatches: vi.fn(),
    renamePageBatch: vi.fn(),
    deletePageBatch: vi.fn(),
    buildChapters: vi.fn(),
    uploadPage: vi.fn(),
    pageImageUrl: (pid, id) => `/api/projects/${pid}/pages/${id}/image`,
  },
}))

const toast = vi.fn()
vi.mock('../toast', () => ({ useToast: () => toast }))
const confirm = vi.fn()
vi.mock('../confirm', () => ({ useConfirm: () => confirm }))

const PID = 'abcdef012345'

// Mirrors server/pages.py summary() — pages/batches/build/counts/totals plus the
// chapter count and rev, with each row carrying `has_text` and `chars` that only exist
// on the row, never the record. counts() seeds EVERY status to 0, so the keys are
// always present.
const STATUSES = ['new', 'queued', 'ocr-running', 'ok', 'needs-check', 'edited',
  'skipped', 'failed']

function page(over = {}) {
  return {
    id: 'aaaaaaaa', seq: 1, file: 'page-0001.jpg', name: '', bytes: 1000,
    sha256: 'x', batch: 'b1', added_at: '2026-01-01T00:00:00+00:00',
    status: 'new', confidence: '', notes: [], heading: null,
    starts_mid_sentence: false, ends_mid_sentence: false, ends_mid_word: false,
    join_prev: '', join_prev_source: '', join_glue: 'space', join_reason: '', join_prev_id: null,
    hangul_fraction: 0.9, chars: 0, has_text: false, ocr: null, verify: null,
    error: null, ...over,
  }
}

// A box row: the server works out its state and what holds it back, and numbers a box
// not built yet after the built chapters.
function box(over = {}) {
  return {
    id: 'b1', label: '', title: 'Chapter 1', at: '2026-01-01T00:00:00+00:00', count: 2,
    chapter: null, number: 1, state: 'unread', blockers: [], ...over,
  }
}

function railPayload(pages, { batches, rev = 1, chapterCount = 0 } = {}) {
  const counts = Object.fromEntries(STATUSES.map((s) => [s, 0]))
  for (const p of pages) counts[p.status] = (counts[p.status] || 0) + 1
  counts.total = pages.length
  return {
    pages,
    batches: batches || [box({ count: pages.length })],
    build: null,
    counts,
    totals: { cost_usd: 0 },
    chapter_count: chapterCount,
    rev,
  }
}

// What a structural write answers with (contract.md): the fresh summary and its rev.
const summary = (rows, { batches, rev = 2, chapterCount } = {}) => ({
  rev, pages: railPayload(rows, { batches, rev, chapterCount }),
})

const failure = (status, message) => Object.assign(new Error(message), { status })

// server/pages.py box_blockers and box_state, in short: what holds a box not built yet
// back, and the one word its pill shows for that.
const READ = ['ok', 'edited', 'needs-check']
const HOLDS = [
  ['reading', (p) => ['queued', 'ocr-running'].includes(p.status), 'still being read'],
  ['unread', (p) => !['queued', 'ocr-running', 'failed', ...READ].includes(p.status), 'not read yet'],
  ['failed', (p) => p.status === 'failed', 'couldn’t be read'],
  ['no-text', (p) => READ.includes(p.status) && !p.has_text, 'no text'],
  ['to-check', (p) => p.status === 'needs-check' && p.has_text, 'needs checking'],
]

function holdUp(rows) {
  if (!rows.length) {
    return { state: 'empty', blockers: [{ kind: 'empty', count: 0, page_ids: [], message: 'No photos yet.' }] }
  }
  const live = rows.filter((p) => p.status !== 'skipped')
  if (!live.length) {
    return { state: 'skipped', blockers: [{ kind: 'all-skipped', count: rows.length,
                                            page_ids: rows.map((p) => p.id), message: 'Every photo skipped.' }] }
  }
  const blockers = HOLDS.map(([kind, holds, says]) => [kind, live.filter(holds), says])
    .filter(([, group]) => group.length)
    .map(([kind, group, says]) => ({ kind, count: group.length, page_ids: group.map((p) => p.id),
                                     message: `${group.map((p) => p.name || p.id).join(', ')}: ${says}.` }))
  const kinds = new Set(blockers.map((b) => b.kind))
  const state = kinds.has('reading') ? 'reading'
    : kinds.has('unread') || kinds.has('failed') ? 'unread' : blockers.length ? 'to-check' : 'ready'
  return { state, blockers }
}

// The server's side of every write the tab sends, kept as consistent as the real one
// (contract.md): each photo lands at the end of its box, a photo already in the novel —
// here, one with the same name — answers as a duplicate saying where it is, a new box
// goes at the end (or after `after`), every answer carries a rev that only goes up, and a
// box not built yet says what holds it back from the photos in it as they are now.
// Mocks that answered otherwise let tests pass that the real server would fail: an upload
// answer with no summary never put its photo on screen, a page save that always answered
// rev 2 from box b1 hid every rev-ordering bug, and box pills that never followed a Skip
// or a Looks good couldn't show a screen that failed to.
function fakeServer(rows, batches, chapterCount = 0) {
  let list = [...rows]
  let boxes = [...batches]
  const texts = new Map()
  let rev = 1
  let photos = rows.length
  let made = 8
  const now = () => summary(list, { batches: boxes, rev, chapterCount })
  const recount = () => {
    let k = 0
    boxes = boxes.map((b) => {
      const inside = list.filter((p) => p.batch === b.id)
      if (b.chapter != null || b.legacy) return { ...b, count: inside.length }
      k += 1
      return { ...b, count: inside.length, number: chapterCount + k, ...holdUp(inside) }
    })
  }
  const saved = () => {
    const rank = (p) => boxes.findIndex((b) => b.id === p.batch)
    list = [...list].sort((x, y) => rank(x) - rank(y))
    recount()
    rev += 1
    return now()
  }
  const row = (id) => {
    const found = list.find((p) => p.id === id)
    if (!found) throw failure(404, 'That page isn’t in this novel.')
    return found
  }
  return {
    pages: async () => now().pages,
    page: async (_pid, id) => ({ ...row(id), text: texts.get(id) ?? '', raw_text: '' }),
    upload: async (_pid, f, { batch = '', label = '' } = {}) => {
      const dup = list.find((p) => p.name === f.name)
      if (dup) {
        return {
          page: { id: dup.id }, duplicate: true, existing_batch: dup.batch,
          existing_number: boxes.find((b) => b.id === dup.batch)?.number ?? null,
          total: list.length, ...now(),
        }
      }
      let into = batch
      if (!into) {
        into = `b${++made}`
        boxes = [...boxes, box({ id: into, label, count: 0, state: 'empty' })]
      } else if (!boxes.some((b) => b.id === into)) {
        throw failure(404, 'That chapter box isn’t in this novel any more.')
      }
      photos += 1
      const id = `ffff${String(photos).padStart(4, '0')}`
      list = [...list, page({ id, seq: photos, batch: into, name: f.name })]
      const res = saved()
      return {
        page: { id }, duplicate: false, batch: into,
        number: boxes.find((b) => b.id === into).number, total: list.length, ...res,
      }
    },
    createBox: async (_pid, { label = '', after = null, ids = [] } = {}) => {
      const id = `b${++made}`
      const at = after == null ? boxes.length : boxes.findIndex((b) => b.id === after) + 1
      boxes = [...boxes.slice(0, at), box({ id, label, count: 0, state: 'empty' }), ...boxes.slice(at)]
      const moving = ids.map((pageId) => ({ ...row(pageId), batch: id }))
      list = [...list.filter((p) => !ids.includes(p.id)), ...moving]
      const res = saved()
      return { batch: res.pages.batches.find((b) => b.id === id), ...res }
    },
    deleteBox: async (_pid, id, { expect_page_ids: expected } = {}) => {
      if (!boxes.some((b) => b.id === id)) throw failure(404, 'That chapter box isn’t in this novel any more.')
      const inside = list.filter((p) => p.batch === id).map((p) => p.id)
      if (expected && (expected.length !== inside.length || inside.some((p) => !expected.includes(p)))) {
        throw failure(409, 'The photos in that chapter changed. Look again before deleting it.')
      }
      boxes = boxes.filter((b) => b.id !== id)
      list = list.filter((p) => p.batch !== id)
      return { ok: true, removed: inside.length, ...saved() }
    },
    // Every box, once each: an order that leaves one out is refused, not guessed at.
    reorder: async (_pid, ids) => {
      if ([...ids].sort().join() !== boxes.map((b) => b.id).sort().join()) {
        throw failure(400, 'That chapter order doesn’t match this novel’s chapters. Reload and try again.')
      }
      boxes = ids.map((id) => boxes.find((b) => b.id === id))
      return { ok: true, ...saved() }
    },
    // A text saved without a status marks the page edited, unless it's skipped; a status
    // sent with it wins.
    save: async (_pid, id, { text, ...fields }) => {
      const was = row(id)
      let next = { ...was, ...fields }
      if (text !== undefined) {
        texts.set(id, text)
        const status = fields.status ?? (was.status === 'skipped' ? 'skipped' : 'edited')
        next = { ...next, chars: text.length, has_text: !!text.trim(), status }
      }
      list = list.map((p) => (p.id === id ? next : p))
      recount()
      rev += 1
      const { has_text: _rowOnly, ...record } = next
      return { ...record, text: texts.get(id) ?? '', raw_text: '', rev }
    },
  }
}

// One per test: a Pages tab opened again in the same test is the same novel.
let server = null
const serve = (...args) => server.upload(...args)

// Holds the next upload on its way. The server sees it only once released, and then
// answers as it would have.
function holdUpload() {
  const gate = deferred()
  api.uploadPage.mockImplementationOnce(async (...args) => {
    await gate.promise
    return serve(...args)
  })
  return gate
}

function renderPages({
  submitTask, running = false, pages: rows, batches, chapters = [], at = '', chapterCount,
} = {}) {
  const submit = submitTask || vi.fn(async (call) => call())
  const ctx = {
    pid: PID,
    running,
    submitTask: submit,
    showError: vi.fn(),
    reload: vi.fn(),
    setProjectMeta: vi.fn(),
    // As ProjectLayout answers it: the novel on screen now, whichever render is asking.
    isShowing: (id) => outletContext().pid === id,
    chapters,
  }
  outletContext.mockReturnValue(ctx)
  const list = rows || [page(), page({ id: 'bbbbbbbb', seq: 2 })]
  const payload = railPayload(list, { batches, chapterCount })
  server ||= fakeServer(list, payload.batches, chapterCount)
  api.pages.mockImplementation(server.pages)
  api.page.mockImplementation(server.page)
  api.uploadPage.mockImplementation(server.upload)
  api.createPageBatch.mockImplementation(server.createBox)
  api.deletePageBatch.mockImplementation(server.deleteBox)
  api.reorderPageBatches.mockImplementation(server.reorder)
  api.savePage.mockImplementation(server.save)
  const tree = () => (
    <MemoryRouter initialEntries={[`/novel/${PID}/pages${at}`]}><PagesPage /></MemoryRouter>
  )
  const view = render(tree())
  const setContext = (over) => {
    outletContext.mockReturnValue({ ...ctx, ...over })
    view.rerender(tree())
  }
  const setRunning = (value) => setContext({ running: value })
  const setPid = (value) => setContext({ pid: value })
  return { submit, list, ctx, payload, setRunning, setPid, ...view }
}

const file = (name, type = 'image/jpeg') => new File(['x'], name, { type })

function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

// Lets queued writes and uploads run, for asserting that something did NOT happen.
const settle = () => new Promise((r) => setTimeout(r, 0))

// Under fake timers React still renders on the real scheduler it captured at import, so
// without act() the DOM can trail the state — and a poll that wrongly put back the old
// order would go unseen by an assertion made in between.
const tick = (ms) => act(() => vi.advanceTimersByTimeAsync(ms))

const filesDrop = (files) => ({ dataTransfer: { types: ['Files'], files } })

// A photo drag as a browser runs it: the drop carries only the types its dragstart set,
// so a tile that stopped calling setData (Firefox starts no drag without it) can't be
// dragged here either. One drag for the dragstart and the drop, as in a browser.
function tileDrag() {
  const data = new Map()
  return {
    dataTransfer: {
      get types() { return [...data.keys()] },
      setData: vi.fn((type, value) => { data.set(type, value) }),
      getData: (type) => data.get(type) ?? '',
    },
  }
}

function dragPhoto(from, to) {
  const drag = tileDrag()
  fireEvent.dragStart(from, drag)
  fireEvent.drop(to, drag)
  return drag
}
const names = () => api.uploadPage.mock.calls.map((c) => c[1].name)
const sentTo = () => api.uploadPage.mock.calls.map((c) => c[2].batch)
const tileIds = (region) => Array.from(region.querySelectorAll('[data-page-id]'))
  .map((el) => el.getAttribute('data-page-id'))
const tiles = (region) => within(region).getAllByRole('button', { name: /^Photo \d/ })
const chapter = (n) => screen.findByRole('region', { name: `Chapter ${n}` })
const textBox = () => screen.findByRole('textbox', { name: 'Korean text read from this photo' })
const newChapterZone = () => screen.findByRole('button', { name: /^New chapter — drop photos here/ })

beforeEach(() => {
  vi.resetAllMocks()
  // The upload queue outlives any one Pages tab, so it outlives a test too.
  resetUploads()
  server = null
  confirm.mockResolvedValue(true)
  api.readPages.mockResolvedValue({ job_id: 'job-1', kind: 'ocr', current: 1, pending: [2] })
  api.verifyPages.mockResolvedValue({ job_id: 'job-2', kind: 'ocr-verify', current: 1, pending: [] })
})

describe('PagesPage renders', () => {
  it('renders the boxes without throwing', async () => {
    renderPages()
    await waitFor(() => expect(api.pages).toHaveBeenCalled())
    expect(await screen.findByText(/Read 2 pages/i)).toBeTruthy()
  })

  it('renders with no pages at all', async () => {
    renderPages({ pages: [], batches: [] })
    expect(await screen.findByText(/Drop chapter 1.s photos here/)).toBeTruthy()
  })
})

describe('starting a page-reading run', () => {
  it('goes through submitTask, so the job is actually attached', async () => {
    // The whole bug in one assertion: calling api.readPages without submitTask
    // discards the job_id, and nothing ever streams or polls.
    const { submit } = renderPages()
    await waitFor(() => expect(api.pages).toHaveBeenCalled())

    fireEvent.click(await screen.findByRole('button', { name: /Read 2 pages/i }))

    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1))
    expect(api.readPages).toHaveBeenCalledWith('abcdef012345', [])
  })

  it('double-checking flagged pages goes through submitTask too', async () => {
    const rows = [page({ status: 'needs-check', has_text: true, chars: 20 })]
    const { submit } = renderPages({ pages: rows })
    await waitFor(() => expect(api.pages).toHaveBeenCalled())

    fireEvent.click(await screen.findByRole('button', { name: /Double-check/i }))

    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1))
    expect(api.verifyPages).toHaveBeenCalledWith('abcdef012345', [])
  })

  it('refreshes the rail and confirms, once the job really started', async () => {
    renderPages()
    await waitFor(() => expect(api.pages).toHaveBeenCalledTimes(1))

    fireEvent.click(await screen.findByRole('button', { name: /Read 2 pages/i }))

    await waitFor(() => expect(api.pages).toHaveBeenCalledTimes(2))
    expect(toast).toHaveBeenCalledWith(expect.stringMatching(/Reading 2 pages/))
  })

  it('says nothing started when submitTask reports a failure', async () => {
    // submitTask returns null on error, having already shown the dialog. Claiming
    // "Reading 2 pages…" then would be a lie the user waits on.
    const submit = vi.fn(async () => null)
    renderPages({ submitTask: submit })
    await waitFor(() => expect(api.pages).toHaveBeenCalledTimes(1))

    fireEvent.click(await screen.findByRole('button', { name: /Read 2 pages/i }))

    await waitFor(() => expect(submit).toHaveBeenCalled())
    expect(toast).not.toHaveBeenCalled()
    expect(api.pages).toHaveBeenCalledTimes(1)
  })

  it('reads just one chapter’s unread photos from its box', async () => {
    const { submit } = renderPages({
      pages: [page({ status: 'ok', has_text: true }), page({ id: 'bbbbbbbb', seq: 2 })],
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Read 1' }))
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1))
    expect(api.readPages).toHaveBeenCalledWith(PID, ['bbbbbbbb'])
  })
})

describe('the rail poll', () => {
  it('does not poll while nothing is running', async () => {
    vi.useFakeTimers()
    try {
      renderPages({ running: false })
      await vi.waitFor(() => expect(api.pages).toHaveBeenCalledTimes(1))
      await vi.advanceTimersByTimeAsync(8000)
      expect(api.pages).toHaveBeenCalledTimes(1)
    } finally { vi.useRealTimers() }
  })

  it('polls while a job runs, which is what keeps the rows moving', async () => {
    // Gated on `running`. Before the fix `running` never became true from this
    // screen, so this poll never started and every row stayed on "Queued".
    vi.useFakeTimers()
    try {
      renderPages({ running: true })
      await vi.waitFor(() => expect(api.pages).toHaveBeenCalledTimes(1))
      await vi.advanceTimersByTimeAsync(2600)
      expect(api.pages.mock.calls.length).toBeGreaterThan(1)
    } finally { vi.useRealTimers() }
  })

  it('refreshes the boxes once more when the job finishes', async () => {
    // The last page usually finishes between two ticks, and with the poll stopped it
    // sat on "Reading" until something else happened to reload.
    const { setRunning } = renderPages({ running: true })
    await waitFor(() => expect(api.pages).toHaveBeenCalledTimes(1))
    setRunning(false)
    await waitFor(() => expect(api.pages).toHaveBeenCalledTimes(2))
  })

  it('a poll that answers after a local move does not undo it', async () => {
    vi.useFakeTimers()
    try {
      const { payload } = renderPages({ running: true })
      await vi.waitFor(() => expect(api.pages).toHaveBeenCalledTimes(1))
      const moved = deferred()
      api.movePages.mockReturnValue(moved.promise)
      const region = await vi.waitFor(() => screen.getByRole('region', { name: 'Chapter 1' }))
      const [a, b] = tiles(region)
      dragPhoto(a, b)
      expect(tileIds(region)).toEqual(['bbbbbbbb', 'aaaaaaaa'])

      // The poll still answers with the order from before the move.
      await tick(2600)
      expect(api.pages.mock.calls.length).toBeGreaterThan(1)
      expect(tileIds(region)).toEqual(['bbbbbbbb', 'aaaaaaaa'])

      moved.resolve({
        ok: true, changed: true, rev: 2,
        pages: { ...payload, rev: 2, pages: [{ ...payload.pages[1] }, { ...payload.pages[0] }] },
      })
      await tick(0)
      expect(tileIds(screen.getByRole('region', { name: 'Chapter 1' }))).toEqual(['bbbbbbbb', 'aaaaaaaa'])
    } finally { vi.useRealTimers() }
  })

  it('drops an answer older than what the screen already shows', async () => {
    // A refresh that left before a move and arrives after it carries the order from
    // before. Its rev says so.
    const { payload } = renderPages()
    const region = await chapter(1)
    const slow = deferred()
    api.pages.mockReturnValueOnce(slow.promise)
    fireEvent.click(screen.getByRole('button', { name: /Read 2 pages/ }))
    await waitFor(() => expect(api.pages).toHaveBeenCalledTimes(2))

    api.movePages.mockResolvedValue({
      ok: true, changed: true, rev: 2,
      pages: { ...payload, rev: 2, pages: [{ ...payload.pages[1] }, { ...payload.pages[0] }] },
    })
    const [a, b] = tiles(region)
    dragPhoto(a, b)
    await waitFor(() => expect(api.movePages).toHaveBeenCalled())
    await settle()

    slow.resolve(payload)   // rev 1
    await settle()
    expect(tileIds(region)).toEqual(['bbbbbbbb', 'aaaaaaaa'])
  })

  it('a photo’s upload answer that arrives while a move is pending does not undo the move', async () => {
    // The upload's summary was made before the server saw the move, so it carries the old
    // order. Shown, it would put a photo back where it was dragged from.
    const { list } = renderPages()
    const region = await chapter(1)
    const moved = deferred()
    api.movePages.mockReturnValue(moved.promise)
    const [a, b] = tiles(region)
    dragPhoto(a, b)

    fireEvent.drop(region, filesDrop([file('IMG_3.jpg')]))
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(1))
    await settle()
    expect(tileIds(region)).toEqual(['bbbbbbbb', 'aaaaaaaa'])

    moved.resolve({ ok: true, changed: true, ...summary([list[1], list[0], page({ id: 'ffff0003', seq: 3 })], { rev: 3 }) })
    await waitFor(() => expect(tileIds(region)).toEqual(['bbbbbbbb', 'aaaaaaaa', 'ffff0003']))
  })

  it('a refresh sent before a join is changed, and answered after, does not put the old join back', async () => {
    // The join's save is answered by a fetch of the boxes, and it left the screen's rev
    // where it was: an older refresh landing after it was taken as current, put the old
    // join back — and kept it there when the fetch after the save failed.
    const { payload } = renderPages({
      pages: [page(), page({ id: 'bbbbbbbb', seq: 2, join_prev: 'sentence', join_prev_source: 'auto' })],
    })
    const seam = (label) => screen.findByRole('button', { name: `Join before photo 2: ${label}` })
    const slow = deferred()
    const after = deferred()
    const read = await screen.findByRole('button', { name: /Read 2 pages/ })
    api.pages.mockReturnValueOnce(slow.promise).mockReturnValueOnce(after.promise)
    fireEvent.click(read)
    await waitFor(() => expect(api.pages).toHaveBeenCalledTimes(2))

    fireEvent.click(await seam('Sentence continues'))
    await waitFor(() => expect(api.pages).toHaveBeenCalledTimes(3))   // the fetch after the save
    slow.resolve(payload)   // rev 1, from before the click
    await settle()
    expect(await seam('New paragraph')).toBeTruthy()

    after.reject(new Error('Failed to fetch'))
    await settle()
    expect(await seam('New paragraph')).toBeTruthy()
  })
})

describe('the chapter boxes', () => {
  it('shows one box per chapter, in order, with its photos numbered from 1', async () => {
    renderPages({
      pages: [page(), page({ id: 'bbbbbbbb', seq: 2 }), page({ id: 'cccccccc', seq: 3, batch: 'b2' })],
      batches: [box(), box({ id: 'b2', number: 2, count: 1 })],
    })
    await chapter(2)
    const regions = screen.getAllByRole('region')
    expect(regions.map((r) => r.getAttribute('aria-label'))).toEqual(['Chapter 1', 'Chapter 2'])
    expect(within(regions[0]).getByRole('button', { name: /^Photo 2 of Chapter 1/ }))
      .toHaveAttribute('data-page-id', 'bbbbbbbb')
    expect(within(regions[1]).getByRole('button', { name: /^Photo 1 of Chapter 2/ }))
      .toHaveAttribute('data-page-id', 'cccccccc')
  })

  // Where Add chapter takes the reader: the box scrolled into view, its + ready for photos.
  function watchScrolls() {
    const scrolled = []
    vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(function scrollIntoView() {
      scrolled.push(this)
    })
    return scrolled
  }

  it('Add chapter makes a box at the end and shows it', async () => {
    const { list } = renderPages()
    const scrolled = watchScrolls()
    const made = box({ id: 'b2', number: 2, count: 0, state: 'empty' })
    api.createPageBatch.mockResolvedValue({
      batch: made, rev: 2, pages: railPayload(list, { batches: [box(), made], rev: 2 }),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Add chapter' }))
    await waitFor(() => expect(api.createPageBatch).toHaveBeenCalledWith(PID, {}))
    const region = await chapter(2)
    await waitFor(() => expect(document.activeElement)
      .toBe(within(region).getByRole('button', { name: 'Add photos to Chapter 2' })))
    expect(scrolled).toContain(region)
  })

  it('Add chapter goes to an empty last box instead of making another', async () => {
    // An empty box holds up every box after it at Build time, so a second one would
    // only be in the way. On a long novel it's off-screen, so the click has to go there.
    renderPages({ batches: [box(), box({ id: 'b2', number: 2, count: 0, state: 'empty' })] })
    const region = await chapter(2)
    const scrolled = watchScrolls()
    fireEvent.click(screen.getByRole('button', { name: 'Add chapter' }))
    await waitFor(() => expect(document.activeElement)
      .toBe(within(region).getByRole('button', { name: 'Add photos to Chapter 2' })))
    expect(scrolled).toContain(region)
    expect(api.createPageBatch).not.toHaveBeenCalled()
  })

  it('built chapters cannot be moved or deleted', async () => {
    renderPages({
      pages: [page({ status: 'ok', has_text: true }), page({ id: 'cccccccc', seq: 3, batch: 'b2' })],
      batches: [box({ chapter: 1, state: 'built', count: 1 }), box({ id: 'b2', number: 2, count: 1 })],
      chapterCount: 1,
      chapters: [{ index: 1, title: 'Chapter 1', status: 'pending', source_changed: false }],
    })
    await chapter(1)
    expect(screen.queryByRole('button', { name: 'Delete Chapter 1' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Move Chapter 1 down' })).toBeNull()
    const up = screen.getByRole('button', { name: 'Move Chapter 2 up' })
    expect(up).toBeDisabled()
    // The only way to put a chapter in front of a built one, so the refusal names it.
    expect(up).toHaveAttribute('title', expect.stringMatching(/Start over \(in Build chapters\)/))
    expect(screen.getByRole('button', { name: 'Delete Chapter 2' })).toBeEnabled()
  })

  it('moving a box sends the whole new order', async () => {
    const rows = [page({ status: 'ok' }), page({ id: 'bbbbbbbb', batch: 'b2' }), page({ id: 'cccccccc', batch: 'b3' })]
    const built = box({ chapter: 1, state: 'built', count: 1 })
    renderPages({
      pages: rows,
      batches: [built, box({ id: 'b2', number: 2, count: 1 }), box({ id: 'b3', number: 3, count: 1 })],
      chapterCount: 1,
    })
    const answered = deferred()
    api.reorderPageBatches.mockReturnValue(answered.promise)
    fireEvent.click(await screen.findByRole('button', { name: 'Move Chapter 2 down' }))
    await waitFor(() => expect(api.reorderPageBatches).toHaveBeenCalledWith(PID, ['b1', 'b3', 'b2']))
    const second = () => within(screen.getByRole('region', { name: 'Chapter 2' }))
      .getByRole('button', { name: /^Photo 1/ })
    // Shown at once: the box that was third is now Chapter 2.
    expect(second()).toHaveAttribute('data-page-id', 'cccccccc')

    answered.resolve({ ok: true, ...summary([rows[0], rows[2], rows[1]], {
      batches: [built, box({ id: 'b3', number: 2, count: 1 }), box({ id: 'b2', number: 3, count: 1 })],
    }) })
    await settle()
    expect(second()).toHaveAttribute('data-page-id', 'cccccccc')
  })

  it('a box moved while a new chapter’s box is on its way sends an order with that box in it', async () => {
    // Planned at the click, the order left out the box still being made ahead of it in the
    // queue, and the server refused an order that doesn't name every box.
    const { ctx } = renderPages({
      pages: [page(), page({ id: 'cccccccc', seq: 2, batch: 'b2' })],
      batches: [box({ count: 1 }), box({ id: 'b2', number: 2, count: 1 })],
    })
    const making = deferred()
    api.createPageBatch.mockImplementationOnce(async (...args) => {
      await making.promise
      return server.createBox(...args)
    })
    fireEvent.drop(await newChapterZone(), filesDrop([file('C3_1.jpg')]))
    fireEvent.click(await screen.findByRole('button', { name: 'Move Chapter 2 up' }))
    making.resolve()

    await waitFor(() => expect(api.reorderPageBatches).toHaveBeenCalledWith(PID, ['b2', 'b1', 'b9']))
    await waitFor(() => expect(tileIds(screen.getByRole('region', { name: 'Chapter 3' }))).toEqual(['ffff0003']))
    expect(tileIds(screen.getByRole('region', { name: 'Chapter 1' }))).toEqual(['cccccccc'])
    expect(ctx.showError).not.toHaveBeenCalled()
  })

  it('a box move still waiting its turn when the tab moves to another novel is still sent', async () => {
    // Planned when its turn came, from the boxes on screen by then — the other novel's — it
    // found no move to make and was dropped without a word.
    const { setPid } = renderPages({
      pages: [page(), page({ id: 'bbbbbbbb', seq: 2, batch: 'b2' }), page({ id: 'cccccccc', seq: 3, batch: 'b3' })],
      batches: [box({ count: 1 }), box({ id: 'b2', number: 2, count: 1 }), box({ id: 'b3', number: 3, count: 1 })],
    })
    const first = deferred()
    api.reorderPageBatches.mockImplementationOnce(async (...args) => {
      await first.promise
      return server.reorder(...args)
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Move Chapter 3 up' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Move Chapter 2 up' }))
    setPid('999999999999')
    first.resolve()

    await waitFor(() => expect(api.reorderPageBatches).toHaveBeenCalledTimes(2))
    expect(api.reorderPageBatches.mock.calls).toEqual([
      [PID, ['b1', 'b3', 'b2']],
      [PID, ['b3', 'b1', 'b2']],
    ])
  })

  it('a box move still waiting when the tab leaves the novel and comes back lands as tapped', async () => {
    // Planned from the boxes fetched on the way back, before the move ahead of it had
    // landed, the second tap was taken for one already made and dropped, and the screen
    // kept an order the server didn't have.
    const { setPid } = renderPages({
      pages: [page(), page({ id: 'bbbbbbbb', seq: 2, batch: 'b2' }), page({ id: 'cccccccc', seq: 3, batch: 'b3' })],
      batches: [box({ count: 1 }), box({ id: 'b2', number: 2, count: 1 }), box({ id: 'b3', number: 3, count: 1 })],
    })
    const first = deferred()
    api.reorderPageBatches.mockImplementationOnce(async (...args) => {
      await first.promise
      return server.reorder(...args)
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Move Chapter 3 up' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Move Chapter 2 up' }))
    setPid('999999999999')
    setPid(PID)
    await screen.findByRole('button', { name: 'Move Chapter 3 up' })
    // The look taken once the first move lands answers slowly, as on a phone link: the
    // second move must not be planned from the boxes fetched on the way back meanwhile.
    const look = deferred()
    api.pages.mockImplementationOnce(async (...args) => {
      await look.promise
      return server.pages(...args)
    })
    first.resolve()

    await waitFor(() => expect(api.reorderPageBatches).toHaveBeenCalledTimes(2))
    expect(api.reorderPageBatches.mock.calls[1]).toEqual([PID, ['b3', 'b1', 'b2']])
    look.resolve()
    const onScreen = () => Array.from(document.querySelectorAll('section[id^="box-"]')).map((s) => s.id.slice(4))
    await waitFor(() => expect(onScreen()).toEqual(['b3', 'b1', 'b2']))
  })

  const fourBoxes = {
    pages: [page(), page({ id: 'bbbbbbbb', seq: 2, batch: 'b2' }), page({ id: 'cccccccc', seq: 3, batch: 'b3' }),
      page({ id: 'dddddddd', seq: 4, batch: 'b4' })],
    batches: [box({ count: 1 }), box({ id: 'b2', number: 2, count: 1 }), box({ id: 'b3', number: 3, count: 1 }),
      box({ id: 'b4', number: 4, count: 1 })],
  }

  it('a box move tapped back in a novel left mid-move is planned from the moves before it', async () => {
    // Planned from the boxes fetched on the way back, which the moves still queued then
    // weren't in, it sent an order that put back the last of them.
    const { setPid } = renderPages(fourBoxes)
    const first = deferred()
    api.reorderPageBatches.mockImplementationOnce(async (...args) => {
      await first.promise
      return server.reorder(...args)
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Move Chapter 4 up' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Move Chapter 3 up' }))
    setPid('999999999999')
    setPid(PID)
    fireEvent.click(await screen.findByRole('button', { name: 'Move Chapter 1 down' }))
    // The looks taken after each move land answer slowly, as on a phone link; only the
    // one the second move plans from comes back at once.
    const slow = [deferred(), deferred()]
    api.pages
      .mockImplementationOnce(async (...args) => { await slow[0].promise; return server.pages(...args) })
      .mockImplementationOnce(server.pages)
      .mockImplementationOnce(async (...args) => { await slow[1].promise; return server.pages(...args) })
    first.resolve()

    await waitFor(() => expect(api.reorderPageBatches).toHaveBeenCalledTimes(3))
    expect(api.reorderPageBatches.mock.calls.map((c) => c[1])).toEqual([
      ['b1', 'b2', 'b4', 'b3'], ['b1', 'b4', 'b2', 'b3'], ['b4', 'b1', 'b2', 'b3'],
    ])
    slow.forEach((s) => s.resolve())
    const onScreen = () => Array.from(document.querySelectorAll('section[id^="box-"]')).map((s) => s.id.slice(4))
    await waitFor(() => expect(onScreen()).toEqual(['b4', 'b1', 'b2', 'b3']))
  })

  it('a box move sent after the tab moved to another novel names a new chapter’s box made ahead of it', async () => {
    // Sent in the order planned at the tap, it left that box out, the server refused an
    // order that doesn't name every box, and the move was lost.
    const { ctx, setPid } = renderPages({
      pages: [page(), page({ id: 'cccccccc', seq: 2, batch: 'b2' })],
      batches: [box({ count: 1 }), box({ id: 'b2', number: 2, count: 1 })],
    })
    const making = deferred()
    api.createPageBatch.mockImplementationOnce(async (...args) => {
      await making.promise
      return server.createBox(...args)
    })
    fireEvent.drop(await newChapterZone(), filesDrop([file('C3_1.jpg')]))
    fireEvent.click(await screen.findByRole('button', { name: 'Move Chapter 2 up' }))
    setPid('999999999999')
    making.resolve()

    await waitFor(() => expect(api.reorderPageBatches).toHaveBeenCalledWith(PID, ['b2', 'b1', 'b9']))
    expect(ctx.showError).not.toHaveBeenCalled()
  })

  it('a box move that fails after the tab moved to another novel says where it was made', async () => {
    const { ctx, setPid } = renderPages(fourBoxes)
    const first = deferred()
    api.reorderPageBatches.mockImplementationOnce(() => first.promise)
    fireEvent.click(await screen.findByRole('button', { name: 'Move Chapter 4 up' }))
    setPid('999999999999')
    first.reject(failure(503, 'Failed to fetch'))
    await waitFor(() => expect(ctx.showError).toHaveBeenCalledWith(
      expect.anything(), { context: 'moving that chapter (begun before you changed novels)' }))
  })

  const twoBoxes = {
    pages: [page(), page({ id: 'cccccccc', batch: 'b2' }), page({ id: 'dddddddd', batch: 'b2' })],
    batches: [box({ count: 1 }), box({ id: 'b2', number: 2 })],
  }

  it('a chapter delete confirmed after the tab moved to another novel deletes nothing', async () => {
    // Answered on the next novel's screen, it deleted the chapter left behind, and
    // "Chapter 2 deleted" read as the next novel's.
    const { setPid } = renderPages(twoBoxes)
    const answer = deferred()
    confirm.mockImplementationOnce(() => answer.promise)
    fireEvent.click(await screen.findByRole('button', { name: 'Delete Chapter 2' }))
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1))
    setPid('999999999999')
    answer.resolve(true)
    await settle()
    await settle()
    expect(api.deletePageBatch).not.toHaveBeenCalled()
    expect(toast).not.toHaveBeenCalled()
  })

  it('deleting a box asks first, and sends the photos it expects to delete', async () => {
    renderPages(twoBoxes)
    api.deletePageBatch.mockResolvedValue({
      ok: true, removed: 2, ...summary([twoBoxes.pages[0]], { batches: [box({ count: 1 })] }),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Delete Chapter 2' }))
    await waitFor(() => expect(api.deletePageBatch)
      .toHaveBeenCalledWith(PID, 'b2', { expect_page_ids: ['cccccccc', 'dddddddd'] }))
    expect(confirm).toHaveBeenCalledWith(expect.objectContaining({
      danger: true, body: expect.stringMatching(/2 photos/),
    }))
    expect(confirm.mock.invocationCallOrder[0]).toBeLessThan(api.deletePageBatch.mock.invocationCallOrder[0])
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Chapter 2' })).toBeNull())
  })

  it('a box with photos is not deleted when the confirm is answered no', async () => {
    // Its photos go with it, and the text read out of them.
    renderPages(twoBoxes)
    confirm.mockResolvedValueOnce(false)
    fireEvent.click(await screen.findByRole('button', { name: 'Delete Chapter 2' }))
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1))
    await settle()
    expect(api.deletePageBatch).not.toHaveBeenCalled()
    expect(screen.getByRole('region', { name: 'Chapter 2' })).toBeTruthy()
  })

  it('an empty box being deleted goes at once, and a new chapter dropped meanwhile gets a box of its own', async () => {
    // Still on screen until the delete was answered, it was the empty box at the end, and
    // the new chapter's photos were sent into the box being deleted — and stopped there.
    renderPages({ batches: [box(), box({ id: 'b2', number: 2, count: 0, state: 'empty' })] })
    const deleting = deferred()
    api.deletePageBatch.mockImplementationOnce(async (...args) => {
      await deleting.promise
      return server.deleteBox(...args)
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Delete Chapter 2' }))
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Chapter 2' })).toBeNull())
    fireEvent.drop(await newChapterZone(), filesDrop([file('IMG_1.jpg')]))
    deleting.resolve()

    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(1))
    expect(api.deletePageBatch).toHaveBeenCalledWith(PID, 'b2', { expect_page_ids: [] })
    expect(sentTo()).toEqual(['b9'])
    await waitFor(() => expect(tileIds(screen.getByRole('region', { name: 'Chapter 2' }))).toEqual(['ffff0003']))
    expect(screen.queryByText(/Stopped at/)).toBeNull()
  })

  it('says what holds a box back, in the server’s words', async () => {
    renderPages({
      pages: [page({ status: 'needs-check', has_text: true }), page({ id: 'bbbbbbbb', status: 'ok' })],
      batches: [box({
        state: 'to-check',
        blockers: [
          { kind: 'no-text', count: 1, page_ids: ['bbbbbbbb'],
            message: 'IMG_5.jpg has no text. If it’s a picture or a blank page, mark it Skip.' },
          { kind: 'to-check', count: 1, page_ids: ['aaaaaaaa'], message: 'Photo 1 still needs a check.' },
        ],
      })],
    })
    expect(await screen.findByText('1 with no text · 1 to check')).toBeTruthy()
    expect(screen.getByText(/IMG_5\.jpg has no text/)).toBeTruthy()
  })

  it('lists nothing holding back a chapter that is built and up to date', async () => {
    // Built with photos still marked to check, the box kept saying to include them when
    // building — and building again answered that every chapter was up to date.
    renderPages({
      pages: [page({ status: 'needs-check', has_text: true }), page({ id: 'bbbbbbbb', seq: 2, status: 'ok', has_text: true })],
      batches: [box({
        chapter: 1, state: 'built',
        blockers: [{ kind: 'to-check', count: 1, page_ids: ['aaaaaaaa'],
                     message: 'IMG_1.jpg needs checking. Check its text and press Looks good, '
                       + 'or include photos marked to check when you build.' }],
      })],
      chapterCount: 1,
      chapters: [{ index: 1, title: 'Chapter 1', status: 'pending', source_changed: false }],
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Show the photos in Chapter 1' }))
    expect(await screen.findByRole('button', { name: /^Photo 1 of Chapter 1/ })).toBeTruthy()
    expect(screen.queryByText(/IMG_1\.jpg needs checking/)).toBeNull()
    expect(screen.getByText('Built · not translated yet')).not.toHaveAttribute('title')
  })

  it('a box’s Read also reads a photo with no text that nothing else would read', async () => {
    // Marked good before it was ever read, it had no text and a status no Read looks for,
    // and its box said to mark it Skip — dropping a page of the story.
    const { submit } = renderPages({
      pages: [page({ status: 'ok', has_text: true }), page({ id: 'bbbbbbbb', seq: 2, status: 'ok' }),
        page({ id: 'cccccccc', seq: 3, status: 'skipped' })],
      batches: [box({ count: 3, state: 'to-check' })],
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Read 1' }))
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1))
    expect(api.readPages).toHaveBeenCalledWith(PID, ['bbbbbbbb'])
  })

  it('says a built chapter needs translating again once its text has changed', async () => {
    // The chapter list reports such a chapter as "pending", which on its own reads as
    // never translated.
    renderPages({
      pages: [page({ status: 'ok', has_text: true })],
      batches: [box({ chapter: 1, state: 'built', count: 1 })],
      chapterCount: 1,
      chapters: [{ index: 1, title: 'Chapter 1', status: 'pending', source_changed: true }],
    })
    expect(await screen.findByText('Changed since translated — translate again')).toBeTruthy()
  })

  it('clicking a join between two photos cycles it', async () => {
    renderPages({
      pages: [page(), page({ id: 'bbbbbbbb', seq: 2, join_prev: 'sentence', join_prev_source: 'auto' })],
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Join before photo 2: Sentence continues' }))
    await waitFor(() => expect(api.savePage).toHaveBeenCalledWith(PID, 'bbbbbbbb', { join_prev: 'paragraph' }))
  })

  it('Accept N confident approves each of those pages', async () => {
    renderPages({
      pages: [page({ status: 'needs-check', confidence: 'high', has_text: true }),
        page({ id: 'bbbbbbbb', seq: 2, status: 'needs-check', confidence: 'low', has_text: true })],
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Accept 1 confident' }))
    await waitFor(() => expect(toast).toHaveBeenCalledWith('1 page accepted'))
    expect(api.savePage).toHaveBeenCalledTimes(1)
    expect(api.savePage).toHaveBeenCalledWith(PID, 'aaaaaaaa', { status: 'ok' })
  })

  it('a box’s pill follows its photos being marked good', async () => {
    // A save answers with its page alone, which says nothing of the box, so the pill
    // stayed on "1 to check" with nothing left to check.
    const rows = [page({ status: 'needs-check', confidence: 'high', has_text: true }),
      page({ id: 'bbbbbbbb', seq: 2, status: 'ok', has_text: true })]
    renderPages({ pages: rows, batches: [box({ state: 'to-check', blockers: [
      { kind: 'to-check', count: 1, page_ids: ['aaaaaaaa'], message: 'Photo 1 needs checking.' }] })] })
    const region = await chapter(1)
    expect(within(region).getByText('1 to check')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Accept 1 confident' }))
    expect(await within(region).findByText('Ready to build')).toBeTruthy()
  })
})

describe('adding photos', () => {
  it('uploads picked photos in name order into the box whose + was pressed', async () => {
    const { container } = renderPages({
      pages: [page(), page({ id: 'cccccccc', batch: 'b2' })],
      batches: [box({ count: 1 }), box({ id: 'b2', number: 2, count: 1 })],
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Add photos to Chapter 2' }))
    fireEvent.change(container.querySelector('input[type="file"]'), {
      target: { files: [file('IMG_10.jpg'), file('IMG_1.jpg'), file('IMG_9.jpg')] },
    })
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(3))
    expect(names()).toEqual(['IMG_1.jpg', 'IMG_9.jpg', 'IMG_10.jpg'])
    expect(sentTo()).toEqual(['b2', 'b2', 'b2'])
    // On screen from the uploads' own answers, at the end of that chapter.
    await waitFor(() => expect(tileIds(screen.getByRole('region', { name: 'Chapter 2' })))
      .toEqual(['cccccccc', 'ffff0003', 'ffff0004', 'ffff0005']))
    expect(tileIds(screen.getByRole('region', { name: 'Chapter 1' }))).toEqual(['aaaaaaaa'])
    await waitFor(() => expect(toast).toHaveBeenCalledWith('3 photos added to Chapter 2'))
  })

  it('a new-chapter drop makes its box first, and every photo goes into it', async () => {
    renderPages()
    // Named as the guide and the missed-drop toast name it.
    fireEvent.drop(await newChapterZone(), filesDrop([file('IMG_1.jpg'), file('IMG_2.jpg'), file('IMG_3.jpg')]))
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(3))
    expect(api.createPageBatch).toHaveBeenCalledWith(PID, {})
    expect(api.createPageBatch.mock.invocationCallOrder[0]).toBeLessThan(api.uploadPage.mock.invocationCallOrder[0])
    expect(sentTo()).toEqual(['b9', 'b9', 'b9'])
    await waitFor(() => expect(tileIds(screen.getByRole('region', { name: 'Chapter 2' })))
      .toEqual(['ffff0003', 'ffff0004', 'ffff0005']))
  })

  it('a new chapter keeps its place behind a drop still going in, whatever is added after it', async () => {
    // Its box used to be made by its first photo when that photo's turn came, at the end
    // of the novel as it was by then. A box added meanwhile went in ahead of it, and here
    // the photos dropped last became Chapter 2 and the ones dropped first Chapter 3.
    renderPages()
    const first = holdUpload()
    fireEvent.drop(await chapter(1), filesDrop([file('IMG_1.jpg')]))
    fireEvent.drop(await newChapterZone(), filesDrop([file('C4_1.jpg'), file('C4_2.jpg')]))
    await chapter(2)
    fireEvent.click(screen.getByRole('button', { name: 'Add chapter' }))
    await chapter(3)
    fireEvent.drop(await newChapterZone(), filesDrop([file('C5_1.jpg')]))
    await settle()
    first.resolve()

    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(4))
    expect(names()).toEqual(['IMG_1.jpg', 'C4_1.jpg', 'C4_2.jpg', 'C5_1.jpg'])
    expect(sentTo()).toEqual(['b1', 'b9', 'b9', 'b10'])
    await waitFor(() => expect(tileIds(screen.getByRole('region', { name: 'Chapter 3' }))).toEqual(['ffff0006']))
    expect(tileIds(screen.getByRole('region', { name: 'Chapter 2' }))).toEqual(['ffff0004', 'ffff0005'])
  })

  it('a photo dragged to New chapter while a new-chapter drop waits starts the chapter after it', async () => {
    renderPages({
      pages: [page(), page({ id: 'cccccccc', seq: 2, batch: 'b2' }), page({ id: 'dddddddd', seq: 3, batch: 'b2' })],
      batches: [box({ count: 1 }), box({ id: 'b2', number: 2 })],
    })
    const first = holdUpload()
    fireEvent.drop(await chapter(1), filesDrop([file('IMG_1.jpg')]))
    fireEvent.drop(await newChapterZone(), filesDrop([file('C4_1.jpg')]))
    await chapter(3)
    dragPhoto(tiles(await chapter(2))[1], await newChapterZone())
    await waitFor(() => expect(api.createPageBatch).toHaveBeenLastCalledWith(PID, { ids: ['dddddddd'] }))
    first.resolve()

    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(tileIds(screen.getByRole('region', { name: 'Chapter 3' }))).toEqual(['ffff0005']))
    expect(tileIds(screen.getByRole('region', { name: 'Chapter 4' }))).toEqual(['dddddddd'])
  })

  it('a new chapter dropped while the box for the one before is still being made never goes ahead of it', async () => {
    // The empty box at the end is reused for a new chapter — but while another new
    // chapter's box is still on its way, that box isn't the end any more.
    renderPages({
      pages: [page({ name: 'IMG_A.jpg' }), page({ id: 'bbbbbbbb', seq: 2 })],
      batches: [box(), box({ id: 'b2', number: 2, count: 0, state: 'empty' })],
    })
    const making = deferred()
    api.createPageBatch.mockImplementationOnce(async (...args) => {
      await making.promise
      return server.createBox(...args)
    })
    const first = holdUpload()
    fireEvent.drop(await chapter(2), filesDrop([file('IMG_A.jpg')]))   // already in Chapter 1
    fireEvent.drop(await newChapterZone(), filesDrop([file('C1.jpg')]))
    first.resolve()
    await screen.findByText(/IMG_A\.jpg is already in Chapter 1/)
    fireEvent.drop(await newChapterZone(), filesDrop([file('C2.jpg')]))
    making.resolve()

    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(3))
    expect(names()).toEqual(['IMG_A.jpg', 'C1.jpg', 'C2.jpg'])
    expect(sentTo()).toEqual(['b2', 'b9', 'b10'])
  })

  it('a new-chapter drop whose photos are all in the novel already leaves no box behind', async () => {
    // Its box sat empty at the end, and Build held every later box back with "No photos yet".
    renderPages({ pages: [page({ name: 'IMG_1.jpg' }), page({ id: 'bbbbbbbb', seq: 2, name: 'IMG_2.jpg' })] })
    fireEvent.drop(await newChapterZone(), filesDrop([file('IMG_1.jpg'), file('IMG_2.jpg')]))
    await waitFor(() => expect(api.deletePageBatch).toHaveBeenCalledWith(PID, 'b9', { expect_page_ids: [] }))
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Chapter 2' })).toBeNull())
    expect(await screen.findByText('IMG_1.jpg, IMG_2.jpg are already in Chapter 1, so they weren’t added again.'))
      .toBeTruthy()
    expect(sentTo()).toEqual(['b9', 'b9'])
  })

  it('a new chapter whose first answer was lost goes on into its own box when tried again', async () => {
    // The server kept that first photo, so sent again it answers as a duplicate. With the
    // box taken from the first answer, the drop had none, and the rest of the chapter
    // made a second one: one chapter became two.
    renderPages()
    api.uploadPage.mockImplementationOnce(async (...args) => {
      await serve(...args)
      throw new Error('Failed to fetch')
    })
    fireEvent.drop(await newChapterZone(), filesDrop([file('IMG_1.jpg'), file('IMG_2.jpg'), file('IMG_3.jpg')]))
    await screen.findByText(/Stopped at IMG_1\.jpg \(photo 1 of 3\)/)

    fireEvent.click(screen.getByRole('button', { name: 'Try the rest again' }))
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(4))
    expect(sentTo()).toEqual(['b9', 'b9', 'b9', 'b9'])
    await waitFor(() => expect(tileIds(screen.getByRole('region', { name: 'Chapter 2' })))
      .toEqual(['ffff0003', 'ffff0004', 'ffff0005']))
    expect(screen.queryByRole('region', { name: 'Chapter 3' })).toBeNull()
    await waitFor(() => expect(toast).toHaveBeenCalledWith('2 photos added to Chapter 2 · 1 already in it'))
    expect(api.deletePageBatch).not.toHaveBeenCalled()
  })

  it('says a new chapter is being started until its box is made', async () => {
    renderPages()
    const making = deferred()
    api.createPageBatch.mockImplementationOnce(async (...args) => {
      await making.promise
      return server.createBox(...args)
    })
    fireEvent.drop(await newChapterZone(), filesDrop([file('IMG_1.jpg'), file('IMG_2.jpg')]))
    expect(await screen.findByText('Starting a new chapter for 2 photos…')).toBeTruthy()
    await settle()
    expect(api.uploadPage).not.toHaveBeenCalled()
    making.resolve()
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(2))
  })

  it('a new chapter whose box cannot be made stops there, and says why', async () => {
    // It used to vanish behind an error dialog that named no photo, and nothing said its
    // photos had gone nowhere.
    renderPages()
    api.createPageBatch.mockRejectedValueOnce(failure(500, 'Internal Server Error'))
    fireEvent.drop(await newChapterZone(), filesDrop([file('C4_1.jpg'), file('C4_2.jpg')]))
    expect(await screen.findByText('Couldn’t start a new chapter for C4_1.jpg (photo 1 of 2): '
      + 'Internal Server Error. The 2 photos from there on were not sent.')).toBeTruthy()
    // With no box, there is nothing to try the rest again in, or skip a photo of.
    expect(screen.queryByRole('button', { name: 'Try the rest again' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Skip this photo' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Cancel the rest' }))
    await settle()
    expect(screen.queryByText(/Couldn’t start a new chapter/)).toBeNull()
    expect(api.uploadPage).not.toHaveBeenCalled()
  })

  it('a drop still going in keeps saying so when a new chapter dropped after it stops', async () => {
    // The stop hid it, and it looked as if every photo had stopped.
    renderPages()
    const first = holdUpload()
    fireEvent.drop(await chapter(1), filesDrop([file('IMG_1.jpg'), file('IMG_2.jpg'), file('IMG_3.jpg')]))
    await screen.findByText(/Adding 1 of 3 to Chapter 1/)
    api.createPageBatch.mockRejectedValueOnce(failure(500, 'Internal Server Error'))
    fireEvent.drop(await newChapterZone(), filesDrop([file('C4_1.jpg')]))
    await screen.findByText(/Couldn’t start a new chapter for C4_1\.jpg/)
    expect(screen.getByText(/Adding 1 of 3 to Chapter 1/)).toBeTruthy()
    // A drop behind the stop waits for it, and the stop says so; the line going in doesn't
    // count it as coming next.
    fireEvent.drop(await chapter(1), filesDrop([file('IMG_5.jpg')]))
    await screen.findByText(/1 more drop is waiting behind it/)
    expect(screen.getByText(/Adding 1 of 3 to Chapter 1/).textContent).not.toMatch(/Then/)

    first.resolve()
    await waitFor(() => expect(names()).toEqual(['IMG_1.jpg', 'IMG_2.jpg', 'IMG_3.jpg']))
  })

  it('a new chapter still being made ahead of a stopped drop keeps saying so', async () => {
    renderPages()
    const making = deferred()
    api.createPageBatch.mockImplementationOnce(async (...args) => {
      await making.promise
      return server.createBox(...args)
    })
    fireEvent.drop(await newChapterZone(), filesDrop([file('C2_1.jpg'), file('C2_2.jpg')]))
    api.uploadPage.mockRejectedValueOnce(failure(500, 'Internal Server Error'))
    fireEvent.drop(await chapter(1), filesDrop([file('IMG_9.jpg')]))
    await screen.findByText(/Stopped at IMG_9\.jpg/)
    fireEvent.drop(await chapter(1), filesDrop([file('IMG_10.jpg')]))
    await screen.findByText(/1 more drop is waiting behind it/)
    expect(screen.getByText(/Starting a new chapter for 2 photos/)).toBeTruthy()

    making.resolve()
    await waitFor(() => expect(names()).toEqual(['IMG_9.jpg', 'C2_1.jpg', 'C2_2.jpg']))
  })

  it('a new chapter dropped after one whose box could not be made waits for it, and comes after it', async () => {
    // Its box was made while the first one's failed, and its photos went in. Dropped
    // again, the first chapter's photos became the chapter after it.
    renderPages()
    const making = deferred()
    api.createPageBatch.mockImplementationOnce(() => making.promise)
    fireEvent.drop(await newChapterZone(), filesDrop([file('C4_1.jpg'), file('C4_2.jpg')]))
    fireEvent.drop(await newChapterZone(), filesDrop([file('C5_1.jpg')]))
    making.reject(failure(500, 'Internal Server Error'))
    expect(await screen.findByText(/Couldn’t start a new chapter for C4_1\.jpg/)).toBeTruthy()
    expect(screen.getByText(/1 more drop is waiting behind it/)).toBeTruthy()
    await settle()
    expect(api.createPageBatch).toHaveBeenCalledTimes(1)
    expect(api.uploadPage).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Add the rest as a new chapter' }))
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(3))
    expect(names()).toEqual(['C4_1.jpg', 'C4_2.jpg', 'C5_1.jpg'])
    expect(sentTo()).toEqual(['b9', 'b9', 'b10'])
    await waitFor(() => expect(tileIds(screen.getByRole('region', { name: 'Chapter 3' }))).toEqual(['ffff0005']))
    expect(tileIds(screen.getByRole('region', { name: 'Chapter 2' }))).toEqual(['ffff0003', 'ffff0004'])
  })

  it('Add chapter makes no box ahead of a new chapter whose box could not be made', async () => {
    // Pressed while that box was on its way, it was made once that failed — and the
    // chapter then dropped into it came before the one dropped first.
    renderPages()
    const making = deferred()
    api.createPageBatch.mockImplementationOnce(() => making.promise)
    fireEvent.drop(await newChapterZone(), filesDrop([file('C4_1.jpg')]))
    fireEvent.click(screen.getByRole('button', { name: 'Add chapter' }))
    making.reject(new Error('Failed to fetch'))
    await screen.findByText(/Couldn’t start a new chapter for C4_1\.jpg/)
    await waitFor(() => expect(toast).toHaveBeenCalledWith(expect.stringMatching(/still waiting for their new chapter/)))
    expect(api.createPageBatch).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('region', { name: 'Chapter 2' })).toBeNull()
  })

  it('a new chapter cancelled while its box is being made leaves no box behind', async () => {
    // The box was still on its way when the drop was cancelled, so nothing knew to take it
    // away again, and Build held every box after it back with "No photos yet".
    renderPages()
    api.uploadPage
      .mockImplementationOnce(serve)
      .mockRejectedValueOnce(new Error('Failed to fetch'))
    fireEvent.drop(await chapter(1), filesDrop([file('IMG_1.jpg'), file('IMG_2.jpg')]))
    await screen.findByText(/Stopped at IMG_2\.jpg/)
    const making = deferred()
    api.createPageBatch.mockImplementationOnce(async (...args) => {
      await making.promise
      return server.createBox(...args)
    })
    fireEvent.drop(await newChapterZone(), filesDrop([file('C4_1.jpg')]))
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel the rest and the drop behind it' }))
    making.resolve()

    await waitFor(() => expect(api.deletePageBatch).toHaveBeenCalledWith(PID, 'b9', { expect_page_ids: [] }))
    await settle()
    expect((await server.pages()).batches.map((b) => b.id)).toEqual(['b1'])
    expect(screen.queryByRole('region', { name: 'Chapter 2' })).toBeNull()
    expect(api.uploadPage).toHaveBeenCalledTimes(2)
  })

  it('a new chapter that got its photos keeps its box, however far behind the screen is', async () => {
    // With its answers passed over while a move was on its way, the box still looked
    // empty, and went from the screen just as "2 photos added to Chapter 2" was said.
    renderPages()
    const first = holdUpload()
    fireEvent.drop(await newChapterZone(), filesDrop([file('IMG_1.jpg'), file('IMG_2.jpg')]))
    await chapter(2)
    const moved = deferred()
    api.movePages.mockReturnValueOnce(moved.promise)
    const [a, b] = tiles(await chapter(1))
    dragPhoto(a, b)
    first.resolve()
    await waitFor(() => expect(toast).toHaveBeenCalledWith('2 photos added to Chapter 2'))
    await settle()
    expect(screen.getByRole('region', { name: 'Chapter 2' })).toBeTruthy()

    const now = await server.pages()
    moved.resolve({ ok: true, changed: true, rev: now.rev, pages: now })
    await waitFor(() => expect(tileIds(screen.getByRole('region', { name: 'Chapter 2' })))
      .toEqual(['ffff0003', 'ffff0004']))
    expect(api.deletePageBatch).not.toHaveBeenCalled()
  })

  it('photos dropped on New chapter go into an empty last box instead of making another', async () => {
    // "Add chapter", then dropping the photos on New chapter, left an empty box in front
    // of the new one, and every later box waited behind its "No photos yet".
    renderPages({ batches: [box(), box({ id: 'b2', number: 2, count: 0, state: 'empty' })] })
    fireEvent.drop(await screen.findByRole('button', { name: /^New chapter/ }),
                   filesDrop([file('IMG_1.jpg'), file('IMG_2.jpg')]))
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(2))
    expect(sentTo()).toEqual(['b2', 'b2'])
  })

  it('the Add chapter button takes a photo drop into an empty last box too', async () => {
    renderPages({ batches: [box(), box({ id: 'b2', number: 2, count: 0, state: 'empty' })] })
    fireEvent.drop(await screen.findByRole('button', { name: 'Add chapter' }), filesDrop([file('IMG_1.jpg')]))
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(1))
    expect(sentTo()).toEqual(['b2'])
  })

  it('an empty last box that photos are already on their way into is not taken again', async () => {
    // Those photos make it a chapter; a new-chapter drop must still make its own.
    renderPages({ batches: [box(), box({ id: 'b2', number: 2, count: 0, state: 'empty' })] })
    const first = holdUpload()
    fireEvent.drop(await chapter(2), filesDrop([file('IMG_1.jpg')]))
    fireEvent.drop(screen.getByRole('button', { name: /^New chapter/ }), filesDrop([file('NEW_1.jpg')]))
    first.resolve()
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(2))
    expect(api.createPageBatch).toHaveBeenCalledWith(PID, {})
    expect(sentTo()).toEqual(['b2', 'b9'])
  })

  it('a photo already in another chapter never redirects the rest of the drop there', async () => {
    // Taking the box from a duplicate's answer sent the rest of a new chapter into an
    // old one.
    renderPages({ pages: [page({ name: 'IMG_1.jpg' }), page({ id: 'bbbbbbbb', seq: 2 })] })
    fireEvent.drop(await newChapterZone(), filesDrop([file('IMG_1.jpg'), file('IMG_2.jpg')]))
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(2))
    expect(sentTo()).toEqual(['b9', 'b9'])
    expect(await screen.findByText('IMG_1.jpg is already in Chapter 1 (photo 1), so it wasn’t added to Chapter 2.'))
      .toBeTruthy()
    expect(tileIds(screen.getByRole('region', { name: 'Chapter 2' }))).toEqual(['ffff0003'])
  })

  it('a second drop waits until the first has gone in', async () => {
    renderPages()
    const first = holdUpload()
    const second = holdUpload()
    fireEvent.drop(await chapter(1), filesDrop([file('IMG_1.jpg'), file('IMG_2.jpg')]))
    fireEvent.drop(screen.getByRole('button', { name: /^New chapter/ }), filesDrop([file('NEW_1.jpg')]))
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(1))

    first.resolve()
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(2))
    expect(names()).toEqual(['IMG_1.jpg', 'IMG_2.jpg'])

    second.resolve()
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(3))
    expect(names()).toEqual(['IMG_1.jpg', 'IMG_2.jpg', 'NEW_1.jpg'])
    expect(sentTo()).toEqual(['b1', 'b1', 'b9'])
  })

  it('stops at the first photo that fails, says where, and sends the rest into the same box', async () => {
    const { container } = renderPages()
    api.uploadPage
      .mockImplementationOnce(serve)
      .mockRejectedValueOnce(new Error('Failed to fetch'))
    fireEvent.click(await screen.findByRole('button', { name: 'Add photos to Chapter 1' }))
    fireEvent.change(container.querySelector('input[type="file"]'), {
      target: { files: [file('IMG_1.jpg'), file('IMG_2.jpg'), file('IMG_3.jpg')] },
    })
    expect(await screen.findByText(/Stopped at IMG_2\.jpg \(photo 2 of 3\)/)).toBeTruthy()
    expect(api.uploadPage).toHaveBeenCalledTimes(2)

    fireEvent.click(screen.getByRole('button', { name: 'Try the rest again' }))
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(4))
    expect(names()).toEqual(['IMG_1.jpg', 'IMG_2.jpg', 'IMG_2.jpg', 'IMG_3.jpg'])
    expect(sentTo()).toEqual(['b1', 'b1', 'b1', 'b1'])
  })

  it('a new chapter whose drop stopped gets the rest of it, not another new chapter', async () => {
    // Starting the retry from no box at all split one chapter in two — what one failed
    // photo used to do.
    renderPages()
    api.uploadPage
      .mockImplementationOnce(serve)
      .mockRejectedValueOnce(new Error('Failed to fetch'))
    fireEvent.drop(await newChapterZone(), filesDrop([file('IMG_1.jpg'), file('IMG_2.jpg'), file('IMG_3.jpg')]))
    await screen.findByText(/Stopped at IMG_2\.jpg \(photo 2 of 3\)/)

    fireEvent.click(screen.getByRole('button', { name: 'Try the rest again' }))
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(4))
    expect(names()).toEqual(['IMG_1.jpg', 'IMG_2.jpg', 'IMG_2.jpg', 'IMG_3.jpg'])
    expect(sentTo()).toEqual(['b9', 'b9', 'b9', 'b9'])
    await waitFor(() => expect(tileIds(screen.getByRole('region', { name: 'Chapter 2' })))
      .toEqual(['ffff0003', 'ffff0004', 'ffff0005']))
  })

  // A drop into Chapter 1 that stops at its second photo, with another drop behind it.
  async function stopWithOneBehind() {
    renderPages()
    api.uploadPage
      .mockImplementationOnce(serve)
      .mockRejectedValueOnce(new Error('Failed to fetch'))
    const region = await chapter(1)
    fireEvent.drop(region, filesDrop([file('IMG_1.jpg'), file('IMG_2.jpg'), file('IMG_3.jpg')]))
    await screen.findByText(/Stopped at IMG_2\.jpg/)
    fireEvent.drop(region, filesDrop([file('NEXT_1.jpg'), file('NEXT_2.jpg')]))
    expect(await screen.findByText(/1 more drop is waiting behind it/)).toBeTruthy()
    await settle()
    expect(api.uploadPage).toHaveBeenCalledTimes(2)
  }

  it('holds later drops back while one has stopped, and the retry goes first', async () => {
    // Going on would put the next drop's photos ahead of the ones that never went in.
    await stopWithOneBehind()
    fireEvent.click(screen.getByRole('button', { name: 'Try the rest again' }))
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(6))
    expect(names()).toEqual(['IMG_1.jpg', 'IMG_2.jpg', 'IMG_2.jpg', 'IMG_3.jpg', 'NEXT_1.jpg', 'NEXT_2.jpg'])
  })

  it('cancelling a stopped drop cancels the drops behind it too, and says which', async () => {
    // Sent anyway, they would go in ahead of the photos that never were. The button has
    // to say they go too — "Cancel the rest" read as the rest of this drop only.
    await stopWithOneBehind()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel the rest and the drop behind it' }))
    await settle()
    expect(api.uploadPage).toHaveBeenCalledTimes(2)
    expect(screen.queryByText(/Stopped at/)).toBeNull()
    expect(await screen.findByText(
      /Cancelled with the drop that stopped, so not added: NEXT_1\.jpg … NEXT_2\.jpg \(2 photos\) for Chapter 1/,
    )).toBeTruthy()
    expect(toast).toHaveBeenCalledWith('1 photo added to Chapter 1')
  })

  it('a stopped drop can skip the photo that failed and go on with the next', async () => {
    renderPages()
    api.uploadPage
      .mockImplementationOnce(serve)
      .mockRejectedValueOnce(new Error('Failed to fetch'))
    fireEvent.drop(await chapter(1), filesDrop([file('IMG_1.jpg'), file('IMG_2.jpg'), file('IMG_3.jpg')]))
    await screen.findByText(/Stopped at IMG_2\.jpg/)
    fireEvent.click(screen.getByRole('button', { name: 'Skip this photo' }))
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(3))
    expect(names()).toEqual(['IMG_1.jpg', 'IMG_2.jpg', 'IMG_3.jpg'])
    expect(sentTo()).toEqual(['b1', 'b1', 'b1'])
    expect(await screen.findByText('Left out: IMG_2.jpg (skipped after it failed).')).toBeTruthy()
  })

  it('a drop whose box has gone can put the rest into a new chapter', async () => {
    renderPages()
    api.uploadPage.mockRejectedValueOnce(failure(404, 'That chapter box isn’t in this novel any more.'))
    fireEvent.drop(await chapter(1), filesDrop([file('IMG_1.jpg'), file('IMG_2.jpg')]))
    fireEvent.click(await screen.findByRole('button', { name: 'Add the rest as a new chapter' }))
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(3))
    expect(names()).toEqual(['IMG_1.jpg', 'IMG_1.jpg', 'IMG_2.jpg'])
    // Its box is made before the rest goes, as for any new chapter.
    expect(api.createPageBatch).toHaveBeenCalledWith(PID, {})
    expect(sentTo()).toEqual(['b1', 'b9', 'b9'])
  })

  it('a drop whose new box cannot be made stays stopped, to be tried again', async () => {
    // Dropped instead, the photos it still had to send would have gone without a word.
    renderPages()
    api.uploadPage.mockRejectedValueOnce(failure(404, 'That chapter box isn’t in this novel any more.'))
    api.createPageBatch.mockRejectedValueOnce(new Error('Failed to fetch'))
    fireEvent.drop(await chapter(1), filesDrop([file('IMG_1.jpg'), file('IMG_2.jpg')]))
    fireEvent.click(await screen.findByRole('button', { name: 'Add the rest as a new chapter' }))
    await waitFor(() => expect(api.createPageBatch).toHaveBeenCalledTimes(1))
    fireEvent.click(await screen.findByRole('button', { name: 'Add the rest as a new chapter' }))
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(3))
    expect(sentTo()).toEqual(['b1', 'b9', 'b9'])
  })

  it('refuses a drop holding a HEIC photo before sending any of it, with the fix for each phone', async () => {
    renderPages()
    fireEvent.drop(await chapter(1), filesDrop([file('IMG_1.jpg'), file('IMG_2.HEIC', '')]))
    expect(await screen.findByText(/IMG_2\.HEIC — a HEIC\/HEIF photo/)).toBeTruthy()
    // Android phones save these too, and this one's reader uses one.
    expect(screen.getByText(/turn off “High efficiency pictures”/)).toBeTruthy()
    expect(screen.getByText(/Most Compatible/)).toBeTruthy()
    await settle()
    expect(api.uploadPage).not.toHaveBeenCalled()
  })

  it('files dropped on a box are uploaded even after a photo drag that never ended', async () => {
    renderPages()
    const region = await chapter(1)
    fireEvent.dragStart(tiles(region)[0], tileDrag())
    // No dragend: that drag was let go outside the window.
    fireEvent.drop(region, filesDrop([file('IMG_7.jpg')]))
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(1))
    await settle()
    expect(api.movePages).not.toHaveBeenCalled()
  })

  it('a drop that misses every box opens nothing, and says where to drop', async () => {
    renderPages()
    await chapter(1)
    // Not prevented, the browser opens the photo in place of the app, and whatever was
    // typed and not yet saved goes with it. fireEvent answers false once it was.
    expect(fireEvent.dragOver(document.body, filesDrop([file('IMG_7.jpg')]))).toBe(false)
    expect(fireEvent.drop(document.body, filesDrop([file('IMG_7.jpg')]))).toBe(false)
    expect(toast).toHaveBeenCalledWith(expect.stringMatching(/Drop photos onto a chapter box, or onto “New chapter”/))
    await settle()
    expect(api.uploadPage).not.toHaveBeenCalled()
  })
})

describe('a drop that outlives the Pages tab', () => {
  // The queue was the tab's own state. Leaving the tab mid-drop left its loop running in
  // a page no longer on screen, and coming back started a second, empty queue — so the
  // next drop into the same box went in alongside the first, photo by photo.

  it('keeps going after the tab is left, and a drop made on coming back waits for it', async () => {
    const first = holdUpload()
    const view = renderPages()
    fireEvent.drop(await chapter(1), filesDrop([file('IMG_1.jpg'), file('IMG_2.jpg')]))
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(1))
    view.unmount()   // off to the Chapters tab

    renderPages()
    expect(await screen.findByText(/Adding 1 of 2 to Chapter 1/)).toBeTruthy()
    fireEvent.drop(await chapter(1), filesDrop([file('IMG_3.jpg')]))
    await settle()
    expect(api.uploadPage).toHaveBeenCalledTimes(1)

    first.resolve()
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(3))
    expect(names()).toEqual(['IMG_1.jpg', 'IMG_2.jpg', 'IMG_3.jpg'])
    expect(sentTo()).toEqual(['b1', 'b1', 'b1'])
  })

  it('a drop that stopped while the tab was closed is still there to retry', async () => {
    api.uploadPage.mockRejectedValueOnce(new Error('Failed to fetch'))
    const view = renderPages()
    fireEvent.drop(await chapter(1), filesDrop([file('IMG_1.jpg'), file('IMG_2.jpg')]))
    view.unmount()
    await settle()

    renderPages()
    expect(await screen.findByText(/Stopped at IMG_1\.jpg \(photo 1 of 2\)/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Try the rest again' }))
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(3))
  })

  it('a drop that finished while the tab was closed is reported when it opens', async () => {
    const first = holdUpload()
    const view = renderPages()
    fireEvent.drop(await chapter(1), filesDrop([file('IMG_1.jpg'), file('IMG_2.jpg')]))
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(1))
    view.unmount()
    first.resolve()
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(2))
    await settle()
    expect(toast).not.toHaveBeenCalled()

    renderPages()
    await waitFor(() => expect(toast).toHaveBeenCalledWith('2 photos added to Chapter 1'))
    expect(toast).toHaveBeenCalledTimes(1)
  })

  it('the browser asks before leaving while a drop is going in, and not after', async () => {
    // A reload mid-drop sends nothing more, and nothing says how far it got.
    const leave = () => {
      const e = new Event('beforeunload', { cancelable: true })
      window.dispatchEvent(e)
      return e.defaultPrevented
    }
    const first = holdUpload()
    renderPages()
    expect(leave()).toBe(false)
    fireEvent.drop(await chapter(1), filesDrop([file('IMG_1.jpg')]))
    expect(leave()).toBe(true)
    first.resolve()
    await waitFor(() => expect(leave()).toBe(false))
  })

  it('an upload left going in one novel goes on there, and is reported back there', async () => {
    const OTHER = '999999999999'
    const first = holdUpload()
    const { payload, setPid } = renderPages()
    api.pages.mockImplementation(async (p) => (p === PID ? payload : railPayload([], { batches: [] })))
    fireEvent.drop(await chapter(1), filesDrop([file('IMG_1.jpg'), file('IMG_2.jpg')]))
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(1))

    setPid(OTHER)
    expect(await screen.findByText(/Drop chapter 1.s photos here/)).toBeTruthy()
    first.resolve()
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(2))
    await settle()
    expect(api.uploadPage.mock.calls.map((c) => c[0])).toEqual([PID, PID])
    expect(screen.queryByText(/Adding/)).toBeNull()
    expect(toast).not.toHaveBeenCalled()

    setPid(PID)
    await waitFor(() => expect(toast).toHaveBeenCalledWith('2 photos added to Chapter 1'))
  })

  it('a new chapter’s box made after the tab moved to another novel neither shows there nor holds it up', async () => {
    // A drop waiting for its box gets it once the drop before it has one, which can be after
    // the reader has moved on. That edit is the first novel's: its answer, shown here, put
    // that novel's boxes on this one's screen, and counted as this novel's edit it held
    // back the answer to every edit made here.
    const OTHER = '999999999999'
    const { setPid } = renderPages()
    const other = fakeServer([page({ id: 'oooooooo', batch: 'o1' })], [box({ id: 'o1', count: 1 })])
    const by = (method) => (p, ...args) => (p === OTHER ? other : server)[method](p, ...args)
    api.pages.mockImplementation(by('pages'))
    api.createPageBatch.mockImplementation(by('createBox'))
    // The first new chapter's box can't be made; the drop after it waits for it.
    api.createPageBatch.mockRejectedValueOnce(failure(500, 'Internal Server Error'))
    fireEvent.drop(await newChapterZone(), filesDrop([file('C2_1.jpg')]))
    await screen.findByText(/Couldn’t start a new chapter for C2_1\.jpg/)
    fireEvent.drop(await newChapterZone(), filesDrop([file('C3_1.jpg')]))
    await settle()
    // Tried again, and still on its way when the reader moves to another novel.
    const making = deferred()
    api.createPageBatch.mockImplementationOnce(async (...args) => {
      await making.promise
      return server.createBox(...args)
    })
    fireEvent.click(screen.getByRole('button', { name: 'Add the rest as a new chapter' }))
    await waitFor(() => expect(api.createPageBatch).toHaveBeenCalledTimes(2))

    setPid(OTHER)
    const onScreen = () => Array.from(document.querySelectorAll('section[id^="box-"]')).map((s) => s.id.slice(4))
    await waitFor(() => expect(onScreen()).toEqual(['o1']))
    making.resolve()
    await waitFor(() => expect(api.uploadPage.mock.calls.map((c) => [c[0], c[1].name, c[2].batch]))
      .toEqual([[PID, 'C2_1.jpg', 'b9'], [PID, 'C3_1.jpg', 'b10']]))
    expect(onScreen()).toEqual(['o1'])

    fireEvent.click(screen.getByRole('button', { name: 'Add chapter' }))
    await waitFor(() => expect(onScreen()).toEqual(['o1', 'b9']))
  })
})

describe('moving photos', () => {
  it('dragging a photo onto the right half of another puts it after that one', async () => {
    const { list: [a0, b0] } = renderPages()
    api.movePages.mockResolvedValue({ ok: true, changed: true, ...summary([b0, a0]) })
    const region = await chapter(1)
    const [a, b] = tiles(region)
    const drag = dragPhoto(a, b)
    // Firefox starts no drag at all without data set on it.
    expect(drag.dataTransfer.setData).toHaveBeenCalledWith(TILE_MIME, 'aaaaaaaa')
    await waitFor(() => expect(api.movePages)
      .toHaveBeenCalledWith(PID, { ids: ['aaaaaaaa'], batch: 'b1', before: null }))
    await settle()
    expect(tileIds(region)).toEqual(['bbbbbbbb', 'aaaaaaaa'])
  })

  it('dragging onto the left half of a photo puts it before that one', async () => {
    const rows = [page(), page({ id: 'bbbbbbbb', seq: 2 }), page({ id: 'cccccccc', seq: 3 })]
    renderPages({ pages: rows })
    api.movePages.mockResolvedValue({ ok: true, changed: true, ...summary([rows[2], rows[0], rows[1]]) })
    const [a, , c] = tiles(await chapter(1))
    a.getBoundingClientRect = () => ({ left: 0, width: 100, top: 0, height: 100, right: 100, bottom: 100 })
    const drag = tileDrag()
    fireEvent.dragStart(c, drag)
    const drop = createEvent.drop(a, drag)
    Object.defineProperty(drop, 'clientX', { value: 10 })
    fireEvent(a, drop)
    await waitFor(() => expect(api.movePages)
      .toHaveBeenCalledWith(PID, { ids: ['cccccccc'], batch: 'b1', before: 'aaaaaaaa' }))
  })

  it('dropping a photo back where it was sends nothing', async () => {
    renderPages()
    const [a] = tiles(await chapter(1))
    const drag = dragPhoto(a, a)
    fireEvent.dragEnd(a, drag)
    await settle()
    expect(api.movePages).not.toHaveBeenCalled()
  })

  it('a move the server refuses is put back, and says why', async () => {
    // Left as it was, the screen would show an order the server doesn't have, and the
    // next build would quietly use the server's.
    const { ctx } = renderPages()
    api.movePages.mockRejectedValue(new Error('Failed to fetch'))
    const region = await chapter(1)
    const [a, b] = tiles(region)
    dragPhoto(a, b)
    expect(tileIds(region)).toEqual(['bbbbbbbb', 'aaaaaaaa'])
    await waitFor(() => expect(ctx.showError).toHaveBeenCalled())
    await waitFor(() => expect(api.pages).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(tileIds(region)).toEqual(['aaaaaaaa', 'bbbbbbbb']))
  })

  it('a photo dropped on New chapter goes into an empty last box instead of making another', async () => {
    const rows = [page(), page({ id: 'bbbbbbbb', seq: 2 })]
    renderPages({ pages: rows, batches: [box(), box({ id: 'b2', number: 2, count: 0, state: 'empty' })] })
    api.movePages.mockResolvedValue({ ok: true, changed: true, ...summary([rows[0], { ...rows[1], batch: 'b2' }], {
      batches: [box({ count: 1 }), box({ id: 'b2', number: 2, count: 1 })],
    }) })
    const [, b] = tiles(await chapter(1))
    dragPhoto(b, screen.getByRole('button', { name: /^New chapter/ }))
    await waitFor(() => expect(api.movePages)
      .toHaveBeenCalledWith(PID, { ids: ['bbbbbbbb'], batch: 'b2', before: null }))
    expect(api.createPageBatch).not.toHaveBeenCalled()
    await waitFor(() => expect(tileIds(screen.getByRole('region', { name: 'Chapter 2' }))).toEqual(['bbbbbbbb']))
  })

  it('a photo dropped on New chapter starts a chapter of its own', async () => {
    const { list: [a, b0] } = renderPages()
    const made = box({ id: 'b2', number: 2, count: 1 })
    api.createPageBatch.mockResolvedValue({
      batch: made, ...summary([a, { ...b0, batch: 'b2' }], { batches: [box({ count: 1 }), made] }),
    })
    const [, b] = tiles(await chapter(1))
    dragPhoto(b, screen.getByRole('button', { name: /^New chapter/ }))
    await waitFor(() => expect(api.createPageBatch).toHaveBeenCalledWith(PID, { ids: ['bbbbbbbb'] }))
    expect(api.movePages).not.toHaveBeenCalled()
    expect(tileIds(await chapter(2))).toEqual(['bbbbbbbb'])
  })

  it('Earlier and Later move the photo within its chapter', async () => {
    const [a, b, c] = [page(), page({ id: 'bbbbbbbb', seq: 2 }), page({ id: 'cccccccc', seq: 3 })]
    renderPages({ pages: [a, b, c], batches: [box({ count: 3 })], at: '?photo=bbbbbbbb' })
    api.movePages
      .mockResolvedValueOnce({ ok: true, changed: true, ...summary([a, c, b], { batches: [box({ count: 3 })] }) })
      .mockResolvedValueOnce({ ok: true, changed: true, ...summary([a, b, c], { batches: [box({ count: 3 })], rev: 3 }) })
    fireEvent.click(await screen.findByRole('button', { name: 'Later ▶' }))
    await waitFor(() => expect(api.movePages)
      .toHaveBeenLastCalledWith(PID, { ids: ['bbbbbbbb'], batch: 'b1', before: null }))
    expect(await screen.findByText(/photo 3 of 3/)).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: '◀ Earlier' }))
    await waitFor(() => expect(api.movePages)
      .toHaveBeenLastCalledWith(PID, { ids: ['bbbbbbbb'], batch: 'b1', before: 'cccccccc' }))
    expect(await screen.findByText(/photo 2 of 3/)).toBeTruthy()
  })

  it('two quick taps on Later are sent one after the other, and the first answer never undoes the second', async () => {
    // Sent side by side, the server could apply them in either order, and an anchor
    // the second one named might not be where the screen showed it yet. And the first
    // answer predates the second tap: shown, it would put the photo back a place.
    const rows = ['aaaaaaaa', 'bbbbbbbb', 'cccccccc', 'dddddddd'].map((id, i) => page({ id, seq: i + 1 }))
    const [a, b, c, d] = rows
    renderPages({ pages: rows, batches: [box({ count: 4 })], at: '?photo=aaaaaaaa' })
    const first = deferred()
    const second = deferred()
    api.movePages
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise)
    const later = await screen.findByRole('button', { name: 'Later ▶' })
    fireEvent.click(later)
    fireEvent.click(later)
    await waitFor(() => expect(api.movePages).toHaveBeenCalledTimes(1))
    expect(api.movePages).toHaveBeenLastCalledWith(PID, { ids: ['aaaaaaaa'], batch: 'b1', before: 'cccccccc' })
    expect(screen.getByText(/photo 3 of 4/)).toBeTruthy()
    await settle()
    expect(api.movePages).toHaveBeenCalledTimes(1)

    first.resolve({ ok: true, changed: true, ...summary([b, a, c, d], { batches: [box({ count: 4 })] }) })
    await waitFor(() => expect(api.movePages).toHaveBeenCalledTimes(2))
    expect(api.movePages).toHaveBeenLastCalledWith(PID, { ids: ['aaaaaaaa'], batch: 'b1', before: 'dddddddd' })
    await settle()
    expect(screen.getByText(/photo 3 of 4/)).toBeTruthy()

    second.resolve({ ok: true, changed: true, ...summary([b, c, a, d], { batches: [box({ count: 4 })], rev: 3 }) })
    await settle()
    expect(screen.getByText(/photo 3 of 4/)).toBeTruthy()
  })

  const twoChapters = {
    pages: [page(), page({ id: 'cccccccc', seq: 3, batch: 'b2' }), page({ id: 'dddddddd', seq: 4, batch: 'b2' })],
    batches: [box({ count: 1 }), box({ id: 'b2', number: 2 })],
  }

  it('Move to chapter sends a photo to the end of an earlier chapter', async () => {
    const [a, c, d] = twoChapters.pages
    renderPages({ ...twoChapters, at: '?photo=cccccccc' })
    api.movePages.mockResolvedValue({ ok: true, changed: true, ...summary([a, { ...c, batch: 'b1' }, d], {
      batches: [box({ count: 2 }), box({ id: 'b2', number: 2, count: 1 })],
    }) })
    fireEvent.change(await screen.findByRole('combobox', { name: 'Move to another chapter' }),
                     { target: { value: 'b1' } })
    await waitFor(() => expect(api.movePages)
      .toHaveBeenCalledWith(PID, { ids: ['cccccccc'], batch: 'b1', before: null }))
    // The view stays on the photo, now the last of Chapter 1.
    expect(await screen.findByText(/photo 2 of 2/)).toBeTruthy()
    expect(screen.getByRole('heading', { level: 1 }).textContent).toMatch(/^Chapter 1/)
  })

  it('Move to chapter sends a photo to the start of a later chapter', async () => {
    const [a, c, d] = twoChapters.pages
    renderPages({ ...twoChapters, at: '?photo=aaaaaaaa' })
    api.movePages.mockResolvedValue({ ok: true, changed: true, ...summary([{ ...a, batch: 'b2' }, c, d], {
      batches: [box({ count: 0, state: 'empty' }), box({ id: 'b2', number: 2, count: 3 })],
    }) })
    fireEvent.change(await screen.findByRole('combobox', { name: 'Move to another chapter' }),
                     { target: { value: 'b2' } })
    await waitFor(() => expect(api.movePages)
      .toHaveBeenCalledWith(PID, { ids: ['aaaaaaaa'], batch: 'b2', before: 'cccccccc' }))
    expect(await screen.findByText(/photo 1 of 3/)).toBeTruthy()
  })

  it('Split chapter here starts a new chapter from this photo on', async () => {
    const [a, b, c] = [page(), page({ id: 'bbbbbbbb', seq: 2 }), page({ id: 'cccccccc', seq: 3 })]
    renderPages({ pages: [a, b, c], batches: [box({ count: 3 })], at: '?photo=bbbbbbbb' })
    const made = box({ id: 'b2', number: 2, count: 2 })
    api.createPageBatch.mockResolvedValue({
      batch: made,
      ...summary([a, { ...b, batch: 'b2' }, { ...c, batch: 'b2' }], { batches: [box({ count: 1 }), made] }),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Split chapter here' }))
    await waitFor(() => expect(api.createPageBatch)
      .toHaveBeenCalledWith(PID, { after: 'b1', ids: ['bbbbbbbb', 'cccccccc'] }))
    await waitFor(() => expect(toast).toHaveBeenCalledWith('Chapter 2 now starts at this photo'))
    expect(await screen.findByText(/photo 1 of 2/)).toBeTruthy()
  })

  // Chapter 1 with three photos, and a drop going into it whose first photo is held.
  const threeAndMore = {
    pages: [page(), page({ id: 'bbbbbbbb', seq: 2 }), page({ id: 'cccccccc', seq: 3 }),
      page({ id: 'dddddddd', seq: 4, batch: 'b2' })],
    batches: [box({ count: 3 }), box({ id: 'b2', number: 2, count: 1 })],
  }

  it('a chapter photos are still going into can’t be split, or its photos moved, until they are in', async () => {
    // Each photo of a drop lands at the end of its box. Split while the drop went in, the
    // rest of it kept landing in the earlier chapter, after the photo before the split.
    renderPages(threeAndMore)
    const first = holdUpload()
    fireEvent.drop(await chapter(1), filesDrop([file('IMG_4.jpg'), file('IMG_5.jpg')]))
    fireEvent.click(await screen.findByRole('button', { name: /^Photo 2 of Chapter 1/ }))
    await screen.findByText(/photo 2 of 3/)

    const controls = ['Split chapter here', '◀ Earlier', 'Later ▶'].map((name) => screen.getByRole('button', { name }))
    const move = screen.getByRole('combobox', { name: 'Move to another chapter' })
    for (const control of [...controls, move]) expect(control).toBeDisabled()
    expect(screen.getByText('Photos are still being added to this chapter — wait for them to finish.')).toBeTruthy()

    first.resolve()
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Split chapter here' })).toBeEnabled())
    for (const control of [...controls, move]) expect(control).toBeEnabled()
    expect(api.createPageBatch).not.toHaveBeenCalled()
    expect(api.movePages).not.toHaveBeenCalled()
  })

  it('no photo moves into a chapter photos are still going into', async () => {
    // Landed at its end, a photo moved there was overtaken by the rest of the drop.
    renderPages(threeAndMore)
    const first = holdUpload()
    fireEvent.drop(await chapter(1), filesDrop([file('IMG_4.jpg')]))
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(1))

    // Dragged onto a photo there, or onto the box: nothing is sent.
    const region = screen.getByRole('region', { name: 'Chapter 1' })
    dragPhoto(tiles(await chapter(2))[0], tiles(region)[0])
    dragPhoto(tiles(screen.getByRole('region', { name: 'Chapter 2' }))[0], region)
    // And its own photos can't be dragged out.
    expect(tiles(region)[0]).toHaveAttribute('draggable', 'false')

    // Chosen from another photo's list, it's there but can't be picked.
    fireEvent.click(tiles(screen.getByRole('region', { name: 'Chapter 2' }))[0])
    const option = await screen.findByRole('option', { name: /^Chapter 1/ })
    expect(option).toBeDisabled()
    expect(option.textContent).toMatch(/photos still being added/)
    await settle()
    expect(api.movePages).not.toHaveBeenCalled()
    first.resolve()
  })
})

describe('the photo view', () => {
  it('opens a photo beside its text, and Back returns to the boxes', async () => {
    renderPages()
    fireEvent.click(await screen.findByRole('button', { name: /^Photo 1 of Chapter 1/ }))
    await waitFor(() => expect(api.page).toHaveBeenCalledWith(PID, 'aaaaaaaa'))
    expect(await textBox()).toBeTruthy()
    expect(screen.getByText(/photo 1 of 2/)).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: '← Back to chapters' }))
    expect(await chapter(1)).toBeTruthy()
    expect(screen.queryByRole('textbox', { name: 'Korean text read from this photo' })).toBeNull()
  })

  it('Back saves a pending edit first, and only then fetches the boxes', async () => {
    // Fetched alongside the save, the boxes came back with the pill from before the edit.
    renderPages({ at: '?photo=aaaaaaaa' })
    fireEvent.change(await textBox(), { target: { value: '고친 문장' } })
    fireEvent.click(screen.getByRole('button', { name: '← Back to chapters' }))
    await waitFor(() => expect(api.pages).toHaveBeenCalledTimes(2))
    expect(api.savePage).toHaveBeenCalledWith(PID, 'aaaaaaaa', { text: '고친 문장' })
    expect(api.savePage.mock.invocationCallOrder[0]).toBeLessThan(api.pages.mock.invocationCallOrder[1])
  })

  it('a photo opened from a link whose boxes fail to load says so, with a retry', async () => {
    // It sat on a skeleton for good, with the error dialog behind it.
    api.pages.mockRejectedValueOnce(new Error('Failed to fetch'))
    renderPages({ at: '?photo=aaaaaaaa' })
    expect(await screen.findByText('Failed to fetch')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByText(/photo 1 of 2/)).toBeTruthy()
  })

  it('a photo whose text fails to load says so, with a retry', async () => {
    // The editor stayed on its skeleton for good, which on a phone link looks like a
    // photo still loading.
    api.page.mockRejectedValueOnce(new Error('Failed to fetch'))
    renderPages({ at: '?photo=aaaaaaaa' })
    expect(await screen.findByText(/Couldn’t load this photo’s text: Failed to fetch/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await textBox()).toBeTruthy()
    expect(api.page).toHaveBeenCalledTimes(2)
  })

  it('arrow keys step through the photo’s own chapter only, and not while typing', async () => {
    renderPages({
      pages: [page(), page({ id: 'bbbbbbbb', seq: 2 }), page({ id: 'cccccccc', seq: 3, batch: 'b2' })],
      batches: [box(), box({ id: 'b2', number: 2, count: 1 })],
      at: '?photo=bbbbbbbb',
    })
    await screen.findByText(/photo 2 of 2/)
    fireEvent.keyDown(window, { key: 'ArrowRight' })   // the last photo: chapter 2 isn't next
    await settle()
    expect(screen.getByText(/photo 2 of 2/)).toBeTruthy()
    expect(api.page).not.toHaveBeenCalledWith(PID, 'cccccccc')

    fireEvent.keyDown(window, { key: 'ArrowLeft' })
    await screen.findByText(/photo 1 of 2/)
    fireEvent.keyDown(await textBox(), { key: 'ArrowRight' })
    await settle()
    expect(screen.getByText(/photo 1 of 2/)).toBeTruthy()
  })

  it('arrow keys leave the photo alone while a dialog is open', async () => {
    renderPages({ at: '?photo=aaaaaaaa' })
    await screen.findByText(/photo 1 of 2/)
    const dialog = document.createElement('div')
    dialog.setAttribute('aria-modal', 'true')
    document.body.appendChild(dialog)
    try {
      fireEvent.keyDown(window, { key: 'ArrowRight' })
      await settle()
      expect(screen.getByText(/photo 1 of 2/)).toBeTruthy()
    } finally { dialog.remove() }
  })

  it('arrow keys with a modifier held are left to the browser', async () => {
    // Alt+← is the browser's own Back.
    renderPages({ at: '?photo=aaaaaaaa' })
    await screen.findByText(/photo 1 of 2/)
    fireEvent.keyDown(window, { key: 'ArrowRight', altKey: true })
    fireEvent.keyDown(window, { key: 'ArrowRight', ctrlKey: true })
    fireEvent.keyDown(window, { key: 'ArrowRight', metaKey: true })
    await settle()
    expect(screen.getByText(/photo 1 of 2/)).toBeTruthy()
  })

  it('a pending edit is saved, not dropped, when stepping to the next photo', async () => {
    renderPages({ at: '?photo=aaaaaaaa' })
    fireEvent.change(await textBox(), { target: { value: '고친 문장' } })
    fireEvent.click(screen.getByRole('button', { name: 'Next photo' }))
    await waitFor(() => expect(api.savePage).toHaveBeenCalledWith(PID, 'aaaaaaaa', { text: '고친 문장' }))
    expect(await screen.findByText(/photo 2 of 2/)).toBeTruthy()
  })

  it('a correction saved as the tab moves to another novel goes after the save before it', async () => {
    // Sent ahead of the queue, it could reach the server first, and the earlier save of the
    // same photo, landing last, put the shorter text back without a word.
    const { setPid } = renderPages({ at: '?photo=aaaaaaaa' })
    const first = deferred()
    api.savePage.mockImplementationOnce(async (...args) => {
      await first.promise
      return server.save(...args)
    })
    const text = await textBox()
    fireEvent.change(text, { target: { value: 'fix 1' } })
    await waitFor(() => expect(api.savePage).toHaveBeenCalledTimes(1), { timeout: 2000 })
    fireEvent.change(text, { target: { value: 'fix 1 and 2' } })
    setPid('999999999999')
    await settle()
    expect(api.savePage).toHaveBeenCalledTimes(1)

    first.resolve()
    await waitFor(() => expect(api.savePage).toHaveBeenCalledTimes(2))
    expect(api.savePage.mock.calls[1]).toEqual([PID, 'aaaaaaaa', { text: 'fix 1 and 2' }])
    expect((await server.page(PID, 'aaaaaaaa')).text).toBe('fix 1 and 2')
  })

  it.each([
    ['Re-read from scratch', 'readPages'],
    ['Check against the photo', 'verifyPages'],
  ])('a pending edit is saved before %s starts', async (button, call) => {
    // Sent after, it marked the page "edited" while it sat queued for reading, and a
    // check compared the photo with the text from before the correction.
    renderPages({ at: '?photo=aaaaaaaa' })
    fireEvent.change(await textBox(), { target: { value: '고친 문장' } })
    fireEvent.click(screen.getByRole('button', { name: button }))
    await waitFor(() => expect(api[call]).toHaveBeenCalledWith(PID, ['aaaaaaaa']))
    expect(api.savePage).toHaveBeenCalledWith(PID, 'aaaaaaaa', { text: '고친 문장' })
    expect(api.savePage.mock.invocationCallOrder[0]).toBeLessThan(api[call].mock.invocationCallOrder[0])
  })

  it('deleting the open photo opens the next one, and never saves an edit onto it', async () => {
    const { list: [, b] } = renderPages({ at: '?photo=aaaaaaaa' })
    api.deletePages.mockResolvedValue({ ok: true, removed: 1, ...summary([b], { batches: [box({ count: 1 })] }) })
    fireEvent.change(await textBox(), { target: { value: '고친 문장' } })
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(api.deletePages).toHaveBeenCalledWith(PID, ['aaaaaaaa']))
    expect(await screen.findByText(/photo 1 of 1/)).toBeTruthy()
    expect(screen.getByRole('img', { name: 'Page 2' })).toBeTruthy()
    // Past the autosave's 700ms: the save typed into the deleted photo is never sent.
    await act(() => new Promise((r) => setTimeout(r, 750)))
    expect(api.savePage).not.toHaveBeenCalled()
  })

  it('a deleted photo is kept when its confirm is answered no', async () => {
    renderPages({ at: '?photo=aaaaaaaa' })
    confirm.mockResolvedValueOnce(false)
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1))
    await settle()
    expect(api.deletePages).not.toHaveBeenCalled()
    expect(screen.getByText(/photo 1 of 2/)).toBeTruthy()
  })

  it('a photo delete confirmed after the tab moved to another novel deletes nothing', async () => {
    // The confirm outlives the change of novel; answered there, it deleted a photo of the
    // novel left behind and put its neighbour's id in the other novel's address.
    const { setPid } = renderPages({ at: '?photo=aaaaaaaa' })
    const answer = deferred()
    confirm.mockImplementationOnce(() => answer.promise)
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1))
    setPid('999999999999')
    answer.resolve(true)
    await settle()
    await settle()
    expect(api.deletePages).not.toHaveBeenCalled()
  })

  it('shows the text as soon as Claude finishes reading the open photo', async () => {
    vi.useFakeTimers()
    try {
      const { payload } = renderPages({
        running: true,
        pages: [page({ status: 'queued' })],
        batches: [box({ count: 1, state: 'reading' })],
        at: '?photo=aaaaaaaa',
      })
      await vi.waitFor(() => expect(api.page).toHaveBeenCalledTimes(1))
      await vi.waitFor(() => screen.getByRole('textbox', { name: 'Korean text read from this photo' }))
      api.pages.mockResolvedValue({
        ...payload, rev: 2, pages: [page({ status: 'needs-check', has_text: true, chars: 6 })],
      })
      api.page.mockResolvedValue({ ...page({ status: 'needs-check' }), text: '문을 열었다', raw_text: '' })

      await vi.advanceTimersByTimeAsync(2600)
      await vi.waitFor(() => expect(
        screen.getByRole('textbox', { name: 'Korean text read from this photo' }).value,
      ).toBe('문을 열었다'))
      expect(api.page).toHaveBeenCalledTimes(2)
    } finally { vi.useRealTimers() }
  })

  it('shows the new text even when the boxes never showed the photo being read', async () => {
    // An answer that comes while edits are pending is passed over, and the next one can
    // already be done reading. The row's reading time is what says it changed.
    const { payload, setRunning } = renderPages({
      running: true,
      pages: [page({ status: 'needs-check', ocr: { at: 't1' }, has_text: true })],
      batches: [box({ count: 1, state: 'to-check' })],
      at: '?photo=aaaaaaaa',
    })
    await textBox()
    expect(api.page).toHaveBeenCalledTimes(1)
    api.pages.mockResolvedValue({
      ...payload, rev: 2, pages: [page({ status: 'needs-check', ocr: { at: 't2' }, has_text: true, chars: 6 })],
    })
    api.page.mockResolvedValue({ ...page({ status: 'needs-check', ocr: { at: 't2' } }), text: '문을 열었다', raw_text: '' })

    setRunning(false)   // one last look at the boxes
    await waitFor(() => expect((screen.getByRole('textbox', { name: 'Korean text read from this photo' })).value)
      .toBe('문을 열었다'))
    expect(api.page).toHaveBeenCalledTimes(2)
  })

  it('marking a photo good keeps the correction just typed instead of fetching the text again', async () => {
    // Fetched again, the server's text replaced the correction on screen while the
    // autosave still sent it.
    renderPages({ pages: [page({ status: 'needs-check', has_text: true }), page({ id: 'bbbbbbbb', seq: 2 })],
                  at: '?photo=aaaaaaaa' })
    fireEvent.change(await textBox(), { target: { value: '고친 문장' } })
    fireEvent.click(screen.getByRole('button', { name: 'Looks good' }))
    await waitFor(() => expect(api.savePage).toHaveBeenCalledWith(PID, 'aaaaaaaa', { text: '고친 문장', status: 'ok' }))
    await waitFor(() => expect(api.pages).toHaveBeenCalledTimes(2))
    await settle()
    expect(api.page).toHaveBeenCalledTimes(1)
    expect((await textBox()).value).toBe('고친 문장')
  })

  it('Skip pressed straight after typing sends the text with it, so the photo stays skipped', async () => {
    // The text went on its own 700ms later, after the skip, and text saved with no status
    // marks a page edited: the cover just cleared and skipped came back un-skipped, and
    // went into the chapter at the next build.
    vi.useFakeTimers()
    try {
      renderPages({ pages: [page({ status: 'needs-check', has_text: true }), page({ id: 'bbbbbbbb', seq: 2 })],
                    at: '?photo=aaaaaaaa' })
      const text = await vi.waitFor(() => screen.getByRole('textbox', { name: 'Korean text read from this photo' }))
      fireEvent.change(text, { target: { value: '표지' } })
      fireEvent.click(screen.getByRole('button', { name: 'Skip (cover / blank)' }))
      await tick(1000)
      expect(api.savePage.mock.calls).toEqual([[PID, 'aaaaaaaa', { text: '표지', status: 'skipped' }]])
      expect(screen.getByRole('button', { name: 'Un-skip' })).toBeTruthy()
    } finally { vi.useRealTimers() }
  })

  it('a skipped photo is put back by Un-skip: to be read when it has no text, to be checked when it has', async () => {
    // "Looks good" was the way back, and on a photo never read it approved a page with no
    // text — one no Read would ever look at again.
    renderPages({
      pages: [page({ status: 'skipped' }), page({ id: 'bbbbbbbb', seq: 2, status: 'skipped', has_text: true })],
      at: '?photo=aaaaaaaa',
    })
    await textBox()
    expect(screen.queryByRole('button', { name: 'Looks good' })).toBeNull()
    expect(screen.getByText(/“Un-skip” puts this one back, to be read/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Un-skip' }))
    await waitFor(() => expect(api.savePage).toHaveBeenLastCalledWith(PID, 'aaaaaaaa', { status: 'new' }))

    api.page.mockImplementation(async (_pid, id) => ({ ...page({ id, status: 'skipped' }), text: '문을 열었다', raw_text: '' }))
    fireEvent.click(screen.getByRole('button', { name: 'Next photo' }))
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Korean text read from this photo' }).value)
      .toBe('문을 열었다'))
    fireEvent.click(screen.getByRole('button', { name: 'Un-skip' }))
    await waitFor(() => expect(api.savePage).toHaveBeenLastCalledWith(PID, 'bbbbbbbb', { status: 'needs-check' }))
  })

  it('a photo with no text cannot be marked good', async () => {
    renderPages({ at: '?photo=aaaaaaaa' })
    fireEvent.change(await textBox(), { target: { value: '  ' } })
    const good = screen.getByRole('button', { name: 'Looks good' })
    expect(good).toBeDisabled()
    expect(good).toHaveAttribute('title', expect.stringMatching(/Read it, or type its text, first/))
    fireEvent.change(await textBox(), { target: { value: '문을 열었다' } })
    expect(good).toBeEnabled()
  })

  it('a last look at the boxes passed over during a page save is fetched again, so the photo stops being read', async () => {
    // A save answers with its page alone, folded into the rail the screen already had,
    // yet it stamped that rail with its own rev. The job's last look, passed over while the
    // save was out, then counted as older, was never fetched again, and the photo sat on
    // "Claude is reading this photo" with an empty, read-only box.
    const rows = [page({ status: 'needs-check', has_text: true }), page({ id: 'bbbbbbbb', seq: 2, status: 'queued' })]
    const { payload, setRunning } = renderPages({
      running: true, pages: rows, batches: [box({ state: 'reading' })], at: '?photo=aaaaaaaa',
    })
    const saved = deferred()
    api.savePage.mockImplementationOnce(() => saved.promise)
    fireEvent.change(await textBox(), { target: { value: '고친 문장' } })
    fireEvent.click(screen.getByRole('button', { name: 'Next photo' }))
    await waitFor(() => expect(api.savePage).toHaveBeenCalledTimes(1))
    expect(await textBox()).toHaveAttribute('readonly')

    const read = [rows[0], { ...rows[1], status: 'needs-check', has_text: true, chars: 6, ocr: { at: 't1' } }]
    api.pages
      .mockResolvedValueOnce({ ...payload, rev: 5, pages: read })   // the last look, while the save is out
      .mockResolvedValue({ ...payload, rev: 6, pages: read })
    api.page.mockResolvedValue({ ...read[1], text: '문을 열었다', raw_text: '' })
    const calls = api.pages.mock.calls.length
    setRunning(false)
    await waitFor(() => expect(api.pages).toHaveBeenCalledTimes(calls + 1))
    saved.resolve({ ...rows[0], status: 'edited', text: '고친 문장', raw_text: '', rev: 6 })

    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Korean text read from this photo' }).value)
      .toBe('문을 열었다'))
    expect(await textBox()).not.toHaveAttribute('readonly')
  })

  it('a last look at the boxes read just before a page save, and answered after it, is fetched again', async () => {
    // The save's small answer came first and stamped the screen with its rev, so the last
    // look — read before the save landed, its rev one lower — was dropped as old, with the
    // last read results in it. Nothing fetched them again.
    const rows = [page({ status: 'needs-check', has_text: true }), page({ id: 'bbbbbbbb', seq: 2, status: 'queued' })]
    const { payload, setRunning } = renderPages({
      running: true, pages: rows, batches: [box({ state: 'reading' })], at: '?photo=aaaaaaaa',
    })
    const saved = deferred()
    api.savePage.mockImplementationOnce(() => saved.promise)
    fireEvent.change(await textBox(), { target: { value: '고친 문장' } })
    fireEvent.click(screen.getByRole('button', { name: 'Next photo' }))
    await waitFor(() => expect(api.savePage).toHaveBeenCalledTimes(1))
    expect(await textBox()).toHaveAttribute('readonly')

    const read = [rows[0], { ...rows[1], status: 'needs-check', has_text: true, chars: 6, ocr: { at: 't1' } }]
    const look = deferred()
    api.pages
      .mockImplementationOnce(() => look.promise)
      .mockResolvedValue({ ...payload, rev: 6, pages: read })
    api.page.mockResolvedValue({ ...read[1], text: '문을 열었다', raw_text: '' })
    const calls = api.pages.mock.calls.length
    setRunning(false)
    await waitFor(() => expect(api.pages).toHaveBeenCalledTimes(calls + 1))
    saved.resolve({ ...rows[0], status: 'edited', text: '고친 문장', raw_text: '', rev: 6 })
    await settle()
    look.resolve({ ...payload, rev: 5, pages: read })

    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Korean text read from this photo' }).value)
      .toBe('문을 열었다'))
    expect(await textBox()).not.toHaveAttribute('readonly')
  })

  it('a page save answered while the next one is on its way still reaches the screen', async () => {
    // Only the last answer is shown, and a save's answer is its page alone: the first one
    // was passed over, and the photo typed into stayed "Not read yet" — counted in its
    // box's Read, which would read over the text typed.
    renderPages({ at: '?photo=aaaaaaaa' })
    const first = deferred()
    api.savePage.mockImplementationOnce(async (...args) => {
      await first.promise
      return server.save(...args)
    })
    fireEvent.change(await textBox(), { target: { value: '손으로 친 글' } })
    fireEvent.click(screen.getByRole('button', { name: 'Next photo' }))
    await screen.findByText(/photo 2 of 2/)
    const note = await screen.findByPlaceholderText('e.g. the bottom two lines are cut off')
    fireEvent.change(note, { target: { value: 'the last line is cut off' } })
    fireEvent.blur(note)
    first.resolve()
    await waitFor(() => expect(api.savePage).toHaveBeenCalledTimes(2))

    fireEvent.click(screen.getByRole('button', { name: 'Previous photo' }))
    await screen.findByText(/photo 1 of 2/)
    expect(await screen.findByText('Edited')).toBeTruthy()
  })

  it('keeps the text box read-only while the photo is being read', async () => {
    // The server overwrites the text when reading finishes; anything typed meanwhile
    // would be lost.
    renderPages({
      pages: [page({ status: 'ocr-running' })],
      batches: [box({ count: 1, state: 'reading' })],
      at: '?photo=aaaaaaaa',
    })
    expect(await textBox()).toHaveAttribute('readonly')
  })

  it('sets how a photo joins the one before it', async () => {
    renderPages({ at: '?photo=bbbbbbbb' })
    fireEvent.click(await screen.findByRole('button', { name: '¶ New paragraph' }))
    await waitFor(() => expect(api.savePage).toHaveBeenCalledWith(PID, 'bbbbbbbb', { join_prev: 'paragraph' }))
  })
})

describe('working out the joins', () => {
  // Stitch runs outside the write queue (its model call is slow), so its answer is
  // offered like a poll's — never shown over a move still on its way.
  const twoRead = () => [
    page({ status: 'ok', has_text: true, chars: 10 }),
    page({ id: 'bbbbbbbb', seq: 2, status: 'ok', has_text: true, chars: 10 }),
  ]
  const stitched = (rows, over = {}) => ({
    ok: true, seams: 1, asked_model: 1, gaps: 0, remaining: 0, cost_usd: 0.01, usage: {},
    pages: railPayload(rows, { rev: 3 }), ...over,
  })

  it('says how many joins it worked out, and shows them', async () => {
    const rows = twoRead()
    renderPages({ pages: rows })
    api.stitchPages.mockResolvedValue(stitched([rows[0], { ...rows[1], join_prev: 'sentence', join_prev_source: 'model' }]))
    fireEvent.click(await screen.findByRole('button', { name: 'Work out the joins' }))
    await waitFor(() => expect(toast).toHaveBeenCalledWith('1 seam worked out'))
    expect(api.stitchPages).toHaveBeenCalledWith(PID, { use_model: true })
    expect(await screen.findByRole('button', { name: 'Join before photo 2: Sentence continues' })).toBeTruthy()
  })

  it('names the possible missing pages, and the joins still unsure', async () => {
    const rows = twoRead()
    renderPages({ pages: rows })
    api.stitchPages.mockResolvedValue(stitched(rows, { seams: 4, gaps: 1, remaining: 3 }))
    fireEvent.click(await screen.findByRole('button', { name: 'Work out the joins' }))
    await waitFor(() => expect(toast)
      .toHaveBeenCalledWith('4 seams · 1 possible missing page · 3 more unsure — press again'))
  })

  it('an answer that comes while a move is on its way is passed over, not shown over it', async () => {
    const rows = twoRead()
    renderPages({ pages: rows })
    const moved = deferred()
    api.movePages.mockReturnValue(moved.promise)
    // Worked out against the order from before the move.
    api.stitchPages.mockResolvedValue(stitched(rows))
    const region = await chapter(1)
    const [a, b] = tiles(region)
    dragPhoto(a, b)
    fireEvent.click(screen.getByRole('button', { name: 'Work out the joins' }))
    await waitFor(() => expect(toast).toHaveBeenCalledWith('1 seam worked out'))
    expect(tileIds(region)).toEqual(['bbbbbbbb', 'aaaaaaaa'])

    moved.resolve({ ok: true, changed: true, ...summary([rows[1], rows[0]], { rev: 4 }) })
    await settle()
    expect(tileIds(region)).toEqual(['bbbbbbbb', 'aaaaaaaa'])
  })
})

describe('building chapters', () => {
  const plan = (over = {}) => ({
    ok: true, preview: true, chapters: 0, added: [], refreshed: [], updated: [], waiting: [],
    warnings: [], unworked_seams: 0, ...over,
  })
  const withText = () => [
    page({ status: 'ok', has_text: true, chars: 10 }),
    page({ id: 'bbbbbbbb', seq: 2, status: 'needs-check', has_text: true, chars: 10 }),
  ]
  const openBuild = async () => fireEvent.click(await screen.findByRole('button', { name: 'Build chapters →' }))

  it('previews the build, and says why boxes wait', async () => {
    renderPages({ pages: withText() })
    api.buildChapters.mockResolvedValue(plan({
      waiting: [
        { batch: 'b1', number: 1, title: 'Chapter 1', built: false,
          reasons: ['Photo 2 still needs a check.'], blocked_by: null },
        { batch: 'b2', number: 2, title: 'Chapter 2', built: false,
          reasons: ['Comes after chapter 1, which isn’t ready yet.'], blocked_by: 1 },
      ],
      warnings: ['Chapter 1: a heading sits inside this box.'],
      unworked_seams: 2,
    }))
    await openBuild()
    await waitFor(() => expect(api.buildChapters)
      .toHaveBeenCalledWith(PID, { include: 'approved', append: true, preview: true }))
    expect(await screen.findByText('Photo 2 still needs a check.')).toBeTruthy()
    expect(screen.getByText(/Chapter 2 waits for chapter 1/)).toBeTruthy()
    expect(screen.getByText(/2 joins between photos/)).toBeTruthy()
    // Before anything is built, not only after.
    expect(screen.getByText('Chapter 1: a heading sits inside this box.')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Build' })).toBeDisabled()

    fireEvent.click(screen.getByRole('checkbox', { name: /Also use photos marked/ }))
    await waitFor(() => expect(api.buildChapters)
      .toHaveBeenLastCalledWith(PID, { include: 'all', append: true, preview: true }))
  })

  it('lets a title-only change be built', async () => {
    renderPages({ pages: withText() })
    api.buildChapters.mockResolvedValue(plan({ updated: [1] }))
    await openBuild()
    expect(await screen.findByText(/its title or photo list changed/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Build' })).toBeEnabled()
  })

  it('lets a text change to a translated chapter be built, saying it needs translating again', async () => {
    renderPages({
      pages: [page({ status: 'ok', has_text: true, chars: 10 })],
      batches: [box({ chapter: 1, state: 'changed', count: 1 })],
      chapterCount: 1,
      chapters: [{ index: 1, title: 'Chapter 1', status: 'translated', source_changed: false }],
    })
    api.buildChapters.mockResolvedValue(plan({ refreshed: [1] }))
    await openBuild()
    expect(await screen.findByText(/its text changed since it was built.*translate it again afterwards/))
      .toBeTruthy()
    expect(screen.getByRole('button', { name: 'Build' })).toBeEnabled()
  })

  it('shows every warning a build returns instead of closing', async () => {
    const { ctx } = renderPages({ pages: withText() })
    api.buildChapters.mockImplementation(async (_pid, body) => (body.preview
      ? plan({ added: [1] })
      : {
        ...plan({ added: [1], chapters: 1 }),
        preview: false,
        project: { id: PID },
        warnings: ['Chapter 1: not sure how IMG_2.jpg joins the page before it.',
          'Chapter 1: a heading sits inside this box.'],
      }))
    await openBuild()
    const build = await screen.findByRole('button', { name: 'Build' })
    await waitFor(() => expect(build).toBeEnabled())
    fireEvent.click(build)

    expect(await screen.findByText(/not sure how IMG_2\.jpg joins/)).toBeTruthy()
    expect(screen.getByText(/a heading sits inside this box/)).toBeTruthy()
    expect(api.buildChapters).toHaveBeenLastCalledWith(PID, { include: 'approved', append: true, force: false })
    expect(ctx.setProjectMeta).toHaveBeenCalledWith({ id: PID })
    expect(ctx.reload).toHaveBeenCalledWith(true)

    fireEvent.click(screen.getByRole('button', { name: 'Done' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('can be closed while it builds, and the Pages tab says what the build said', async () => {
    // Kept open until the build answered, so its warnings were seen, a build whose request
    // hung left the dialog on "Building…" with no way out but reloading the app. Closed
    // mid-build before that, its warnings went to a dialog no longer there.
    const { ctx } = renderPages({ pages: withText() })
    const built = deferred()
    api.buildChapters.mockImplementation(async (_pid, body) => (body.preview ? plan({ added: [1] }) : built.promise))
    await openBuild()
    const build = await screen.findByRole('button', { name: 'Build' })
    await waitFor(() => expect(build).toBeEnabled())
    fireEvent.click(build)
    const dialog = screen.getByRole('dialog')
    await within(dialog).findByRole('button', { name: 'Building…' })
    expect(within(dialog).getByRole('button', { name: 'Close' })).toBeEnabled()

    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    // Opened again now, it could start a second build beside the first.
    expect(screen.getByRole('button', { name: 'Building…' })).toBeDisabled()

    built.resolve({
      ...plan({ added: [1], chapters: 1 }), preview: false, project: { id: PID },
      waiting: [{ batch: 'b2', number: 2, title: 'Chapter 2', built: false,
                  reasons: ['Photo 1 hasn’t been read yet.'], blocked_by: null }],
      warnings: ['Chapter 1: a heading sits inside this box.'],
    })
    await waitFor(() => expect(toast).toHaveBeenCalledWith('1 chapter added'))
    expect(await screen.findByText('Chapters built: 1 chapter added. · Chapter 2: Photo 1 hasn’t been '
      + 'read yet. · Chapter 1: a heading sits inside this box.', { normalizer: (s) => s.replace(/\s+/g, ' ') }))
      .toBeTruthy()
    expect(ctx.setProjectMeta).toHaveBeenCalledWith({ id: PID })
    expect(screen.getByRole('button', { name: 'Build chapters →' })).toBeEnabled()
  })

  it('a build closed and answered after the tab moved to another novel stays with its novel', async () => {
    // The Pages tab and its layout stay open when the novel changes. The build's project
    // and chapter list went into the next novel's layout, its toast went over that novel,
    // and that novel's Build button sat on "Building…" until the first build answered.
    const { ctx, setPid } = renderPages({ pages: withText() })
    const built = deferred()
    api.buildChapters.mockImplementation(async (_pid, body) => (body.preview ? plan({ added: [1] }) : built.promise))
    await openBuild()
    const build = await screen.findByRole('button', { name: 'Build' })
    await waitFor(() => expect(build).toBeEnabled())
    fireEvent.click(build)
    await within(screen.getByRole('dialog')).findByRole('button', { name: 'Building…' })
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())

    setPid('999999999999')
    expect(await screen.findByRole('button', { name: 'Build chapters →' })).toBeEnabled()
    built.resolve({ ...plan({ added: [1], chapters: 1 }), preview: false, project: { id: PID, name: 'Novel A' } })
    await settle()
    await settle()
    expect(ctx.setProjectMeta).not.toHaveBeenCalled()
    expect(ctx.reload).not.toHaveBeenCalled()
    expect(toast).not.toHaveBeenCalledWith('1 chapter added')
  })

  it('a dialog left open when the tab moves to another novel closes', async () => {
    const { setPid } = renderPages({ pages: withText() })
    api.buildChapters.mockResolvedValue(plan({ added: [1] }))
    await openBuild()
    await screen.findByRole('dialog')
    setPid('999999999999')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('a Start over confirm answered after the tab moved to another novel starts nothing', async () => {
    // The confirm outlives the dialog: answered on the next novel's screen, it started over
    // the novel the reader had left.
    const { setPid } = renderPages({ pages: withText() })
    api.buildChapters.mockResolvedValue(plan({ added: [1], chapters: 1 }))
    const answer = deferred()
    confirm.mockImplementationOnce(() => answer.promise)
    await openBuild()
    fireEvent.click(await screen.findByText('Start over…'))
    fireEvent.click(screen.getByRole('button', { name: 'Start over' }))
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1))
    setPid('999999999999')
    answer.resolve(true)
    await settle()
    await settle()
    expect(api.buildChapters.mock.calls.filter(([, body]) => !body.preview)).toEqual([])
    // Not dropped without a word: the reader would think their novel had been started over.
    expect(toast).toHaveBeenCalledWith(expect.stringMatching(/^Start over wasn’t run/))
  })

  it('a second Start over confirm answered after the tab moved to another novel renumbers nothing', async () => {
    const { setPid } = renderPages({ pages: withText() })
    api.buildChapters.mockImplementation(async (_pid, body) => {
      if (body.preview) return plan({ added: [1], chapters: 1 })
      throw failure(400, 'Rebuilding would renumber 1 chapter(s) you’ve already translated. Cancel to '
        + 'keep their numbers, or Start over anyway to redo them.')
    })
    const second = deferred()
    confirm.mockResolvedValueOnce(true).mockImplementationOnce(() => second.promise)
    await openBuild()
    fireEvent.click(await screen.findByText('Start over…'))
    fireEvent.click(screen.getByRole('button', { name: 'Start over' }))
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(2))
    setPid('999999999999')
    second.resolve(true)
    await settle()
    await settle()
    expect(api.buildChapters.mock.calls.filter(([, body]) => body.force)).toEqual([])
    expect(toast).toHaveBeenCalledWith(expect.stringMatching(/^Start over wasn’t run/))
  })

  it('says what an update-only build did as a sentence', async () => {
    renderPages({ pages: withText() })
    api.buildChapters.mockImplementation(async (_pid, body) => (body.preview
      ? plan({ updated: [3] })
      : { ...plan({ updated: [3], chapters: 3 }), preview: false, project: {} }))
    await openBuild()
    const build = await screen.findByRole('button', { name: 'Build' })
    await waitFor(() => expect(build).toBeEnabled())
    fireEvent.click(build)
    await waitFor(() => expect(toast).toHaveBeenCalledWith('Chapter 3 updated'))
  })

  it('closes with a toast when the build has nothing more to say', async () => {
    renderPages({ pages: withText() })
    api.buildChapters.mockImplementation(async (_pid, body) => (body.preview
      ? plan({ added: [1] })
      : { ...plan({ added: [1], chapters: 1 }), preview: false, project: {} }))
    await openBuild()
    const build = await screen.findByRole('button', { name: 'Build' })
    await waitFor(() => expect(build).toBeEnabled())
    fireEvent.click(build)
    await waitFor(() => expect(toast).toHaveBeenCalledWith('1 chapter added'))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('Start over asks first, then forces only after a second yes', async () => {
    renderPages({ pages: withText() })
    api.buildChapters.mockImplementation(async (_pid, body) => {
      if (body.preview) return plan({ added: [1], chapters: 1 })
      if (!body.force) {
        throw Object.assign(new Error('Rebuilding would renumber 1 chapter(s) you’ve already translated.'),
                            { status: 400 })
      }
      return { ...plan({ added: [1], chapters: 1 }), preview: false, project: {} }
    })
    await openBuild()
    fireEvent.click(await screen.findByText('Start over…'))
    fireEvent.click(screen.getByRole('button', { name: 'Start over' }))

    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(2))
    expect(confirm.mock.calls.map(([o]) => o.title)).toEqual(['Start over?', 'Translated chapters would change'])
    await waitFor(() => expect(api.buildChapters.mock.calls.filter(([, b]) => !b.preview).map(([, b]) => b))
      .toEqual([
        { include: 'approved', append: false, force: false },
        { include: 'approved', append: false, force: true },
      ]))
    expect(api.buildChapters).toHaveBeenCalledWith(PID, { include: 'approved', append: false, preview: true })
  })

  // Start over renumbers chapters that may already be translated, so a no at either
  // confirm must stop it.
  function startOverRenumbers() {
    api.buildChapters.mockImplementation(async (_pid, body) => {
      if (body.preview) return plan({ added: [1], chapters: 1 })
      if (!body.force) {
        throw Object.assign(new Error('Rebuilding would renumber 1 chapter(s) you’ve already translated.'),
                            { status: 400 })
      }
      return { ...plan({ added: [1], chapters: 1 }), preview: false, project: {} }
    })
  }
  const realBuilds = () => api.buildChapters.mock.calls.filter(([, b]) => !b.preview).map(([, b]) => b)

  it('Start over says what its preview warns of before asking, in words that fit the confirm', async () => {
    // The renumbering of translated chapters came only after the first yes, in a second
    // dialog; a heading inside a box or an unsure join was never said at all. Then said in
    // the server's words, the renumbering named buttons this confirm doesn't have, and the
    // second confirm said it all again.
    renderPages({ pages: withText() })
    const renumber = 'Rebuilding would renumber 2 chapter(s) you\'ve already translated. Cancel to '
      + 'keep their numbers, or Start over anyway to redo them.'
    api.buildChapters.mockResolvedValue(plan({
      added: [1], chapters: 1, warnings: [renumber, 'Chapter 1: a heading sits inside this box.'],
    }))
    confirm.mockResolvedValueOnce(false)
    await openBuild()
    fireEvent.click(await screen.findByText('Start over…'))
    fireEvent.click(screen.getByRole('button', { name: 'Start over' }))
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1))
    expect(confirm.mock.calls[0][0].body).toMatch(new RegExp('· 2 chapters you’ve already translated would be '
      + 'renumbered\\. You’ll be asked once more before that happens\\.\\n'
      + '· Chapter 1: a heading sits inside this box\\.$'))
    expect(confirm.mock.calls[0][0].body).not.toMatch(/Cancel to keep|Start over anyway/)
  })

  it('Start over builds nothing when its first confirm is answered no', async () => {
    renderPages({ pages: withText() })
    startOverRenumbers()
    confirm.mockResolvedValueOnce(false)
    await openBuild()
    fireEvent.click(await screen.findByText('Start over…'))
    fireEvent.click(screen.getByRole('button', { name: 'Start over' }))
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1))
    await settle()
    expect(realBuilds()).toEqual([])
  })

  it('Start over never forces past translated chapters when the second confirm is answered no', async () => {
    renderPages({ pages: withText() })
    startOverRenumbers()
    confirm.mockResolvedValueOnce(true).mockResolvedValueOnce(false)
    await openBuild()
    fireEvent.click(await screen.findByText('Start over…'))
    fireEvent.click(screen.getByRole('button', { name: 'Start over' }))
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(2))
    await settle()
    expect(realBuilds()).toEqual([{ include: 'approved', append: false, force: false }])
  })

  it('Start over names the boxes that are not ready instead of asking', async () => {
    renderPages({ pages: withText() })
    api.buildChapters.mockImplementation(async (_pid, body) => (body.append === false
      ? plan({ waiting: [{ batch: 'b1', number: 1, title: 'Chapter 1', built: false,
                           reasons: ['Photo 2 still needs a check.'], blocked_by: null }] })
      : plan()))
    await openBuild()
    fireEvent.click(await screen.findByText('Start over…'))
    fireEvent.click(screen.getByRole('button', { name: 'Start over' }))
    expect(await screen.findByText(/Start over needs every box with photos to be ready first/)).toBeTruthy()
    expect(confirm).not.toHaveBeenCalled()
    expect(api.buildChapters.mock.calls.filter(([, b]) => !b.preview)).toEqual([])
  })

  it('Start over names a box that is not ready as the Pages tab does, and what it would become', async () => {
    // Start over numbers the boxes again from 1, so the number in its answer is the one
    // the box WOULD get. Looked up by that number, the list named a built chapter with
    // nothing wrong with it — and a box only waiting behind it isn't "not ready" at all.
    // Here the empty box ahead of The River is removed, so The River moves up a number,
    // and the box after it waits on that new number — as server/ocr_build.py has it.
    renderPages({
      pages: [page({ status: 'ok', has_text: true }), page({ id: 'bbbbbbbb', seq: 2, batch: 'b3' }),
        page({ id: 'cccccccc', seq: 3, batch: 'b4', status: 'ok', has_text: true })],
      batches: [box({ chapter: 1, state: 'built', count: 1, label: 'The Gate' }),
        box({ id: 'b2', number: 2, count: 0, state: 'empty' }),
        box({ id: 'b3', number: 3, count: 1, label: 'The River' }),
        box({ id: 'b4', number: 4, count: 1, state: 'ready', label: 'The Hill' })],
      chapterCount: 1,
    })
    api.buildChapters.mockImplementation(async (_pid, body) => (body.append === false
      ? plan({ added: [1], chapters: 1, waiting: [
        { batch: 'b3', number: 2, title: 'The River', built: false,
          reasons: ['Photo 1 hasn’t been read yet.'], blocked_by: null },
        { batch: 'b4', number: 3, title: 'The Hill', built: false,
          reasons: ['Comes after chapter 2, which isn’t ready yet.'], blocked_by: 2 },
      ] })
      : plan()))
    await openBuild()
    fireEvent.click(await screen.findByText('Start over…'))
    fireEvent.click(screen.getByRole('button', { name: 'Start over' }))
    const list = (await screen.findByText(/Start over needs every box with photos/)).closest('div')
    expect(within(list).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      '· Chapter 3 · The River (would become chapter 2): Photo 1 hasn’t been read yet.',
    ])
  })
})
