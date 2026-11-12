import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import StatsPage from './StatsPage'
import { api } from '../api'

// The Stats page: the token total up top, the counts under it, and a features list whose
// one-line explanations appear on hover.

vi.mock('../api', () => ({ api: { stats: vi.fn() } }))

const sample = {
  tokens: {
    input_tokens: 47_390_074, output_tokens: 27_288_715,
    cache_creation_input_tokens: 40_226_333, cache_read_input_tokens: 83_182_458,
    total: 198_087_580,
  },
  cost_usd: 1223.12,
  chapters: { translated: 4115, needs_review: 1, english: 0 },
  novels: { total: 48, archived: 7, active: 41 },
  posted: { chapters: 1330, novels: 24, last_at: '2026-11-11T17:43:33+00:00' },
  app_updates: { count: 73, since: '2026-06-10T23:10:03+08:00' },
  pages_read: 0, rewrites: 0, glossary_names: 4387,
  since: '2026-06-12T16:06:40+00:00', unreadable: 0,
  monthly: {
    months: [
      { month: '2026-06', chapters: 908, novels: 9 },
      { month: '2026-07', chapters: 812, novels: 11 },
      { month: '2026-08', chapters: 683, novels: 8 },
      { month: '2026-09', chapters: 1550, novels: 14 },
      { month: '2026-10', chapters: 0, novels: 0 },
      { month: '2026-11', chapters: 584, novels: 6 },
    ],
    undated: 0,
  },
}

beforeEach(() => {
  vi.clearAllMocks()
  api.stats.mockResolvedValue(sample)
})

describe('stats page', () => {
  it('leads with the token total and shows the counts', async () => {
    render(<StatsPage />)
    expect(await screen.findByText('198.1M')).toBeTruthy()
    expect(screen.getByText((198_087_580).toLocaleString() + ' tokens')).toBeTruthy()
    expect(screen.getByText((4115).toLocaleString())).toBeTruthy()
    expect(screen.getByText('48')).toBeTruthy()
    expect(screen.getByText((1330).toLocaleString())).toBeTruthy()
    expect(screen.getByText('73')).toBeTruthy()
  })

  it('explains a feature on hover and hides it again', async () => {
    render(<StatsPage />)
    const chip = await screen.findByRole('button', { name: 'Duplicate guard' })
    expect(screen.queryByRole('tooltip')).toBeNull()
    fireEvent.mouseEnter(chip)
    expect(screen.getByRole('tooltip').textContent).toMatch(/nothing goes up twice/)
    fireEvent.mouseLeave(chip)
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('shows chapters translated each month, with the best one called out', async () => {
    render(<StatsPage />)
    const chart = await screen.findByRole('list', { name: 'Chapters translated per month' })
    expect(chart.querySelectorAll('li')).toHaveLength(6)
    // Every month is readable without hovering, including the quiet one and this one.
    expect(screen.getByLabelText(/September 2026: 1,550 chapters across 14 novels/)).toBeTruthy()
    expect(screen.getByLabelText(/October 2026: 0 chapters$/)).toBeTruthy()
    expect(screen.getByLabelText(/November 2026 \(so far\): 584 chapters/)).toBeTruthy()
    // The average leaves out the month still in progress: (908+812+683+1550+0) / 5.
    expect(screen.getByText(/Best: September 2026, 1,550 chapters/).textContent).toMatch(/about 791 a month/)
  })

  it('gives a month’s numbers when it is pointed at', async () => {
    render(<StatsPage />)
    const july = await screen.findByLabelText(/July 2026/)
    fireEvent.mouseEnter(july)
    expect(screen.getByText('July 2026: 812 chapters across 11 novels')).toBeTruthy()
    fireEvent.mouseLeave(july)
    expect(screen.getByText('Point at a month for its numbers.')).toBeTruthy()
  })

  it('leaves the chart out when no chapter has a date', async () => {
    api.stats.mockResolvedValue({ ...sample, monthly: { months: [], undated: 3 } })
    render(<StatsPage />)
    await screen.findByText('198.1M')
    expect(screen.queryByRole('list', { name: 'Chapters translated per month' })).toBeNull()
  })

  it('says so when the numbers cannot be loaded', async () => {
    api.stats.mockRejectedValue(new Error('The app is not running.'))
    render(<StatsPage />)
    expect(await screen.findByText('The app is not running.')).toBeTruthy()
  })
})
