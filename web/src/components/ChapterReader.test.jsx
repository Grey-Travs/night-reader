import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import ChapterReader from './ChapterReader'
import { api } from '../api'

// The reader shipped broken: `openParagraph` was named in a useEffect dependency
// array declared 128 lines before the const itself, so the array — an ordinary
// expression evaluated during render — threw ReferenceError on EVERY render. The
// error boundary swallowed it and the reader was completely unreachable.
//
// `npm run build` passed the whole time, because a bad dependency array is valid
// JavaScript. Only rendering the component catches this, which is what these do.

vi.mock('../api', () => ({
  api: {
    chapter: vi.fn(),
    saveChapter: vi.fn(),
    previousChapter: vi.fn(),
    scanChapter: vi.fn(),
    deepScanChapter: vi.fn(),
    fixChapter: vi.fn(),
    acceptChapter: vi.fn(),
    chapterVariants: vi.fn(),
    paragraphSource: vi.fn(),
    pageImageUrl: (pid, id) => `/api/projects/${pid}/pages/${id}/image`,
  },
}))

const CHAPTERS = [
  { index: 1, title: 'Chapter one', status: 'validated' },
  { index: 2, title: 'Chapter two', status: 'validated' },
]

// Mirrors chapter_detail()'s response in server/app.py. Keep the SHAPES honest —
// `flags` and `failures` are lists there (failure_flags returns list[str]), and a
// fixture that gets that wrong tests the fixture rather than the component.
function chapterPayload(over = {}) {
  return {
    index: 1,
    title: 'Chapter one',
    number: '1',
    language: 'korean',
    source: '그는 천천히 문을 열었다.\n\n밖에는 아무도 없었다.',
    translation: 'The door slid open.\n\n"Are you coming?" she asked.\n\nHe said nothing.\n',
    status: 'validated',
    validation: null,
    failures: [],
    diagnosis: [],
    flags: [],
    manual_edit: false,
    has_previous: false,
    offline: false,
    page_ids: [],
    ...over,
  }
}

function renderReader(over = {}) {
  return render(
    <ChapterReader
      pid="abcdef012345"
      index={1}
      chapters={CHAPTERS}
      glossary={[]}
      onClose={() => {}}
      onNavigate={() => {}}
      onChanged={() => {}}
      {...over}
    />,
  )
}

describe('ChapterReader renders', () => {
  beforeEach(() => {
    api.chapter.mockResolvedValue(chapterPayload())
  })

  it('shows a translated chapter without throwing', async () => {
    // If the component throws during render, RTL propagates it and this fails.
    renderReader()
    expect(await screen.findByText(/The door slid open/)).toBeInTheDocument()
  })

  it('renders every paragraph of the translation', async () => {
    renderReader()
    await screen.findByText(/The door slid open/)
    expect(screen.getByText(/Are you coming\?/)).toBeInTheDocument()
    expect(screen.getByText(/He said nothing/)).toBeInTheDocument()
  })

  it('gives each prose paragraph an addressable ordinal', async () => {
    // The rewrite feature addresses a paragraph by this ordinal, and the server
    // proves it against the exact text. If data-para stops matching the server's
    // block index, a rewrite lands on the wrong paragraph.
    const { container } = renderReader()
    await screen.findByText(/The door slid open/)
    const paras = [...container.querySelectorAll('.reading p.para[data-para]')]
    expect(paras.map((p) => p.dataset.para)).toEqual(['0', '1', '2'])
  })

  it('renders an untranslated chapter', async () => {
    api.chapter.mockResolvedValue(chapterPayload({ translation: null, status: 'pending' }))
    renderReader()
    expect(await screen.findByText(/그는 천천히 문을 열었다/)).toBeInTheDocument()
  })

  it('renders an empty chapter', async () => {
    api.chapter.mockResolvedValue(
      chapterPayload({ translation: null, source: '', language: 'empty' }))
    renderReader()
    expect(await screen.findByText(/This tab is empty/)).toBeInTheDocument()
  })

  it('renders a chapter built from page photos', async () => {
    api.chapter.mockResolvedValue(chapterPayload({ page_ids: ['aaaaaaaa', 'bbbbbbbb'] }))
    renderReader()
    expect(await screen.findByText(/The door slid open/)).toBeInTheDocument()
  })

  it('survives a chapter whose translation is a single paragraph', async () => {
    api.chapter.mockResolvedValue(chapterPayload({ translation: 'Just the one.\n' }))
    renderReader()
    expect(await screen.findByText(/Just the one/)).toBeInTheDocument()
  })

  it('surfaces a load failure instead of hanging', async () => {
    api.chapter.mockRejectedValue(new Error('backend is down'))
    renderReader()
    await waitFor(() => expect(screen.getByText(/backend is down/)).toBeInTheDocument())
  })
})
