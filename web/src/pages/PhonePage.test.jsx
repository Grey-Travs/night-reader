import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import PhonePage from './PhonePage'
import { api } from '../api'

// The phone page starts real runs, from a device where a mistake is hardest to notice and
// hardest to undo — no second monitor, no extension panel, and quite possibly nobody at
// the desk. So these check the same two things PostingPage.test.jsx does, plus the ones
// that are specific to being a phone.
//
// The one that matters most is the pricing guard. 21 of the 22 real linked series have no
// coin price set, and a run started at zero coins posts locked chapters readers cannot
// buy. The guard lives in posting.js and is shared with the desktop, but "shared" is a
// claim about the code — this asserts it actually reaches the button.

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

const TARGET = {
  id: 't1',
  site: 'meiko',
  series_url: 'https://meiko.studio/page/x/series/y/',
  free_through: 47,
  coin_price: 55,
  note: 'keep me',
}

const SERIES = { id: 's1', name: 'Test Novel', publish_targets: [TARGET] }
const OTHER = {
  id: 's2',
  name: 'Second Novel',
  publish_targets: [{ ...TARGET, id: 't2' }],
}

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

function setup({ series = [SERIES], planDoc, run = null } = {}) {
  api.listSeries.mockResolvedValue({ series })
  api.postingPlan.mockResolvedValue(planDoc || plan([item(72), item(73), item(74)]))
  api.postingRun.mockResolvedValue({ run, stale_after: 90, poll_seconds: 30 })
  return render(<PhonePage />)
}

const startButton = () => screen.findByRole('button', { name: /start posting/i })

beforeEach(() => {
  vi.clearAllMocks()
})

