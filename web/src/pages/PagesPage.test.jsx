import { act, createEvent, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import PagesPage from './PagesPage'
import { api } from '../api'
import { TILE_MIME } from '../pageBoxes'

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
    chapters,
  }
  outletContext.mockReturnValue(ctx)
  const list = rows || [page(), page({ id: 'bbbbbbbb', seq: 2 })]
  const payload = railPayload(list, { batches, chapterCount })
  api.pages.mockResolvedValue(payload)
  api.page.mockImplementation(async (_pid, id) => ({ ...list.find((p) => p.id === id), text: '', raw_text: '' }))
  const tree = () => (
    <MemoryRouter initialEntries={[`/novel/${PID}/pages${at}`]}><PagesPage /></MemoryRouter>
  )
  const view = render(tree())
  const setRunning = (value) => {
    outletContext.mockReturnValue({ ...ctx, running: value })
    view.rerender(tree())
  }
  return { submit, list, ctx, payload, setRunning, ...view }
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
const tileDrag = () => ({ dataTransfer: { types: [TILE_MIME], setData: vi.fn(), getData: () => '' } })
const names = () => api.uploadPage.mock.calls.map((c) => c[1].name)
const sentTo = () => api.uploadPage.mock.calls.map((c) => c[2].batch)
const tileIds = (region) => Array.from(region.querySelectorAll('[data-page-id]'))
  .map((el) => el.getAttribute('data-page-id'))
const tiles = (region) => within(region).getAllByRole('button', { name: /^Photo \d/ })
const chapter = (n) => screen.findByRole('region', { name: `Chapter ${n}` })
const textBox = () => screen.findByRole('textbox', { name: 'Korean text read from this photo' })
const added = (batch, over = {}) => ({
  page: { id: 'eeeeeeee' }, duplicate: false, batch, number: 1, total: 3, rev: 2, ...over,
})

