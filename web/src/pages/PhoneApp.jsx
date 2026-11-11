import { Suspense, useEffect } from 'react'
import { Outlet } from 'react-router-dom'
import { outboxReady } from '../outbox'

// The installed phone app: every screen under /phone, which is the manifest's scope, so
// nothing here ever leaves the app for the desktop site.
//
// It waits for nothing from the computer before drawing. The computer may be asleep, and
// the camera and the photos saved on the phone have to work regardless.
//
// It keeps the outbox sending: when the app opens, when the signal comes back, and when
// the app comes back to the front — the moments a photo that couldn't go before can.
export default function PhoneApp() {
  useEffect(() => {
    let box = null
    let alive = true
    let persisted = false
    const kick = () => box?.pump()
    const back = () => box?.retryNow()
    const visible = () => { if (document.visibilityState === 'visible') back() }
    outboxReady().then((opened) => {
      if (!alive) return
      box = opened
      // Ask once that the browser never clears saved photos to free space.
      const unsubscribe = box.subscribe(() => {
        if (persisted || !box.getSnapshot().unsent) return
        persisted = true
        navigator.storage?.persist?.().catch(() => {})
        unsubscribe()
      })
    }).catch(() => {})
    window.addEventListener('online', back)
    window.addEventListener('focus', kick)
    document.addEventListener('visibilitychange', visible)
    return () => {
      alive = false
      window.removeEventListener('online', back)
      window.removeEventListener('focus', kick)
      document.removeEventListener('visibilitychange', visible)
    }
  }, [])

  // A copy of the app kept on the phone (public/phone-sw.js), so it opens with the
  // computer asleep. Only over https: a service worker needs a secure context. The other
  // screens are fetched once now so that the copy has them too.
  useEffect(() => {
    if (!globalThis.isSecureContext || !navigator.serviceWorker?.register) return
    navigator.serviceWorker.register('/phone-sw.js', { scope: '/phone', updateViaCache: 'none' })
      .then(() => Promise.all([import('./PhoneCapturePage'), import('./PhoneCheckPage')]))
      .catch(() => {})
  }, [])

  return (
    <Suspense fallback={<p className="py-10 text-center text-sm text-hint">Loading…</p>}>
      <Outlet />
    </Suspense>
  )
}
