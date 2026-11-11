import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import ThisComputer from './ThisComputer'
import { api } from '../api'

// Settings → This computer: the two switches that keep it ready for the phone.

vi.mock('../api', () => ({ api: { computer: vi.fn(), updateComputer: vi.fn() } }))

const state = (over = {}) => ({ autostart: { supported: true, on: false, current: false }, keep_awake: true, ...over })

beforeEach(() => {
  vi.clearAllMocks()
  api.computer.mockResolvedValue(state())
})

describe('this computer', () => {
  it('switches starting with Windows on', async () => {
    api.updateComputer.mockResolvedValue(state({ autostart: { supported: true, on: true, current: true } }))
    render(<ThisComputer />)
    const box = await screen.findByRole('checkbox', { name: /Start Night Reader with Windows/ })
    fireEvent.click(box)
    await waitFor(() => expect(api.updateComputer).toHaveBeenCalledWith({ autostart: true }))
    await waitFor(() => expect(box).toBeChecked())
  })

  it('switches keeping awake off', async () => {
    api.updateComputer.mockResolvedValue(state({ keep_awake: false }))
    render(<ThisComputer />)
    fireEvent.click(await screen.findByRole('checkbox', { name: /Keep the computer awake/ }))
    await waitFor(() => expect(api.updateComputer).toHaveBeenCalledWith({ keep_awake: false }))
  })

  it('says when the startup entry is for another copy', async () => {
    api.computer.mockResolvedValue(state({ autostart: { supported: true, on: true, current: false } }))
    render(<ThisComputer />)
    expect(await screen.findByText(/points at another copy/)).toBeTruthy()
  })

  it('offers no startup switch off Windows', async () => {
    api.computer.mockResolvedValue(state({ autostart: { supported: false, on: false, current: false } }))
    render(<ThisComputer />)
    await screen.findByRole('checkbox', { name: /Keep the computer awake/ })
    expect(screen.queryByRole('checkbox', { name: /Start Night Reader/ })).toBeNull()
  })
})
