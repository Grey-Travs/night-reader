import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { Modal } from './ui'

const TYPES = ['name', 'place', 'skill', 'term', 'other']
const BLANK = { korean: '', english: '', type: 'name', note: '', pronoun: '', register: '' }

// Minimal RFC-4180-ish CSV parser (handles quotes, commas and newlines in fields).
function parseCsv(text) {
  const rows = []
  let row = [], cur = '', q = false
  for (let i = 0; i < text.length; i++) {
    const c = text[i]
    if (q) {
      if (c === '"') { if (text[i + 1] === '"') { cur += '"'; i++ } else q = false }
      else cur += c
    } else if (c === '"') q = true
    else if (c === ',') { row.push(cur); cur = '' }
    else if (c === '\n') { row.push(cur); rows.push(row); row = []; cur = '' }
    else if (c !== '\r') cur += c
  }
  if (cur !== '' || row.length) { row.push(cur); rows.push(row) }
  return rows.filter((r) => r.some((x) => x.trim() !== ''))
}

function parseGlossaryFile(name, text) {
  if (name.toLowerCase().endsWith('.json') || text.trim().startsWith('[')) {
    const arr = JSON.parse(text)
    return (Array.isArray(arr) ? arr : []).map((e) => ({
      korean: e.korean || '', english: e.english || '', type: e.type || 'other',
      note: e.note || '', pronoun: e.pronoun || '', register: e.register || '',
    }))
  }
  const rows = parseCsv(text)
  if (!rows.length) return []
  const header = rows[0].map((h) => h.trim().toLowerCase())
  const hasHeader = header.includes('korean') && header.includes('english')
  const at = (n) => header.indexOf(n)
  const col = hasHeader
    ? { korean: at('korean'), english: at('english'), type: at('type'), note: at('note'), pronoun: at('pronoun'), register: at('register') }
    : { korean: 0, english: 1, type: 2, note: 5, pronoun: 3, register: 4 }
  const body = hasHeader ? rows.slice(1) : rows
  const cell = (r, i) => (i >= 0 && i < r.length ? (r[i] || '').trim() : '')
  return body.map((r) => ({
    korean: cell(r, col.korean), english: cell(r, col.english),
    type: cell(r, col.type) || 'other', note: cell(r, col.note),
    pronoun: cell(r, col.pronoun), register: cell(r, col.register),
  }))
}

