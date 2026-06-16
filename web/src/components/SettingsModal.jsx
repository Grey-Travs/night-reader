import { useEffect, useState } from 'react'
import { api } from '../api'
import { Modal } from './ui'

const MODELS = [
  { id: 'claude-opus-4-8', label: 'Opus — best quality (recommended)' },
  { id: 'claude-sonnet-4-6', label: 'Sonnet — faster, lighter' },
  { id: 'claude-haiku-4-5', label: 'Haiku — fastest, simplest' },
]
const EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max']

export default function SettingsModal({ onClose }) {
  const [s, setS] = useState(null)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState(null)

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

  return (
    <Modal onClose={onClose}>
      <div className="flex items-center justify-between border-b border-line px-6 py-4">
        <h3 className="font-medium">Settings</h3>
        <button onClick={onClose} className="btn btn-quiet text-lg leading-none">✕</button>
      </div>
      <div className="space-y-5 px-6 py-5">
        {error && <div className="rounded-btn px-3 py-2 text-sm pill-review">{error}</div>}
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
      </div>
    </Modal>
  )
}
