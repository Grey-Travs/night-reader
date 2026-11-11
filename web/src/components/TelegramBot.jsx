import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'

// Settings → Telegram: another way to send pages. Photos and scanner PDFs sent to a bot of
// your own go into the novel chosen in the chat, as a chapter, and are read, built and
// translated like pages from the phone; the phone's notices come back in the chat too.
//
// The token is the key to the bot, so it is typed here on the computer and never shown
// again. Who may send pages in is decided here as well: the code shown here, sent from the
// chat, pairs that one person. A phone using the app's link sees only how it stands.

const LOOK_EVERY_MS = 2000

const PROBLEMS = {
  'token-rejected': 'Telegram no longer accepts this bot’s token. Make a new bot with @BotFather and save its token here.',
  conflict: 'Something else is reading this bot’s messages: another copy of Night Reader, or another program using the same bot.',
  unreachable: 'Can’t reach Telegram from this computer right now. It keeps trying.',
  busy: 'Telegram asked for a pause. It carries on by itself.',
}

const HOW_TO_SEND = 'In the chat: /novel to choose the novel, then the chapter’s pages in order, then /done. '
  + 'For the sharpest pages, choose them in the gallery, then ⋮ → Send without compression. '
  + 'A scanner PDF of up to 20 MB is a chapter by itself.'
const PRIVACY = 'Photos sent to the bot pass through Telegram’s servers and stay there, and chats with a bot '
  + 'aren’t end-to-end encrypted. The token stays on this computer.'

function problemText(error) {
  if (!error) return ''
  return PROBLEMS[error.kind] || error.message || 'Something went wrong talking to Telegram.'
}

function TokenForm({ busy, onSave, label = 'Save' }) {
  const [token, setToken] = useState('')
  return (
    <form
      className="flex gap-2"
      onSubmit={async (e) => {
        e.preventDefault()
        if (await onSave(token.trim())) setToken('')
      }}
    >
      <input
        type="password"
        autoComplete="off"
        spellCheck={false}
        className="input min-w-0 flex-1"
        aria-label="Bot token"
        placeholder="123456789:AAH…"
        value={token}
        onChange={(e) => setToken(e.target.value)}
      />
      <button type="submit" className="btn btn-primary px-4 py-2 text-sm" disabled={busy || !token.trim()}>
        {label}
      </button>
    </form>
  )
}

