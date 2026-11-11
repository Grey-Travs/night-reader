// @vitest-environment node
import { describe, expect, it } from 'vitest'

import { LIMITS, flagLine, judge, measure } from './photoCheck'

// The instant check, on drawn test images: crisp text-like stripes, the same stripes
// smeared, a page in the dark, and a page with a patch of glare.

function image(width, height, pixel) {
  const data = new Uint8ClampedArray(width * height * 4)
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const v = pixel(x, y)
      const i = (y * width + x) * 4
      data[i] = v; data[i + 1] = v; data[i + 2] = v; data[i + 3] = 255
    }
  }
  return { data, width, height }
}

const W = 160
const H = 200
const crisp = image(W, H, (x, y) => ((Math.floor(x / 2) + Math.floor(y / 3)) % 2 ? 40 : 210))
// The same pattern seen through a soft lens: barely any jump from pixel to pixel.
const smeared = image(W, H, (x, y) => 125 + 8 * Math.sin(x / 9) * Math.cos(y / 11))
const dark = image(W, H, (x, y) => ((Math.floor(x / 2) + Math.floor(y / 3)) % 2 ? 10 : 50))
const glare = image(W, H, (x, y) => (x > W / 2 && y > H / 2 ? 255
  : ((Math.floor(x / 2) + Math.floor(y / 3)) % 2 ? 40 : 210)))

describe('measuring', () => {
  it('scores crisp text high and a soft photo low', () => {
    expect(measure(crisp).sharpness).toBeGreaterThan(1000)
    expect(measure(smeared).sharpness).toBeLessThan(LIMITS.sharpFloor)
  })

  it('reads brightness and blown-out pixels', () => {
    expect(measure(dark).brightness).toBeLessThan(LIMITS.dark)
    expect(measure(crisp).brightness).toBeGreaterThan(100)
    expect(measure(glare).glare).toBeGreaterThan(0.2)
    expect(measure(crisp).glare).toBe(0)
  })
})

describe('judging', () => {
  it('flags what is wrong, and nothing on a good page', () => {
    expect(judge(measure(crisp))).toEqual([])
    expect(judge(measure(smeared))).toEqual(['blurry'])
    expect(judge(measure(dark))).toEqual(['dark'])
    expect(judge(measure(glare))).toEqual(['glare'])
  })

  it('compares a photo with the chapter’s earlier ones once there are enough', () => {
    const m = { sharpness: 400, brightness: 180, glare: 0 }
    expect(judge(m, [1000, 1100])).toEqual([], 'two photos are not yet a baseline')
    expect(judge(m, [1000, 1100, 900])).toEqual(['blurry'])
    expect(judge({ ...m, sharpness: 700 }, [1000, 1100, 900])).toEqual([])
  })

  it('says it in one line', () => {
    expect(flagLine(['blurry', 'dark'], 7)).toBe('Photo 7 looks blurry')
    expect(flagLine(['dark'], 2)).toBe('Photo 2 looks too dark')
    expect(flagLine(['glare'], 3)).toBe('Photo 3 has glare on it')
    expect(flagLine([], 3)).toBe('')
  })
})
