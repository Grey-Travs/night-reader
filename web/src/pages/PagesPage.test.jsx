import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import PagesPage from './PagesPage'
import { api } from '../api'

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

const outletContext = vi.fn()
vi.mock('react-router-dom', () => ({ useOutletContext: () => outletContext() }))

vi.mock('../api', () => ({
  api: {
    pages: vi.fn(),
    page: vi.fn(),
    readPages: vi.fn(),
    verifyPages: vi.fn(),
    stitchPages: vi.fn(),
    savePage: vi.fn(),
    deletePages: vi.fn(),
    reorderPages: vi.fn(),
    buildChapters: vi.fn(),
    uploadPages: vi.fn(),
    pageImageUrl: (pid, id) => `/api/projects/${pid}/pages/${id}/image`,
  },
}))

const toast = vi.fn()
vi.mock('../toast', () => ({ useToast: () => toast }))
vi.mock('../confirm', () => ({ useConfirm: () => vi.fn(async () => true) }))

// Mirrors server/pages.py summary() — pages/batches/build/counts/totals, with each
// row carrying `has_text` and `chars` that only exist on the row, never the record.
// counts() seeds EVERY status to 0, so the keys are always present.
const STATUSES = ['new', 'queued', 'ocr-running', 'ok', 'needs-check', 'edited',
  'skipped', 'failed']

function page(over = {}) {
  return {
    id: 'aaaaaaaa', seq: 1, file: 'page-0001.jpg', name: '', bytes: 1000,
    sha256: 'x', batch: 'b1', added_at: '2026-01-01T00:00:00+00:00',
    status: 'new', confidence: '', notes: [], heading: null,
    starts_mid_sentence: false, ends_mid_sentence: false, ends_mid_word: false,
    join_prev: '', join_prev_source: '', join_glue: 'space', join_reason: '',
    hangul_fraction: 0.9, chars: 0, has_text: false, ocr: null, verify: null,
    error: null, ...over,
  }
}

function railPayload(pages, over = {}) {
  const counts = Object.fromEntries(STATUSES.map((s) => [s, 0]))
  for (const p of pages) counts[p.status] = (counts[p.status] || 0) + 1
  counts.total = pages.length
  return {
    pages,
    batches: [{ id: 'b1', label: '', added_at: '2026-01-01T00:00:00+00:00' }],
    build: null,
    counts,
    totals: { cost_usd: 0 },
    ...over,
  }
}

function renderPages({ submitTask, running = false, pages: rows } = {}) {
  const submit = submitTask || vi.fn(async (call) => call())
  outletContext.mockReturnValue({
    pid: 'abcdef012345',
    running,
    submitTask: submit,
    showError: vi.fn(),
    reload: vi.fn(),
    setProjectMeta: vi.fn(),
  })
  const list = rows || [page(), page({ id: 'bbbbbbbb', seq: 2 })]
  api.pages.mockResolvedValue(railPayload(list))
  api.page.mockResolvedValue({ ...list[0], text: '', raw_text: '' })
  return { submit, list, ...render(<PagesPage />) }
}

beforeEach(() => {
  vi.clearAllMocks()
  api.readPages.mockResolvedValue({ job_id: 'job-1', kind: 'ocr', current: 1, pending: [2] })
  api.verifyPages.mockResolvedValue({ job_id: 'job-2', kind: 'ocr-verify', current: 1, pending: [] })
})

describe('PagesPage renders', () => {
  it('renders the rail without throwing', async () => {
    renderPages()
    await waitFor(() => expect(api.pages).toHaveBeenCalled())
    expect(await screen.findByText(/Read 2 pages/i)).toBeTruthy()
  })

  it('renders with no pages at all', async () => {
    renderPages({ pages: [] })
    await waitFor(() => expect(api.pages).toHaveBeenCalled())
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
})
