// @vitest-environment node
import { describe, expect, it, vi } from 'vitest'

import {
  JPEG_QUALITY, STILL_MAX_WIDTH, captureStill, encodePage, encodePicked, fitWithin, framesOnly,
  openCamera, wakeLock,
} from './camera'

// The camera, with every browser object faked: what is asked of the phone, and what a page
// photo comes out as.

describe('the size a page photo is encoded at', () => {
  it('fits what Claude reads at full detail', () => {
    expect(fitWithin(4000, 3000)).toEqual({ width: 2236, height: 1677 })
    expect(fitWithin(3000, 4000)).toEqual({ width: 1677, height: 2236 })
    expect(fitWithin(1080, 1920)).toEqual({ width: 1080, height: 1920 })   // already small enough
    expect(fitWithin(9000, 1000)).toEqual({ width: 2576, height: 286 })    // the long edge decides
  })
})

function fakeTrack({ torch = false } = {}) {
  return {
    stopped: false,
    applied: [],
    getCapabilities: () => (torch ? { torch: true } : {}),
    applyConstraints: vi.fn(async function apply(c) { this.applied.push(c) }),
    stop() { this.stopped = true },
  }
}

function fakeMedia(track, error = null) {
  return {
    asked: null,
    getUserMedia: vi.fn(async function ask(c) {
      this.asked = c
      if (error) throw error
      return { getVideoTracks: () => [track], getTracks: () => [track] }
    }),
  }
}

class FakeCapture {
  constructor(track) { this.track = track; this.taken = [] }
  async getPhotoCapabilities() { return { imageWidth: { max: 8000 }, fillLightMode: ['auto', 'off', 'flash'] } }
  async takePhoto(settings) { this.taken.push(settings); FakeCapture.last = this; return new Blob(['jpeg']) }
}

describe('opening the camera', () => {
  it('asks for the back camera, with sensor stills when the phone has them', async () => {
    const track = fakeTrack()
    const media = fakeMedia(track)
    const cam = await openCamera({ media, ImageCaptureCtor: FakeCapture })
    expect(media.asked.video.facingMode).toEqual({ ideal: 'environment' })
    expect(media.asked.video.width).toEqual({ ideal: 1920 })
    expect(cam.hasStills).toBe(true)
    await cam.stillBlob()
    expect(FakeCapture.last.taken[0]).toEqual({ imageWidth: STILL_MAX_WIDTH, fillLightMode: 'off' })
    cam.stop()
    expect(track.stopped).toBe(true)
  })

  it('asks for a big preview when stills come from it', async () => {
    const media = fakeMedia(fakeTrack())
    const cam = await openCamera({ media, ImageCaptureCtor: null })
    expect(media.asked.video.width).toEqual({ ideal: 4096 })
    expect(cam.hasStills).toBe(false)
  })

  it('offers the torch only when the camera has one, and keeps it on after a still', async () => {
    const plain = await openCamera({ media: fakeMedia(fakeTrack()), ImageCaptureCtor: FakeCapture })
    expect(plain.canTorch).toBe(false)
    const track = fakeTrack({ torch: true })
    const cam = await openCamera({ media: fakeMedia(track), ImageCaptureCtor: FakeCapture })
    expect(cam.canTorch).toBe(true)
    await cam.setTorch(true)
    await cam.stillBlob()
    expect(track.applied).toEqual([{ advanced: [{ torch: true }] }, { advanced: [{ torch: true }] }])
  })

  it('says why it couldn’t', async () => {
    await expect(openCamera({ media: null })).rejects.toMatchObject({ code: 'unavailable' })
    const denied = Object.assign(new Error('no'), { name: 'NotAllowedError' })
    await expect(openCamera({ media: fakeMedia(fakeTrack(), denied) })).rejects.toMatchObject({ code: 'denied' })
  })
})

