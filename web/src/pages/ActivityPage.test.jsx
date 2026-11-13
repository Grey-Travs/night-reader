import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import ActivityPage from './ActivityPage'
import { StatusLine } from '../components/AppShell'
import { api } from '../api'

// The Activity page: each novel's queue with Pause and "do this one next", the failed
// chapters with Retry, free memory with its hold, and which notices to send.

vi.mock('../api', () => ({
  api: {
    activity: vi.fn(), activitySettings: vi.fn(), pauseQueue: vi.fn(), continueQueue: vi.fn(),
    queueNext: vi.fn(), translate: vi.fn(), allPendingTerms: vi.fn(), testPopup: vi.fn(),
  },
}))

const SETTINGS = {
  notify_finished: true, notify_failed: true, notify_limit: true, toasts: true, popup_style: 'box',
  hold_on_low_memory: false, low_memory_mb: 500,
}

const snapshot = (over = {}) => ({
  now: 1_800_000_000,
  jobs: [{
    pid: 'p1', name: 'When That Day Comes', kind: 'translate', current: 87, title: 'Tab 26',
    step: 'paragraph 34 of 80', elapsed: 240, pending: [88, 89, 90], in_queue: 4,
    waiting: null, paused: false, hold: null, eta: 1_800_000_900,
  }],
  queued: 4, eta: 1_800_000_900, typical_seconds: 300,
  today: { done: 14, failed: 1 }, failed_count: 1, last_failed_at: '2026-11-13T12:00:00+00:00',
  failed: [{ pid: 'p2', name: 'Time-Limited Hunter', index: 31, title: 'Tab 31',
             error: 'agent error: stopped: refusal', at: '2026-11-13T12:00:00+00:00' }],
  memory: { free_mb: 2900, total_mb: 8000, low: false, hold_on: false, threshold_mb: 500 },
  settings: SETTINGS,
  ...over,
})

const page = () => render(<MemoryRouter><ActivityPage /></MemoryRouter>)

describe('ActivityPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.activity.mockResolvedValue(snapshot())
    for (const fn of ['pauseQueue', 'continueQueue', 'queueNext', 'translate', 'testPopup']) api[fn].mockResolvedValue({ ok: true })
    api.activitySettings.mockImplementation(async (change) => ({
      settings: { ...SETTINGS, ...change }, memory: snapshot().memory,
    }))
  })

  it('shows the chapter, the step, how long it has run and when the queue is done', async () => {
    page()
    expect(await screen.findByText('When That Day Comes')).toBeTruthy()
    expect(screen.getByText('Translating ch 87 · paragraph 34 of 80 · 4 min so far')).toBeTruthy()
    expect(screen.getByText(/^Done around/)).toBeTruthy()
    expect(screen.getByText('Today: 14 done · 1 failed')).toBeTruthy()
  })

  it('pauses a novel, and moves a waiting chapter to the front', async () => {
    page()
    fireEvent.click(await screen.findByRole('button', { name: 'Pause' }))
    await waitFor(() => expect(api.pauseQueue).toHaveBeenCalledWith('p1'))
    // The first chip is already next, so it can't be pressed.
    expect(screen.getByRole('button', { name: '88' }).disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: '90' }))
    await waitFor(() => expect(api.queueNext).toHaveBeenCalledWith('p1', 90))
  })

  it('offers Continue for a paused novel', async () => {
    const paused = snapshot()
    paused.jobs[0] = { ...paused.jobs[0], paused: true, hold: { reason: 'paused' }, current: null, eta: null }
    api.activity.mockResolvedValue(paused)
    page()
    fireEvent.click(await screen.findByRole('button', { name: 'Continue' }))
    await waitFor(() => expect(api.continueQueue).toHaveBeenCalledWith('p1'))
    expect(screen.getByText('Paused. Press Continue to carry on.')).toBeTruthy()
  })

  it('lists failed chapters with their reason and retries one', async () => {
    page()
    const list = await screen.findByRole('region', { name: 'Failed chapters' })
    expect(list.textContent).toContain('agent error: stopped: refusal')
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(api.translate).toHaveBeenCalledWith('p2', { indices: [31] }))
  })

  it('switches on the memory hold and changes which notices are sent', async () => {
    page()
    fireEvent.click(await screen.findByRole('checkbox', { name: /Hold new chapters/ }))
    await waitFor(() => expect(api.activitySettings).toHaveBeenCalledWith({ hold_on_low_memory: true }))
    fireEvent.click(screen.getByRole('checkbox', { name: /A chapter fails/ }))
    await waitFor(() => expect(api.activitySettings).toHaveBeenCalledWith({ notify_failed: false }))
    expect(screen.getByText('2.8 GB free of 7.8 GB')).toBeTruthy()
  })
})

describe('the desktop pop-up', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.activity.mockResolvedValue(snapshot())
    api.testPopup.mockResolvedValue({ shown: true })
    api.activitySettings.mockImplementation(async (change) => ({
      settings: { ...SETTINGS, ...change }, memory: snapshot().memory,
    }))
  })

  it('is a box that waits for OK unless the corner is chosen, and can be tried', async () => {
    page()
    const box = await screen.findByRole('radio', { name: /stays until I press OK/ })
    expect(box.checked).toBe(true)
    fireEvent.click(screen.getByRole('radio', { name: /in the corner/ }))
    await waitFor(() => expect(api.activitySettings).toHaveBeenCalledWith({ popup_style: 'corner' }))
    fireEvent.click(screen.getByRole('button', { name: 'Show a test pop-up' }))
    await waitFor(() => expect(api.testPopup).toHaveBeenCalled())
  })

  it('says why when this copy of the app cannot pop anything up', async () => {
    api.testPopup.mockRejectedValue(new Error("This copy of the app has no icon by the clock"))
    page()
    fireEvent.click(await screen.findByRole('button', { name: 'Show a test pop-up' }))
    expect((await screen.findByRole('alert')).textContent).toContain('no icon by the clock')
  })
})

describe('the sidebar status line', () => {
  it('names the chapter and novel and says what is left', () => {
    render(<MemoryRouter><StatusLine act={snapshot()} /></MemoryRouter>)
    expect(screen.getByText('Translating ch 87 · When That Day Comes')).toBeTruthy()
    expect(screen.getByText(/^4 chapters left · done ~/)).toBeTruthy()
    expect(screen.getByText('Today: 14 done · 1 failed')).toBeTruthy()
  })

  it('is empty when nothing has happened', () => {
    const { container } = render(
      <MemoryRouter><StatusLine act={snapshot({ jobs: [], queued: 0, today: { done: 0, failed: 0 } })} /></MemoryRouter>)
    expect(container.textContent).toBe('')
  })
})
