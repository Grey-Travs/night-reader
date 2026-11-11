// The phone's camera, for photographing book pages. Every browser object comes in as a
// parameter, so the tests run without a camera, a canvas or a phone.
//
// What a page photo is turned into: at most 2576 px on the long edge and 3.75 megapixels,
// JPEG at quality 0.9. That is what Claude's vision reads at full detail; anything bigger
// it shrinks itself, so the extra pixels would only cost mobile data, phone storage and
// disk. A 12 MP page comes out at 1677 × 2236, about 1 MB. It is encoded once, so a resend
// is the same bytes — which is what lets the computer recognise it.

export const MAX_EDGE = 2576
export const MAX_PIXELS = 3_750_000
export const JPEG_QUALITY = 0.9
// Stills at ~12 MP: a 50 or 108 MP mode is slow, and the encoder throws the rest away.
export const STILL_MAX_WIDTH = 4032
const FRAMES_KEY = 'nr.phone.frames'

/** The size an image of w × h is encoded at. */
export function fitWithin(w, h, { maxEdge = MAX_EDGE, maxPixels = MAX_PIXELS } = {}) {
  const s = Math.min(1, maxEdge / Math.max(w, h), Math.sqrt(maxPixels / (w * h)))
  return { width: Math.max(1, Math.round(w * s)), height: Math.max(1, Math.round(h * s)) }
}

export function makeCanvas(width, height) {
  if (typeof OffscreenCanvas === 'function') return new OffscreenCanvas(width, height)
  const canvas = document.createElement('canvas')
  canvas.width = width
  canvas.height = height
  return canvas
}

function toBlob(canvas, type, quality) {
  if (canvas.convertToBlob) return canvas.convertToBlob({ type, quality })
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error('The photo couldn’t be saved.'))), type, quality)
  })
}

const sizeOf = (source) => ({
  width: source.videoWidth || source.naturalWidth || source.width,
  height: source.videoHeight || source.naturalHeight || source.height,
})

/** Encode an image, bitmap, canvas or video frame as a page photo. */
export async function encodePage(source, { canvas = makeCanvas, quality = JPEG_QUALITY, ...limits } = {}) {
  const from = sizeOf(source)
  const { width, height } = fitWithin(from.width, from.height, limits)
  const target = canvas(width, height)
  const ctx = target.getContext('2d')
  ctx.imageSmoothingQuality = 'high'
  ctx.drawImage(source, 0, 0, width, height)
  return { blob: await toBlob(target, 'image/jpeg', quality), width, height }
}

/** A small copy for the instant check: its pixels, at most `edge` on the long side. */
export function sample(source, { edge = 640, canvas = makeCanvas } = {}) {
  const from = sizeOf(source)
  const s = Math.min(1, edge / Math.max(from.width, from.height))
  const width = Math.max(1, Math.round(from.width * s))
  const height = Math.max(1, Math.round(from.height * s))
  const target = canvas(width, height)
  const ctx = target.getContext('2d', { willReadFrequently: true })
  ctx.drawImage(source, 0, 0, width, height)
  return ctx.getImageData(0, 0, width, height)
}

/**
 * Opens the back camera. With ImageCapture, stills come from the sensor at up to ~12 MP;
 * without it they are frames grabbed from the preview, so the preview is asked for big.
 */
