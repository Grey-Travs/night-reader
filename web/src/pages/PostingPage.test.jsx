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
    site: { fetched_at: null, count: 0, posted: [], max_posted: null,
            numbered: 0, trusted: true, why: '', age_seconds: null },
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

  // audit_pricing spans the WHOLE back catalogue, not this run, so one changed coin
  // price puts every posted chapter in it at once. Listing them per chapter would be
  // thousands of characters; the page has to fold them the way `held` folds.
  it('folds a mispriced back catalogue into one line with a count', async () => {
    const audit = Array.from({ length: 60 }, (_, i) => ({
      title: `Chapter ${201 + i}`, global: 201 + i,
      problems: ['costs 5 coins on the site, not 55'],
    }))
    setup({ planDoc: plan([item(72)], { audit }) })
    await screen.findByText(/already on the site, but not as set/i)
    expect(screen.getByText(/costs 5 coins on the site, not 55 \(60\)/)).toBeTruthy()
    // the chapters themselves are NOT named -- that is the whole point
    expect(screen.queryByText(/Chapter 201/)).toBeNull()
  })

  it('does not call a correctly priced chapter mispriced just because it is hidden', async () => {
    // audit_pricing reports a wrong STATE independently of the price, so the heading has
    // to cover both. "On the site at the wrong price" would be a lie for this row.
    setup({
      planDoc: plan([item(72)], {
        audit: [{ title: 'Chapter 300', global: 300,
                  problems: ['is private on the site, so readers cannot see it'] }],
      }),
    })
    await screen.findByText(/already on the site, but not as set/i)
    expect(screen.getByText(/is private on the site, so readers cannot see it \(1\)/))
      .toBeTruthy()
    expect(screen.queryByText(/wrong price/i)).toBeNull()
  })

  it('says nothing when the site agrees with the series', async () => {
    setup({ planDoc: plan([item(72)], { audit: [] }) })
    await screen.findByText(/Test Novel/i)
    expect(screen.queryByText(/but not as set/i)).toBeNull()
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

const siteWith = (over) => ({
  fetched_at: '2026-09-20T00:00:00Z', count: 196, posted: [[1, 196]], max_posted: 196,
  numbered: 196, trusted: true, why: '', age_seconds: 60, ...over,
})

describe('taking the range from what is actually posted', () => {
  it('offers the next run and applies it without asking the server again', async () => {
    // The real shape, from "Becoming the Top's Older Brother": the site ends at 196, and
    // 198, 199, 200 and 223 are waiting here. 223 sits past a gap, so the offer is not a
    // no-op and is worth showing. Range changes are pure client arithmetic, which is what
    // keeps the page responsive.
    setup({
      planDoc: plan([item(198), item(199), item(200), item(223)],
        { site: siteWith({}) }),
    })
    const offer = await screen.findByRole('button', { name: /the site ends at 196/i })
    api.postingPlan.mockClear()
    fireEvent.click(offer)
    expect(screen.getByLabelText('From chapter')).toHaveValue(198)
    expect(screen.getByLabelText('To chapter')).toHaveValue(200)
    expect(api.postingPlan).not.toHaveBeenCalled()
  })

  it('stops offering once the range already matches', async () => {
    setup({
      planDoc: plan([item(198), item(199), item(200), item(223)],
        { site: siteWith({}) }),
    })
    fireEvent.click(await screen.findByRole('button', { name: /the site ends at 196/i }))
    await waitFor(() => expect(
      screen.queryByRole('button', { name: /the site ends at 196/i })).toBeNull())
  })

  it('does not offer a range that would quietly drop the side stories', async () => {
    // With nothing apart, blank boxes already post exactly the suggestion PLUS the side
    // stories — so the button would be a no-op that loses them.
    const side = { ...item(201), global: null, kind: 'side', title: 'Side Stories 1' }
    setup({ planDoc: plan([item(198), item(199), side], { site: siteWith({}) }) })
    await screen.findByRole('button', { name: /start posting/i })
    expect(screen.queryByRole('button', { name: /post 198/i })).toBeNull()
  })

  it('names what the site already holds', async () => {
    setup({ planDoc: plan([item(198)], { site: siteWith({}) }) })
    expect(await screen.findByText(/it has 1–196/)).toBeInTheDocument()
  })

  it('says plainly when nothing of yours is missing', async () => {
    // The state most of the real library is in, and the direct answer to the question.
    setup({ planDoc: plan([], { site: siteWith({}) }) })
    expect(await screen.findByText(/nothing of yours is missing from it/i)).toBeInTheDocument()
  })

  it('says what a range leaves out, and offers to undo it', async () => {
    const side = { ...item(300), global: null, kind: 'side', title: 'Side Stories 1' }
    setup({ planDoc: plan([item(198), item(199), side], { site: siteWith({}) }) })
    await screen.findByRole('button', { name: /start posting/i })
    fireEvent.change(screen.getByLabelText('From chapter'), { target: { value: '198' } })
    expect(await screen.findByText(/1 side story is not in this run/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /post everything ready instead/i }))
    await waitFor(() => expect(screen.getByLabelText('From chapter')).toHaveValue(null))
  })
})

describe('refusing a chapter the site already carries', () => {
  it('will not start, and says why', async () => {
    // Title drift: the server saw no matching TITLE, so 118 reads as ready, while the
    // site's own list says that NUMBER is there. That is how one gets posted twice.
    setup({ planDoc: plan([item(118)], { site: siteWith({}) }) })
    expect(await screen.findByText(/already on the site, by number/i)).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: /start posting/i })).toBeDisabled()
    fireEvent.click(await screen.findByRole('button', { name: /start posting/i }))
    expect(api.startPostingRun).not.toHaveBeenCalled()
  })

  it('warns about a series-list reading but does not block on it', async () => {
    // Stale or untrusted is a warning, never a block: the threshold that blocks a two-day
    // old snapshot also blocks every legitimate run on a machine that has not opened the
    // tab today.
    setup({
      planDoc: plan([item(5)], {
        site: siteWith({ count: 94, numbered: 0, posted: [], max_posted: null }),
      }),
    })
    expect(await screen.findByText(/series list/i)).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: /start posting/i })).toBeEnabled()
  })
})
