import { useState } from 'react'
import { api } from '../api'
import { useToast } from '../toast'

// Where a series gets published, and on what terms.
//
// This is what the browser extension looks up: it knows the page it is on and asks the app
// which series publishes there, so the URL saved here is what ties the two together.
//
// The free/paid cutoff is the setting worth being careful with. It is painful to change on
// the site once readers have been through a chapter, so it is stored per series rather than
// retyped per run, and the posting preview spells out exactly which chapters each side of
// the line before anything is sent.
export default function SeriesPublishTarget({ sid, targets, onSaved }) {
  const toast = useToast()
  const existing = (targets || [])[0] || null
  const [url, setUrl] = useState(existing?.series_url || '')
  const [freeThrough, setFreeThrough] = useState(existing?.free_through ?? 0)
  const [coinPrice, setCoinPrice] = useState(existing?.coin_price ?? 0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const dirty = url !== (existing?.series_url || '')
    || Number(freeThrough) !== Number(existing?.free_through ?? 0)
    || Number(coinPrice) !== Number(existing?.coin_price ?? 0)

  async function save() {
    setBusy(true)
    setError(null)
    try {
      await api.updateSeries(sid, {
        publish_targets: [{
          id: existing?.id || 't1',
          site: 'meiko',
          series_url: url.trim(),
          free_through: Number(freeThrough) || 0,
          coin_price: Number(coinPrice) || 0,
        }],
      })
      toast('Publishing details saved')
      onSaved?.()
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="mt-8 rounded-card border border-line p-3">
      <h2 className="text-sm font-medium">Publishing</h2>
      <p className="mt-0.5 text-xs text-hint">
        The series’ admin page on meiko.studio. The browser extension matches the page
        you’re on against this to know which novel it’s looking at.
      </p>

      {error && (
        <div className="mt-3 rounded-card px-3 py-2 text-sm pill-review">
          {error.message || String(error)}
        </div>
      )}

      <label className="mt-3 block text-xs text-muted">
        Admin page
        <input
          type="text"
          className="mt-1 w-full rounded border border-line bg-transparent px-2 py-1 text-xs"
          placeholder="https://meiko.studio/page/…/series/…/"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
        />
      </label>

      <div className="mt-3 flex flex-wrap gap-4">
        <label className="text-xs text-muted">
          Free through chapter
          <input
            type="number"
            className="mt-1 block w-28 rounded border border-line bg-transparent px-2 py-1 text-xs"
            value={freeThrough}
            onChange={(e) => setFreeThrough(e.target.value)}
          />
        </label>
        <label className="text-xs text-muted">
          Coins after that
          <input
            type="number"
            className="mt-1 block w-28 rounded border border-line bg-transparent px-2 py-1 text-xs"
            value={coinPrice}
            onChange={(e) => setCoinPrice(e.target.value)}
          />
        </label>
      </div>

      <p className="mt-2 text-xs text-hint">
        Chapter {Number(freeThrough) || 0} and below post free; everything after costs{' '}
        {Number(coinPrice) || 0} coins. You can read both numbers off the site’s own
        chapter list.
      </p>

      <button
        className="btn btn-primary mt-3 px-3 py-1.5 text-xs"
        disabled={!dirty || busy}
        onClick={save}
      >
        {busy ? 'Saving…' : 'Save publishing details'}
      </button>
    </section>
  )
}
