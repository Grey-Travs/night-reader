import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import PhoneSharePage from './PhoneSharePage'
import { api } from '../api'
import { createOutbox } from '../outbox'
import { memoryStore } from '../outboxStore'

// Sharing photos or a scanner PDF to Night Reader: the order they go in, the novel and
// the chapter, and what is said when a share can't be used.

const h = vi.hoisted(() => ({ box: null, share: null, deleted: [] }))
vi.mock('../outbox', async (actual) => {
  const real = await actual()
  return { ...real, usePhoneOutbox: () => [h.box, real.useOutbox(h.box)] }
})
vi.mock('../shareInbox', async (actual) => ({
  ...(await actual()),
  readShare: vi.fn(async () => h.share),
  deleteShare: vi.fn(async (id) => { h.deleted.push(id) }),
}))
vi.mock('../camera', () => ({ encodePicked: vi.fn(async (file) => ({ blob: file })) }))
vi.mock('../api', () => ({ api: { scans: vi.fn(), pages: vi.fn() } }))

const never = () => new Promise(() => {})
const file = (name, type = 'image/jpeg') => new File(['x'], name, { type })

function renderAt(search) {
  return render(
    <MemoryRouter initialEntries={[`/phone/share${search}`]}>
      <Routes>
        <Route path="/phone/share" element={<PhoneSharePage />} />
        <Route path="/phone" element={<div>The hub</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(async () => {
  localStorage.clear()
  h.deleted = []
  h.share = { id: 's1', text: '', files: [file('IMG_10.jpg'), file('IMG_2.jpg'), file('IMG_1.jpg')] }
  h.box = createOutbox({ store: memoryStore(), api: { createPageBatch: never, uploadPage: never, uploadPdf: never }, locks: null })
  await h.box.resume()
  api.scans.mockResolvedValue({ novels: [{ id: 'p1', name: '달빛 소설', next_number: 7 }] })
  api.pages.mockResolvedValue({ batches: [
    { id: 'b5', chapter: 5, number: 5 }, { id: 'b6', chapter: null, number: 6 },
  ] })
})

const names = () => within(screen.getByRole('list', { name: /Shared files/ })).getAllByRole('listitem')
  .map((li) => li.textContent.replace(/[↑✕]/g, '').replace(/^\d+/, ''))

describe('a share', () => {
  it('lists the files in page order, and can turn it round', async () => {
    renderAt('?id=s1')
    await screen.findByRole('list', { name: /Shared files/ })
    expect(names()).toEqual(['IMG_1.jpg', 'IMG_2.jpg', 'IMG_10.jpg'])
    fireEvent.click(screen.getByRole('button', { name: 'Reverse the order' }))
    expect(names()).toEqual(['IMG_10.jpg', 'IMG_2.jpg', 'IMG_1.jpg'])
  })

  it('sends them as a new chapter through the outbox, then forgets the share', async () => {
    renderAt('?id=s1')
    await screen.findByRole('list', { name: /Shared files/ })
    fireEvent.change(await screen.findByRole('combobox', { name: 'Novel' }), { target: { value: 'p1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send 3 to Night Reader' }))
    await screen.findByText('The hub')
    const [ch] = h.box.getSnapshot().chapters
    expect(ch).toMatchObject({ pid: 'p1', origin: 'share:s1', box: null, state: 'closed' })
    expect(ch.photos.map((p) => p.name)).toEqual(['IMG_1.jpg', 'IMG_2.jpg', 'IMG_10.jpg'])
    expect(h.deleted).toEqual(['s1'])
    expect(localStorage.getItem('nr.phone.novel')).toBe('p1')
  })

  it('can add them to the end of a chapter not built yet', async () => {
    localStorage.setItem('nr.phone.novel', 'p1')
    renderAt('?id=s1')
    const where = await screen.findByRole('combobox', { name: 'Where' })
    await waitFor(() => expect(within(where).getAllByRole('option')).toHaveLength(2))
    expect(within(where).getAllByRole('option').map((o) => o.textContent))
      .toEqual(['New chapter at the end — Chapter 7', 'End of Chapter 6'])
    fireEvent.change(where, { target: { value: 'b6' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send 3 to Night Reader' }))
    await screen.findByText('The hub')
    expect(h.box.getSnapshot().chapters[0].box).toBe('b6')
  })

  it('sends a PDF as it is', async () => {
    h.share = { id: 's2', text: '', files: [file('scan.pdf', 'application/pdf')] }
    localStorage.setItem('nr.phone.novel', 'p1')
    renderAt('?id=s2')
    fireEvent.click(await screen.findByRole('button', { name: 'Send 1 to Night Reader' }))
    await screen.findByText('The hub')
    expect(h.box.getSnapshot().chapters[0].photos[0].type).toBe('application/pdf')
  })

  it('refuses a HEIC photo, with what to do about it', async () => {
    h.share = { id: 's3', text: '', files: [file('IMG_1.HEIC', 'image/heic'), file('IMG_2.jpg')] }
    localStorage.setItem('nr.phone.novel', 'p1')
    renderAt('?id=s3')
    expect(await screen.findByText(/High efficiency pictures/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Send 1 to Night Reader' })).toBeDisabled()
  })

  it('explains a Google Drive link', async () => {
    h.share = { id: 's4', text: 'https://drive.google.com/file/d/abc/view', files: [] }
    renderAt('?id=s4')
    expect(await screen.findByText(/In Drive, use ⋮ → Send a copy/)).toBeTruthy()
  })

  it('says when a share was missed', async () => {
    renderAt('?missed=1')
    expect(await screen.findByText(/before Night Reader was ready/)).toBeTruthy()
  })

  it('doesn’t send a share twice', async () => {
    localStorage.setItem('nr.phone.novel', 'p1')
    const first = renderAt('?id=s1')
    fireEvent.click(await screen.findByRole('button', { name: 'Send 3 to Night Reader' }))
    await screen.findByText('The hub')
    first.unmount()
    renderAt('?id=s1')
    fireEvent.click(await screen.findByRole('button', { name: 'Already sent — done' }))
    await screen.findByText('The hub')
    expect(h.box.getSnapshot().chapters).toHaveLength(1)
  })
})
