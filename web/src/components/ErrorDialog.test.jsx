import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ErrorDialog } from './ErrorDialog'
import { api } from '../api'

// The button in an error has to DO what it says. "Reconnect Google" used to close the
// dialog and nothing else, which left the Doc export's permission error with no way out.

vi.mock('../api', () => ({ api: { googleLogin: vi.fn() } }))

const scopeError = {
  code: 'google-scope', title: 'Night Reader was never given permission to write to Google Docs',
  what: '', fixes: [], action: 'reconnect-google',
}
const expiredError = { ...scopeError, code: 'google-auth-expired', title: 'Your Google sign-in expired' }

function show(e, props = {}) {
  return render(
    <MemoryRouter initialEntries={['/novel']}>
      <Routes>
        <Route path="/novel" element={<ErrorDialog e={e} onClose={() => {}} {...props} />} />
        <Route path="/settings" element={<div>settings page</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => { vi.clearAllMocks() })

describe('error dialog actions', () => {
  it('asks Google for the write permission, then tries the export again', async () => {
    let finish
    api.googleLogin.mockReturnValue(new Promise((r) => { finish = r }))
    const onClose = vi.fn(), onRetry = vi.fn()
    show(scopeError, { onClose, onRetry })

    fireEvent.click(screen.getByRole('button', { name: 'Allow writing to Docs' }))
    expect(api.googleLogin).toHaveBeenCalledWith(true)
    // Still open while the person is in Google's window, and says where to look.
    expect(screen.getByRole('button', { name: 'Waiting for Google…' }).disabled).toBe(true)
    expect(screen.getByRole('status').textContent).toMatch(/Approve it there/)
    expect(onClose).not.toHaveBeenCalled()

    finish({ ok: true, can_write: true })
    await waitFor(() => expect(onRetry).toHaveBeenCalledTimes(1))
    expect(onClose).toHaveBeenCalled()
  })

  it('a plain expired sign-in reconnects without asking for more', async () => {
    api.googleLogin.mockResolvedValue({ ok: true })
    show(expiredError)
    fireEvent.click(screen.getByRole('button', { name: 'Reconnect Google' }))
    expect(api.googleLogin).toHaveBeenCalledWith(false)
  })

  it('stays open and says why when sign-in fails', async () => {
    api.googleLogin.mockRejectedValue(new Error('OAuth client secret not found'))
    const onClose = vi.fn(), onRetry = vi.fn()
    show(scopeError, { onClose, onRetry })
    fireEvent.click(screen.getByRole('button', { name: 'Allow writing to Docs' }))
    expect(await screen.findByText('OAuth client secret not found')).toBeTruthy()
    expect(onClose).not.toHaveBeenCalled()
    expect(onRetry).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Allow writing to Docs' }).disabled).toBe(false)
  })

  it('opens settings when that is the advice', () => {
    show({ ...scopeError, code: 'claude-limit', action: 'settings' })
    fireEvent.click(screen.getByRole('button', { name: 'Open settings' }))
    expect(screen.getByText('settings page')).toBeTruthy()
  })

  it('shows no button that would do nothing', () => {
    show({ ...scopeError, code: 'engine-down', action: 'setup' })
    expect(screen.queryByRole('button', { name: 'Run full setup' })).toBeNull()
    show({ ...scopeError, code: 'x', action: 'retry' })
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
  })

  it('a caller’s own handler still wins', () => {
    const onAction = vi.fn()
    show(scopeError, { onAction })
    fireEvent.click(screen.getByRole('button', { name: 'Allow writing to Docs' }))
    expect(onAction).toHaveBeenCalled()
    expect(api.googleLogin).not.toHaveBeenCalled()
  })
})
