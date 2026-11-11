import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import PhoneCheckPage from './PhoneCheckPage'
import { api } from '../api'

// Checking pages on the phone: what is listed, what each button saves, and that an edit
// stays on the phone until a button is pressed.

const h = vi.hoisted(() => ({ outbox: { chapters: [] }, confirm: true }))
vi.mock('../outbox', () => ({ usePhoneOutbox: () => [null, h.outbox] }))
vi.mock('../toast', () => ({ useToast: () => () => {} }))
vi.mock('../confirm', () => ({ useConfirm: () => async () => h.confirm }))
vi.mock('../api', () => ({
  api: {
    phoneProgress: vi.fn(), page: vi.fn(), savePage: vi.fn(), readPages: vi.fn(),
    createPageBatch: vi.fn(), markCaptured: vi.fn(), deletePageBatch: vi.fn(),
    pageImageUrl: (pid, id) => `/api/projects/${pid}/pages/${id}/image`,
  },
}))

const novels = (rows) => ({ novels: [{ pid: 'p1', name: '달빛', stopped: false, chapters: rows }] })
const row = (over = {}) => ({
  batch: 'b1', number: 12, step: 'needs-you', photos: 3, origin: 'phone:x', message: '', needs_you: [], ...over,
})
const need = (over = {}) => ({ page_id: 'pg2', photo: 2, kind: 'to-check', message: 'Photo 2 needs checking.', ...over })
const record = (over = {}) => ({
  id: 'pg2', batch: 'b1', text: '본문입니다.', status: 'needs-check', notes: ['the corner is folded'],
  ocr: { at: 'r1' }, verify: { at: 'v1', verdict: 'discrepancies',
    issues: [{ kind: 'missing', where: '본문', page_says: '본문이 더', suggest: '본문이 더' }] },
  look: null, ...over,
})

let where = null
function Where() { where = useLocation(); return null }

function renderAt(path) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Where />
      <Routes>
        <Route path="/phone/check" element={<PhoneCheckPage />} />
        <Route path="/phone/check/:pid/:pageId" element={<PhoneCheckPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  h.outbox = { chapters: [] }
  h.confirm = true
  api.phoneProgress.mockResolvedValue(novels([row({ needs_you: [need()] })]))
  api.page.mockResolvedValue(record())
  api.savePage.mockResolvedValue({})
  api.readPages.mockResolvedValue({})
})

describe('the list', () => {
  it('lists each page that needs you, linked to it', async () => {
    renderAt('/phone/check')
    const link = await screen.findByRole('link', { name: /Chapter 12 · photo 2/ })
    expect(link.getAttribute('href')).toBe('/phone/check/p1/pg2')
  })

  it('offers Finish and Delete on a chapter left open that this phone isn’t shooting', async () => {
    api.phoneProgress.mockResolvedValue(novels([row({ step: 'capturing', origin: 'tg:abc' })]))
    renderAt('/phone/check')
    fireEvent.click(await screen.findByRole('button', { name: 'Finish it' }))
    await waitFor(() => expect(api.markCaptured).toHaveBeenCalledWith('p1', 'b1'))
    fireEvent.click(screen.getByRole('button', { name: 'Delete it' }))
    await waitFor(() => expect(api.deletePageBatch).toHaveBeenCalledWith('p1', 'b1'))
  })

  it('leaves alone the chapter this phone is shooting', async () => {
    h.outbox = { chapters: [{ state: 'open', origin: 'phone:x' }] }
    api.phoneProgress.mockResolvedValue(novels([row({ step: 'capturing', origin: 'phone:x' })]))
    renderAt('/phone/check')
    await screen.findByText('Nothing needs you.')
    expect(screen.queryByRole('button', { name: 'Finish it' })).toBeNull()
  })
})

