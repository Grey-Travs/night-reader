import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, vi } from 'vitest'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

// jsdom implements neither of these, and both are called during a normal render of
// the reader (scroll restore) and the rewrite handle (nearest-paragraph search).
// Without them a component throws for a reason that has nothing to do with the code
// under test.
if (!window.matchMedia) {
  window.matchMedia = (query) => ({
    matches: false,
    media: query,
    addEventListener() {},
    removeEventListener() {},
    addListener() {},
    removeListener() {},
    dispatchEvent() { return false },
  })
}

if (!global.requestAnimationFrame) {
  global.requestAnimationFrame = (cb) => setTimeout(() => cb(0), 0)
  global.cancelAnimationFrame = (id) => clearTimeout(id)
}

// The Pages tab scrolls a chapter box or a photo back into view after a change, and to
// the top when a photo opens. jsdom lacks the first and only logs "not implemented"
// for the second, so either would fail or litter a test that is about something else.
if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = () => {}
window.scrollTo = () => {}

if (!window.EventSource) {
  // ProjectLayout opens one on mount. Tests that don't exercise streaming just need
  // it to exist and stay quiet.
  window.EventSource = class {
    constructor() { this.readyState = 0 }
    close() { this.readyState = 2 }
    addEventListener() {}
    removeEventListener() {}
  }
}
