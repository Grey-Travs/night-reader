import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import PhoneCapturePage from './PhoneCapturePage'
import { api } from '../api'
import { createOutbox, useOutbox } from '../outbox'
import { memoryStore } from '../outboxStore'

// "Capture a chapter", with the camera faked: the novel, the chapter number, the shutter,
// the instant check, retakes, the next chapter and back, Done, and the ways in when the
// fast camera can't be used.

const h = vi.hoisted(() => ({ box: null, camera: null }))

vi.mock('../outbox', async (actual) => {
  const real = await actual()
  return { ...real, usePhoneOutbox: () => [h.box, real.useOutbox(h.box)] }
})
vi.mock('../api', () => ({ api: { scans: vi.fn(), createImagesProject: vi.fn(), phoneProgress: vi.fn() } }))

// The camera: a fake stream, stills that are fake bitmaps, and a check sample that each
// test decides — sharp text, or a blur.
const SHARP = (() => {
  const w = 40, h2 = 40, data = new Uint8ClampedArray(w * h2 * 4)
  for (let i = 0; i < w * h2; i += 1) {
    const v = (Math.floor((i % w) / 2) + Math.floor(i / w / 3)) % 2 ? 40 : 210
    data.set([v, v, v, 255], i * 4)
  }
  return { data, width: w, height: h2 }
})()
const BLURRY = { data: new Uint8ClampedArray(40 * 40 * 4).fill(150), width: 40, height: 40 }

vi.mock('../camera', () => ({
  openCamera: vi.fn(async () => h.camera.cam),
  captureStill: vi.fn(async () => h.camera.still()),
  sample: vi.fn(() => h.camera.sample),
  encodePage: vi.fn(async () => ({ blob: new Blob(['jpeg'], { type: 'image/jpeg' }), width: 10, height: 10 })),
  encodePicked: vi.fn(async (file) => ({ blob: file })),
  wakeLock: () => h.camera.lock,
}))

import * as camera from '../camera'

const never = () => new Promise(() => {})
const quietApi = {
  createPageBatch: never, uploadPage: never, replacePage: never, deletePages: never,
  markCaptured: never, deletePageBatch: never,
}

let where = null
function Where() {
  where = useLocation()
  return null
}

