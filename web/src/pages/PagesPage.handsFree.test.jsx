import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import PagesPage from './PagesPage'
import { api } from '../api'
import { resetUploads } from '../uploadQueue'

// Phone chapters on the desktop. A chapter the phone is still photographing never builds,
// so one whose Done never comes — the phone lost, a Telegram /done forgotten — would hold
// back every chapter after it unless the desktop can finish it. And the empty box the phone
// makes before its first photo lands is the phone's, not a box for a desktop drop.

const outletContext = vi.fn()
vi.mock('react-router-dom', async (actual) => ({
  ...(await actual()),
  useOutletContext: () => outletContext(),
}))

vi.mock('../api', () => ({
  api: {
    pages: vi.fn(),
    page: vi.fn(),
    uploadPage: vi.fn(),
    createPageBatch: vi.fn(),
    deletePageBatch: vi.fn(),
    markCaptured: vi.fn(),
    resumeHandsFree: vi.fn(),
    pageImageUrl: (pid, id) => `/api/projects/${pid}/pages/${id}/image`,
  },
}))

const toast = vi.fn()
vi.mock('../toast', () => ({ useToast: () => toast }))
vi.mock('../confirm', () => ({ useConfirm: () => async () => true }))

const PID = 'abcdef012345'
const STATUSES = ['new', 'queued', 'ocr-running', 'ok', 'needs-check', 'edited', 'skipped', 'failed']
const CAPTURING = 'This chapter is still being photographed. When every page is in, tap Done on the phone or press Finish.'

function page(id, batch) {
  return {
    id, seq: 1, file: 'page-0001.jpg', name: '', batch, status: 'ok', has_text: true, chars: 10,
    notes: [], heading: null, join_prev: '', join_prev_id: null, ocr: null, verify: null,
  }
}

function phoneBox(id, number, { captured = false, count = 1 } = {}) {
  return {
    id, label: '', title: `Chapter ${number}`, at: '', count, chapter: null, number,
    state: captured ? 'ready' : 'capturing',
    blockers: captured ? [] : [{ kind: 'capturing', count: 0, page_ids: [], message: CAPTURING }],
    hands_free: true, origin: `phone:${id}`, captured_at: captured ? '2026-10-02T00:00:00+00:00' : null,
    expect_number: null,
  }
}

function rail(pages, batches, rev = 1) {
  const counts = Object.fromEntries(STATUSES.map((s) => [s, 0]))
  counts.ok = pages.length
  counts.total = pages.length
  return { pages, batches, build: null, counts, totals: { cost_usd: 0 }, chapter_count: 0, rev }
}

function renderPages() {
  outletContext.mockReturnValue({
    pid: PID, running: false, submitTask: vi.fn(async (call) => call()), showError: vi.fn(),
    reload: vi.fn(), setProjectMeta: vi.fn(), isShowing: (id) => id === PID, chapters: [],
  })
  return render(<MemoryRouter initialEntries={[`/novel/${PID}/pages`]}><PagesPage /></MemoryRouter>)
}

beforeEach(() => {
  vi.resetAllMocks()
  resetUploads()
})

