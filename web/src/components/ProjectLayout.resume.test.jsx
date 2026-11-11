import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Outlet, Route, Routes, useOutletContext } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import ProjectLayout from './ProjectLayout'
import { api } from '../api'

// A job that gave up on a rate limit is over on the server, so Resume queues what it still
// had waiting. That list used to be `pending`, which mixes page numbers in with chapters,
// and it all went back as forced translations: page 3 became "re-translate chapter 3",
// and a finished chapter queued without force was translated again. Resume now uses only
// the server's `resumable` list, each item as it was queued.

vi.mock('../toast', () => ({ useToast: () => vi.fn() }))
vi.mock('../confirm', () => ({ useConfirm: () => async () => true }))
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

const PID = 'aaaaaaaaaaaa'

function ResumeButton() {
  const { resumeJob, paused } = useOutletContext()
  return <button disabled={!paused} onClick={() => resumeJob()}>Resume now</button>
}

function TranslateButton() {
  const { enqueue } = useOutletContext()
  return <button onClick={() => enqueue([1])}>Translate chapter 1</button>
}

function renderLayout(child = <ResumeButton />) {
  return render(
    <MemoryRouter initialEntries={[`/novel/${PID}/activity`]}>
      <Routes>
        <Route element={<Outlet context={{ status: {} }} />}>
          <Route path="novel/:pid" element={<ProjectLayout />}>
            <Route path="activity" element={child} />
          </Route>
        </Route>
      </Routes>
    </MemoryRouter>,
  )
}

function storePause(info) {
  localStorage.setItem('nr.paused', JSON.stringify({ [PID]: { message: 'Limit', resets_at: null, ...info } }))
}

beforeEach(() => {
  localStorage.clear()
  for (const name of ['translate', 'resolveChapter', 'fixPronouns']) api[name].mockReset()
  api.translate.mockImplementation(async () => ({}))
  api.chapters.mockImplementation(async (pid) => ({
    project: { id: pid, name: 'Novel', source_type: 'images' }, chapters: [], total: 0,
  }))
  api.glossary.mockImplementation(async () => ({ pending: [], locked: [] }))
  api.activeJob.mockImplementation(async () => ({}))
})

describe('resuming a paused job', () => {
  it('queues only the chapter work, each with its own force', async () => {
    storePause({
      pending: [3, 5, 7, 9],
      resumable: [
        { index: 5, kind: 'translate', force: false },
        { index: 7, kind: 'translate', force: true },
        { index: 9, kind: 'resolve', force: false },
      ],
    })
    renderLayout()
    const resume = await screen.findByRole('button', { name: 'Resume now' })
    await waitFor(() => expect(resume).toBeEnabled())
    fireEvent.click(resume)

    await waitFor(() => expect(api.resolveChapter).toHaveBeenCalledWith(PID, 9))
    expect(api.translate.mock.calls).toEqual([
      [PID, { indices: [5], force: false }],
      [PID, { indices: [7], force: true }],
    ])
    expect(api.fixPronouns).not.toHaveBeenCalled()
  })

  it('resumes nothing from a pause saved before the list existed', async () => {
    storePause({ pending: [3, 5] })
    renderLayout()
    const resume = await screen.findByRole('button', { name: 'Resume now' })
    await waitFor(() => expect(resume).toBeEnabled())
    fireEvent.click(resume)

    await waitFor(() => expect(screen.getByRole('button', { name: 'Resume now' })).toBeDisabled())
    expect(api.translate).not.toHaveBeenCalled()
  })

  it('folds the stopped work into a new translation the same way', async () => {
    storePause({ pending: [3, 5], resumable: [{ index: 5, kind: 'translate', force: false }] })
    api.translate.mockImplementation(async () => ({ job_id: 'j1', current: null, pending: [] }))
    api.streamUrl.mockImplementation(() => 'about:blank')
    renderLayout(<TranslateButton />)
    const translate = await screen.findByRole('button', { name: 'Translate chapter 1' })
    fireEvent.click(translate)

    await waitFor(() => expect(api.translate).toHaveBeenCalledTimes(2))
    expect(api.translate.mock.calls).toEqual([
      [PID, { indices: [1], force: false }],
      [PID, { indices: [5], force: false }],
    ])
  })
})
