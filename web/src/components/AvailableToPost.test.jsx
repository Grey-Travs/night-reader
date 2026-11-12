import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import AvailableToPost from './AvailableToPost'
import { api } from '../api'

// "Available to post" at the top of the Posting page: which novels have chapters waiting,
// and how far each count can be trusted.

vi.mock('../api', () => ({ api: { postingAvailable: vi.fn() } }))

const now = new Date().toISOString()
const longAgo = new Date(Date.now() - 53 * 86400e3).toISOString()
const site = (over = {}) => ({ fetched_at: now, age_seconds: 60, fresh: true, trusted: true, why: '', max_posted: 10, ...over })
const row = (over = {}) => ({
  sid: 's1', target_id: 't1', name: 'Novel', views: 0, ready: 0, ready_numbers: [],
  unfinished: 0, low_confidence: 0, numbered: true, site: site(), auto_numbered: [], unnumbered: [], ...over,
})

beforeEach(() => {
  vi.clearAllMocks()
  api.postingAvailable.mockResolvedValue({ series: [
    row({ sid: 'a', name: 'Cooked Rice', ready: 3, ready_numbers: [201, 202, 203], unfinished: 22, site: site({ max_posted: 178 }) }),
    row({ sid: 'b', name: 'Store Owner', ready: 2, ready_numbers: [128, 129] , site: site({ max_posted: 127 }) }),
    row({ sid: 'c', name: 'S Rank Stepmother', ready: 1, ready_numbers: [91], site: site({ fetched_at: longAgo, fresh: false, max_posted: 90 }) }),
    row({ sid: 'd', name: 'This Priest', ready: 233, ready_numbers: [3], site: site({ fetched_at: null, fresh: false, max_posted: null }) }),
    row({ sid: 'e', name: 'Finished One', site: site({ max_posted: 50 }) }),
  ] })
})

describe('available to post', () => {
  it('lists the novels with chapters waiting, with their ranges', async () => {
    render(<AvailableToPost currentSid="" onPick={() => {}} />)
    expect(await screen.findByText('Cooked Rice')).toBeTruthy()
    expect(screen.getByText(/6 chapters across 3 novels/)).toBeTruthy()
    expect(screen.getByRole('button', { name: /Store Owner/ }).textContent).toMatch(/2 ready · ch 128–129/)
  })

  it('points out a hole between the site and the first chapter ready', async () => {
    render(<AvailableToPost currentSid="" onPick={() => {}} />)
    const rice = await screen.findByRole('button', { name: /Cooked Rice/ })
    expect(rice.textContent).toMatch(/ch 179–200 aren’t ready yet/)
    expect(rice.textContent).toMatch(/22 more chapters not translated yet/)
    expect(screen.getByRole('button', { name: /Store Owner/ }).textContent).not.toMatch(/gap/)
  })

  it('says when a count comes from an old reading', async () => {
    render(<AvailableToPost currentSid="" onPick={() => {}} />)
    expect((await screen.findByRole('button', { name: /S Rank Stepmother/ })).textContent).toMatch(/may be out of date/)
  })

  it('keeps a never-read novel out of the list instead of claiming its count', async () => {
    render(<AvailableToPost currentSid="" onPick={() => {}} />)
    await screen.findByText('Cooked Rice')
    expect(screen.queryByRole('button', { name: /This Priest.*ready/ })).toBeNull()
    expect(screen.getByText(/1 novel never read from meiko.studio/)).toBeTruthy()
    expect(screen.getByText(/1 novel with nothing left to post/)).toBeTruthy()
  })

  it('opens a novel’s plan when it is picked', async () => {
    const onPick = vi.fn()
    render(<AvailableToPost currentSid="" onPick={onPick} />)
    fireEvent.click(await screen.findByRole('button', { name: /Store Owner/ }))
    expect(onPick).toHaveBeenCalledWith('b')
  })
})