export default function GlossaryPanel({ pid, onClose, onChanged, onRetranslate }) {
  const [data, setData] = useState(null)
  const [drafts, setDrafts] = useState({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const [editing, setEditing] = useState(null)
  const [editDraft, setEditDraft] = useState(BLANK)
  const [editOrig, setEditOrig] = useState({ korean: '', english: '' })
  const [adding, setAdding] = useState(false)
  const [newTerm, setNewTerm] = useState(BLANK)
  const [query, setQuery] = useState('')
  const [affected, setAffected] = useState(null)
  const [learning, setLearning] = useState(false)
  const [learnMsg, setLearnMsg] = useState(null)
  const fileRef = useRef(null)

  // Stable id for a locked entry: a mapped term is keyed by Korean, a canonical
  // English-only name by its English spelling.
  const entryId = (e) => (e.korean ? `k:${e.korean}` : `e:${e.english}`)

  // Pending rows are keyed by Korean+English: a conflict (same Korean, two English
  // spellings) keeps both proposals independently editable instead of colliding.
  const pkey = (p) => `${p.korean}|${p.english}`

  async function load() {
    const d = await api.glossary(pid)
    setData(d)
    const init = {}
    for (const p of d.pending) init[pkey(p)] = { english: p.english, type: p.type || 'other', note: p.note || '' }
    setDrafts(init)
  }
  useEffect(() => { load().catch((e) => setError(String(e.message || e))) }, [pid])

  function edit(key, field, value) {
    setDrafts((d) => ({ ...d, [key]: { ...d[key], [field]: value } }))
  }

  // ---- pending review queue --------------------------------------------------
  async function decide(approve, reject) {
    setBusy(true); setError(null)
    try { await api.reviewGlossary(pid, { approve, reject }); await load(); onChanged?.() }
    catch (e) { setError(String(e.message || e)) }
    finally { setBusy(false) }
  }
  const approveOne = (p) => decide([{ korean: p.korean, ...drafts[pkey(p)] }], [])
  const rejectOne = (p) => decide([], [p.korean])
  const approveAll = () => decide((data?.pending || []).map((p) => ({ korean: p.korean, ...drafts[pkey(p)] })), [])

  // ---- locked-term add / edit / delete / import / export ---------------------
  async function saveTerm(body) {
    setBusy(true); setError(null)
    try {
      const d = await api.saveGlossaryTerm(pid, body)
      setData((cur) => cur && { ...cur, locked: d.locked })
      if (d.affected?.length) setAffected(d.affected)
      return true
    } catch (e) { setError(String(e.message || e)); return false }
    finally { setBusy(false) }
  }

  async function addTerm() {
    if (!newTerm.english.trim()) { setError('Enter at least the English spelling.'); return }
    if (await saveTerm(newTerm)) { setNewTerm(BLANK); setAdding(false) }
  }
  function startEdit(e) {
    setEditing(entryId(e))
    setEditOrig({ korean: e.korean || '', english: e.english || '' })
    setEditDraft({ korean: e.korean || '', english: e.english, type: e.type || 'other', note: e.note || '', pronoun: e.pronoun || '', register: e.register || '' })
  }
  async function saveEdit() {
    if (!editDraft.english.trim()) { setError('Enter at least the English spelling.'); return }
    if (await saveTerm({ ...editDraft, original_korean: editOrig.korean, original_english: editOrig.english })) setEditing(null)
  }
  async function removeTerm(e) {
    if (!window.confirm(`Remove “${e.korean || e.english}” from the glossary?`)) return
    setBusy(true); setError(null)
    try {
      const d = await api.deleteGlossaryTerm(pid, { korean: e.korean || '', english: e.english || '' })
      setData((cur) => cur && { ...cur, locked: d.locked })
      if (editing === entryId(e)) setEditing(null)
    } catch (err) { setError(String(err.message || err)) }
    finally { setBusy(false) }
  }

  // Read the already-English chapters and pull their established names into the glossary.
  async function learnNames() {
    setLearning(true); setError(null); setLearnMsg(null)
    try {
      const d = await api.learnGlossary(pid)
      setData((cur) => cur && { ...cur, locked: d.locked })
      setLearnMsg(`Found ${d.learned} new name${d.learned === 1 ? '' : 's'} from ${d.from_chapters} English chapter${d.from_chapters === 1 ? '' : 's'}.`)
    } catch (e) { setError(String(e.message || e)) }
    finally { setLearning(false) }
  }

  async function onImportFile(e) {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    setBusy(true); setError(null)
    try {
      const entries = parseGlossaryFile(file.name, await file.text()).filter((x) => x.english)
      if (!entries.length) { setError('No valid rows (need at least an English column).'); return }
      const d = await api.importGlossary(pid, { entries, mode: 'merge' })
      setData((cur) => cur && { ...cur, locked: d.locked })
    } catch (e) { setError('Import failed: ' + String(e.message || e)) }
    finally { setBusy(false) }
  }

  const locked = data?.locked || []
  const q = query.trim().toLowerCase()
  const shownLocked = q
    ? locked.filter((e) => (e.korean + e.english + (e.note || '')).toLowerCase().includes(q))
    : locked

  const fieldRow = (term, onField) => (
    <>
      <input value={term.korean} onChange={(e) => onField('korean', e.target.value)} placeholder="Korean (optional)" className="input font-korean min-w-[6rem] flex-1 !py-1" />
      <span className="text-hint">→</span>
      <input value={term.english} onChange={(e) => onField('english', e.target.value)} placeholder="English" className="input min-w-[7rem] flex-1 !py-1" />
      <select value={term.type} onChange={(e) => onField('type', e.target.value)} className="input !py-1">
        {TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
      </select>
      <input value={term.pronoun} onChange={(e) => onField('pronoun', e.target.value)} placeholder="he/she/they" className="input w-24 !py-1" title="Pronoun (for characters)" />
      <input value={term.register} onChange={(e) => onField('register', e.target.value)} placeholder="register" className="input w-24 !py-1" title="Speech register (formal/casual…)" />
      <input value={term.note} onChange={(e) => onField('note', e.target.value)} placeholder="Note" className="input min-w-[6rem] flex-1 !py-1" />
    </>
  )

  return (
    <Modal onClose={onClose} wide>
      <div className="flex items-center justify-between border-b border-line px-6 py-4">
        <h3 className="font-medium">Glossary</h3>
        <button onClick={onClose} className="btn btn-quiet text-lg leading-none">✕</button>
      </div>

      <div className="max-h-[72vh] overflow-y-auto px-6 py-5">
        {error && <div className="mb-3 rounded-btn px-3 py-2 text-sm pill-review">{error}</div>}

        {affected && (
          <div className="mb-4 rounded-card border border-line p-3 text-sm" style={{ background: 'var(--b-queued-bg)', color: 'var(--b-queued-tx)' }}>
            <strong>{affected.length}</strong> already-translated chapter{affected.length === 1 ? '' : 's'} use this term and may now be out of date.
            <div className="mt-2 flex gap-2">
              <button onClick={() => { onRetranslate?.(affected.map((a) => a.index)); setAffected(null); onClose() }} className="btn btn-primary px-3 py-1 text-xs">Re-translate {affected.length}</button>
              <button onClick={() => setAffected(null)} className="btn btn-ghost px-3 py-1 text-xs">Dismiss</button>
            </div>
          </div>
        )}

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
              <div key={pkey(p)} className="rounded-card border border-line p-3">
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <span className="font-korean font-medium">{p.korean}</span>
                  <span className="text-hint">→</span>
                  <input value={drafts[pkey(p)]?.english ?? ''} onChange={(e) => edit(pkey(p), 'english', e.target.value)} className="input min-w-[10rem] flex-1 !py-1" />
                  <select value={drafts[pkey(p)]?.type ?? 'other'} onChange={(e) => edit(pkey(p), 'type', e.target.value)} className="input !py-1">
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

        <div className="mb-2 rounded-card border border-line p-3" style={{ background: 'var(--b-english-bg)', color: 'var(--b-english-tx)' }}>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-sm">
              <strong>Names from your English chapters.</strong> Pull the cast, places, and terms out of
              the chapters already in English so new translations use the same spellings.
            </div>
            <button onClick={learnNames} disabled={learning || busy} className="btn btn-primary shrink-0 px-3 py-1.5 text-xs">
              {learning ? 'Reading chapters…' : 'Learn names'}
            </button>
          </div>
          {learnMsg && <div className="mt-2 text-xs">{learnMsg}</div>}
          <div className="mt-1 text-xs opacity-80">Uses your Claude plan. Review the results below — a name with no Korean yet is a canonical English spelling to match.</div>
        </div>

        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <h4 className="text-sm font-medium text-muted">Locked terms {data ? `(${locked.length})` : ''}</h4>
          <div className="flex flex-wrap items-center gap-2">
            {locked.length > 0 && (
              <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search…" className="input !py-1 text-xs" />
            )}
            <a href={api.glossaryExportUrl(pid, 'csv')} className="btn btn-ghost px-3 py-1.5 text-xs" title="Download as CSV">Export</a>
            <button onClick={() => fileRef.current?.click()} disabled={busy} className="btn btn-ghost px-3 py-1.5 text-xs">Import</button>
            <input ref={fileRef} type="file" accept=".csv,.json,text/csv,application/json" onChange={onImportFile} className="hidden" />
            <button onClick={() => { setAdding((v) => !v); setNewTerm(BLANK) }} disabled={busy} className="btn btn-ghost px-3 py-1.5 text-xs">
              {adding ? 'Cancel' : '＋ Add term'}
            </button>
          </div>
        </div>

        {adding && (
          <div className="mb-3 rounded-card border border-line p-3">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              {fieldRow(newTerm, (f, v) => setNewTerm((t) => ({ ...t, [f]: v })))}
            </div>
            <div className="mt-2 flex gap-2">
              <button onClick={addTerm} disabled={busy} className="btn btn-primary px-3 py-1 text-xs">Add</button>
              <button onClick={() => { setAdding(false); setNewTerm(BLANK) }} disabled={busy} className="btn btn-ghost px-3 py-1 text-xs">Cancel</button>
            </div>
          </div>
        )}

        <div className="overflow-x-auto rounded-card border border-line">
          <table className="w-full min-w-[380px] text-sm">
            <tbody>
              {shownLocked.length === 0 && (
                <tr><td className="px-3 py-2 text-sm text-muted">{locked.length === 0 ? 'No locked terms yet.' : 'No terms match your search.'}</td></tr>
              )}
              {shownLocked.map((e) => (
                editing === entryId(e) ? (
                  <tr key={entryId(e)} className="border-t border-line first:border-t-0">
                    <td colSpan={4} className="px-3 py-2">
                      <div className="flex flex-wrap items-center gap-2">
                        {fieldRow(editDraft, (f, v) => setEditDraft((t) => ({ ...t, [f]: v })))}
                        <button onClick={saveEdit} disabled={busy} className="btn btn-primary px-3 py-1 text-xs">Save</button>
                        <button onClick={() => setEditing(null)} disabled={busy} className="btn btn-ghost px-3 py-1 text-xs">Cancel</button>
                      </div>
                    </td>
                  </tr>
                ) : (
                  <tr key={entryId(e)} className="rowhover border-t border-line first:border-t-0">
                    <td className="px-3 py-1.5 font-korean font-medium">{e.korean || <span className="font-ui text-xs text-hint" title="Canonical English spelling — Korean not known yet">— EN</span>}</td>
                    <td className="px-3 py-1.5">
                      {e.english}
                      {(e.pronoun || e.register) && <span className="ml-2 text-xs text-hint">{[e.pronoun, e.register].filter(Boolean).join(' · ')}</span>}
                      {e.note && <span className="ml-2 text-xs text-hint">— {e.note}</span>}
                    </td>
                    <td className="px-3 py-1.5 text-xs text-hint">{e.type}</td>
                    <td className="px-3 py-1.5 text-right whitespace-nowrap">
                      <button onClick={() => startEdit(e)} disabled={busy} className="btn btn-ghost px-2.5 py-1 text-xs">Edit</button>
                      <button onClick={() => removeTerm(e)} disabled={busy} className="btn btn-ghost ml-1.5 px-2.5 py-1 text-xs" style={{ color: 'var(--danger)' }}>Delete</button>
                    </td>
                  </tr>
                )
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </Modal>
  )
}
