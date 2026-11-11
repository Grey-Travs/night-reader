import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { FAST_MS, SLOW_MS, isMoving, pagesNeedingYou, stepLabel, usePhoneProgress } from './phoneProgress'
import { api } from './api'

vi.mock('./api', () => ({ api: { phoneProgress: vi.fn() } }))

const row = (over = {}) => ({
  batch: 'b1', number: 12, step: 'capturing', photos: 3, message: '', needs_you: [],
  pages: { read: 0, reading: 0, unread: 0, to_check: 0, failed: 0, skipped: 0 }, ...over,
})
const novel = (rows, over = {}) => ({ pid: 'p1', name: 'Novel', stopped: false, waiting: null, gave_up: null, chapters: rows, ...over })

describe('what the phone says about a chapter', () => {
  it('names each step plainly', () => {
    expect(stepLabel(row())).toBe('Still being photographed · 3 photos')
    expect(stepLabel(row({ step: 'reading', photos: 10, pages: { unread: 3, reading: 1 } }))).toBe('Reading · 6 of 10 done')
    expect(stepLabel(row({ step: 'checking', pages: { to_check: 2 } }))).toBe('Claude isn’t sure of 2 pages — double-checking…')
    expect(stepLabel(row({ step: 'needs-you', message: 'Photo 4 needs checking.' }))).toBe('Photo 4 needs checking.')
    expect(stepLabel(row({ step: 'translated' }))).toBe('Translated')
  })

  it('collects the pages that need a person, with where they are', () => {
    const found = pagesNeedingYou([novel([row({ needs_you: [{ page_id: 'pg1', photo: 4, kind: 'to-check' }] })])])
    expect(found).toEqual([{ page_id: 'pg1', photo: 4, kind: 'to-check', pid: 'p1', novel: 'Novel', number: 12 }])
  })

  it('knows when something is moving', () => {
    expect(isMoving([novel([row({ step: 'translated' })])])).toBe(false)
    expect(isMoving([novel([row({ step: 'reading' })])])).toBe(true)
    expect(isMoving([novel([], { waiting: { resume_at: 1 } })])).toBe(true)
  })
})

describe('polling', () => {
  let visibility = 'visible'
  beforeEach(() => {
    vi.useFakeTimers()
    visibility = 'visible'
    vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility)
    api.phoneProgress.mockReset()
  })
  afterEach(() => { vi.useRealTimers() })

  const flush = () => act(() => vi.advanceTimersByTimeAsync(0))

  it('asks again soon while something moves, and rarely when nothing does', async () => {
    api.phoneProgress.mockResolvedValue({ novels: [novel([row({ step: 'reading' })])] })
    renderHook(() => usePhoneProgress({ pid: 'p1' }))
    await flush()
    expect(api.phoneProgress).toHaveBeenCalledTimes(1)
    expect(api.phoneProgress).toHaveBeenCalledWith({ pid: 'p1', box: '' })
    await act(() => vi.advanceTimersByTimeAsync(FAST_MS))
    expect(api.phoneProgress).toHaveBeenCalledTimes(2)

    api.phoneProgress.mockResolvedValue({ novels: [novel([row({ step: 'translated' })])] })
    await act(() => vi.advanceTimersByTimeAsync(FAST_MS))
    expect(api.phoneProgress).toHaveBeenCalledTimes(3)
    await act(() => vi.advanceTimersByTimeAsync(SLOW_MS - 1))
    expect(api.phoneProgress).toHaveBeenCalledTimes(3)
    await act(() => vi.advanceTimersByTimeAsync(1))
    expect(api.phoneProgress).toHaveBeenCalledTimes(4)
  })

  it('stops while the app is hidden and asks at once when it comes back', async () => {
    api.phoneProgress.mockResolvedValue({ novels: [novel([row({ step: 'reading' })])] })
    renderHook(() => usePhoneProgress())
    await flush()
    visibility = 'hidden'
    await act(() => vi.advanceTimersByTimeAsync(FAST_MS * 3))
    expect(api.phoneProgress).toHaveBeenCalledTimes(1)
    visibility = 'visible'
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')) })
    await flush()
    expect(api.phoneProgress).toHaveBeenCalledTimes(2)
  })

  it('uses a fixed interval when one is given, and passes the chapter on screen', async () => {
    api.phoneProgress.mockResolvedValue({ novels: [] })
    renderHook(() => usePhoneProgress({ pid: 'p1', box: 'b1', every: 2000 }))
    await flush()
    expect(api.phoneProgress).toHaveBeenCalledWith({ pid: 'p1', box: 'b1' })
    await act(() => vi.advanceTimersByTimeAsync(2000))
    expect(api.phoneProgress).toHaveBeenCalledTimes(2)
  })

  it('keeps going after the computer can’t be reached', async () => {
    api.phoneProgress.mockRejectedValueOnce(new Error('offline')).mockResolvedValue({ novels: [] })
    const { result } = renderHook(() => usePhoneProgress())
    await flush()
    expect(result.current.error).toBeTruthy()
    await act(() => vi.advanceTimersByTimeAsync(SLOW_MS))
    expect(result.current.error).toBeNull()
    expect(result.current.novels).toEqual([])
  })
})
