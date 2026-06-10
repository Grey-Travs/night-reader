import { useEffect, useState } from 'react'
import { api } from '../api'
import { Modal } from './ui'

const TYPES = ['name', 'place', 'skill', 'term', 'other']

export default function GlossaryPanel({ pid, onClose, onChanged }) {
  const [data, setData] = useState(null)
  const [drafts, setDrafts] = useState({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function load() {
    const d = await api.glossary(pid)
    setData(d)
    const init = {}
    for (const p of d.pending) init[p.korean] = { english: p.english, type: p.type || 'other', note: p.note || '' }
    setDrafts(init)
  }
  useEffect(() => { load().catch((e) => setError(String(e.message || e))) }, [pid])

  function edit(korean, field, value) {
    setDrafts((d) => ({ ...d, [korean]: { ...d[korean], [field]: value } }))
  }

  async function decide(approve, reject) {
    setBusy(true)
    setError(null)
    try {
      await api.reviewGlossary(pid, { approve, reject })
      await load()
      onChanged?.()
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }

  const approveOne = (p) => decide([{ korean: p.korean, ...drafts[p.korean] }], [])
  const rejectOne = (p) => decide([], [p.korean])
  const approveAll = () => decide((data?.pending || []).map((p) => ({ korean: p.korean, ...drafts[p.korean] })), [])

  return (
    <Modal onClose={onClose} wide>
      <div className="flex items-center justify-between border-b border-line px-6 py-4">
        <h3 className="font-medium">Glossary</h3>
        <button onClick={onClose} className="btn btn-quiet text-lg leading-none">✕</button>
      </div>

      <div className="max-h-[70vh] overflow-y-auto px-6 py-5">
        {error && <div className="mb-3 rounded-btn px-3 py-2 text-sm pill-review">{error}</div>}

        <div className="mb-6">
          <div className="mb-2 flex items-center justify-between">
            <h4 className="text-sm font-medium text-muted">New terms to review {data ? `(${data.pending.length})` : ''}</h4>
            {data?.pending.length > 0 && (
              <button onClick={approveAll} disabled={busy} className="btn btn-primary px-3 py-1.5 text-xs">Approve all</button>
            )}
          </div>
          {data?.pending.length === 0 && (
            <div className="sunken px-3 py-2 text-sm text-muted">Nothing waiting. New names appear here after translating.</div>
          )}
          <div className="space-y-2">
            {data?.pending.map((p) => (
              <div key={p.korean} className="rounded-card border border-line p-3">
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <span className="font-korean font-medium">{p.korean}</span>
                  <span className="text-hint">→</span>
                  <input value={drafts[p.korean]?.english ?? ''} onChange={(e) => edit(p.korean, 'english', e.target.value)} className="input min-w-[10rem] flex-1 !py-1" />
                  <select value={drafts[p.korean]?.type ?? 'other'} onChange={(e) => edit(p.korean, 'type', e.target.value)} className="input !py-1">
                    {TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                  </select>
                  {p.chapter && <span className="text-xs text-hint">ch.{p.chapter}</span>}
                </div>
                {(p.note || p.conflict_with) && (
                  <div className="mt-1 text-xs text-muted">
                    {p.conflict_with && <span className="font-medium" style={{ color: 'var(--b-review-tx)' }}>conflict with “{p.conflict_with}” · </span>}
                    {p.note}
                  </div>
                )}
                <div className="mt-2 flex gap-2">
                  <button onClick={() => approveOne(p)} disabled={busy} className="btn px-3 py-1 text-xs" style={{ background: 'var(--b-translated-bg)', color: 'var(--b-translated-tx)' }}>Approve</button>
                  <button onClick={() => rejectOne(p)} disabled={busy} className="btn btn-ghost px-3 py-1 text-xs">Reject</button>
                </div>
              </div>
            ))}
          </div>
        </div>

        <h4 className="mb-2 text-sm font-medium text-muted">Locked terms {data ? `(${data.locked.length})` : ''}</h4>
        <div className="overflow-x-auto rounded-card border border-line">
          <table className="w-full min-w-[380px] text-sm">
            <tbody>
              {data?.locked.map((e) => (
                <tr key={e.korean} className="border-t border-line first:border-t-0">
                  <td className="px-3 py-1.5 font-korean font-medium">{e.korean}</td>
                  <td className="px-3 py-1.5">{e.english}</td>
                  <td className="px-3 py-1.5 text-xs text-hint">{e.type}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </Modal>
  )
}