export async function openCamera({ media = globalThis.navigator?.mediaDevices,
  ImageCaptureCtor = globalThis.ImageCapture, deviceId = null } = {}) {
  if (!media?.getUserMedia) {
    throw Object.assign(new Error('The camera can’t be opened here.'), { code: 'unavailable' })
  }
  const where = deviceId ? { deviceId: { exact: deviceId } } : { facingMode: { ideal: 'environment' } }
  const size = ImageCaptureCtor
    ? { width: { ideal: 1920 }, height: { ideal: 1080 } }
    : { width: { ideal: 4096 }, height: { ideal: 3072 } }
  let stream
  try {
    stream = await media.getUserMedia({ audio: false, video: { ...where, ...size } })
  } catch (e) {
    const denied = e?.name === 'NotAllowedError' || e?.name === 'SecurityError'
    throw Object.assign(new Error(denied ? 'The camera wasn’t allowed.' : 'The camera couldn’t be opened.'),
      { code: denied ? 'denied' : 'unavailable', cause: e })
  }
  const track = stream.getVideoTracks()[0]
  const caps = track?.getCapabilities?.() || {}
  const canTorch = !!caps.torch
  const capture = ImageCaptureCtor && track ? new ImageCaptureCtor(track) : null
  let torch = false

  async function setTorch(on) {
    if (!canTorch) return
    torch = !!on
    await track.applyConstraints({ advanced: [{ torch: torch }] })
  }

  async function stillBlob() {
    const settings = {}
    try {
      const pc = await capture.getPhotoCapabilities()
      if (pc?.imageWidth?.max) settings.imageWidth = Math.min(pc.imageWidth.max, STILL_MAX_WIDTH)
      // The flash glares on paper; the torch, if on, is the person's own choice.
      if (pc?.fillLightMode?.includes?.('off')) settings.fillLightMode = 'off'
    } catch { /* take it with what the camera chooses */ }
    const blob = await capture.takePhoto(settings)
    // Some phones turn the torch off for a still.
    if (torch) await setTorch(true).catch(() => {})
    return blob
  }

  return {
    stream, track, canTorch, setTorch, hasStills: !!capture,
    stillBlob: capture ? stillBlob : null,
    stop() { for (const t of stream.getTracks()) t.stop() },
  }
}

export function framesOnly(storage = globalThis.localStorage) {
  try { return storage?.getItem(FRAMES_KEY) === '1' } catch { return false }
}

function rememberFramesOnly(storage = globalThis.localStorage) {
  try { storage?.setItem(FRAMES_KEY, '1') } catch { /* it just asks again next time */ }
}

/** The preview's current frame, copied now: drawn later, it would be a later frame. */
export function snapshot(video, { canvas = makeCanvas } = {}) {
  const { width, height } = sizeOf(video)
  const target = canvas(width, height)
  target.getContext('2d').drawImage(video, 0, 0, width, height)
  return target
}

/**
 * One upright page from the camera. A sensor still when the camera gives them; a frame
 * from the preview otherwise. A phone whose stills come out on their side — a landscape
 * still behind a portrait preview — is remembered, and gets frames from then on.
 */
export async function captureStill(cam, video, { decode = globalThis.createImageBitmap,
  storage = globalThis.localStorage, canvas = makeCanvas } = {}) {
  if (cam.hasStills && !framesOnly(storage)) {
    try {
      const blob = await cam.stillBlob()
      const image = await decode(blob, { imageOrientation: 'from-image' })
      const portrait = video.videoHeight > video.videoWidth
      if (!(portrait && image.width > image.height)) return image
      image.close?.()
      rememberFramesOnly(storage)
    } catch { /* fall back to the preview */ }
  }
  return snapshot(video, { canvas })
}

/** Keeps the screen on while the camera is open. */
export function wakeLock(nav = globalThis.navigator) {
  let sentinel = null
  return {
    async acquire() {
      try { sentinel = await nav?.wakeLock?.request?.('screen') } catch { sentinel = null }
    },
    async release() {
      try { await sentinel?.release?.() } catch { /* already gone */ }
      sentinel = null
    },
  }
}

/** A photo picked from the gallery, upright and the size a page photo is. */
export async function encodePicked(file, { decode = globalThis.createImageBitmap, ...options } = {}) {
  let image
  try {
    image = await decode(file, { imageOrientation: 'from-image' })
  } catch {
    return { blob: file, width: null, height: null }   // sent as it is; the computer decides
  }
  try {
    return await encodePage(image, options)
  } finally {
    image.close?.()
  }
}
