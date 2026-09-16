import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import SeriesDetailPage from './SeriesDetailPage'
import { api } from '../api'

// Adding a document to a series is how a novel carries on once its own Google Doc is
// full — and the only way an IMPORTED novel ever gets a next chapter, because it has no
// document at all and the importer puts each one in a series of its own.
//
// Worth testing at the page level for the reason PagesPage.test.jsx records: a page that
// renders perfectly and never calls the function doing the work looks finished and is
// useless. The specific failure this guards is subtler than a dead button, though — the
// stored numbering was resolved BEFORE the new document existed, so a page that adds a
// member without re-resolving shows a series that appears to have lost every chapter of
// the document just added.

vi.mock('react-router-dom', () => ({
  Link: ({ to, children, ...rest }) => <a href={to} {...rest}>{children}</a>,
  useParams: () => ({ sid: 's1' }),
}))

vi.mock('../toast', () => ({ useToast: () => () => {} }))

vi.mock('../api', () => ({
  api: {
    seriesMapping: vi.fn(),
    confirmSeriesMapping: vi.fn(),
    unlinkedNovels: vi.fn(),
    addSeriesMember: vi.fn(),
    mergeSeriesGlossary: vi.fn(),
    updateSeries: vi.fn(),
  },
}))

const MEMBER = {
  project_id: 'p1',
  name: 'Imported Novel',
  chapter_count: 104,
  start_chapter: 1,
  sealed: false,
  missing: false,
  offline: false,
  needs_review: 0,
}

const SERIES = {
  id: 's1',
  name: 'Imported Novel',
  members: [MEMBER],
  publish_targets: [],
  glossary_merged_at: '',
  resolved: true,
}

function row(n) {
  return { index: n, global: n, kind: 'chapter', source: 'header', confidence: 'high' }
}

function mapping(over = {}) {
  return {
    series: SERIES,
    members: { p1: { rows: [row(1), row(2), row(104)] } },
    first: 1,
    last: 104,
    total: 104,
    gaps: [],
    duplicates: [],
    ...over,
  }
}

const FREE = [
  { id: 'p2', name: 'Imported Novel 2', chapter_count: 6, source_type: 'gdoc' },
  { id: 'p3', name: 'Something Else', chapter_count: 40, source_type: 'gdoc' },
]

function setup({ novels = FREE, doc = mapping() } = {}) {
  api.seriesMapping.mockResolvedValue(doc)
  api.unlinkedNovels.mockResolvedValue({ novels })
  return render(<SeriesDetailPage />)
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('adding a document to a series', () => {
  it('offers the novels that are in no series', async () => {
    setup()
    const select = await screen.findByLabelText(/add a document/i)
    await waitFor(() => expect(select.options.length).toBe(3)) // the placeholder plus two
    expect(screen.getByRole('option', { name: /Imported Novel 2/ })).toBeTruthy()
  })

  it('adds the novel that was picked', async () => {
    setup()
    api.addSeriesMember.mockResolvedValue({ ...SERIES, needs_resolve: false })
    const select = await screen.findByLabelText(/add a document/i)
    await waitFor(() => expect(select.options.length).toBe(3))
    fireEvent.change(select, { target: { value: 'p2' } })
    fireEvent.click(screen.getByRole('button', { name: /^add$/i }))
    await waitFor(() => expect(api.addSeriesMember).toHaveBeenCalledWith('s1', 'p2'))
  })

  it('re-reads the numbering, because the stored mapping predates the document', async () => {
    setup()
    api.addSeriesMember.mockResolvedValue({ ...SERIES, needs_resolve: true })
    const select = await screen.findByLabelText(/add a document/i)
    await waitFor(() => expect(select.options.length).toBe(3))
    api.seriesMapping.mockClear()
    fireEvent.change(select, { target: { value: 'p2' } })
    fireEvent.click(screen.getByRole('button', { name: /^add$/i }))
    // refresh=true. Without it the page serves the stored mapping, which holds no rows
    // for the document just added.
    await waitFor(() => expect(api.seriesMapping).toHaveBeenCalledWith('s1', true))
  })

  it('will not add until a novel is picked', async () => {
    setup()
    await screen.findByLabelText(/add a document/i)
    expect(screen.getByRole('button', { name: /^add$/i }).disabled).toBe(true)
  })

  it('says which chapter the new document carries on from', async () => {
    setup()
    // The series ends at 104, so the document being added is chapter 105 onwards. This
    // is the number the user needs before pasting anything into a new Doc.
    expect(await screen.findByText(/carries on from chapter 105/i)).toBeTruthy()
  })

  it('says so when there is nothing free to add', async () => {
    setup({ novels: [] })
    expect(await screen.findByText(/already in a series/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: /^add$/i }).disabled).toBe(true)
  })

  it('keeps the numbering table up when the novel list cannot be read', async () => {
    api.seriesMapping.mockResolvedValue(mapping())
    api.unlinkedNovels.mockRejectedValue(new Error('offline'))
    render(<SeriesDetailPage />)
    // The picker is empty, but the page itself still works.
    expect(await screen.findByText(/Documents, in reading order/i)).toBeTruthy()
  })
})
