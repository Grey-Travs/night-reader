import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { MemoryRouter } from 'react-router-dom'
import PhoneHomePage from './PhoneHome'
import { api } from '../api'

// The hub: the chapters sent from the phone above the posting console. It has to say
// where each chapter stands in plain words, count the pages waiting for a person, and
// offer the one way on from each kind of pause.

vi.mock('../toast', () => ({ useToast: () => () => {} }))

// The hub links to the other phone screens, so it needs a router around it.
const PhoneHome = () => <MemoryRouter><PhoneHomePage /></MemoryRouter>

// The outbox as the hub sees it: what is on the phone, and the actions on it.
const outbox = vi.hoisted(() => ({
  box: null,
  state: null,
}))
vi.mock('../outbox', () => ({ usePhoneOutbox: () => [outbox.box, outbox.state] }))
const quiet = () => ({ ready: true, durable: true, status: { state: 'idle', retryAt: null, message: '' }, chapters: [], unsent: 0 })
const onPhone = (over = {}) => ({
  id: 'c1', pid: 'p1', seq: 1, state: 'open', box: 'b9', number: 7, shown: 7, stop: null,
  photos: [{ id: 'f1', order: 1, state: 'sent' }, { id: 'f2', order: 2, state: 'waiting' }], waiting: 1, held: false, ...over,
})
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

const row = (over = {}) => ({
  batch: 'b1', number: 12, step: 'capturing', photos: 3, message: '', needs_you: [],
  pages: { read: 0, reading: 0, unread: 0, to_check: 0, failed: 0, skipped: 0 }, ...over,
})

beforeEach(() => {
  outbox.box = {
    retryNow: vi.fn(), closeChapter: vi.fn(), resendAsNew: vi.fn(), discard: vi.fn(),
    skipPhoto: vi.fn(), retryChapter: vi.fn(),
  }
  outbox.state = quiet()
  api.phoneProgress.mockReset()
  api.resumeHandsFree.mockReset()
  api.resumeHandsFree.mockResolvedValue({ ok: true })
  api.listSeries.mockResolvedValue({ series: [] })
})

describe('the hub', () => {
  it('shows where each chapter stands and how many pages need you', async () => {
    api.phoneProgress.mockResolvedValue({ novels: [{
      pid: 'p1', name: '달빛 소설', stopped: false, waiting: null, gave_up: null,
      chapters: [
        row({ batch: 'b1', number: 11, step: 'needs-you', message: 'Photo 4 needs checking.',
          needs_you: [{ page_id: 'x', photo: 4, kind: 'to-check' }] }),
        row({ batch: 'b2', number: 12 }),
      ],
    }] })
    render(<PhoneHome />)
    const card = (await screen.findByText('달빛 소설')).closest('div')
    expect(within(card).getByText('Photo 4 needs checking.')).toBeTruthy()
    expect(within(card).getByText('Still being photographed · 3 photos')).toBeTruthy()
    expect(screen.getByRole('link', { name: '1 page needs you →' }).getAttribute('href')).toBe('/phone/check')
    expect(screen.getByText('Night Reader')).toBeTruthy()   // the posting console is still there
  })

  it('offers Resume for a stopped novel', async () => {
    api.phoneProgress.mockResolvedValue({ novels: [{
      pid: 'p1', name: 'Novel', stopped: true, waiting: null, gave_up: null, chapters: [row()],
    }] })
    render(<PhoneHome />)
    fireEvent.click(await screen.findByRole('button', { name: 'Resume' }))
    await waitFor(() => expect(api.resumeHandsFree).toHaveBeenCalledWith('p1'))
  })

  it('says when photos are waiting for the computer, and tries again on a tap', async () => {
    api.phoneProgress.mockResolvedValue({ novels: [] })
    outbox.state = { ...quiet(), unsent: 3, chapters: [onPhone({ state: 'closed' })],
      status: { state: 'offline', retryAt: Date.now() + 60000, message: '' } }
    render(<PhoneHome />)
    const line = await screen.findByText(/3 photos saved on this phone/)
    fireEvent.click(within(line.parentElement).getByRole('button', { name: 'Try now' }))
    expect(outbox.box.retryNow).toHaveBeenCalled()
  })

  it('offers Finish for a chapter left open', async () => {
    api.phoneProgress.mockResolvedValue({ novels: [] })
    outbox.state = { ...quiet(), chapters: [onPhone()] }
    render(<PhoneHome />)
    expect(await screen.findByText('Chapter 7 · 2 photos, not finished')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Finish' }))
    expect(outbox.box.closeChapter).toHaveBeenCalledWith('c1')
  })

  it('offers a way on for a chapter deleted on the computer, and for a refused photo', async () => {
    api.phoneProgress.mockResolvedValue({ novels: [] })
    outbox.state = { ...quiet(), chapters: [
      onPhone({ id: 'gone', stop: { kind: 'box-gone', message: '' } }),
      onPhone({ id: 'bad', number: 8, stop: { kind: 'refused', message: 'That image is bigger than 25 MB.', photoId: 'f2' } }),
    ] }
    render(<PhoneHome />)
    fireEvent.click(await screen.findByRole('button', { name: 'Send as a new chapter' }))
    expect(outbox.box.resendAsNew).toHaveBeenCalledWith('gone')
    expect(screen.getByText('Chapter 8, photo 2: That image is bigger than 25 MB.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Skip that photo' }))
    expect(outbox.box.skipPhoto).toHaveBeenCalledWith('f2')
  })

  it('warns when the phone isn’t keeping photos', async () => {
    api.phoneProgress.mockResolvedValue({ novels: [] })
    outbox.state = { ...quiet(), durable: false }
    render(<PhoneHome />)
    expect(await screen.findByText(/isn’t keeping photos on the phone/)).toBeTruthy()
  })

  it('leads with Capture, and shows nothing else when no chapter came from the phone', async () => {
    api.phoneProgress.mockResolvedValue({ novels: [] })
    render(<PhoneHome />)
    const lead = await screen.findByRole('region', { name: 'Chapters from the phone' })
    expect(within(lead).getByRole('link', { name: 'Capture a chapter' }).getAttribute('href')).toBe('/phone/capture')
    expect(within(lead).queryByText(/Chapter \d/)).toBeNull()
  })
})
