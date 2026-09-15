import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import PostingPage from './PostingPage'
import { api } from '../api'

// The Posting page is now the thing that STARTS a run, which makes it the one place where
// "the build passed" proves nothing — see the note on PagesPage.test.jsx, where a page
// shipped unusable because it never called the function that begins the work. The same
// mistake here would leave a Start button that looks fine and posts nothing, or worse,
// starts a run at the wrong price.
//
// Two of these guard money rather than behaviour: 21 of the 22 real linked series have no
// coin price set, so every chapter would go out as paid-at-zero-coins, and a price is
// painful to change once readers have been through a chapter.

vi.mock('react-router-dom', () => ({
  Link: ({ to, children, ...rest }) => <a href={to} {...rest}>{children}</a>,
}))

vi.mock('../toast', () => ({ useToast: () => () => {} }))

vi.mock('../api', () => ({
  api: {
    listSeries: vi.fn(),
    postingPlan: vi.fn(),
    postingRun: vi.fn(),
    startPostingRun: vi.fn(),
    cancelPostingRun: vi.fn(),
    resumePostingRun: vi.fn(),
    updateSeries: vi.fn(),
  },
}))

// A stray key on the target, to prove a save does not drop what this page does not know
// about — publish_targets is a whole-list replace on the server.
const TARGET = {
  id: 't1',
  site: 'meiko',
  series_url: 'https://meiko.studio/page/x/series/y/',
  free_through: 47,
  coin_price: 55,
  note: 'keep me',
}

const SERIES = { id: 's1', name: 'Test Novel', publish_targets: [TARGET] }

function item(n, { paid = true, blockers = [] } = {}) {
  return {
    project_id: 'p1',
    index: n,
    global: n,
    kind: 'chapter',
    title: `Chapter ${n}`,
    status: 'validated',
    paid,
    coins: paid ? 55 : 0,
    html_chars: 9000,
    confidence: 'high',
    blockers,
  }
}

function plan(items, over = {}) {
  const ready = items.filter((i) => !i.blockers.length)
  return {
    series: { id: 's1', name: 'Test Novel' },
    target: TARGET,
    adapter: {},
    items,
    ready: ready.length,
    blocked: items.length - ready.length,
    free_through: 47,
    coin_price: 55,
    site: { fetched_at: null, count: 0 },
    summary: { free: [], paid: ready.map((i) => i.global) },
    ...over,
  }
}

