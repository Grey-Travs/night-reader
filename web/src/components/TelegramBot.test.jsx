import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import TelegramBot from './TelegramBot'
import { api } from '../api'

// Settings → Telegram: setting the bot up on the computer, pairing it, and what a phone
// using the app's link is shown instead.

vi.mock('../api', () => ({
  api: {
    telegram: vi.fn(),
    updateTelegram: vi.fn(),
    pairTelegram: vi.fn(),
    unpairTelegram: vi.fn(),
    forgetTelegram: vi.fn(),
    testTelegram: vi.fn(),
  },
}))

const TOKEN = '123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
const bare = { configured: false, bot_username: '', paired: null, notify: true, running: false, last_poll_at: null, error: null, local: true }
const pairing = { ...bare, configured: true, bot_username: 'night_reader_bot', running: true,
  pairing: { code: '482913', expires_at: 0, link: 'https://t.me/night_reader_bot?start=482913' } }
const paired = { ...bare, configured: true, bot_username: 'night_reader_bot', running: true,
  paired: { name: 'Sam', username: 'sam' } }

beforeEach(() => {
  vi.clearAllMocks()
  api.telegram.mockResolvedValue(bare)
})

describe('setting the bot up', () => {
  it('takes the token in a hidden field, then shows the code to send from the chat', async () => {
    api.updateTelegram.mockResolvedValue(pairing)
    render(<TelegramBot />)
    expect(await screen.findByText(/@BotFather/)).toBeTruthy()
    expect(screen.getByText(/aren’t end-to-end encrypted/)).toBeTruthy()
    const field = screen.getByLabelText('Bot token')
    expect(field.type).toBe('password')
    fireEvent.change(field, { target: { value: `  ${TOKEN} ` } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.updateTelegram).toHaveBeenCalledWith({ token: TOKEN }))
    expect((await screen.findByLabelText('Pairing code')).textContent).toBe('482913')
    expect(screen.getByRole('link', { name: /code already in it/ }).getAttribute('href'))
      .toBe('https://t.me/night_reader_bot?start=482913')
    expect(screen.queryByLabelText('Bot token')).toBeNull()
  })

  it('says why a token was refused, and keeps it to try again', async () => {
    api.updateTelegram.mockRejectedValue(new Error('Telegram doesn’t accept that token. Copy it again from @BotFather.'))
    render(<TelegramBot />)
    const field = await screen.findByLabelText('Bot token')
    fireEvent.change(field, { target: { value: TOKEN } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText(/doesn’t accept that token/)).toBeTruthy()
    expect(screen.getByLabelText('Bot token').value).toBe(TOKEN)
  })

  it('looks for the pairing every two seconds until it comes', async () => {
    api.telegram.mockResolvedValueOnce(pairing).mockResolvedValue(paired)
    render(<TelegramBot />)
    await screen.findByLabelText('Pairing code')
    expect(await screen.findByText(/Only they can send pages to it/, {}, { timeout: 4000 })).toBeTruthy()
    expect(screen.getByText('Sam @sam')).toBeTruthy()
    expect(screen.queryByLabelText('Pairing code')).toBeNull()
  })
})

describe('once paired', () => {
  beforeEach(() => { api.telegram.mockResolvedValue(paired) })

  it('sends a test, switches notices off, and unpairs', async () => {
    api.testTelegram.mockResolvedValue({ sent: true })
    api.updateTelegram.mockResolvedValue({ ...paired, notify: false })
    api.unpairTelegram.mockResolvedValue({ ...paired, paired: null })
    render(<TelegramBot />)
    fireEvent.click(await screen.findByRole('button', { name: 'Send a test message' }))
    expect(await screen.findByText('Test sent — check the chat')).toBeTruthy()
    const notices = screen.getByRole('checkbox', { name: /notices to the chat/ })
    fireEvent.click(notices)
    await waitFor(() => expect(api.updateTelegram).toHaveBeenCalledWith({ notify: false }))
    await waitFor(() => expect(notices).not.toBeChecked())
    fireEvent.click(screen.getByRole('button', { name: 'Unpair' }))
    expect(await screen.findByRole('button', { name: 'Pair with your Telegram' })).toBeTruthy()
  })

  it('asks before removing the bot', async () => {
    api.forgetTelegram.mockResolvedValue(bare)
    render(<TelegramBot />)
    fireEvent.click(await screen.findByRole('button', { name: 'Remove the bot' }))
    expect(api.forgetTelegram).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }))
    expect(await screen.findByLabelText('Bot token')).toBeTruthy()
    expect(api.forgetTelegram).toHaveBeenCalledTimes(1)
  })
})

describe('what goes wrong', () => {
  it('asks for a new token when Telegram stops accepting this one', async () => {
    api.telegram.mockResolvedValue({ ...paired, running: false, error: { kind: 'token-rejected', message: 'x', since: 0 } })
    render(<TelegramBot />)
    expect(await screen.findByText(/no longer accepts this bot’s token/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Replace' })).toBeTruthy()
  })

  it('says when something else is reading the bot’s messages', async () => {
    api.telegram.mockResolvedValue({ ...paired, error: { kind: 'conflict', message: 'x', since: 0 } })
    render(<TelegramBot />)
    expect(await screen.findByText(/Something else is reading this bot’s messages/)).toBeTruthy()
  })
})

describe('from a phone', () => {
  it('shows how it stands, with nothing to change but the notices', async () => {
    api.telegram.mockResolvedValue({ ...paired, local: false })
    render(<TelegramBot />)
    expect(await screen.findByText(/Set up, and paired with Sam @sam/)).toBeTruthy()
    expect(screen.queryByLabelText('Bot token')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Unpair' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Remove the bot' })).toBeNull()
    expect(screen.getByRole('checkbox', { name: /notices to the chat/ })).toBeTruthy()
  })
})
