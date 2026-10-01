import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Outlet, Route, Routes, useNavigate } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import ProjectLayout from './ProjectLayout'
import PagesPage from '../pages/PagesPage'
import { api } from '../api'
import { resetUploads } from '../uploadQueue'

// A build carries on when its dialog is closed, and the reader can be in another novel by
// the time it answers. ProjectLayout stays mounted when only the novel changes, so the
// build's project and chapter list, put into it, showed one novel's chapters under the
// other's name, and its toast said "1 chapter added" over a novel that had none. These
// drive the real layout around the Pages tab, since that's where the two meet.

const toast = vi.fn()
vi.mock('../toast', () => ({ useToast: () => toast }))
vi.mock('../confirm', () => ({ useConfirm: () => async () => true }))
// Every call the layout makes on mount answers with nothing, unless a test says otherwise.
vi.mock('../api', () => {
  const calls = {}
  return {
    api: new Proxy(calls, {
      get: (made, name) => {
        if (!(name in made)) made[name] = vi.fn(async () => ({}))
        return made[name]
      },
    }),
  }
})

const A = 'aaaaaaaaaaaa'
const B = 'bbbbbbbbbbbb'
const STATUSES = ['new', 'queued', 'ocr-running', 'ok', 'needs-check', 'edited', 'skipped', 'failed']

function rail(pid) {
  const counts = Object.fromEntries(STATUSES.map((s) => [s, 0]))
  counts.ok = 1
  counts.total = 1
  return {
    pages: [{
      id: `${pid.slice(0, 7)}1`, seq: 1, file: 'page-0001.jpg', name: '', batch: `x${pid[0]}`,
      status: 'ok', has_text: true, chars: 10, notes: [], ocr: null, verify: null, join_prev: '',
    }],
    batches: [{ id: `x${pid[0]}`, label: '', title: 'Chapter 1', count: 1, chapter: null, number: 1,
                state: 'ready', blockers: [] }],
    build: null, counts, totals: { cost_usd: 0 }, chapter_count: 0, rev: 1,
  }
}

const plan = (over = {}) => ({
  ok: true, preview: true, chapters: 0, added: [], refreshed: [], updated: [], waiting: [],
  warnings: [], unworked_seams: 0, ...over,
})

function deferred() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}

const settle = () => act(() => new Promise((done) => setTimeout(done, 0)))

let go = null
function Navigator() {
  go = useNavigate()
  return null
}

function renderApp() {
  return render(
    <MemoryRouter initialEntries={[`/novel/${A}/pages`]}>
      <Navigator />
      <Routes>
        <Route element={<Outlet context={{ status: {} }} />}>
          <Route index element={<div>Library</div>} />
          <Route path="novel/:pid" element={<ProjectLayout />}>
            <Route index element={<div>Chapters tab</div>} />
            <Route path="pages" element={<PagesPage />} />
          </Route>
        </Route>
      </Routes>
    </MemoryRouter>,
  )
}

let built = null
beforeEach(() => {
  resetUploads()
  toast.mockReset()
  api.chapters.mockReset()
  api.chapters.mockImplementation(async (pid) => ({
    project: { id: pid, name: `Novel ${pid[0].toUpperCase()}`, source_type: 'images' }, chapters: [], total: 0,
  }))
  api.glossary.mockImplementation(async () => ({ pending: [], locked: [] }))
  api.activeJob.mockImplementation(async () => ({}))
  api.pages.mockImplementation(async (pid) => rail(pid))
  api.page.mockImplementation(async () => ({ text: '' }))
  built = deferred()
  api.buildChapters.mockImplementation(async (_pid, body) => (body.preview ? plan({ added: [1] }) : built.promise))
})

async function buildAndClose() {
  fireEvent.click(await screen.findByRole('button', { name: 'Build chapters →' }))
  const build = await screen.findByRole('button', { name: 'Build' })
  await waitFor(() => expect(build).toBeEnabled())
  fireEvent.click(build)
  await within(screen.getByRole('dialog')).findByRole('button', { name: 'Building…' })
  fireEvent.keyDown(window, { key: 'Escape' })
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
}

async function answerBuild() {
  built.resolve({ ...plan({ added: [1], chapters: 1 }), preview: false, project: { id: A, name: 'Novel A' } })
  await settle()
  await settle()
}

describe('a build closed in one novel and answered in another', () => {
  it('leaves the other novel alone when only the novel changed', async () => {
    renderApp()
    await screen.findByRole('heading', { name: 'Novel A' })
    await buildAndClose()
    act(() => go(`/novel/${B}/pages`))
    await screen.findByRole('heading', { name: 'Novel B' })
    const fetched = api.chapters.mock.calls.length

    await answerBuild()
    expect(screen.getByRole('heading', { name: 'Novel B' })).toBeTruthy()
    expect(api.chapters.mock.calls.length).toBe(fetched)
    expect(toast).not.toHaveBeenCalled()
  })

  it('leaves the other novel alone when it was opened from the library', async () => {
    // The first layout is gone then, and the one showing novel B is a new one.
    renderApp()
    await screen.findByRole('heading', { name: 'Novel A' })
    await buildAndClose()
    act(() => go('/'))
    await screen.findByText('Library')
    act(() => go(`/novel/${B}/pages`))
    await screen.findByRole('heading', { name: 'Novel B' })

    await answerBuild()
    expect(screen.getByRole('heading', { name: 'Novel B' })).toBeTruthy()
    expect(toast).not.toHaveBeenCalled()
  })

  it('still reports to its own novel when that one is on screen', async () => {
    renderApp()
    await screen.findByRole('heading', { name: 'Novel A' })
    await buildAndClose()
    await answerBuild()
    expect(toast).toHaveBeenCalledWith('1 chapter added')
    expect(api.chapters).toHaveBeenLastCalledWith(A, true)
  })
})