export default function TelegramBot() {
  const [state, setState] = useState(null)
  const [error, setError] = useState(null)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [removing, setRemoving] = useState(false)

  const load = useCallback(
    () => api.telegram().then(setState).catch((e) => setError(e.message || String(e))),
    [],
  )
  useEffect(() => { load() }, [load])

  // While a code waits to be sent from the chat, look for the pairing every two seconds.
  // The code stops being offered once it runs out, which ends the looking too.
  const waiting = !!state?.pairing && !state?.paired
  useEffect(() => {
    if (!waiting) return undefined
    const timer = setInterval(() => { api.telegram().then(setState).catch(() => {}) }, LOOK_EVERY_MS)
    return () => clearInterval(timer)
  }, [waiting])

  async function act(run, done = '') {
    setBusy(true)
    setError(null)
    setNote('')
    try {
      const next = await run()
      if (next && typeof next === 'object' && 'configured' in next) setState(next)
      setNote(done)
      return true
    } catch (e) {
      setError(e.message || String(e))
      return false
    } finally {
      setBusy(false)
    }
  }

  const saveToken = (token) => act(() => api.updateTelegram({ token }))
  const problem = problemText(state?.error)
  const paired = state?.paired
  const who = paired ? [paired.name, paired.username && `@${paired.username}`].filter(Boolean).join(' ') : ''

  let body
  if (!state) {
    body = <div className="text-hint">Loading…</div>
  } else if (!state.local) {
    body = (
      <>
        <p className="text-sm">
          {!state.configured ? 'Not set up.' : paired ? `Set up, and paired with ${who}.` : 'Set up, but no one is paired with it yet.'}
          {' '}It is set up on the computer itself.
        </p>
        {problem && <p className="rounded-btn px-3 py-2 text-xs pill-review">{problem}</p>}
      </>
    )
  } else if (!state.configured) {
    body = (
      <>
        <ol className="list-decimal space-y-1 pl-5 text-sm">
          <li>In Telegram, open a chat with <span className="font-medium">@BotFather</span> and send /newbot.</li>
          <li>Give it any name, and a username ending in “bot”.</li>
          <li>BotFather answers with the bot’s token. Paste it here.</li>
        </ol>
        <TokenForm busy={busy} onSave={saveToken} />
        <p className="text-xs text-hint">{PRIVACY}</p>
      </>
    )
  } else {
    body = (
      <>
        <p className="text-sm">
          The bot is <span className="font-medium">@{state.bot_username}</span>.
          {!problem && (state.running ? ' It is listening for messages.' : ' It isn’t running: restart Night Reader to start it.')}
        </p>
        {problem && <p className="rounded-btn px-3 py-2 text-xs pill-review">{problem}</p>}
        {state.error?.kind === 'token-rejected' && <TokenForm busy={busy} onSave={saveToken} label="Replace" />}

        {paired ? (
          <div className="space-y-3">
            <p className="text-sm">Paired with <span className="font-medium">{who}</span>. Only they can send pages to it.</p>
            <div className="flex flex-wrap gap-2">
              <button className="btn btn-ghost px-3 py-1.5 text-xs" disabled={busy}
                onClick={() => act(() => api.testTelegram(), 'Test sent — check the chat')}>
                Send a test message
              </button>
              <button className="btn btn-ghost px-3 py-1.5 text-xs" disabled={busy}
                onClick={() => act(() => api.unpairTelegram())}>
                Unpair
              </button>
            </div>
          </div>
        ) : state.pairing ? (
          <div className="space-y-2 rounded-card border border-line p-3">
            <p className="text-sm">
              In Telegram, open <span className="font-medium">@{state.bot_username}</span> and send this code:
            </p>
            <p className="text-2xl font-medium tracking-widest" aria-label="Pairing code">{state.pairing.code}</p>
            {state.pairing.link && (
              <a className="text-sm underline" href={state.pairing.link} target="_blank" rel="noreferrer">
                Or open the bot with the code already in it
              </a>
            )}
            <p className="text-xs text-hint">The code works once, for ten minutes. Whoever sends it is the one person the bot takes pages from.</p>
          </div>
        ) : (
          <button className="btn btn-ghost px-3 py-1.5 text-xs" disabled={busy}
            onClick={() => act(() => api.pairTelegram())}>
            Pair with your Telegram
          </button>
        )}

        <p className="text-xs text-hint">{HOW_TO_SEND}</p>
        <p className="text-xs text-hint">{PRIVACY}</p>

        {removing ? (
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span>Remove the bot? Night Reader forgets its token and who is paired with it.</span>
            <button className="btn btn-ghost px-3 py-1.5 text-xs" disabled={busy}
              onClick={async () => { if (await act(() => api.forgetTelegram())) setRemoving(false) }}>
              Remove
            </button>
            <button className="btn btn-ghost px-3 py-1.5 text-xs" onClick={() => setRemoving(false)}>Keep it</button>
          </div>
        ) : (
          <button className="btn btn-ghost px-3 py-1.5 text-xs" onClick={() => setRemoving(true)}>
            Remove the bot
          </button>
        )}
      </>
    )
  }

  return (
    <section className="card mt-6 space-y-4 p-6" aria-label="Telegram">
      <h2 className="text-base font-medium">Telegram</h2>
      <p className="text-sm text-hint">
        Send a chapter’s pages to a Telegram bot of your own — photos, an album or a scanner
        PDF — and they are read, built and translated like pages from the phone.
      </p>
      {error && <div className="rounded-btn px-3 py-2 text-sm pill-review">{error}</div>}
      {note && <div className="text-sm text-hint" role="status">{note}</div>}
      {body}
      {state?.configured && paired && (
        <label className="flex items-start gap-3 text-sm">
          <input type="checkbox" className="mt-0.5" checked={!!state.notify} disabled={busy}
            onChange={(e) => act(() => api.updateTelegram({ notify: e.target.checked }))} />
          <span>Send the notices to the chat too: a chapter translated, or a page that needs you.</span>
        </label>
      )}
    </section>
  )
}