function renderAt(path) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Where />
      <Routes>
        <Route path="/phone/capture" element={<PhoneCapturePage />} />
        <Route path="/phone" element={<div>The hub</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(async () => {
  localStorage.clear()
  globalThis.isSecureContext = true
  HTMLMediaElement.prototype.play = vi.fn(async () => {})
  h.camera = {
    cam: { stream: {}, canTorch: false, hasStills: true, stop: vi.fn(), setTorch: vi.fn(async () => {}) },
    still: () => ({ width: 3000, height: 4000, close: vi.fn() }),
    sample: SHARP,
    lock: { acquire: vi.fn(async () => {}), release: vi.fn(async () => {}) },
  }
  camera.openCamera.mockClear()
  h.box = createOutbox({ store: memoryStore(), api: quietApi, locks: null })
  await h.box.resume()
  api.scans.mockResolvedValue({ novels: [{ id: 'p1', name: '달빛 소설', next_number: 5, chapter_count: 4 }] })
  api.phoneProgress.mockReset()
  api.phoneProgress.mockResolvedValue({ novels: [] })
})

// A computer that answers: boxes and photos land, so the camera can follow each photo.
function answeringApi() {
  let pages = 0
  return {
    ...quietApi,
    createPageBatch: vi.fn(async () => ({ created: true, batch: { id: 'b1', number: 5 } })),
    uploadPage: vi.fn(async () => { pages += 1; return { duplicate: false, page: { id: `pg${pages}` }, batch: 'b1', number: 5 } }),
  }
}
afterEach(() => { delete globalThis.isSecureContext })

const chapters = () => h.box.getSnapshot().chapters
const shutter = () => screen.getByRole('button', { name: /^(Take page|Retake photo)/ })

async function startShooting() {
  renderAt('/phone/capture?pid=p1')
  fireEvent.click(await screen.findByRole('button', { name: 'Capture Chapter 5' }))
  await screen.findByRole('dialog', { name: 'Camera, chapter 5' })
  await waitFor(() => expect(shutter()).toBeEnabled())
}

async function shoot() {
  const before = chapters()[0]?.photos.length ?? 0
  fireEvent.click(shutter())
  await waitFor(() => expect(chapters().find((c) => c.state === 'open')?.photos.length ?? 0).not.toBe(before))
}

describe('choosing the novel', () => {
  it('lists the novels with their next chapter, and goes on to the one chosen', async () => {
    renderAt('/phone/capture')
    fireEvent.click(await screen.findByRole('button', { name: /달빛 소설/ }))
    expect(where.search).toBe('?pid=p1')
    expect(await screen.findByRole('button', { name: 'Capture Chapter 5' })).toBeTruthy()
  })

  it('shows the novels as last seen when the computer can’t be reached', async () => {
    await h.box.cacheNovels([{ id: 'p1', name: 'Saved novel', next_number: 2 }])
    api.scans.mockRejectedValue(new Error('offline'))
    renderAt('/phone/capture')
    expect(await screen.findByText(/as last seen/)).toBeTruthy()
    expect(screen.getByRole('button', { name: /Saved novel/ })).toBeTruthy()
  })
})

describe('the camera', () => {
  it('saves each shot in order, into a new chapter shown with its number', async () => {
    await startShooting()
    await shoot()
    await shoot()
    await shoot()
    const [ch] = chapters()
    expect(ch).toMatchObject({ pid: 'p1', shown: 5, expect: 5, state: 'open' })
    expect(ch.photos.map((p) => p.order)).toEqual([1, 2, 3])
    expect(screen.getByText('Chapter 5 · 3 pages')).toBeTruthy()
  })

  it('holds the shutter while a shot is being taken', async () => {
    await startShooting()
    let finish
    h.camera.still = () => new Promise((done) => { finish = done })
    fireEvent.click(shutter())
    await waitFor(() => expect(shutter()).toBeDisabled())
    await act(async () => finish({ width: 3, height: 4, close: () => {} }))
    await waitFor(() => expect(shutter()).toBeEnabled())
  })

  it('flags a blurry shot at once, and Retake puts the new one in its place', async () => {
    await startShooting()
    h.camera.sample = BLURRY
    await shoot()
    const banner = await screen.findByRole('alert')
    expect(banner.textContent).toMatch(/Photo 1 looks blurry/)
    fireEvent.click(within(banner).getByRole('button', { name: 'Retake' }))
    expect(screen.getByRole('status').textContent).toMatch(/Retaking photo 1/)
    h.camera.sample = SHARP
    fireEvent.click(screen.getByRole('button', { name: 'Retake photo 1' }))
    await waitFor(() => expect(chapters()[0].photos[0].version).toBe(2))
    expect(chapters()[0].photos).toHaveLength(1)
    expect(chapters()[0].photos[0].check.flags).toEqual([])
  })

  it('goes on to the next chapter with the camera still open, and can go back', async () => {
    await startShooting()
    await shoot()
    fireEvent.click(screen.getByRole('button', { name: 'Chapter 6 →' }))
    await screen.findByRole('dialog', { name: 'Camera, chapter 6' })
    expect(camera.openCamera).toHaveBeenCalledTimes(1)
    const [five, six] = chapters()
    expect(five).toMatchObject({ state: 'closed', held: true })
    expect(six).toMatchObject({ state: 'open', shown: 6 })

    fireEvent.click(screen.getByRole('button', { name: '← Back to Chapter 5' }))
    await screen.findByRole('dialog', { name: 'Camera, chapter 5' })
    await waitFor(() => expect(chapters()).toHaveLength(1))
    expect(chapters()[0]).toMatchObject({ state: 'open', held: false })
  })

  it('lets go of the earlier chapter once the next one has its first page', async () => {
    await startShooting()
    await shoot()
    fireEvent.click(screen.getByRole('button', { name: 'Chapter 6 →' }))
    await screen.findByRole('dialog', { name: 'Camera, chapter 6' })
    await shoot()
    await waitFor(() => expect(chapters()[0].held).toBe(false))
    expect(screen.queryByRole('button', { name: '← Back to Chapter 5' })).toBeNull()
  })

  it('asks before finishing while photos are still on their way, then finishes', async () => {
    await startShooting()
    await shoot()
    fireEvent.click(screen.getByRole('button', { name: 'Done' }))
    const sheet = screen.getByRole('dialog', { name: 'Before you go' })
    expect(within(sheet).getByText(/1 still sending/)).toBeTruthy()
    fireEvent.click(within(sheet).getByRole('button', { name: 'Finish' }))
    await screen.findByText('The hub')
    await waitFor(() => expect(chapters()[0].state).toBe('closed'))
    expect(chapters()[0].held).toBe(false)
  })

  it('follows each photo on the computer: read, double-checking, and what was found', async () => {
    h.box = createOutbox({ store: memoryStore(), api: answeringApi(), locks: null })
    await h.box.resume()
    api.phoneProgress.mockResolvedValue({ novels: [{ pid: 'p1', name: '달빛 소설', chapters: [], photos: [
      { id: 'pg1', n: 1, step: 'read', note: '', look: null, looking: false },
      { id: 'pg2', n: 2, step: 'double-checking', note: '', look: null, looking: false },
    ] }] })
    await startShooting()
    await shoot()
    await shoot()
    await waitFor(() => expect(chapters()[0].photos.every((p) => p.state === 'sent')).toBe(true))
    const second = await screen.findByRole('button', { name: /Photo 2: Photo 2: Claude isn’t sure — double-checking…/ })
    expect(within(second).getByText('Checking…')).toBeTruthy()
    expect(within(screen.getByRole('button', { name: /^Photo 1:/ })).getByText('✓')).toBeTruthy()
    expect(api.phoneProgress).toHaveBeenCalledWith({ pid: 'p1', box: 'b1' })

    fireEvent.click(screen.getByRole('button', { name: '1 flagged' }))
    const list = screen.getByRole('dialog', { name: 'Flagged photos' })
    fireEvent.click(within(list).getByRole('button', { name: 'Retake' }))
    expect(screen.getByRole('status').textContent).toMatch(/Retaking photo 2/)
  })

  it('lists what still needs a look before finishing, each with Retake', async () => {
    h.box = createOutbox({ store: memoryStore(), api: answeringApi(), locks: null })
    await h.box.resume()
    api.phoneProgress.mockResolvedValue({ novels: [{ pid: 'p1', name: '달빛 소설', chapters: [], photos: [
      { id: 'pg1', n: 1, step: 'needs-you', note: 'Some words were read wrong.', look: null, looking: false },
    ] }] })
    await startShooting()
    await shoot()
    await waitFor(() => expect(chapters()[0].photos[0].state).toBe('sent'))
    await screen.findByRole('button', { name: '1 flagged' })
    fireEvent.click(screen.getByRole('button', { name: 'Done' }))
    const sheet = screen.getByRole('dialog', { name: 'Before you go' })
    expect(within(sheet).getByText('Photo 1: Some words were read wrong.')).toBeTruthy()
    expect(within(sheet).getByText(/0 of 1 read/)).toBeTruthy()
    fireEvent.click(within(sheet).getByRole('button', { name: 'Finish anyway' }))
    await screen.findByText('The hub')
  })

  it('finishes at once when every photo is read', async () => {
    h.box = createOutbox({ store: memoryStore(), api: answeringApi(), locks: null })
    await h.box.resume()
    api.phoneProgress.mockResolvedValue({ novels: [{ pid: 'p1', name: '달빛 소설', chapters: [], photos: [
      { id: 'pg1', n: 1, step: 'read', note: '', look: null, looking: false },
    ] }] })
    await startShooting()
    await shoot()
    await waitFor(() => expect(chapters()[0].photos[0].state).toBe('sent'))
    await waitFor(() => expect(within(screen.getByRole('button', { name: /^Photo 1:/ })).getByText('✓')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Done' }))
    await screen.findByText('The hub')
  })

  it('forgets a chapter finished with no pages', async () => {
    await startShooting()
    fireEvent.click(screen.getByRole('button', { name: 'Done' }))
    await screen.findByText('The hub')
    expect(chapters()).toEqual([])
  })

  it('lets go of the camera and the screen when it closes', async () => {
    await startShooting()
    fireEvent.click(screen.getByRole('button', { name: 'Close the camera' }))
    await screen.findByText('The hub')
    expect(h.camera.cam.stop).toHaveBeenCalled()
    expect(h.camera.lock.release).toHaveBeenCalled()
  })

  it('asks before leaving a chapter with pages, and keeps it open if asked to', async () => {
    await startShooting()
    await shoot()
    fireEvent.click(screen.getByRole('button', { name: 'Close the camera' }))
    const ask = screen.getByRole('dialog', { name: 'Leave the camera' })
    fireEvent.click(within(ask).getByRole('button', { name: 'Keep it open' }))
    await screen.findByText('The hub')
    expect(chapters()[0].state).toBe('open')
  })
})

describe('Auto', () => {
  beforeEach(() => localStorage.clear())

  it('takes the first page by itself once the view is still, and no second without a turn', async () => {
    await startShooting()
    fireEvent.click(screen.getByRole('button', { name: 'Auto' }))
    expect(screen.getByRole('button', { name: 'Auto on' }).getAttribute('aria-pressed')).toBe('true')
    expect(localStorage.getItem('nr.phone.auto')).toBe('1')
    await waitFor(() => expect(chapters()[0].photos).toHaveLength(1), { timeout: 4000 })
    await new Promise((done) => setTimeout(done, 1200))
    expect(chapters()[0].photos).toHaveLength(1)
  })

  it('waits for a sharp view', async () => {
    await startShooting()
    h.camera.sample = BLURRY
    fireEvent.click(screen.getByRole('button', { name: 'Auto' }))
    await new Promise((done) => setTimeout(done, 2200))
    expect(chapters()[0].photos).toHaveLength(0)
  })

  it('pauses on a tap of the picture', async () => {
    await startShooting()
    fireEvent.click(screen.getByRole('button', { name: 'Auto' }))
    fireEvent.click(screen.getByLabelText('Tap to pause Auto'))
    expect(screen.getByRole('button', { name: 'Auto paused' })).toBeTruthy()
    await new Promise((done) => setTimeout(done, 2000))
    expect(chapters()[0].photos).toHaveLength(0)
  })
})

describe('when the fast camera can’t be used', () => {
  it('uses the phone’s camera app over plain http, and says how to get the fast one', async () => {
    globalThis.isSecureContext = false
    renderAt('/phone/capture?pid=p1')
    fireEvent.click(await screen.findByRole('button', { name: 'Capture Chapter 5' }))
    await screen.findByText(/open Night Reader from its home-screen icon/)
    const input = document.querySelector('input[capture="environment"]')
    expect(input).toBeTruthy()
    expect(camera.openCamera).not.toHaveBeenCalled()
  })

  it('says how to allow the camera when it was refused', async () => {
    camera.openCamera.mockRejectedValueOnce(Object.assign(new Error('no'), { code: 'denied' }))
    renderAt('/phone/capture?pid=p1')
    fireEvent.click(await screen.findByRole('button', { name: 'Capture Chapter 5' }))
    expect(await screen.findByText(/Site settings → Camera/)).toBeTruthy()
  })
})