function setup({ series = SERIES, planDoc, run = null } = {}) {
  api.listSeries.mockResolvedValue({ series: [series] })
  api.postingPlan.mockResolvedValue(planDoc || plan([item(72), item(73), item(74)]))
  api.postingRun.mockResolvedValue({ run, stale_after: 90, poll_seconds: 30 })
  return render(<PostingPage />)
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('starting a run', () => {
  it('queues the range, the caps and the state it was given', async () => {
    setup()
    api.startPostingRun.mockResolvedValue({
      state: 'queued', progress: { total: 2, posted: [], failed: [] },
    })
    const start = await screen.findByRole('button', { name: /start posting/i })
    fireEvent.change(screen.getByLabelText(/at most/i), { target: { value: '2' } })
    fireEvent.change(screen.getByLabelText(/publish as/i), { target: { value: 'private' } })
    fireEvent.click(start)
    await waitFor(() => expect(api.startPostingRun).toHaveBeenCalled())
    const [sid, targetId, options] = api.startPostingRun.mock.calls[0]
    expect(sid).toBe('s1')
    expect(targetId).toBe('t1')
    expect(options).toMatchObject({ limit: 2, post_state: 'private' })
  })

  it('says what it will post, in the range and price it will post it', async () => {
    setup()
    await screen.findByText(/will post/i)
    expect(screen.getByText('Chapter 72 → Chapter 74')).toBeTruthy()
    expect(screen.getByText(/3 chapters · 55 coins each/)).toBeTruthy()
  })

  it('follows a cap without asking the server for a new plan', async () => {
    setup()
    await screen.findByText(/will post/i)
    api.postingPlan.mockClear()
    fireEvent.change(screen.getByLabelText(/stop at chapter/i), { target: { value: '73' } })
    await screen.findByText('Chapter 72 → Chapter 73')
    expect(screen.getByText(/2 chapters/)).toBeTruthy()
    expect(api.postingPlan).not.toHaveBeenCalled()
  })
})

describe('the pricing guard', () => {
  it('refuses to start a series whose price was never set', async () => {
    const bare = { ...TARGET, free_through: 0, coin_price: 0 }
    setup({
      series: { ...SERIES, publish_targets: [bare] },
      planDoc: plan([item(1), item(2)], {
        target: bare, free_through: 0, coin_price: 0,
      }),
    })
    const start = await screen.findByRole('button', { name: /start posting/i })
    expect(start.disabled).toBe(true)
    expect(screen.getByText(/paid at 0 coins/i)).toBeTruthy()
    fireEvent.click(start)
    expect(api.startPostingRun).not.toHaveBeenCalled()
  })

  it('will not start on a price that has been typed but not saved', async () => {
    setup()
    const start = await screen.findByRole('button', { name: /start posting/i })
    expect(start.disabled).toBe(false)
    fireEvent.change(screen.getByLabelText(/coins after that/i), { target: { value: '80' } })
    await waitFor(() => expect(start.disabled).toBe(true))
    expect(screen.getByText(/save the pricing first/i)).toBeTruthy()
  })

  it('keeps keys on the target that this page knows nothing about', async () => {
    setup()
    api.updateSeries.mockResolvedValue({})
    await screen.findByLabelText(/coins after that/i)
    fireEvent.change(screen.getByLabelText(/coins after that/i), { target: { value: '80' } })
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }))
    await waitFor(() => expect(api.updateSeries).toHaveBeenCalled())
    const [, body] = api.updateSeries.mock.calls[0]
    expect(body.publish_targets[0]).toMatchObject({
      id: 't1', note: 'keep me', coin_price: 80, free_through: 47,
      series_url: TARGET.series_url,
    })
  })

  it('sends the side-story pricing choice, on by default', async () => {
    // A side story has no chapter number for the cutoff to compare, and defaulting them
    // free gave away the newest content on the four series that have any.
    setup()
    api.updateSeries.mockResolvedValue({})
    const box = await screen.findByLabelText(/side stories cost coins/i)
    expect(box.checked).toBe(true)
    fireEvent.click(box)
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }))
    await waitFor(() => expect(api.updateSeries).toHaveBeenCalled())
    expect(api.updateSeries.mock.calls[0][1].publish_targets[0].side_paid).toBe(false)
  })

  it('offers the price the site charges rather than asking for it to be typed', async () => {
    // The site export carries no price anywhere, so the extension reads it off the chapter
    // rows instead. Offered, not applied: a price is painful to undo, so it gets a click.
    const unpriced = { ...TARGET, coin_price: 0 }
    setup({
      series: { ...SERIES, publish_targets: [unpriced] },
      planDoc: plan([item(72)], {
        target: unpriced,
        coin_price: 0,
        site: { fetched_at: new Date().toISOString(), count: 71, observed_coins: 55 },
      }),
    })
    fireEvent.click(await screen.findByRole('button', { name: /site charges 55/i }))
    await waitFor(() => {
      expect(screen.getByLabelText(/coins after that/i).value).toBe('55')
    })
    // And once it matches, the offer goes away rather than sitting there as a no-op.
    expect(screen.queryByRole('button', { name: /site charges 55/i })).toBeNull()
  })
})