beforeEach(() => {
  vi.resetAllMocks()
  confirm.mockResolvedValue(true)
  api.readPages.mockResolvedValue({ job_id: 'job-1', kind: 'ocr', current: 1, pending: [2] })
  api.verifyPages.mockResolvedValue({ job_id: 'job-2', kind: 'ocr-verify', current: 1, pending: [] })
  api.savePage.mockImplementation(async (_pid, id, fields) => ({
    ...page({ id }), text: '', ...fields, rev: 2,
  }))
  api.uploadPage.mockImplementation(async (_pid, _file, { batch }) => added(batch || 'b9'))
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
      pages: [page({ status: 'ok' }), page({ id: 'bbbbbbbb', seq: 2 })],
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
      fireEvent.dragStart(a, tileDrag())
      fireEvent.drop(b, tileDrag())
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
    fireEvent.dragStart(a, tileDrag())
    fireEvent.drop(b, tileDrag())
    await waitFor(() => expect(api.movePages).toHaveBeenCalled())
    await settle()

    slow.resolve(payload)   // rev 1
    await settle()
    expect(tileIds(region)).toEqual(['bbbbbbbb', 'aaaaaaaa'])
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

  it('Add chapter makes a box at the end and shows it', async () => {
    const { list } = renderPages()
    const made = box({ id: 'b2', number: 2, count: 0, state: 'empty' })
    api.createPageBatch.mockResolvedValue({
      batch: made, rev: 2, pages: railPayload(list, { batches: [box(), made], rev: 2 }),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Add chapter' }))
    await waitFor(() => expect(api.createPageBatch).toHaveBeenCalledWith(PID, {}))
    expect(await chapter(2)).toBeTruthy()
  })

  it('Add chapter goes to an empty last box instead of making another', async () => {
    // An empty box holds up every box after it at Build time, so a second one would
    // only be in the way.
    renderPages({ batches: [box(), box({ id: 'b2', number: 2, count: 0, state: 'empty' })] })
    fireEvent.click(await screen.findByRole('button', { name: 'Add chapter' }))
    await settle()
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
    expect(screen.getByRole('button', { name: 'Move Chapter 2 up' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Delete Chapter 2' })).toBeEnabled()
  })

  it('moving a box sends the whole new order', async () => {
    renderPages({
      pages: [page({ status: 'ok' }), page({ id: 'bbbbbbbb', batch: 'b2' }), page({ id: 'cccccccc', batch: 'b3' })],
      batches: [box({ chapter: 1, state: 'built', count: 1 }),
        box({ id: 'b2', number: 2, count: 1 }), box({ id: 'b3', number: 3, count: 1 })],
      chapterCount: 1,
    })
    api.reorderPageBatches.mockResolvedValue({ ok: true, rev: 2 })
    fireEvent.click(await screen.findByRole('button', { name: 'Move Chapter 2 down' }))
    await waitFor(() => expect(api.reorderPageBatches).toHaveBeenCalledWith(PID, ['b1', 'b3', 'b2']))
    // Shown at once: the box that was third is now Chapter 2.
    expect(within(screen.getByRole('region', { name: 'Chapter 2' })).getByRole('button', { name: /^Photo 1/ }))
      .toHaveAttribute('data-page-id', 'cccccccc')
  })

  it('deleting a box asks first, and sends the photos it expects to delete', async () => {
    renderPages({
      pages: [page(), page({ id: 'cccccccc', batch: 'b2' }), page({ id: 'dddddddd', batch: 'b2' })],
      batches: [box({ count: 1 }), box({ id: 'b2', number: 2 })],
    })
    api.deletePageBatch.mockResolvedValue({ ok: true, removed: 2, rev: 2 })
    fireEvent.click(await screen.findByRole('button', { name: 'Delete Chapter 2' }))
    await waitFor(() => expect(api.deletePageBatch)
      .toHaveBeenCalledWith(PID, 'b2', { expect_page_ids: ['cccccccc', 'dddddddd'] }))
    expect(confirm).toHaveBeenCalledWith(expect.objectContaining({
      danger: true, body: expect.stringMatching(/2 photos/),
    }))
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
  })

  it('a new-chapter drop puts every photo into the box its first photo made', async () => {
    renderPages()
    const zone = await screen.findByRole('button', { name: /drop photos here to start a new one/ })
    fireEvent.drop(zone, filesDrop([file('IMG_1.jpg'), file('IMG_2.jpg'), file('IMG_3.jpg')]))
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(3))
    expect(sentTo()).toEqual(['', 'b9', 'b9'])
  })

  it('a photo already in another chapter never redirects the rest of the drop there', async () => {
    // Taking the box from a duplicate's answer sent the rest of a new chapter into an
    // old one.
    renderPages()
    api.uploadPage
      .mockResolvedValueOnce({ page: { id: 'aaaaaaaa' }, duplicate: true, existing_batch: 'b1',
                               existing_number: 1, total: 2, rev: 1 })
      .mockResolvedValueOnce(added('b9', { number: 2 }))
    fireEvent.drop(await screen.findByRole('button', { name: /drop photos here/ }),
                   filesDrop([file('IMG_1.jpg'), file('IMG_2.jpg')]))
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(2))
    expect(sentTo()).toEqual(['', ''])
    expect(await screen.findByText(/IMG_1\.jpg is already in Chapter 1 \(photo 1\)/)).toBeTruthy()
  })

  it('a second drop waits until the first has gone in', async () => {
    renderPages()
    const first = deferred()
    const second = deferred()
    api.uploadPage
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise)
    fireEvent.drop(await chapter(1), filesDrop([file('IMG_1.jpg'), file('IMG_2.jpg')]))
    fireEvent.drop(screen.getByRole('button', { name: /drop photos here/ }), filesDrop([file('NEW_1.jpg')]))
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(1))

    first.resolve(added('b1'))
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(2))
    expect(names()).toEqual(['IMG_1.jpg', 'IMG_2.jpg'])

    second.resolve(added('b1'))
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(3))
    expect(names()).toEqual(['IMG_1.jpg', 'IMG_2.jpg', 'NEW_1.jpg'])
    expect(sentTo()).toEqual(['b1', 'b1', ''])
  })

  it('stops at the first photo that fails, says where, and sends the rest into the same box', async () => {
    const { container } = renderPages()
    api.uploadPage
      .mockResolvedValueOnce(added('b1'))
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

  it('holds later drops back while one has stopped, and the retry goes first', async () => {
    // Going on would put the next drop's photos ahead of the ones that never went in.
    renderPages()
    api.uploadPage
      .mockResolvedValueOnce(added('b1'))
      .mockRejectedValueOnce(new Error('Failed to fetch'))
    const region = await chapter(1)
    fireEvent.drop(region, filesDrop([file('IMG_1.jpg'), file('IMG_2.jpg')]))
    await screen.findByText(/Stopped at IMG_2\.jpg/)

    fireEvent.drop(region, filesDrop([file('IMG_3.jpg')]))
    expect(await screen.findByText(/1 more drop is waiting behind it/)).toBeTruthy()
    await settle()
    expect(api.uploadPage).toHaveBeenCalledTimes(2)

    fireEvent.click(screen.getByRole('button', { name: 'Try the rest again' }))
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(4))
    expect(names()).toEqual(['IMG_1.jpg', 'IMG_2.jpg', 'IMG_2.jpg', 'IMG_3.jpg'])
  })

  it('refuses a drop holding an iPhone HEIC photo before sending any of it', async () => {
    renderPages()
    fireEvent.drop(await chapter(1), filesDrop([file('IMG_1.jpg'), file('IMG_2.HEIC', '')]))
    expect(await screen.findByText(/IMG_2\.HEIC — an iPhone HEIC photo/)).toBeTruthy()
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
    fireEvent.drop(document.body, filesDrop([file('IMG_7.jpg')]))
    expect(toast).toHaveBeenCalledWith(expect.stringMatching(/Drop photos onto a chapter box/))
    await settle()
    expect(api.uploadPage).not.toHaveBeenCalled()
  })
})