function storage() {
  const items = new Map()
  return { getItem: (k) => items.get(k) ?? null, setItem: (k, v) => items.set(k, String(v)) }
}

describe('taking a page', () => {
  const portraitVideo = { videoWidth: 1080, videoHeight: 1920 }
  // A frame copied off the preview at the moment of the shutter.
  const frames = () => {
    const made = []
    const canvas = (width, height) => {
      const c = { width, height, getContext: () => ({ drawImage: (src) => { c.from = src } }) }
      made.push(c)
      return c
    }
    return { canvas, made }
  }

  it('uses the sensor still when it comes out upright', async () => {
    const still = { width: 3000, height: 4000, close: vi.fn() }
    const cam = { hasStills: true, stillBlob: vi.fn(async () => new Blob(['x'])) }
    const decode = vi.fn(async () => still)
    const got = await captureStill(cam, portraitVideo, { decode, storage: storage() })
    expect(got).toBe(still)
    expect(decode).toHaveBeenCalledWith(expect.any(Blob), { imageOrientation: 'from-image' })
  })

  it('switches to frames for good when stills come out on their side', async () => {
    const place = storage()
    const { canvas, made } = frames()
    const sideways = { width: 4000, height: 3000, close: vi.fn() }
    const cam = { hasStills: true, stillBlob: vi.fn(async () => new Blob(['x'])) }
    const got = await captureStill(cam, portraitVideo, { decode: async () => sideways, storage: place, canvas })
    expect(got).toBe(made[0])
    expect(made[0]).toMatchObject({ width: 1080, height: 1920, from: portraitVideo })
    expect(sideways.close).toHaveBeenCalled()
    expect(framesOnly(place)).toBe(true)
    cam.stillBlob.mockClear()
    await captureStill(cam, portraitVideo, { decode: async () => sideways, storage: place, canvas })
    expect(cam.stillBlob).not.toHaveBeenCalled()
  })

  it('falls back to a frame of the preview when a still fails', async () => {
    const { canvas, made } = frames()
    const cam = { hasStills: true, stillBlob: vi.fn(async () => { throw new Error('busy') }) }
    expect(await captureStill(cam, portraitVideo, { storage: storage(), canvas })).toBe(made[0])
  })
})

function fakeCanvas() {
  const made = []
  const canvas = (width, height) => {
    const c = {
      width, height, drawn: null,
      getContext: () => ({ drawImage: (src, x, y, w, h) => { c.drawn = [w, h] } }),
      convertToBlob: async ({ type, quality }) => Object.assign(new Blob(['jpeg'], { type }), { quality }),
    }
    made.push(c)
    return c
  }
  return { canvas, made }
}

describe('encoding', () => {
  it('draws the page at the size Claude reads, as a JPEG at 0.9', async () => {
    const { canvas, made } = fakeCanvas()
    const out = await encodePage({ width: 4000, height: 3000 }, { canvas })
    expect(made[0].drawn).toEqual([2236, 1677])
    expect(out).toMatchObject({ width: 2236, height: 1677 })
    expect(out.blob.type).toBe('image/jpeg')
    expect(out.blob.quality).toBe(JPEG_QUALITY)
  })

  it('sends a picked file as it is when the phone can’t decode it', async () => {
    const file = new Blob(['?'], { type: 'image/webp' })
    const out = await encodePicked(file, { decode: async () => { throw new Error('no') } })
    expect(out.blob).toBe(file)
  })
})

describe('keeping the screen on', () => {
  it('takes and lets go of the wake lock', async () => {
    const sentinel = { release: vi.fn(async () => {}) }
    const nav = { wakeLock: { request: vi.fn(async () => sentinel) } }
    const lock = wakeLock(nav)
    await lock.acquire()
    expect(nav.wakeLock.request).toHaveBeenCalledWith('screen')
    await lock.release()
    expect(sentinel.release).toHaveBeenCalled()
    await wakeLock({}).acquire()   // a phone without one is fine
  })
})