describe('a chapter sent from the phone', () => {
  it('says it is still being photographed, and Finish lets it build', async () => {
    const pages = [page('aaaaaaaa', 'b1')]
    api.pages.mockResolvedValue(rail(pages, [phoneBox('b1', 1)]))
    api.markCaptured.mockResolvedValue({
      batch: phoneBox('b1', 1, { captured: true }), rev: 2,
      pages: rail(pages, [phoneBox('b1', 1, { captured: true })], 2),
    })
    renderPages()
    const region = await screen.findByRole('region', { name: 'Chapter 1' })
    expect(within(region).getByText('Still being photographed')).toBeTruthy()
    expect(within(region).getByText('· hands-free')).toBeTruthy()

    fireEvent.click(within(region).getByRole('button', { name: 'Finish Chapter 1' }))
    await waitFor(() => expect(api.markCaptured).toHaveBeenCalledWith(PID, 'b1'))
    await waitFor(() => expect(within(region).getByText('Ready to build')).toBeTruthy())
    expect(within(region).queryByRole('button', { name: 'Finish Chapter 1' })).toBeNull()
  })

  it('finishes a finished chapter again once a drop into it is all in', async () => {
    const pages = [page('aaaaaaaa', 'b1')]
    const finished = phoneBox('b1', 1, { captured: true })
    api.pages.mockResolvedValue(rail(pages, [finished]))
    let sent = 0
    api.uploadPage.mockImplementation(async (_pid, _f, { batch }) => {
      sent += 1
      return {
        page: { id: `cccccc0${sent}` }, duplicate: false, batch, number: 1, total: 1 + sent,
        reopened: sent === 1, rev: 1 + sent, pages: rail(pages, [phoneBox('b1', 1)], 1 + sent),
      }
    })
    api.markCaptured.mockResolvedValue({ batch: finished, rev: 9, pages: rail(pages, [finished], 9) })
    renderPages()
    const region = await screen.findByRole('region', { name: 'Chapter 1' })
    fireEvent.drop(region, { dataTransfer: { types: ['Files'], files: [
      new File(['x'], 'p1.jpg', { type: 'image/jpeg' }), new File(['y'], 'p2.jpg', { type: 'image/jpeg' }),
    ] } })

    await waitFor(() => expect(api.markCaptured).toHaveBeenCalledTimes(1))
    expect(api.markCaptured).toHaveBeenCalledWith(PID, 'b1')
    expect(api.uploadPage).toHaveBeenCalledTimes(2)   // only once the whole drop was in
  })

  it('leaves a chapter the drop reopened open when the drop stopped', async () => {
    const pages = [page('aaaaaaaa', 'b1')]
    api.pages.mockResolvedValue(rail(pages, [phoneBox('b1', 1, { captured: true })]))
    api.uploadPage
      .mockImplementationOnce(async (_pid, _f, { batch }) => ({
        page: { id: 'cccccc01' }, duplicate: false, batch, number: 1, total: 2, reopened: true,
        rev: 2, pages: rail(pages, [phoneBox('b1', 1)], 2),
      }))
      .mockRejectedValueOnce(Object.assign(new Error('offline'), { status: 0 }))
    renderPages()
    const region = await screen.findByRole('region', { name: 'Chapter 1' })
    fireEvent.drop(region, { dataTransfer: { types: ['Files'], files: [
      new File(['x'], 'p1.jpg', { type: 'image/jpeg' }), new File(['y'], 'p2.jpg', { type: 'image/jpeg' }),
    ] } })

    await waitFor(() => expect(api.uploadPage).toHaveBeenCalledTimes(2))
    await new Promise((done) => setTimeout(done, 20))
    expect(api.markCaptured).not.toHaveBeenCalled()
  })

  it('says when Stop paused the phone’s chapters, and Resume lets them carry on', async () => {
    const pages = [page('aaaaaaaa', 'b1')]
    const paused = { ...rail(pages, [phoneBox('b1', 1, { captured: true })]), hands_free_paused_at: '2026-10-02T08:00:00+00:00' }
    api.pages.mockResolvedValueOnce(paused).mockResolvedValue(rail(pages, [phoneBox('b1', 1, { captured: true })], 2))
    api.resumeHandsFree.mockResolvedValue({ ok: true, queued_pages: 0, queued_chapters: 1 })
    renderPages()
    const banner = await screen.findByText(/Hands-free is paused/)
    fireEvent.click(within(banner.parentElement).getByRole('button', { name: 'Resume' }))
    await waitFor(() => expect(api.resumeHandsFree).toHaveBeenCalledWith(PID))
    await waitFor(() => expect(screen.queryByText(/Hands-free is paused/)).toBeNull())
  })

  it('leaves the phone’s empty box alone when photos are dropped on New chapter', async () => {
    const pages = [page('aaaaaaaa', 'b1')]
    const empty = { ...phoneBox('b2', 2, { count: 0 }) }
    api.pages.mockResolvedValue(rail(pages, [phoneBox('b1', 1, { captured: true }), empty]))
    api.createPageBatch.mockResolvedValue({
      batch: { id: 'b3', label: '', title: 'Chapter 3', count: 0, chapter: null, number: 3, state: 'empty', blockers: [] },
      rev: 2, pages: rail(pages, [phoneBox('b1', 1, { captured: true }), empty,
        { id: 'b3', label: '', title: 'Chapter 3', count: 0, chapter: null, number: 3, state: 'empty', blockers: [] }], 2),
    })
    api.uploadPage.mockImplementation(async (_pid, f, { batch }) => ({
      page: { id: 'cccccccc' }, duplicate: false, batch, number: 3, total: 2,
      rev: 3, pages: rail(pages, [phoneBox('b1', 1, { captured: true }), empty], 3),
    }))
    renderPages()
    await screen.findByRole('region', { name: 'Chapter 2' })
    expect(screen.queryByRole('button', { name: 'Finish Chapter 2' })).toBeNull()   // nothing to finish yet

    fireEvent.drop(await screen.findByRole('button', { name: /^New chapter — drop photos here/ }),
      { dataTransfer: { types: ['Files'], files: [new File(['x'], 'p1.jpg', { type: 'image/jpeg' })] } })
    await waitFor(() => expect(api.uploadPage).toHaveBeenCalled())
    expect(api.createPageBatch).toHaveBeenCalled()
    expect(api.uploadPage.mock.calls.map((c) => c[2].batch)).toEqual(['b3'])
  })
})
