// The instant check: is a page photo blurry, too dark or washed out by glare? Run on the
// phone the moment it is taken, on a small copy, in a few milliseconds, so the person can
// take it again while the book is still open at that page.
//
// Sharpness is the variance of the Laplacian — how much the brightness jumps from one
// pixel to the next. Printed text is full of edges, so a sharp page scores high and a
// blurred one low. What "low" means depends on the book, the light and the phone, so a
// photo is compared with the earlier photos of the same chapter as well as with a floor.
//
// These limits are a first guess, to be tuned on real photos from the phone.

export const LIMITS = {
  sharpFloor: 25,     // below this a page is blurry whatever came before
  relative: 0.45,     // below this share of the chapter's typical sharpness, it is blurry
  baseline: 3,        // photos needed before the chapter's own sharpness counts
  dark: 60,           // mean brightness, 0-255
  glare: 0.08,        // share of blown-out pixels
}

export const FLAG_TEXT = {
  blurry: 'Blurry?',
  dark: 'Too dark?',
  glare: 'Glare?',
}

/** Sharpness, brightness and glare of an RGBA image (an ImageData, or its fields). */
export function measure({ data, width, height }) {
  const luma = new Float32Array(width * height)
  let sum = 0
  let blown = 0
  for (let i = 0, p = 0; p < luma.length; i += 4, p += 1) {
    const y = 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2]
    luma[p] = y
    sum += y
    if (y >= 250) blown += 1
  }
  // The Laplacian over the middle of the frame: the edges are table, fingers and shadow.
  const x0 = Math.max(1, Math.floor(width * 0.1))
  const x1 = Math.min(width - 1, Math.ceil(width * 0.9))
  const y0 = Math.max(1, Math.floor(height * 0.1))
  const y1 = Math.min(height - 1, Math.ceil(height * 0.9))
  let n = 0
  let mean = 0
  let m2 = 0
  for (let y = y0; y < y1; y += 1) {
    for (let x = x0; x < x1; x += 1) {
      const at = y * width + x
      const lap = luma[at - 1] + luma[at + 1] + luma[at - width] + luma[at + width] - 4 * luma[at]
      n += 1
      const delta = lap - mean
      mean += delta / n
      m2 += delta * (lap - mean)
    }
  }
  return {
    sharpness: n > 1 ? m2 / (n - 1) : 0,
    brightness: luma.length ? sum / luma.length : 0,
    glare: luma.length ? blown / luma.length : 0,
  }
}

const median = (values) => {
  const sorted = [...values].sort((a, b) => a - b)
  const mid = Math.floor(sorted.length / 2)
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2
}

/**
 * What is wrong with a photo, if anything: a list of 'blurry', 'dark' and 'glare'.
 * `earlier` is the sharpness of the chapter's photos so far.
 */
export function judge(m, earlier = [], limits = LIMITS) {
  const flags = []
  const typical = earlier.length >= limits.baseline ? median(earlier) : null
  if (m.sharpness < limits.sharpFloor || (typical != null && m.sharpness < typical * limits.relative)) {
    flags.push('blurry')
  }
  if (m.brightness < limits.dark) flags.push('dark')
  if (m.glare > limits.glare) flags.push('glare')
  return flags
}

/** One line for a flagged photo, as the camera says it. */
export function flagLine(flags, n) {
  if (!flags?.length) return ''
  const what = flags.includes('blurry') ? 'looks blurry'
    : flags.includes('dark') ? 'looks too dark' : 'has glare on it'
  return `Photo ${n} ${what}`
}