describe('moving photos', () => {
  it('dragging a photo onto the right half of another puts it after that one', async () => {
    renderPages()
    api.movePages.mockResolvedValue({ ok: true, changed: true, rev: 2 })
    const [a, b] = tiles(await chapter(1))
    fireEvent.dragStart(a, tileDrag())
    fireEvent.drop(b, tileDrag())
    await waitFor(() => expect(api.movePages)
      .toHaveBeenCalledWith(PID, { ids: ['aaaaaaaa'], batch: 'b1', before: null }))
  })

  it('dragging onto the left half of a photo puts it before that one', async () => {
    renderPages({ pages: [page(), page({ id: 'bbbbbbbb', seq: 2 }), page({ id: 'cccccccc', seq: 3 })] })
    api.movePages.mockResolvedValue({ ok: true, changed: true, rev: 2 })
    const [a, , c] = tiles(await chapter(1))
    a.getBoundingClientRect = () => ({ left: 0, width: 100, top: 0, height: 100, right: 100, bottom: 100 })
    fireEvent.dragStart(c, tileDrag())
    const drop = createEvent.drop(a, tileDrag())
    Object.defineProperty(drop, 'clientX', { value: 10 })
    fireEvent(a, drop)
    await waitFor(() => expect(api.movePages)
      .toHaveBeenCalledWith(PID, { ids: ['cccccccc'], batch: 'b1', before: 'aaaaaaaa' }))
  })

  it('dropping a photo back where it was sends nothing', async () => {
    renderPages()
    const [a] = tiles(await chapter(1))
    fireEvent.dragStart(a, tileDrag())
    fireEvent.drop(a, tileDrag())
    fireEvent.dragEnd(a, tileDrag())
    await settle()
    expect(api.movePages).not.toHaveBeenCalled()
  })

  it('Earlier and Later move the photo within its chapter', async () => {
    renderPages({
      pages: [page(), page({ id: 'bbbbbbbb', seq: 2 }), page({ id: 'cccccccc', seq: 3 })],
      batches: [box({ count: 3 })],
      at: '?photo=bbbbbbbb',
    })
    api.movePages.mockResolvedValue({ ok: true, changed: true, rev: 2 })
    fireEvent.click(await screen.findByRole('button', { name: 'Later ▶' }))
    await waitFor(() => expect(api.movePages)
      .toHaveBeenLastCalledWith(PID, { ids: ['bbbbbbbb'], batch: 'b1', before: null }))
    expect(await screen.findByText(/photo 3 of 3/)).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: '◀ Earlier' }))
    await waitFor(() => expect(api.movePages)
      .toHaveBeenLastCalledWith(PID, { ids: ['bbbbbbbb'], batch: 'b1', before: 'cccccccc' }))
  })

  it('two quick taps on Later are sent one after the other, each from where the last left it', async () => {
    // Sent side by side, the server could apply them in either order, and an anchor
    // the second one named might not be where the screen showed it yet.
    const rows = ['aaaaaaaa', 'bbbbbbbb', 'cccccccc', 'dddddddd'].map((id, i) => page({ id, seq: i + 1 }))
    renderPages({ pages: rows, batches: [box({ count: 4 })], at: '?photo=aaaaaaaa' })
    const first = deferred()
    api.movePages
      .mockImplementationOnce(() => first.promise)
      .mockResolvedValue({ ok: true, changed: true, rev: 3 })
    const later = await screen.findByRole('button', { name: 'Later ▶' })
    fireEvent.click(later)
    fireEvent.click(later)
    await waitFor(() => expect(api.movePages).toHaveBeenCalledTimes(1))
    expect(api.movePages).toHaveBeenLastCalledWith(PID, { ids: ['aaaaaaaa'], batch: 'b1', before: 'cccccccc' })
    await settle()
    expect(api.movePages).toHaveBeenCalledTimes(1)

    first.resolve({ ok: true, changed: true, rev: 2 })
    await waitFor(() => expect(api.movePages).toHaveBeenCalledTimes(2))
    expect(api.movePages).toHaveBeenLastCalledWith(PID, { ids: ['aaaaaaaa'], batch: 'b1', before: 'dddddddd' })
  })

  const twoChapters = {
    pages: [page(), page({ id: 'cccccccc', seq: 3, batch: 'b2' }), page({ id: 'dddddddd', seq: 4, batch: 'b2' })],
    batches: [box({ count: 1 }), box({ id: 'b2', number: 2 })],
  }

  it('Move to chapter sends a photo to the end of an earlier chapter', async () => {
    renderPages({ ...twoChapters, at: '?photo=cccccccc' })
    api.movePages.mockResolvedValue({ ok: true, changed: true, rev: 2 })
    fireEvent.change(await screen.findByRole('combobox', { name: 'Move to another chapter' }),
                     { target: { value: 'b1' } })
    await waitFor(() => expect(api.movePages)
      .toHaveBeenCalledWith(PID, { ids: ['cccccccc'], batch: 'b1', before: null }))
  })

  it('Move to chapter sends a photo to the start of a later chapter', async () => {
    renderPages({ ...twoChapters, at: '?photo=aaaaaaaa' })
    api.movePages.mockResolvedValue({ ok: true, changed: true, rev: 2 })
    fireEvent.change(await screen.findByRole('combobox', { name: 'Move to another chapter' }),
                     { target: { value: 'b2' } })
    await waitFor(() => expect(api.movePages)
      .toHaveBeenCalledWith(PID, { ids: ['aaaaaaaa'], batch: 'b2', before: 'cccccccc' }))
  })

  it('Split chapter here starts a new chapter from this photo on', async () => {
    renderPages({
      pages: [page(), page({ id: 'bbbbbbbb', seq: 2 }), page({ id: 'cccccccc', seq: 3 })],
      batches: [box({ count: 3 })],
      at: '?photo=bbbbbbbb',
    })
    api.createPageBatch.mockResolvedValue({ batch: box({ id: 'b2', number: 2 }), rev: 2 })
    fireEvent.click(await screen.findByRole('button', { name: 'Split chapter here' }))
    await waitFor(() => expect(api.createPageBatch)
      .toHaveBeenCalledWith(PID, { after: 'b1', ids: ['bbbbbbbb', 'cccccccc'] }))
    await waitFor(() => expect(toast).toHaveBeenCalledWith('Chapter 2 now starts at this photo'))
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

  it('a pending edit is saved, not dropped, when stepping to the next photo', async () => {
    renderPages({ at: '?photo=aaaaaaaa' })
    fireEvent.change(await textBox(), { target: { value: '고친 문장' } })
    fireEvent.click(screen.getByRole('button', { name: 'Next photo' }))
    await waitFor(() => expect(api.savePage).toHaveBeenCalledWith(PID, 'aaaaaaaa', { text: '고친 문장' }))
    expect(await screen.findByText(/photo 2 of 2/)).toBeTruthy()
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
      unworked_seams: 2,
    }))
    await openBuild()
    await waitFor(() => expect(api.buildChapters)
      .toHaveBeenCalledWith(PID, { include: 'approved', append: true, preview: true }))
    expect(await screen.findByText('Photo 2 still needs a check.')).toBeTruthy()
    expect(screen.getByText(/Chapter 2 waits for chapter 1/)).toBeTruthy()
    expect(screen.getByText(/2 joins between photos/)).toBeTruthy()
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
})
