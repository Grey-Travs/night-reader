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

  it('says so when the numbers cannot be loaded', async () => {
    api.stats.mockRejectedValue(new Error('The app is not running.'))
    render(<StatsPage />)
    expect(await screen.findByText('The app is not running.')).toBeTruthy()
  })
})
