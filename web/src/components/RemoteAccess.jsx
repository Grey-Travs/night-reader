import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { useToast } from '../toast'

// Turning the phone link into an icon on the home screen.
//
// The catch is not Night Reader's: Chrome will only install a web app from a **secure
// origin**, and a tailnet address over plain http is not one. So on Android the link as it
// stands can only make a browser shortcut that opens in a tab — the icon is there, but the
// address bar comes with it. A certificate is what upgrades it, and `tailscale serve` is
// the one way to get a real one here without putting the port on the internet.
//
// This only ever shows the command. Running it is the user's, because this machine may
// already be serving something else on 443 — and in the case that prompted this, it is.
function HomeScreen({ serve, local, onCopy }) {
  if (!local || !serve) return null
  const { tailnet_name: name, https_url: httpsUrl, serving_other: other, command, would_be: wouldBe } = serve

  if (!name) {
    return (
      <div className="space-y-1 border-t border-line pt-4 text-xs text-hint">
        <div className="text-sm font-medium text-ink">Put it on your home screen</div>
        <p>
          Tailscale isn’t reporting a name for this computer yet. Once it is connected,
          this is where the one command for a proper app icon appears.
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-2 border-t border-line pt-4">
      <div className="text-sm font-medium">Put it on your home screen</div>

      {httpsUrl ? (
        <>
          <p className="text-xs text-hint">
            Ready. Open this on the phone, then use Chrome’s menu → <b>Install app</b>. It
            gets its own icon and opens with no address bar.
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <code className="min-w-0 flex-1 truncate rounded px-2 py-1.5 font-mono text-xs"
              style={{ background: 'var(--b-muted-bg)' }}>{httpsUrl}/phone</code>
            <button className="btn btn-primary px-3 py-1.5 text-xs"
              onClick={() => onCopy(`${httpsUrl}/phone`, 'Link copied')}>
              Copy
            </button>
          </div>
        </>
      ) : (
        <>
          <p className="text-xs text-hint">
            Android only installs an app from an <b>https</b> address, and the link above is
            plain http — so as it stands you’d get a shortcut that opens in a browser tab.
            Tailscale can issue a real certificate for this computer. Run this once:
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <code className="min-w-0 flex-1 truncate rounded px-2 py-1.5 font-mono text-xs"
              style={{ background: 'var(--b-muted-bg)' }}>{command}</code>
            <button className="btn btn-ghost px-3 py-1.5 text-xs"
              onClick={() => onCopy(command, 'Command copied — run it in a terminal')}>
              Copy
            </button>
          </div>
          <p className="text-xs text-hint">
            Then open <code className="font-mono">{wouldBe}/phone</code> on the phone and
            choose <b>Install app</b>.
          </p>
          {Object.keys(other || {}).length > 0 && (
            <p className="text-xs text-hint">
              Using port {String(wouldBe).split(':').pop()} because{' '}
              <code className="font-mono">{Object.keys(other)[0]}</code> is already serving{' '}
              <code className="font-mono">{Object.values(other)[0]}</code> — that stays
              exactly as it is.
            </p>
          )}
          <p className="text-xs text-hint">
            Needs <b>HTTPS Certificates</b> switched on once for your tailnet, under{' '}
            <a className="text-accent-text hover:underline"
              href="https://login.tailscale.com/admin/dns" target="_blank" rel="noreferrer">
              DNS in the Tailscale admin console
            </a>. If the command complains about a certificate, that is what it wants.
          </p>
          <p className="text-xs text-hint">
            The access key still applies over https — a request proxied by Tailscale does
            not arrive from this machine, so it is checked like any other. That is why the
            installed app’s own start address carries the key: it keeps working even after
            the browser forgets the cookie.
          </p>
        </>
      )}
    </div>
  )
}

// Reaching this app from a phone, so a posting run can be started from anywhere.
//
// The run request is what makes that work at all: pressing Start writes a file, and
// whichever Chrome is open on the computer picks it up on its next poll. So there is no
// phone-specific posting code — the only thing missing was a way to reach this interface.
//
// Which is also the risk, and why this screen is wordy. The API has no accounts; being
// bound to loopback IS its authentication. Switching this on gives it an access key and
// starts refusing anything from another device that does not carry it.
export default function RemoteAccess() {
  const toast = useToast()
  const [state, setState] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [notifyUrl, setNotifyUrl] = useState('')
  const [showKey, setShowKey] = useState(false)

  const load = useCallback(async () => {
    try {
      const d = await api.remote()
      setState(d)
      setNotifyUrl(d.notify_url || '')
    } catch (e) { setError(String(e.message || e)) }
  }, [])

  useEffect(() => { load() }, [load])

  async function change(patch, note) {
    setBusy(true); setError(null)
    try {
      const d = await api.updateRemote(patch)
      setState(d)
      setNotifyUrl(d.notify_url || '')
      if (note) toast(note)
    } catch (e) { setError(String(e.message || e)) } finally { setBusy(false) }
  }

  async function copy(text, note) {
    try {
      await navigator.clipboard.writeText(text)
      toast(note)
    } catch { setError('Could not copy — select the text instead.') }
  }

  if (!state) {
    return (
      <section className="card mt-6 space-y-4 p-6">
        <h2 className="text-base font-medium">Use from your phone</h2>
        <div className="text-hint">Loading…</div>
      </section>
    )
  }

  const { enabled, local, addresses = [] } = state
  const tailscale = addresses.find((a) => a.kind === 'tailscale')

  return (
    <section className="card mt-6 space-y-4 p-6">
      <h2 className="text-base font-medium">Use from your phone</h2>
      <p className="text-sm text-muted">
        Start a posting run from anywhere. Your computer does the posting, exactly as it
        does now — the run waits in a file until the browser here picks it up, so nothing
        has to be open on the phone but this page.
      </p>

      {error && <div className="rounded-btn px-3 py-2 text-sm pill-review">{error}</div>}

      {!local && (
        <div className="rounded-btn px-3 py-2 text-xs pill-review">
          You’re looking at this from another device. The access key and its link are shown
          only on the computer running Night Reader.
        </div>
      )}

      <label className="flex items-start gap-3 text-sm">
        <input
          type="checkbox"
          className="mt-0.5"
          checked={enabled}
          disabled={busy}
          onChange={(e) => change({ enabled: e.target.checked },
            e.target.checked ? 'Other devices allowed — restart the app' : 'Phone access off')}
        />
        <span>
          <span className="font-medium">Let other devices reach this app</span>
          <span className="mt-0.5 block text-xs text-hint">
            Off by default. While it’s off, Night Reader answers only this computer and a
            phone cannot reach it at all.
          </span>
        </span>
      </label>

      {enabled && (
        <>
          <div className="rounded-btn px-3 py-2 text-xs text-muted"
            style={{ background: 'var(--b-muted-bg)' }}>
            <span className="font-medium">Restart the app after switching this on.</span>{' '}
            The port it listens on is chosen at startup, so the change takes effect the next
            time you run it.
          </div>

          {local && state.phone_url ? (
            <div className="space-y-2">
              <div className="text-sm font-medium">Open this on your phone, once</div>
              <div className="flex flex-wrap items-center gap-2">
                <code className="min-w-0 flex-1 truncate rounded px-2 py-1.5 font-mono text-xs"
                  style={{ background: 'var(--b-muted-bg)' }}>{state.phone_url}</code>
                <button className="btn btn-primary px-3 py-1.5 text-xs"
                  onClick={() => copy(state.phone_url, 'Link copied — send it to your phone')}>
                  Copy link
                </button>
              </div>
              <p className="text-xs text-hint">
                The key is in that link, so the phone only needs it once — it’s remembered
                afterwards. Send it to yourself however you like, then bookmark the page.
                Treat it like a password: anyone with the link can use your library.
              </p>
            </div>
          ) : local ? (
            <div className="rounded-btn px-3 py-2 text-xs pill-review">
              No address to reach this computer on was found yet. Connect Tailscale (below),
              or add the name you use for this machine under Advanced.
            </div>
          ) : null}

          {addresses.length > 0 && (
            <div className="text-xs text-hint">
              Reachable at{' '}
              {addresses.map((a, i) => (
                <span key={a.host}>
                  {i > 0 && ', '}
                  <code className="font-mono">{a.host}</code>
                  <span> ({a.kind})</span>
                </span>
              ))}
              {!tailscale && (
                <span className="mt-1 block">
                  Only a local-network address, so this works at home but not away from it.
                </span>
              )}
            </div>
          )}

          <HomeScreen serve={state.serve} local={local} onCopy={copy} />

          <div className="space-y-2 border-t border-line pt-4">
            <div className="text-sm font-medium">Tell me when a run stops</div>
            <p className="text-xs text-hint">
              The reason to start a run remotely is that nobody is at the desk — so if it
              stops on an expired session, you want to hear about it. Paste a{' '}
              <a className="text-accent-text hover:underline" href="https://ntfy.sh"
                target="_blank" rel="noreferrer">ntfy.sh</a>{' '}
              topic address (no account needed — pick an unguessable topic name and
              subscribe to it in their app), or any URL that accepts a POST.
            </p>
            <div className="flex flex-wrap items-center gap-2">
              <input
                type="text"
                className="input min-w-0 flex-1 text-xs"
                placeholder="https://ntfy.sh/your-private-topic-name"
                value={notifyUrl}
                onChange={(e) => setNotifyUrl(e.target.value)}
              />
              <button
                className="btn btn-ghost px-3 py-1.5 text-xs"
                disabled={busy || notifyUrl === (state.notify_url || '')}
                onClick={() => change({ notify_url: notifyUrl }, 'Notification address saved')}
              >
                Save
              </button>
              <button
                className="btn btn-ghost px-3 py-1.5 text-xs"
                disabled={busy || !state.notify_url}
                onClick={async () => {
                  try {
                    await api.testRemoteNotification()
                    toast('Test sent — check your phone')
                  } catch (e) { setError(String(e.message || e)) }
                }}
              >
                Send a test
              </button>
            </div>
          </div>

          {local && (
            <details className="text-xs text-hint">
              <summary className="cursor-pointer">Advanced</summary>
              <div className="mt-3 space-y-3">
                <div>
                  <div className="mb-1">Access key</div>
                  <div className="flex flex-wrap items-center gap-2">
                    <code className="min-w-0 flex-1 truncate rounded px-2 py-1 font-mono"
                      style={{ background: 'var(--b-muted-bg)' }}>
                      {showKey ? state.token : '•'.repeat(32)}
                    </code>
                    <button className="btn btn-ghost px-2 py-1" onClick={() => setShowKey((v) => !v)}>
                      {showKey ? 'Hide' : 'Show'}
                    </button>
                    <button className="btn btn-ghost px-2 py-1"
                      disabled={busy}
                      onClick={() => change({ rotate: true }, 'New key — old links stopped working')}>
                      Replace
                    </button>
                  </div>
                  <p className="mt-1">
                    Replacing it stops every link you’ve already sent from working, which is
                    what you want if one went somewhere it shouldn’t have.
                  </p>
                </div>
                <p>
                  While this is on, the app listens on every network this computer joins, and
                  the key is what protects it — which is why Tailscale is worth the setup:
                  on a tailnet, only your own signed-in devices can see the port at all.
                </p>
              </div>
            </details>
          )}
        </>
      )}

      {enabled && !local && (
        <p className="text-xs text-hint">
          Add this page to your home screen from the browser menu.
        </p>
      )}

      {!enabled && (
        <details className="text-xs text-hint">
          <summary className="cursor-pointer">What you’ll need to set up first</summary>
          <ol className="mt-2 ml-4 list-decimal space-y-1">
            <li>
              Install <a className="text-accent-text hover:underline" href="https://tailscale.com/download"
                target="_blank" rel="noreferrer">Tailscale</a> on this computer and on your
              phone, and sign in to the same account on both. That is the only account this
              needs, and it is what makes the app reachable away from home without putting
              it on the public internet.
            </li>
            <li>Switch the box above on, then restart Night Reader.</li>
            <li>Copy the link that appears here and open it once on your phone.</li>
          </ol>
        </details>
      )}
    </section>
  )
}
