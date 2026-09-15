import { fireEvent, render, screen, waitFor } from '@testing-library/react'
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
    source_changed: false,
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

// Rebuilding a scanned novel, or correcting a page already built into a chapter,
// replaces the chapter's Korean while the translation file stays where it is. Both
// panes then show real text that was never a translation of each other, and the state
// record still said "validated", so nothing told the reader.

describe('a chapter whose source changed after translation', () => {
  it('says so', async () => {
    api.chapter.mockResolvedValue(chapterPayload({ source_changed: true }))
    renderReader()
    expect(await screen.findByText(/original changed after this was translated/i))
      .toBeInTheDocument()
  })

  it('still shows the translation, which the reader paid for', async () => {
    api.chapter.mockResolvedValue(chapterPayload({ source_changed: true }))
    renderReader()
    expect(await screen.findByText(/The door slid open/)).toBeInTheDocument()
  })

  it('offers the re-translate that fixes it', async () => {
    const onRetranslate = vi.fn()
    api.chapter.mockResolvedValue(chapterPayload({ source_changed: true }))
    renderReader({ onRetranslate, onClose: () => {} })
    fireEvent.click(await screen.findByRole('button', { name: /Re-translate this chapter/i }))
    expect(onRetranslate).toHaveBeenCalledWith(1)
  })

  it('says nothing when the source still matches', async () => {
    api.chapter.mockResolvedValue(chapterPayload())
    renderReader()
    await screen.findByText(/The door slid open/)
    expect(screen.queryByText(/original changed after this was translated/i)).toBeNull()
  })

  it('says nothing on an untranslated chapter', async () => {
    // No English means nothing to be out of step with, whatever the flag says.
    api.chapter.mockResolvedValue(
      chapterPayload({ source_changed: true, translation: null, status: 'pending' }))
    renderReader()
    await screen.findByText(/그는 천천히 문을 열었다/)
    expect(screen.queryByText(/original changed after this was translated/i)).toBeNull()
  })
})

// The rewrite drawer renders each kept version as Markdown. It used to be handed the
// CHAPTER's components, which put a ✎ handle in every version card's margin — and
// `blocks` describes the chapter while a preview's markdown is one paragraph on its
// own, so every offset in it resolved to block 0. Clicking that handle called
// openParagraph(0): the drawer silently jumped to the chapter's first paragraph,
// dropping the versions you were comparing.

describe('the rewrite drawer', () => {
  beforeEach(() => {
    api.chapter.mockResolvedValue(chapterPayload())
    // Mirrors chapter_variants() in server/app.py.
    api.chapterVariants.mockResolvedValue({
      index: 1,
      paragraph_count: 3,
      groups: [{
        id: 'g1',
        paragraph: 1,
        original: '"Are you coming?" she asked.',
        current_id: 'v0',
        stale: false,
        source_ko: null,
        alignment: null,
        variants: [
          { id: 'v0', kind: 'original', text: '"Are you coming?" she asked.', warnings: [] },
          { id: 'v1', kind: 'rephrase', text: '"Coming?" she said.', warnings: [] },
        ],
      }],
      retranslate_available: true,
      retranslate_reason: null,
    })
    api.paragraphSource.mockResolvedValue({ korean: [], target: 0, confidence: 0 })
  })

  async function openDrawer(container) {
    await screen.findByText(/The door slid open/)
    fireEvent.click(screen.getByRole('button', { name: /Rewrite paragraph 2/i }))
    await waitFor(() => expect(api.chapterVariants).toHaveBeenCalled())
    return container
  }

  it('offers a rewrite handle on the chapter body', async () => {
    const { container } = renderReader()
    await screen.findByText(/The door slid open/)
    expect(container.querySelectorAll('.reading p.para .para-handle').length)
      .toBeGreaterThan(0)
  })

  it('puts no rewrite handle inside the version cards', async () => {
    const { container } = renderReader()
    await openDrawer(container)

    const version = await screen.findByText(/"Coming\?" she said\./)
    const card = version.closest('p') || version
    expect(card.querySelector('.para-handle')).toBeNull()
    expect(card.hasAttribute('data-para')).toBe(false)
  })

  it('still shows the versions themselves', async () => {
    // A guard on the test above: if the drawer failed to render at all, "no handle
    // inside it" would pass for the wrong reason.
    const { container } = renderReader()
    await openDrawer(container)
    expect(await screen.findByText(/"Coming\?" she said\./)).toBeInTheDocument()
  })
})

// A novel longer than ~100 chapters is split across several Google Docs, so the chapter
// after the last one lives in a DIFFERENT novel. The server says which in a neighbour ref.
//
// This shipped broken: the server sends `project_id` and the navigator read `pid`, so
// crossing a boundary silently kept the CURRENT novel's id and used the other novel's
// index. Chapter 100's Next landed on chapter 1 of the same document, and chapter 101's
// Prev asked a 4-chapter document for its chapter 100. Both are URLs the reader builds
// itself, so only exercising the navigation catches it — the build was happy throughout.
describe('ChapterReader crosses a document boundary', () => {
  const NEXT = { project_id: 'ffffffffffff', index: 1, global: 101, kind: 'chapter' }
  const PREV = { project_id: 'aaaaaaaaaaaa', index: 100, global: 100, kind: 'chapter' }

  beforeEach(() => {
    api.chapter.mockResolvedValue(chapterPayload({ global: 100, next: NEXT, prev: PREV }))
  })

  it('sends Next to the neighbour’s own novel, not the current one', async () => {
    const onNavigate = vi.fn()
    renderReader({ onNavigate })
    await screen.findByText(/The door slid open/)
    fireEvent.click(screen.getByRole('button', { name: /Next chapter/i }))
    expect(onNavigate).toHaveBeenCalledTimes(1)
    const ref = onNavigate.mock.calls[0][0]
    expect(ref.project_id).toBe('ffffffffffff')
    expect(ref.index).toBe(1)
  })

  it('sends Prev to the previous novel', async () => {
    const onNavigate = vi.fn()
    const { container } = renderReader({ onNavigate })
    await screen.findByText(/The door slid open/)
    const prev = [...container.querySelectorAll('button')]
      .find((b) => /Prev/i.test(b.textContent))
    fireEvent.click(prev)
    const ref = onNavigate.mock.calls[0][0]
    expect(ref.project_id).toBe('aaaaaaaaaaaa')
    expect(ref.index).toBe(100)
  })

  it('shows the series chapter number, not the document’s own', async () => {
    // Part 2's first tab is its chapter 1 but the series' chapter 101. Labelling it "1"
    // right after chapter 100 made a correct jump look like a loop back to the start.
    renderReader()
    await screen.findByText(/The door slid open/)
    expect(screen.getAllByText(/Chapter 100/).length).toBeGreaterThan(0)
  })

  it('falls back to its own chapter list when the novel is in no series', async () => {
    api.chapter.mockResolvedValue(chapterPayload())  // no prev/next, no global
    const onNavigate = vi.fn()
    renderReader({ onNavigate })
    await screen.findByText(/The door slid open/)
    fireEvent.click(screen.getByRole('button', { name: /Next chapter/i }))
    const ref = onNavigate.mock.calls[0][0]
    expect(ref.project_id).toBe('abcdef012345')   // the novel it is already in
    expect(ref.index).toBe(2)
  })
})
