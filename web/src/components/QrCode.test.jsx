import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import QrCode from './QrCode'
import RemoteAccess from './RemoteAccess'
import { api } from '../api'

// The phone link as a QR code, for setting up a new phone. It carries the access key, so
// it shows only when asked, and hides itself again.

vi.mock('../toast', () => ({ useToast: () => () => {} }))
vi.mock('../api', () => ({ api: { remote: vi.fn(), updateRemote: vi.fn(), testRemoteNotification: vi.fn() } }))

const LINK = 'https://pc.example.ts.net:8443/phone?k=secret'

describe('the QR code', () => {
  it('draws the link as dark squares on white', () => {
    render(<QrCode text={LINK} label="Phone link" />)
    const svg = screen.getByRole('img', { name: 'Phone link' })
    const path = svg.querySelector('path')
    expect(path.getAttribute('d').length).toBeGreaterThan(100)
    expect(path.getAttribute('fill')).toBe('#000000')
  })
})

describe('the phone link card', () => {
  afterEach(() => vi.useRealTimers())

  it('shows the QR code only when asked, and hides it after two minutes', async () => {
    api.remote.mockResolvedValue({
      enabled: true, local: true, token: 'secret', phone_url: 'http://100.1.2.3:8000/phone?k=secret',
      addresses: [], notify_url: '',
      serve: { tailnet_name: 'pc.example.ts.net', https_url: 'https://pc.example.ts.net:8443',
        phone_url: LINK, serving_other: {}, command: '', would_be: '' },
    })
    render(<RemoteAccess />)
    const show = await screen.findByRole('button', { name: 'Show QR code' })
    expect(screen.queryByRole('img', { name: 'QR code of the phone link' })).toBeNull()
    vi.useFakeTimers()
    fireEvent.click(show)
    expect(screen.getByRole('img', { name: 'QR code of the phone link' })).toBeTruthy()
    await act(() => vi.advanceTimersByTimeAsync(2 * 60 * 1000))
    expect(screen.queryByRole('img', { name: 'QR code of the phone link' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Show QR code' })).toBeTruthy()
  })
})