describe('the page stands on its own', () => {
  it('renders without a router, because it is mounted outside the app shell', async () => {
    // Not a formality. PostingPage needs react-router mocked in its own test because it
    // renders <Link>s; if the phone page ever grew one it would throw at runtime inside
    // the standalone route, where there is no navigation to fall back to.
    setup()
    expect(await startButton()).toBeInTheDocument()
  })

  it('offers every linked novel in one dropdown', async () => {
    setup({ series: [SERIES, OTHER] })
    const select = await screen.findByLabelText(/novel/i)
    expect(select.tagName).toBe('SELECT')
    expect(screen.getByRole('option', { name: 'Test Novel' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Second Novel' })).toBeInTheDocument()
  })

  it('re-plans against the novel that was picked', async () => {
    setup({ series: [SERIES, OTHER] })
    await startButton()
    fireEvent.change(await screen.findByLabelText(/novel/i), { target: { value: 's2' } })
    await waitFor(() => expect(api.postingPlan).toHaveBeenCalledWith('s2'))
  })

  it('says so plainly when nothing is linked yet', async () => {
    setup({ series: [] })
    expect(await screen.findByText(/no novel has a publishing link yet/i)).toBeInTheDocument()
  })
})

describe('the dashboard', () => {
  it('shows the counts and the range they cover', async () => {
    setup({ planDoc: plan([item(72), item(73), item(74)]) })
    expect(await screen.findByText('Ready')).toBeInTheDocument()
    expect(screen.getAllByText('72–74').length).toBeGreaterThan(0)
  })

  it('warns when it has never been told what is on the site', async () => {
    setup()
    expect(await screen.findByText(/don’t know what’s on the site yet/i)).toBeInTheDocument()
  })

  it('groups what is held back instead of listing chapters', async () => {
    setup({
      planDoc: plan([
        item(72),
        item(73, { blockers: ['chapter number is not confirmed'] }),
        item(74, { blockers: ['chapter number is not confirmed'] }),
      ]),
    })
    expect(await screen.findByText(/2 waiting on a chapter number/i)).toBeInTheDocument()
    // The per-chapter table is the thing the user asked to be rid of on the desktop; it
    // has no business on a four-inch screen either.
    expect(screen.queryByRole('table')).toBeNull()
  })
})

describe('starting a run', () => {
  it('sends the range, the caps and the visibility', async () => {
    setup()
    api.startPostingRun.mockResolvedValue({
      id: 'r1', state: 'queued', progress: { total: 2, posted: [], failed: [] },
    })
    fireEvent.click(await startButton())
    await waitFor(() => expect(api.startPostingRun).toHaveBeenCalled())
    const [sid, targetId, options] = api.startPostingRun.mock.calls[0]
    expect(sid).toBe('s1')
    expect(targetId).toBe('t1')
    expect(options.post_state).toBe('public')
  })

  it('honours a cap typed into the collapsed range panel', async () => {
    setup({ planDoc: plan([item(72), item(73), item(74)]) })
    await startButton()
    fireEvent.change(screen.getByLabelText('At most'), { target: { value: '2' } })
    api.startPostingRun.mockResolvedValue({
      id: 'r1', state: 'queued', progress: { total: 2, posted: [], failed: [] },
    })
    // The summary is recomputed on the device, without a server round trip, so the number
    // on the button is the number that will post.
    expect(await screen.findByText(/2 chapters/)).toBeInTheDocument()
    fireEvent.click(await startButton())
    await waitFor(() => expect(api.startPostingRun).toHaveBeenCalled())
    expect(api.startPostingRun.mock.calls[0][2].limit).toBe(2)
  })
})

describe('the pricing guard reaches the phone too', () => {
  it('refuses to start a paid run while the price is zero', async () => {
    setup({
      series: [{ ...SERIES, publish_targets: [{ ...TARGET, coin_price: 0 }] }],
      planDoc: plan([item(72), item(73)], { coin_price: 0 }),
    })
    expect(await screen.findByText(/would post as paid at 0 coins/i)).toBeInTheDocument()
    expect(await startButton()).toBeDisabled()
    fireEvent.click(await startButton())
    expect(api.startPostingRun).not.toHaveBeenCalled()
  })

  it('will not start on a price that has been typed but not saved', async () => {
    setup()
    await startButton()
    fireEvent.change(screen.getByLabelText(/coins after/i), { target: { value: '99' } })
    expect(await screen.findByText(/save the pricing first/i)).toBeInTheDocument()
    expect(await startButton()).toBeDisabled()
  })

  it('keeps target keys it knows nothing about when saving a price', async () => {
    // publish_targets is a whole-list replace on the server, so rebuilding the target
    // from the fields on screen would silently drop anything else stored on it.
    setup()
    await startButton()
    fireEvent.change(screen.getByLabelText(/coins after/i), { target: { value: '60' } })
    api.updateSeries.mockResolvedValue({})
    fireEvent.click(screen.getByRole('button', { name: /save price/i }))
    await waitFor(() => expect(api.updateSeries).toHaveBeenCalled())
    const sent = api.updateSeries.mock.calls[0][1].publish_targets[0]
    expect(sent.note).toBe('keep me')
    expect(sent.coin_price).toBe(60)
  })
})

describe('a run in flight', () => {
  const running = {
    id: 'r1',
    state: 'running',
    series_name: 'Test Novel',
    stalled: false,
    progress: {
      total: 10,
      current: 'Chapter 75',
      posted: [{ title: 'Chapter 74', coins: 55, at: '2026-09-18T14:03:00Z' }],
      failed: [],
    },
  }

  it('leads with the run and hides the setup while it is live', async () => {
    setup({ run: running })
    expect(await screen.findByText(/running · 1 of 10/i)).toBeInTheDocument()
    expect(screen.getByText('Chapter 75')).toBeInTheDocument()
    // Nothing to fiddle with mid-run, and no Start to press twice.
    expect(screen.queryByRole('button', { name: /start posting/i })).toBeNull()
    expect(screen.queryByLabelText(/coins after/i)).toBeNull()
  })

  it('offers Cancel while live and Resume once stalled', async () => {
    setup({ run: running })
    expect(await screen.findByRole('button', { name: /cancel/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /resume/i })).toBeNull()
  })

  it('explains a stalled run rather than just showing a word', async () => {
    setup({ run: { ...running, stalled: true } })
    expect(await screen.findByText(/stalled/i)).toBeInTheDocument()
    expect(screen.getByText(/the tab was probably closed/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /resume/i })).toBeInTheDocument()
  })

  it('tells you to open the browser on the computer while queued', async () => {
    // The whole mechanism: the phone writes a request, the desktop Chrome does the work.
    // If that tab is shut, nothing happens and the page must say why.
    setup({ run: { ...running, state: 'queued', progress: { ...running.progress, posted: [] } } })
    // Names the novel to open, not just "open the browser" — with 22 linked series, the
    // instruction is useless without it.
    expect(await screen.findByText(/open Test Novel on meiko\.studio in Chrome there/i))
      .toBeInTheDocument()
  })

  it('surfaces the chapter a run died on', async () => {
    setup({
      run: {
        ...running,
        state: 'failed',
        progress: { ...running.progress, failed: [{ title: 'Chapter 75', error: 'session expired' }] },
      },
    })
    expect(await screen.findByText(/chapter 75: session expired/i)).toBeInTheDocument()
  })
})

const siteWith = (over) => ({
  fetched_at: '2026-09-20T00:00:00Z', count: 196, posted: [[1, 196]], max_posted: 196,
  numbered: 196, trusted: true, why: '', age_seconds: 60, ...over,
})

describe('the range suggestion on a phone', () => {
  it('is reachable without opening the range panel', async () => {
    // The phone's version of "renders without a router": a suggestion inside a collapsed
    // <details> is a suggestion nobody sees, so it sits above it.
    setup({
      planDoc: plan([item(198), item(199), item(200), item(223)],
        { site: siteWith({}) }),
    })
    const offer = await screen.findByRole('button', { name: /the site ends at 196/i })
    expect(offer.closest('details')).toBeNull()
  })

  it('shows the applied range on the collapsed panel', async () => {
    // So a tap has a visible result even with the panel shut.
    setup({
      planDoc: plan([item(198), item(199), item(200), item(223)],
        { site: siteWith({}) }),
    })
    fireEvent.click(await screen.findByRole('button', { name: /the site ends at 196/i }))
    // Scoped to the summary: the tiles legitimately show the same numbers, and asserting
    // on the page as a whole would pass without the panel saying anything at all.
    const summary = document.querySelector('details summary')
    await waitFor(() => expect(summary.textContent).toMatch(/198–200/))
  })

  it('names what the site holds, and where yours picks up', async () => {
    setup({ planDoc: plan([item(198)], { site: siteWith({}) }) })
    expect(await screen.findByText(/it has 1–196/)).toBeInTheDocument()
    expect(screen.getByText(/picks up at 198/i)).toBeInTheDocument()
  })
})

describe('the duplicate refusal reaches the phone too', () => {
  it('will not start on a chapter the site already carries', async () => {
    // The whole point of putting the rule in posting.js: the phone cannot be the lenient
    // screen. A phone is also where a mistake is hardest to notice.
    setup({ planDoc: plan([item(118)], { site: siteWith({}) }) })
    expect(await screen.findByText(/already on the site, by number/i)).toBeInTheDocument()
    expect(await startButton()).toBeDisabled()
    fireEvent.click(await startButton())
    expect(api.startPostingRun).not.toHaveBeenCalled()
  })

  it('warns about an unusable reading without blocking', async () => {
    setup({
      planDoc: plan([item(5)], {
        site: siteWith({ count: 94, numbered: 0, posted: [], max_posted: null }),
      }),
    })
    expect(await screen.findByText(/series list/i)).toBeInTheDocument()
    expect(await startButton()).toBeEnabled()
  })
})
