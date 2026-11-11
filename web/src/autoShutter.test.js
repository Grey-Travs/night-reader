// @vitest-environment node
import { describe, expect, it } from 'vitest'

import { AUTO, change, createAutoShutter, grey } from './autoShutter'

// Auto-shutter on made-up sample sequences: a page held still, a turn, the same page held
// on, a shaking hand, a hand passing over the page.

const N = 64
const page = (seed) => Uint8Array.from({ length: N }, (_, i) => ((i * 37 + seed * 101) % 200) + 20)
const nudged = (sample, by) => Uint8Array.from(sample, (v, i) => Math.max(0, Math.min(255, v + (i % 2 ? by : -by))))

/** Feed samples every AUTO.everyMs from `start`, and say when it was ready. */
function run(auto, samples, start = 0) {
  const fired = []
  samples.forEach((sample, i) => {
    const t = start + i * AUTO.everyMs
    if (auto.feed(sample, t).ready) {
      fired.push(t)
      auto.shot(sample)
    }
  })
  return fired
}

const hold = (sample, ms) => Array.from({ length: Math.floor(ms / AUTO.everyMs) + 1 }, () => sample)
const turn = (from, to) => [nudged(from, 40), nudged(to, 45), nudged(to, 30)]

describe('auto-shutter', () => {
  it('takes the first page once it has been still a little longer', () => {
    const auto = createAutoShutter()
    const fired = run(auto, hold(page(1), 1400))
    expect(fired).toEqual([])
    expect(run(createAutoShutter(), hold(page(1), 1600))).toHaveLength(1)
  })

  it('takes one photo per turn, never the same page twice', () => {
    const auto = createAutoShutter()
    const a = page(1)
    const b = page(2)
    const samples = [...hold(a, 1600), ...hold(a, 3000), ...turn(a, b), ...hold(b, 900), ...hold(b, 3000)]
    expect(run(auto, samples)).toHaveLength(2)
  })

  it('waits while the hand shakes', () => {
    const auto = createAutoShutter()
    const a = page(1)
    const shaking = Array.from({ length: 40 }, (_, i) => nudged(a, i % 2 ? 6 : -6))
    expect(run(auto, shaking)).toEqual([])
  })

  it('takes nothing when a hand passes over the page it already has', () => {
    const auto = createAutoShutter()
    const a = page(1)
    const samples = [...hold(a, 1600), ...turn(a, a), ...hold(a, 2000)]
    expect(run(auto, samples)).toHaveLength(1)
  })

  it('needs a real turn, not the camera drifting onto another page', () => {
    const auto = createAutoShutter()
    const a = page(1)
    const b = page(2)
    // From a to b in small steps: never still, never a turn.
    const drift = Array.from({ length: 30 }, (_, i) => Uint8Array.from(a, (v, k) => Math.round(v + ((b[k] - v) * (i + 1)) / 30)))
    const samples = [...hold(a, 1600), ...drift, ...hold(b, 2000)]
    expect(run(auto, samples)).toHaveLength(1)
  })

  it('shows how far through settling the view is', () => {
    const auto = createAutoShutter()
    const a = page(1)
    auto.feed(a, 0)
    expect(auto.feed(a, 750).progress).toBeCloseTo(0.5, 1)
  })

  it('needs a turn after a photo taken by hand, too', () => {
    const auto = createAutoShutter()
    const a = page(1)
    auto.feed(a, 0)
    auto.shot(a)
    expect(run(auto, hold(a, 2000), 125)).toEqual([])
  })
})

describe('the samples', () => {
  it('turn colour into grey and measure change', () => {
    const rgba = { data: Uint8ClampedArray.from([255, 255, 255, 255, 0, 0, 0, 255]), width: 2, height: 1 }
    expect(Array.from(grey(rgba))).toEqual([255, 0])
    expect(change(Uint8Array.from([0, 10]), Uint8Array.from([4, 4]))).toBe(5)
    expect(change(Uint8Array.from([0]), Uint8Array.from([0, 1]))).toBe(Infinity)
  })
})
