import { useEffect, useState } from 'react'
import { api } from '../api'
import { Modal } from './ui'

// Module-level so its component identity is stable across renders — defining it
// inside the parent would remount the inputs on every keystroke (focus loss).
function Field({ label, hint, children }) {
  return (
    <label className="block">
      <span className="text-sm font-medium">{label}</span>
      {hint && <span className="mt-0.5 block text-xs text-hint">{hint}</span>}
      <div className="mt-1.5">{children}</div>
    </label>
  )
}

// Per-novel translation settings: name + the style knobs that steer the engine
// (genre/tone framing, free-form instructions, honorific handling).
export default function ProjectSettingsModal({ pid, project, onClose, onSaved }) {
  const [form, setForm] = useState({
    name: project?.name || '',
    style_note: project?.style_note || '',
    instructions: project?.instructions || '',
    honorific_note: project?.honorific_note || '',
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  // Load the effective values (project overrides merged over global defaults), so the
  // honorific box shows the real default rather than appearing empty.
  useEffect(() => {
    api.getProject(pid).then((p) => setForm({
      name: p.name || '', style_note: p.style_note || '',
      instructions: p.instructions || '', honorific_note: p.honorific_note || '',
    })).catch(() => {})
  }, [pid])

  function set(field, value) { setForm((f) => ({ ...f, [field]: value })) }

  async function save() {
    setBusy(true)
    setError(null)
    try {
      const updated = await api.updateProject(pid, form)
      onSaved?.(updated)
      onClose()
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal onClose={onClose}>
      <div className="flex items-center justify-between border-b border-line px-6 py-4">
        <h3 className="font-medium">Novel settings</h3>
        <button onClick={onClose} className="btn btn-quiet text-lg leading-none">✕</button>
      </div>
      <div className="space-y-5 px-6 py-5">
        {error && <div className="rounded-btn px-3 py-2 text-sm pill-review">{error}</div>}

        <Field label="Name">
          <input value={form.name} onChange={(e) => set('name', e.target.value)} className="input w-full" />
        </Field>

        <Field label="Style & genre" hint="Framing for the translator — genre, tone, audience. Leave blank for a neutral web novel. e.g. “A wuxia action serial; keep the heroic, fast-paced tone.”">
          <textarea value={form.style_note} onChange={(e) => set('style_note', e.target.value)} rows={2} className="input w-full" placeholder="e.g. A boy's love (BL) web novel; all characters are adults." />
        </Field>

        <Field label="Custom instructions" hint="Applied to every chapter. e.g. “Render sound effects in italics; the protagonist speaks formally.”">
          <textarea value={form.instructions} onChange={(e) => set('instructions', e.target.value)} rows={2} className="input w-full" />
        </Field>

        <Field label="Honorifics" hint="How to handle Korean honorifics and address particles.">
          <textarea value={form.honorific_note} onChange={(e) => set('honorific_note', e.target.value)} rows={2} className="input w-full" />
        </Field>

        <div className="flex justify-end gap-2 border-t border-line pt-4">
          <button onClick={onClose} disabled={busy} className="btn btn-ghost px-4 py-2 text-sm">Cancel</button>
          <button onClick={save} disabled={busy} className="btn btn-primary px-4 py-2 text-sm">{busy ? 'Saving…' : 'Save'}</button>
        </div>
        <p className="text-xs text-hint">Changes apply to chapters translated from now on. Use “Re-translate” to apply them to existing chapters.</p>
      </div>
    </Modal>
  )
}
