import { useEffect, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { api } from '../api'
import { Dot } from '../components/ui'

const MODELS = [
  { id: 'claude-opus-4-8', label: 'Opus — best quality (recommended)' },
  { id: 'claude-sonnet-4-6', label: 'Sonnet — faster, lighter' },
  { id: 'claude-haiku-4-5', label: 'Haiku — fastest, simplest' },
]
const EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max']

export default function SettingsPage() {
  const { status, setStatus, onSetup } = useOutletContext()
  const [s, setS] = useState(null)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState(null)
  const [connecting, setConnecting] = useState(false)

  useEffect(() => { api.settings().then(setS).catch((e) => setError(String(e.message || e))) }, [])

  async function save(patch) {
    setError(null)
    setS((prev) => ({ ...prev, ...patch }))
    try {
      await api.updateSettings(patch)
      setSaved(true)
      setTimeout(() => setSaved(false), 1800)
    } catch (e) {
      setError(String(e.message || e))
    }
  }

  async function reconnectGoogle() {
    setConnecting(true); setError(null)
    try {
      await api.googleLogin()
      setStatus?.(await api.status())
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setConnecting(false)
    }
  }

  return (
    <div className="page page-narrow">
      <h1 className="mb-6 font-reading text-2xl font-medium">Settings</h1>
      {error && <div className="mb-4 rounded-btn px-3 py-2 text-sm pill-review">{error}</div>}

      <section className="card mb-6 space-y-5 p-6">
        <h2 className="text-base font-medium">Translation</h2>
        {!s ? (
          <div className="text-hint">Loading…</div>
        ) : (
          <>
            <label className="block">
              <span className="text-sm font-medium">Translation model</span>
              <select value={s.model} onChange={(e) => save({ model: e.target.value })} className="input mt-1 w-full">
                {MODELS.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
                {!MODELS.some((m) => m.id === s.model) && <option value={s.model}>{s.model}</option>}
              </select>
            </label>

            <label className="block">
              <span className="text-sm font-medium">Effort</span>
              <select value={s.effort} onChange={(e) => save({ effort: e.target.value })} className="input mt-1 w-full">
                {EFFORTS.map((e) => <option key={e} value={e}>{e}</option>)}
              </select>
              <span className="mt-1 block text-xs text-hint">Higher effort = more careful translation, more plan usage. “high” is a good default.</span>
            </label>

            <label className="block">
              <span className="text-sm font-medium">AI deep-check after translation</span>
              <select value={s.deep_check || 'flagged'} onChange={(e) => save({ deep_check: e.target.value })} className="input mt-1 w-full">
                <option value="off">Off — fast checks only</option>
                <option value="flagged">Flagged chapters only (cheaper)</option>
                <option value="always">Every chapter (max safety)</option>
              </select>
              <span className="mt-1 block text-xs text-hint">
                A second AI pass that reads each finished chapter for stray notes/leaks the fast checks can miss, and removes them automatically.
                “Every chapter” catches the most but adds ~1 Claude call per chapter.
              </span>
            </label>

            <div className="text-xs text-hint">
              Validation thresholds and other tunables live in <code className="rounded px-1 font-mono" style={{ background: 'var(--b-muted-bg)' }}>config.toml</code>.
            </div>
            {saved && <div className="text-sm" style={{ color: 'var(--accent-text)' }}>Saved · applies to the next chapter.</div>}
          </>
        )}
      </section>

      <section className="card space-y-4 p-6">
        <h2 className="text-base font-medium">Connections</h2>
        <div className="flex flex-wrap items-center gap-4 text-sm">
          <span className="flex items-center gap-1.5"><Dot ok={status?.google_logged_in} /> Google {status?.google_logged_in ? 'connected' : 'not connected'}</span>
          <span className="flex items-center gap-1.5"><Dot ok={status?.claude_logged_in} /> Claude {status?.claude_logged_in ? 'connected' : 'not connected'}</span>
        </div>
        <div className="flex flex-wrap gap-2">
          <button onClick={reconnectGoogle} disabled={connecting} className="btn btn-ghost px-4 py-2 text-sm">{connecting ? 'Opening Google…' : 'Reconnect Google'}</button>
          <button onClick={() => onSetup?.()} className="btn btn-ghost px-4 py-2 text-sm">Run full setup…</button>
        </div>
        <p className="text-xs text-hint">Google is needed only for reading Google Docs. Pasted-text novels work without it.</p>
      </section>
    </div>
  )
}
