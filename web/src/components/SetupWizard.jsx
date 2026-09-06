import { useState } from 'react'
import { api } from '../api'
import { isFullyConnected, isReady } from '../ready'
import ThemeToggle from './ThemeToggle'

function Step({ done, n, title, children }) {
  return (
    <div className="flex gap-4">
      <div
        className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-sm font-semibold"
        style={done
          ? { background: 'var(--accent)', color: 'var(--accent-ink)' }
          : { background: 'var(--b-muted-bg)', color: 'var(--muted)' }}
      >
        {done ? '✓' : n}
      </div>
      <div className="flex-1 pb-6">
        <div className="font-medium">{title}</div>
        <div className="mt-1 text-sm text-muted">{children}</div>
      </div>
    </div>
  )
}

export default function SetupWizard({ status, setStatus, onDone }) {
  const [busy, setBusy] = useState('')
  const [error, setError] = useState(null)

  // `ready` gates the exit button, so it MUST use the same rule as App.jsx — this
  // wizard replaces the entire route tree, and it has no Esc, no back and no route of
  // its own. When the two rules disagreed, a user without Google could never leave.
  const ready = isReady(status)
  const allConnected = isFullyConnected(status)

  async function run(name, fn) {
    setBusy(name)
    setError(null)
    try {
      await fn()
      setStatus(await api.status())
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-page px-4 py-10 text-ink">
      <div className="w-full max-w-xl">
        <div className="mb-3 flex justify-end"><ThemeToggle /></div>
        <div className="card p-8">
          <h1 className="font-reading text-2xl font-medium">Welcome — let's get set up</h1>
          <p className="mt-1 text-sm text-muted">
            A few one-time steps. This app runs entirely on your computer, on your own Claude plan.
          </p>

          <div className="mt-8">
            <Step done={status?.config_present} n={1} title="Create your settings">
              {status?.config_present ? 'Settings file ready.' : (
                <button onClick={() => run('init', api.initConfig)} disabled={busy === 'init'} className="btn btn-primary mt-1 px-3 py-1.5 text-xs">
                  {busy === 'init' ? 'Creating…' : 'Create settings file'}
                </button>
              )}
            </Step>

            <Step done={status?.claude_logged_in} n={2} title="Sign in to Claude">
              {status?.claude_logged_in
                ? 'Connected to your Claude plan.'
                : 'Open Claude Code and sign in with your Claude Max or Pro plan, then re-check below.'}
            </Step>

            <Step done={status?.google_client_secret_present} n={3} title="Add your Google credential (optional)">
              {status?.google_client_secret_present ? 'Google credential found.' : (
                <>
                  Create a free Google OAuth <strong>Desktop app</strong> credential (Google Cloud
                  Console → enable the Docs API → Credentials), download the JSON, and save it in
                  the app folder as <code className="rounded px-1 font-mono" style={{ background: 'var(--b-muted-bg)' }}>client_secret.json</code>.
                  See the README for click-by-click steps, then re-check.
                </>
              )}
            </Step>

            <Step done={status?.google_logged_in} n={4} title="Connect Google (optional)">
              {status?.google_logged_in ? 'Google connected — read-only Docs access.' : (
                <button
                  onClick={() => run('google', api.googleLogin)}
                  disabled={busy === 'google' || !status?.google_client_secret_present || !status?.config_present}
                  className="btn btn-primary mt-1 px-3 py-1.5 text-xs"
                >
                  {busy === 'google' ? 'Opening browser…' : 'Connect Google'}
                </button>
              )}
            </Step>
          </div>

          {error && <div className="mb-4 rounded-btn px-3 py-2 text-sm pill-review">{error}</div>}

          {/* Steps 1-2 are all the app actually needs. Saying so here stops the exit
              button looking broken when it lights up with 3-4 still outstanding. */}
          {ready && !allConnected && (
            <div className="mb-4 rounded-btn px-3 py-2 text-sm pill-queued">
              You're ready to go. Steps 3 and 4 are only needed to read novels from
              Google Docs — you can add them later from Settings.
            </div>
          )}

          <div className="mt-2 flex items-center justify-between border-t border-line pt-5">
            <button onClick={() => run('refresh', async () => {})} className="btn btn-quiet text-sm">↻ Re-check</button>
            <div className="flex items-center gap-2">
              {/* Always enabled. This wizard replaces the whole app, so it must never
                  be possible to get stuck inside it — Google in particular is optional
                  and a user who only reads photos or pasted text never needs it. */}
              <button onClick={onDone} className="btn btn-quiet px-3 py-2 text-sm">
                Skip for now
              </button>
              <button onClick={onDone} disabled={!ready} className="btn btn-primary px-5 py-2">
                {ready ? 'Go to my library →' : 'Finish steps 1 and 2'}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