describe('one page', () => {
  it('approves an unchanged page as it is, and goes on', async () => {
    renderAt('/phone/check/p1/pg2')
    expect(await screen.findByDisplayValue('본문입니다.')).toBeTruthy()
    expect(screen.getByText('· the corner is folded')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Looks good' }))
    await waitFor(() => expect(api.savePage).toHaveBeenCalledWith('p1', 'pg2', { status: 'ok' }))
    await waitFor(() => expect(where.pathname).toBe('/phone/check'))
  })

  it('saves a corrected page only when Looks good is pressed', async () => {
    renderAt('/phone/check/p1/pg2')
    const box = await screen.findByLabelText('Korean text read from this photo')
    fireEvent.change(box, { target: { value: '고친 본문.' } })
    expect(api.savePage).not.toHaveBeenCalled()
    expect(JSON.parse(localStorage.getItem('nr.phone.draft.pg2'))).toEqual({ base: 'r1|v1', text: '고친 본문.' })
    fireEvent.click(screen.getByRole('button', { name: 'Looks good' }))
    await waitFor(() => expect(api.savePage).toHaveBeenCalledWith('p1', 'pg2', { text: '고친 본문.', status: 'edited' }))
    expect(localStorage.getItem('nr.phone.draft.pg2')).toBeNull()
  })

  it('keeps an unsaved edit, and drops it when the page was read again', async () => {
    localStorage.setItem('nr.phone.draft.pg2', JSON.stringify({ base: 'r1|v1', text: '남은 편집' }))
    const { unmount } = renderAt('/phone/check/p1/pg2')
    expect(await screen.findByDisplayValue('남은 편집')).toBeTruthy()
    unmount()
    api.page.mockResolvedValue(record({ ocr: { at: 'r2' } }))
    renderAt('/phone/check/p1/pg2')
    expect(await screen.findByDisplayValue('본문입니다.')).toBeTruthy()
    expect(screen.getByText(/the edit you hadn’t saved was dropped/)).toBeTruthy()
  })

  it('applies a fix from the double-check to the edit', async () => {
    renderAt('/phone/check/p1/pg2')
    await screen.findByDisplayValue('본문입니다.')
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    expect(screen.getByDisplayValue('본문이 더입니다.')).toBeTruthy()
  })

  it('skips, reads again, and offers Retake', async () => {
    renderAt('/phone/check/p1/pg2')
    await screen.findByDisplayValue('본문입니다.')
    const retake = screen.getByRole('link', { name: 'Retake' }).getAttribute('href')
    expect(retake).toMatch(/^\/phone\/capture\?pid=p1&retake=pg2&label=.*&back=\/phone\/check$/)
    fireEvent.click(screen.getByRole('button', { name: 'Read again' }))
    await waitFor(() => expect(api.readPages).toHaveBeenCalledWith('p1', ['pg2']))
  })

  it('won’t approve a page with no text', async () => {
    api.page.mockResolvedValue(record({ text: '' }))
    renderAt('/phone/check/p1/pg2')
    await screen.findByLabelText('Korean text read from this photo')
    expect(screen.getByRole('button', { name: 'Looks good' })).toBeDisabled()
  })

  it('splits a chapter at a heading, or keeps it together', async () => {
    api.phoneProgress.mockImplementation(async ({ box } = {}) => (box
      ? { novels: [{ pid: 'p1', photos: [{ id: 'pg1' }, { id: 'pg2' }, { id: 'pg3' }] }] }
      : novels([row({ needs_you: [need({ kind: 'heading', message: 'Photo 2 opens with a chapter heading.' })] })])))
    renderAt('/phone/check/p1/pg2')
    await screen.findByDisplayValue('본문입니다.')
    fireEvent.click(await screen.findByRole('button', { name: 'Split here' }))
    await waitFor(() => expect(api.createPageBatch).toHaveBeenCalledWith('p1', { after: 'b1', ids: ['pg2', 'pg3'] }))
  })

  it('keeps a chapter together by setting the join before the heading', async () => {
    api.phoneProgress.mockResolvedValue(novels([row({ needs_you: [need({ kind: 'heading', message: 'Photo 2 opens with a chapter heading.' })] })]))
    renderAt('/phone/check/p1/pg2')
    fireEvent.click(await screen.findByRole('button', { name: 'Keep together' }))
    await waitFor(() => expect(api.savePage).toHaveBeenCalledWith('p1', 'pg2', { join_prev: 'paragraph' }))
  })
})