describe('what the page shows', () => {
  it('does not render the chapter table it replaced', async () => {
    const { container } = setup()
    await screen.findByText(/will post/i)
    expect(container.querySelector('table')).toBeNull()
  })

  it('groups what is held back, and links the numbers worth confirming', async () => {
    setup({
      planDoc: plan([
        item(72),
        item(73, { blockers: ['chapter number not confirmed — check this series numbering'] }),
        item(74, { blockers: ['chapter number not confirmed — check this series numbering'] }),
        item(75, { blockers: ['already on the site'] }),
      ]),
    })
    await screen.findByText(/held back/i)
    const link = screen.getByRole('link', { name: /2 waiting on a chapter number/i })
    expect(link.getAttribute('href')).toBe('/series/s1')
    expect(screen.getByText(/1 already on the site/)).toBeTruthy()
  })

  it('admits when nobody has read the site yet', async () => {
    setup()
    expect(await screen.findByText(/don’t know what’s on the site yet/i)).toBeTruthy()
  })

  it('reports how fresh the site reading is once there is one', async () => {
    setup({
      planDoc: plan([item(72)], {
        site: { fetched_at: new Date(Date.now() - 3600_000).toISOString(), count: 71 },
      }),
    })
    expect(await screen.findByText(/Read 71 chapters off the site 1h ago/i)).toBeTruthy()
  })
})

describe('a run in flight', () => {
  it('shows progress without needing the meiko tab', async () => {
    setup({
      run: {
        id: 'r1', state: 'running', stalled: false, series_name: 'Test Novel',
        progress: {
          total: 3, current: 'Chapter 74', failed: [],
          posted: [{ title: 'Chapter 73', coins: 55, at: new Date().toISOString() }],
        },
      },
    })
    expect(await screen.findByText(/Running · 1 of 3/)).toBeTruthy()
    expect(screen.getByText(/Chapter 74/)).toBeTruthy()
    expect(screen.getByText(/last posted Chapter 73 · 55 coins/)).toBeTruthy()
    // The form is out of the way while a run is going, so nothing can be half-changed
    // under it.
    expect(screen.queryByRole('button', { name: /start posting/i })).toBeNull()
  })

  it('says a queued run is waiting on the browser, and how often it checks', async () => {
    setup({
      run: {
        id: 'r1', state: 'queued', stalled: false, series_name: 'Test Novel',
        progress: { total: 3, current: null, posted: [], failed: [] },
      },
    })
    expect(await screen.findByText(/Waiting for the browser/)).toBeTruthy()
    expect(screen.getByText(/every 30 seconds/)).toBeTruthy()
  })

  it('offers to resume a run whose browser went quiet', async () => {
    setup({
      run: {
        id: 'r1', state: 'running', stalled: true, series_name: 'Test Novel',
        progress: {
          total: 3, current: null, failed: [],
          posted: [{ title: 'Chapter 72', coins: 55, at: new Date().toISOString() }],
        },
      },
    })
    api.resumePostingRun.mockResolvedValue({ run: { id: 'r1', state: 'queued' } })
    fireEvent.click(await screen.findByRole('button', { name: /resume/i }))
    await waitFor(() => expect(api.resumePostingRun).toHaveBeenCalledWith('s1', 't1'))
  })

  it('cancels through the server rather than just stopping the display', async () => {
    setup({
      run: {
        id: 'r1', state: 'running', stalled: false, series_name: 'Test Novel',
        progress: { total: 3, current: 'Chapter 72', posted: [], failed: [] },
      },
    })
    api.cancelPostingRun.mockResolvedValue({ run: { id: 'r1', state: 'cancelled' } })
    fireEvent.click(await screen.findByRole('button', { name: /cancel/i }))
    await waitFor(() => expect(api.cancelPostingRun).toHaveBeenCalledWith('s1', 't1'))
  })

  it('surfaces the error a failed run stopped on', async () => {
    setup({
      run: {
        id: 'r1', state: 'failed', stalled: false, series_name: 'Test Novel',
        progress: {
          total: 3, current: null, posted: [],
          failed: [{ title: 'Chapter 73', error: 'session expired' }],
        },
      },
    })
    expect(await screen.findByText(/Stopped on an error/)).toBeTruthy()
    expect(screen.getByText(/Chapter 73: session expired/)).toBeTruthy()
  })
})
