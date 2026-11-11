import { useEffect, useState } from 'react'
import { api } from '../api'

// Settings → This computer: keeping it ready for the phone. Started with Windows, the
// app is there after a reboot with nobody at the computer; kept awake, a chapter sent in
// the evening is read and translated without the computer sleeping halfway.
export default function ThisComputer() {
  const [state, setState] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => { api.computer().then(setState).catch((e) => setError(e.message || String(e))) }, [])

  async function change(patch) {
    setBusy(true)
    setError(null)
    try {
      setState(await api.updateComputer(patch))
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="card mt-6 space-y-4 p-6" aria-label="This computer">
      <h2 className="text-base font-medium">This computer</h2>
      {error && <div className="rounded-btn px-3 py-2 text-sm pill-review">{error}</div>}
      {!state ? (
        <div className="text-hint">Loading…</div>
      ) : (
        <>
          {state.autostart?.supported && (
            <label className="flex items-start gap-3 text-sm">
              <input type="checkbox" className="mt-0.5" checked={!!state.autostart.on} disabled={busy}
                onChange={(e) => change({ autostart: e.target.checked })} />
              <span>
                <span className="font-medium">Start Night Reader with Windows</span>
                <span className="mt-0.5 block text-xs text-hint">
                  It starts quietly when you sign in — no window, no browser tab — so photos
                  from the phone go straight in after a restart. Task Manager → Startup apps
                  can switch it off too.
                </span>
                {state.autostart.on && !state.autostart.current && (
                  <span className="mt-1 block text-xs pill-review rounded-btn px-2 py-1">
                    It points at another copy of Night Reader. Switch it off and on again to use this one.
                  </span>
                )}
              </span>
            </label>
          )}
          <label className="flex items-start gap-3 text-sm">
            <input type="checkbox" className="mt-0.5" checked={state.keep_awake !== false} disabled={busy}
              onChange={(e) => change({ keep_awake: e.target.checked })} />
            <span>
              <span className="font-medium">Keep the computer awake while it’s working</span>
              <span className="mt-0.5 block text-xs text-hint">
                While pages are being read or chapters translated, and for half an hour after
                a photo arrives, the computer doesn’t go to sleep. The screen still turns off,
                and closing a laptop’s lid still sleeps it.
              </span>
            </span>
          </label>
          <p className="text-xs text-hint">
            A computer that is already asleep can’t be woken from the phone: the photos wait
            on the phone until it is awake. If you shoot away from home, set Windows to never
            sleep when plugged in (Settings → System → Power).
          </p>
        </>
      )}
    </section>
  )
}
