// Auto-shutter: take the photo by itself once a turned page has settled.
//
// The camera hands over a small grey copy of the preview several times a second. A page
// turn shows as a burst of change; the page settling shows as the change dying away. The
// shutter fires once the view has been still for a moment after a turn, provided the view
// differs from the last photo taken — so the same page is never taken twice, however long
// it is held there — and provided the camera finds it sharp (that check is the caller's).
//
// The first page needs no turn: it is taken once the view has been still a little longer
// after Auto is switched on.
//
// The numbers are a first guess, to be tuned on the phone.

export const AUTO = {
  everyMs: 125,        // how often the preview is sampled
  edge: 96,            // the sample's long edge, in pixels
  moving: 12,          // mean change between samples that counts as the page moving
  still: 3,            // below this the view is still
  stillMs: 700,        // how long it has to stay still after a turn
  firstStillMs: 1500,  // ...and before the first page
  newPage: 10,         // how different from the last photo a new page has to look
}

/** One grey value per pixel of an RGBA image. */
export function grey({ data, width, height }) {
  const out = new Uint8Array(width * height)
  for (let i = 0, p = 0; p < out.length; i += 4, p += 1) {
    out[p] = (data[i] * 77 + data[i + 1] * 150 + data[i + 2] * 29) >> 8
  }
  return out
}

/** The mean difference between two grey samples of the same size. */
export function change(a, b) {
  if (!a || !b || a.length !== b.length || !a.length) return Infinity
  let sum = 0
  for (let i = 0; i < a.length; i += 1) sum += Math.abs(a[i] - b[i])
  return sum / a.length
}

export function createAutoShutter(limits = AUTO) {
  let last = null        // the previous sample
  let lastShot = null    // the sample when the last photo was taken
  let turned = false     // movement seen since then
  let stillSince = null
  let first = true

  return {
    /** Feed a sample taken at time `t` (ms). `ready` says the photo may be taken now;
     *  `progress` (0-1) is how far through settling the view is, for the ring. */
    feed(sample, t) {
      const moved = last ? change(sample, last) : 0
      last = sample
      if (moved > limits.moving) {
        turned = true
        stillSince = null
        return { ready: false, progress: 0 }
      }
      if (moved > limits.still) {
        stillSince = null
        return { ready: false, progress: 0 }
      }
      if (!first && !turned) return { ready: false, progress: 0 }
      if (lastShot && change(sample, lastShot) < limits.newPage) return { ready: false, progress: 0 }
      if (stillSince == null) stillSince = t
      const progress = Math.min(1, (t - stillSince) / (first ? limits.firstStillMs : limits.stillMs))
      return { ready: progress >= 1, progress }
    },
    /** A photo was taken (by Auto or by hand): the next needs a turn first. */
    shot(sample = last) {
      lastShot = sample
      turned = false
      stillSince = null
      first = false
    },
    reset() {
      last = null
      lastShot = null
      turned = false
      stillSince = null
      first = true
    },
  }
}

const KEY = 'nr.phone.auto'

export function autoOn(storage = globalThis.localStorage) {
  try { return storage?.getItem(KEY) === '1' } catch { return false }
}

export function rememberAuto(on, storage = globalThis.localStorage) {
  try { storage?.setItem(KEY, on ? '1' : '0') } catch { /* only a convenience */ }
}
