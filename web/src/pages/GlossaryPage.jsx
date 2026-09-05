import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useOutletContext } from 'react-router-dom'
import { api } from '../api'
import Hint from '../components/Hint'
import { useConfirm } from '../confirm'
import { FORMAT_LABELS, MAX_UNTYPED, parseBulk, parseGlossaryFile } from '../glossary-parse'

const TYPES = ['name', 'place', 'skill', 'term', 'other']
const BLANK = { korean: '', english: '', type: 'name', note: '', pronoun: '', register: '' }

export default function GlossaryPage() {
  const { pid, loadPending, enqueue } = useOutletContext()
  const navigate = useNavigate()
  const confirm = useConfirm()
  const onChanged = loadPending
  const onRetranslate = (indices) => indices.length && enqueue(indices, true)

  const [data, setData] = useState(null)
  const [drafts, setDrafts] = useState({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const [editing, setEditing] = useState(null)
  const [editDraft, setEditDraft] = useState(BLANK)
  const [editOrig, setEditOrig] = useState({ korean: '', english: '' })
  const [adding, setAdding] = useState(false)
  const [newTerm, setNewTerm] = useState(BLANK)
  const [bulkOpen, setBulkOpen] = useState(false)
  const [bulkText, setBulkText] = useState('')
  const [bulkBusy, setBulkBusy] = useState(false)
  const [bulkMsg, setBulkMsg] = useState(null)
  const [query, setQuery] = useState('')
  const [affected, setAffected] = useState(null)
  const [learning, setLearning] = useState(false)
  const [learnMsg, setLearnMsg] = useState(null)
  const [detecting, setDetecting] = useState(false)
  const [detectMsg, setDetectMsg] = useState(null)
  const [projects, setProjects] = useState([])
  const [copyFrom, setCopyFrom] = useState('')
  const [copyMsg, setCopyMsg] = useState(null)
  // Multi-select: pending rows keyed by korean|english, locked rows by entryId.
  const [pendingSel, setPendingSel] = useState(() => new Set())
  const [lockedSel, setLockedSel] = useState(() => new Set())
  const [lastPending, setLastPending] = useState(null)  // anchors for shift-click ranges
  const [lastLocked, setLastLocked] = useState(null)
  const fileRef = useRef(null)

  const entryId = (e) => (e.korean ? `k:${e.korean}` : `e:${e.english}`)
  const pkey = (p) => `${p.korean}|${p.english}`

  async function load() {
    const d = await api.glossary(pid)
    d.pending = d.pending || []
    d.locked = d.locked || []
    setData(d)
    const init = {}
    for (const p of d.pending) init[pkey(p)] = { english: p.english, type: p.type || 'other', note: p.note || '', pronoun: p.pronoun || '' }
    setDrafts(init)
    setPendingSel(new Set()); setLastPending(null)  // the queue was just replaced
  }
  useEffect(() => {
    setLockedSel(new Set()); setLastLocked(null)
    load().catch((e) => setError(String(e.message || e)))
  }, [pid])
  useEffect(() => { api.listProjects().then((d) => setProjects(d.projects || [])).catch(() => {}) }, [])

  async function copyFromNovel() {
    if (!copyFrom) return
    setBusy(true); setError(null); setCopyMsg(null)
    try {
      const d = await api.copyGlossary(pid, { source_pid: copyFrom, mode: 'merge' })
      setData((cur) => cur && { ...cur, locked: d.locked })
      const src = projects.find((p) => p.id === copyFrom)
      setCopyMsg(`Copied ${d.copied} term${d.copied === 1 ? '' : 's'} from “${src?.name || 'that novel'}”.`)
      setCopyFrom('')
    } catch (e) { setError(String(e.message || e)) }
    finally { setBusy(false) }
  }

  function edit(key, field, value) {
    setDrafts((d) => ({ ...d, [key]: { ...d[key], [field]: value } }))
  }

  // ---- multi-select ---------------------------------------------------------
  // One click toggles a row; shift-click extends from the last clicked row, using
  // the order the rows are currently rendered in (so it matches what you see).
  function rowCheck(e, id, ids, setSel, anchor, setAnchor) {
    if (e.shiftKey && anchor != null) {
      const a = ids.indexOf(anchor), b = ids.indexOf(id)
      if (a !== -1 && b !== -1) {
        const [lo, hi] = a < b ? [a, b] : [b, a]
        setSel((s) => { const n = new Set(s); ids.slice(lo, hi + 1).forEach((i) => n.add(i)); return n })
        setAnchor(id)
        return
      }
    }
    setSel((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n })
    setAnchor(id)
  }
  function toggleAll(ids, allOn, setSel) {
    setSel((s) => { const n = new Set(s); ids.forEach((i) => allOn ? n.delete(i) : n.add(i)); return n })
  }

  async function decide(approve, reject) {
    setBusy(true); setError(null)
    try { await api.reviewGlossary(pid, { approve, reject }); await load(); onChanged?.() }
    catch (e) { setError(String(e.message || e)) }
    finally { setBusy(false) }
  }
  // Derived from the live list, so an id left over from a row that's since gone
  // simply drops out of the selection instead of showing a phantom count.
  const pendingRows = data?.pending || []
  const selectedPending = pendingRows.filter((p) => pendingSel.has(pkey(p)))
  const allPendingSelected = pendingRows.length > 0 && selectedPending.length === pendingRows.length

  const asDecision = (p) => ({ korean: p.korean, ...drafts[pkey(p)] })
  const approveOne = (p) => decide([asDecision(p)], [])
  const rejectOne = (p) => decide([], [p.korean])
  const approveAll = () => decide((data?.pending || []).map(asDecision), [])
  const approveSelected = () => selectedPending.length && decide(selectedPending.map(asDecision), [])
  async function rejectSelected() {
    const n = selectedPending.length
    if (!n) return
    if (!(await confirm({
      title: `Reject ${n} term${n === 1 ? '' : 's'}?`,
      body: 'They go back to being unknown — a later chapter can propose them again.',
      confirmLabel: 'Reject', danger: true,
    }))) return
    decide([], selectedPending.map((p) => p.korean))
  }

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
    if (!(await confirm({ title: 'Remove term?', body: `Remove “${e.korean || e.english}” from the glossary?`, confirmLabel: 'Remove', danger: true }))) return
    setBusy(true); setError(null)
    try {
      const d = await api.deleteGlossaryTerm(pid, { korean: e.korean || '', english: e.english || '' })
      setData((cur) => cur && { ...cur, locked: d.locked })
      if (editing === entryId(e)) setEditing(null)
    } catch (err) { setError(String(err.message || err)) }
    finally { setBusy(false) }
  }

  async function removeSelectedTerms() {
    const items = selectedLocked
    if (!items.length) return
    const names = items.slice(0, 5).map((e) => e.korean || e.english).join(', ')
    if (!(await confirm({
      title: `Remove ${items.length} term${items.length === 1 ? '' : 's'}?`,
      body: `Remove ${names}${items.length > 5 ? ` and ${items.length - 5} more` : ''} from the glossary?`,
      confirmLabel: 'Remove', danger: true,
    }))) return
    setBusy(true); setError(null)
    try {
      const d = await api.deleteGlossaryTerms(pid, {
        terms: items.map((e) => ({ korean: e.korean || '', english: e.english || '' })),
      })
      setData((cur) => cur && { ...cur, locked: d.locked })
      if (editing && items.some((e) => entryId(e) === editing)) setEditing(null)
      setLockedSel(new Set()); setLastLocked(null)
    } catch (err) { setError(String(err.message || err)) }
    finally { setBusy(false) }
  }

  async function learnNames() {
    setLearning(true); setError(null); setLearnMsg(null)
    try {
      const d = await api.learnGlossary(pid)
      setData((cur) => cur && { ...cur, locked: d.locked })
      setLearnMsg(`Found ${d.learned} new name${d.learned === 1 ? '' : 's'} from ${d.from_chapters} English chapter${d.from_chapters === 1 ? '' : 's'}.`)
    } catch (e) { setError(String(e.message || e)) }
    finally { setLearning(false) }
  }

  async function detectPronouns() {
    setDetecting(true); setError(null); setDetectMsg(null)
    try {
      const d = await api.detectPronouns(pid)
      setData((cur) => cur && { ...cur, locked: d.locked })
      const n = (d.filled || []).length
      const m = (d.unresolved || []).length
      setDetectMsg(`Filled ${n} pronoun${n === 1 ? '' : 's'}` +
        (m ? ` · ${m} character${m === 1 ? '' : 's'} unclear from the text — set manually.` : '.'))
    } catch (e) { setError(String(e.message || e)) }
    finally { setDetecting(false) }
  }

  // Parsed live so the panel can show what the paste was understood as before
  // anything is submitted. type === '' rows are the only ones that use the plan.
  const bulk = useMemo(() => (bulkText.trim() ? parseBulk(bulkText) : null), [bulkText])
  const bulkRows = useMemo(() => (bulk?.rows || []).filter((r) => !r.invalid), [bulk])
  const bulkInvalid = (bulk?.rows.length || 0) - bulkRows.length
  const bulkUntyped = bulkRows.filter((r) => !r.type).length
  const bulkSummary = useMemo(() => {
    if (!bulk || bulk.error || !bulkRows.length) return null
    const byType = {}
    for (const r of bulkRows) if (r.type) byType[r.type] = (byType[r.type] || 0) + 1
    const parts = Object.entries(byType).map(([t, n]) => `${n} ${t}${n === 1 ? '' : 's'}`)
    let s = `${FORMAT_LABELS[bulk.format] || 'Rows'} · ${bulkRows.length} row${bulkRows.length === 1 ? '' : 's'}`
    if (parts.length) s += ` — ${parts.join(', ')}`
    if (bulkUntyped) s += ` · ${bulkUntyped} will have ${bulkUntyped === 1 ? 'its type' : 'types'} auto-detected (uses your Claude plan)`
    return s
  }, [bulk, bulkRows, bulkUntyped])

  async function bulkAdd() {
    if (!bulkRows.length || bulk?.error) return
    setBulkBusy(true); setError(null); setBulkMsg(null)
    try {
      const entries = bulkRows.map(({ korean, english, type, note, pronoun, register }) =>
        ({ korean, english, type, note, pronoun, register }))
      const d = await api.bulkAddGlossary(pid, { entries })
      setData((cur) => cur && { ...cur, locked: d.locked })
      const byType = {}
      for (const a of d.added || []) byType[a.type] = (byType[a.type] || 0) + 1
      const parts = Object.entries(byType).map(([t, n]) => `${n} ${t}${n === 1 ? '' : 's'}`)
      let msg = `Added ${(d.added || []).length}${parts.length ? ` (${parts.join(', ')})` : ''}`
      if (d.updated?.length) msg += ` · ${d.updated.length} updated with Korean`
      if (d.skipped?.length) {
        const byReason = {}
        for (const s of d.skipped) byReason[s.reason || 'already in glossary'] = (byReason[s.reason || 'already in glossary'] || 0) + 1
        msg += ` · skipped ${Object.entries(byReason).map(([r, n]) => `${n} ${r}`).join(', ')}`
      }
      if (d.classify_error) {
        // Typed rows were saved; refill the box with just the leftovers for a retry.
        setBulkText((d.unclassified || []).join(', '))
        setBulkMsg(`${msg} — ${(d.unclassified || []).length} left in the box, ${d.classify_error}`)
      } else {
        setBulkMsg(msg + '.')
        setBulkText('')
        setBulkOpen(false)
      }
    } catch (e) { setError(String(e.message || e)) }
    finally { setBulkBusy(false) }
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
  // Selection survives a search change (it's keyed by term, not row position), so
  // you can build one up across several searches; "select all" only spans what's shown.
  const shownIds = shownLocked.map(entryId)
  const selectedLocked = locked.filter((e) => lockedSel.has(entryId(e)))
  const allShownSelected = shownIds.length > 0 && shownIds.every((id) => lockedSel.has(id))
  const hiddenSelected = selectedLocked.length - shownIds.filter((id) => lockedSel.has(id)).length

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
    <div className="page">
      <h1 className="mb-5 font-reading text-2xl font-medium">Glossary</h1>
      {error && <div className="mb-3 rounded-btn px-3 py-2 text-sm pill-review">{error}</div>}

      {affected && (
        <div className="mb-4 rounded-card border border-line p-3 text-sm" style={{ background: 'var(--b-queued-bg)', color: 'var(--b-queued-tx)' }}>
          <strong>{affected.length}</strong> already-translated chapter{affected.length === 1 ? '' : 's'} use this term and may now be out of date.
          <div className="mt-2 flex gap-2">
            <button onClick={() => { onRetranslate?.(affected.map((a) => a.index)); setAffected(null); navigate(`/novel/${pid}/activity`) }} className="btn btn-primary px-3 py-1 text-xs">Re-translate {affected.length}</button>
            <button onClick={() => setAffected(null)} className="btn btn-ghost px-3 py-1 text-xs">Dismiss</button>
          </div>
        </div>
      )}

      <div className="mb-6">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            {pendingRows.length > 0 && (
              <input type="checkbox" checked={allPendingSelected} onChange={() => toggleAll(pendingRows.map(pkey), allPendingSelected, setPendingSel)}
                aria-label="Select all new terms" title="Select all · tip: shift-click a row to select a range" style={{ accentColor: 'var(--accent)' }} />
            )}
            <h4 className="text-sm font-medium text-muted">New terms to review {data ? `(${data.pending.length})` : ''}</h4>
          </div>
          {selectedPending.length > 0 ? (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs text-muted">{selectedPending.length} selected</span>
              <button onClick={approveSelected} disabled={busy} className="btn btn-primary px-3 py-1.5 text-xs">Approve selected</button>
              <button onClick={rejectSelected} disabled={busy} className="btn btn-ghost px-3 py-1.5 text-xs" style={{ color: 'var(--danger)' }}>Reject selected</button>
              <button onClick={() => { setPendingSel(new Set()); setLastPending(null) }} className="btn btn-ghost px-2.5 py-1.5 text-xs">Clear</button>
            </div>
          ) : pendingRows.length > 0 && (
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
                <input type="checkbox" checked={pendingSel.has(pkey(p))} readOnly
                  onClick={(ev) => rowCheck(ev, pkey(p), pendingRows.map(pkey), setPendingSel, lastPending, setLastPending)}
                  aria-label={`Select ${p.korean}`} style={{ accentColor: 'var(--accent)' }} />
                <span className="font-korean font-medium">{p.korean}</span>
                <span className="text-hint">→</span>
                <input value={drafts[pkey(p)]?.english ?? ''} onChange={(e) => edit(pkey(p), 'english', e.target.value)} className="input min-w-[10rem] flex-1 !py-1" />
                <select value={drafts[pkey(p)]?.type ?? 'other'} onChange={(e) => edit(pkey(p), 'type', e.target.value)} className="input !py-1">
                  {TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
                {(drafts[pkey(p)]?.type ?? p.type) === 'name' && (
                  <select value={drafts[pkey(p)]?.pronoun ?? ''} onChange={(e) => edit(pkey(p), 'pronoun', e.target.value)} className="input !py-1" title="Pronoun — keeps this character's gender consistent across chapters">
                    <option value="">pronoun?</option>
                    <option value="he">he</option>
                    <option value="she">she</option>
                    <option value="they">they</option>
                  </select>
                )}
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
            <Hint text="Claude reads your already-English chapters and lists their names. New translations will then spell those names the same way. Uses your plan." className="ml-1" />
          </div>
          <div className="flex shrink-0 gap-2">
            <button onClick={learnNames} disabled={learning || detecting || busy} className="btn btn-primary px-3 py-1.5 text-xs">
              {learning ? 'Reading chapters…' : 'Learn names'}
            </button>
            <button onClick={detectPronouns} disabled={learning || detecting || busy} className="btn btn-ghost px-3 py-1.5 text-xs"
              title="Fill in he/she/they for characters that don't have a pronoun yet, judged from your English chapters. Never overwrites a pronoun you set yourself.">
              {detecting ? 'Detecting…' : 'Detect pronouns'}
            </button>
          </div>
        </div>
        {learnMsg && <div className="mt-2 text-xs">{learnMsg}</div>}
        {detectMsg && <div className="mt-2 text-xs">{detectMsg}</div>}
        <div className="mt-1 text-xs opacity-80">Uses your Claude plan. Review the results below — a name with no Korean yet is a canonical English spelling to match. Pronouns keep each character's gender consistent across chapters.</div>
      </div>

      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h4 className="text-sm font-medium text-muted">Locked terms {data ? `(${locked.length})` : ''}</h4>
        <div className="flex flex-wrap items-center gap-2">
          {locked.length > 0 && (
            <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search…" className="input !py-1 text-xs" />
          )}
          {selectedLocked.length > 0 ? (
            <>
              <span className="text-xs text-muted">
                {selectedLocked.length} selected{hiddenSelected > 0 ? ` (${hiddenSelected} not shown)` : ''}
              </span>
              <button onClick={removeSelectedTerms} disabled={busy} className="btn btn-ghost px-3 py-1.5 text-xs" style={{ color: 'var(--danger)' }}>Delete selected</button>
              <button onClick={() => { setLockedSel(new Set()); setLastLocked(null) }} className="btn btn-ghost px-2.5 py-1.5 text-xs">Clear</button>
            </>
          ) : (
            <>
              <a href={api.glossaryExportUrl(pid, 'csv')} className="btn btn-ghost px-3 py-1.5 text-xs" title="Download as CSV">Export</a>
              <button onClick={() => fileRef.current?.click()} disabled={busy} className="btn btn-ghost px-3 py-1.5 text-xs">Import</button>
              <input ref={fileRef} type="file" accept=".csv,.json,text/csv,application/json" onChange={onImportFile} className="hidden" />
              {projects.length > 1 && (
                <span className="flex items-center gap-1">
                  <select value={copyFrom} onChange={(e) => setCopyFrom(e.target.value)} disabled={busy} className="input !py-1 text-xs" title="Copy locked terms from another novel — keeps a series consistent">
                    <option value="">Copy from…</option>
                    {projects.filter((p) => p.id !== pid).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                  </select>
                  <button onClick={copyFromNovel} disabled={!copyFrom || busy} className="btn btn-ghost px-2.5 py-1.5 text-xs">Copy</button>
                </span>
              )}
              <button onClick={() => { setBulkOpen((v) => !v); setBulkMsg(null) }} disabled={busy || bulkBusy} className="btn btn-ghost px-3 py-1.5 text-xs" title="Paste a list, a CSV/TSV table, or JSON arrays of terms — types you provide are added instantly; missing ones are detected automatically">
                {bulkOpen ? 'Cancel' : 'Bulk add'}
              </button>
              <button onClick={() => { setAdding((v) => !v); setNewTerm(BLANK) }} disabled={busy} className="btn btn-ghost px-3 py-1.5 text-xs">
                {adding ? 'Cancel' : '＋ Add term'}
              </button>
            </>
          )}
        </div>
      </div>

      {copyMsg && <div className="mb-2 text-xs text-muted">{copyMsg}</div>}
      {bulkMsg && <div className="mb-2 text-xs text-muted">{bulkMsg}</div>}

      {bulkOpen && (
        <div className="mb-3 rounded-card border border-line p-3">
          <div className="mb-2 text-sm text-muted">
            Paste terms as a plain list, a spreadsheet table (CSV/TSV), or JSON — full entries or
            groups like {'{"names": […], "places": […]}'}. Rows that come with a type are added
            instantly and don’t use your plan; only unlabeled terms have their type detected.
          </div>
          <textarea
            value={bulkText}
            onChange={(e) => setBulkText(e.target.value)}
            placeholder={'Kael, Ironhold Citadel (place), mana core, …\nor: [{"korean": "카엘", "english": "Kael", "type": "name", "pronoun": "he"}]'}
            rows={5}
            className="input w-full !py-1.5"
            disabled={bulkBusy}
          />
          {bulk?.error && <div className="mt-2 rounded-btn px-3 py-2 text-xs pill-review">{bulk.error}</div>}
          {bulkSummary && (
            <div className="mt-2 text-xs text-muted">
              {bulkSummary}
              {bulkInvalid > 0 && <span style={{ color: 'var(--b-review-tx)' }}> · {bulkInvalid} row{bulkInvalid === 1 ? '' : 's'} can’t be added — open “Show rows”</span>}
            </div>
          )}
          {bulkUntyped > MAX_UNTYPED && (
            <div className="mt-1 text-xs" style={{ color: 'var(--b-review-tx)' }}>
              That's over the {MAX_UNTYPED}-term auto-detect limit — add types to the rows, or split the paste.
            </div>
          )}
          {(bulk?.notices || []).map((n, i) => (
            <div key={i} className="mt-1 text-xs text-muted">· {n}</div>
          ))}
          {!bulk?.error && (bulk?.rows.length || 0) > 0 && (
            <details className="mt-2">
              <summary className="cursor-pointer text-xs text-muted">Show rows</summary>
              <div className="mt-1 max-h-48 overflow-y-auto rounded-btn border border-line">
                <table className="w-full text-xs">
                  <tbody>
                    {bulk.rows.map((r, i) => (
                      <tr key={i} className="border-t border-line first:border-t-0">
                        <td className="px-2 py-1">{r.english || <span className="text-hint">—</span>}</td>
                        <td className="px-2 py-1 font-korean">{r.korean}</td>
                        <td className="px-2 py-1 text-muted">
                          {r.invalid
                            ? <span style={{ color: 'var(--b-review-tx)' }}>{r.invalid}</span>
                            : (r.type || 'auto-detect')}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          )}
          <div className="mt-2 flex gap-2">
            <button onClick={bulkAdd} disabled={bulkBusy || !bulkRows.length || !!bulk?.error || bulkUntyped > MAX_UNTYPED} className="btn btn-primary px-3 py-1 text-xs">
              {bulkBusy
                ? (bulkUntyped ? 'Detecting types…' : 'Adding…')
                : `Add ${bulkRows.length || 'all'}${bulkUntyped ? ` · detect ${bulkUntyped} type${bulkUntyped === 1 ? '' : 's'}` : ''}`}
            </button>
            <button onClick={() => { setBulkOpen(false); setBulkText('') }} disabled={bulkBusy} className="btn btn-ghost px-3 py-1 text-xs">Cancel</button>
          </div>
        </div>
      )}

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
          {shownLocked.length > 0 && (
            <thead>
              <tr className="border-b border-line">
                <th className="w-9 px-3 py-2">
                  <input type="checkbox" checked={allShownSelected} onChange={() => toggleAll(shownIds, allShownSelected, setLockedSel)}
                    aria-label="Select all terms" title="Select all shown · tip: shift-click a row to select a range" style={{ accentColor: 'var(--accent)' }} />
                </th>
                <th colSpan={4} />
              </tr>
            </thead>
          )}
          <tbody>
            {shownLocked.length === 0 && (
              <tr><td className="px-3 py-2 text-sm text-muted">{locked.length === 0 ? 'No locked terms yet.' : 'No terms match your search.'}</td></tr>
            )}
            {shownLocked.map((e) => (
              editing === entryId(e) ? (
                <tr key={entryId(e)} className="border-t border-line first:border-t-0">
                  <td colSpan={5} className="px-3 py-2">
                    <div className="flex flex-wrap items-center gap-2">
                      {fieldRow(editDraft, (f, v) => setEditDraft((t) => ({ ...t, [f]: v })))}
                      <button onClick={saveEdit} disabled={busy} className="btn btn-primary px-3 py-1 text-xs">Save</button>
                      <button onClick={() => setEditing(null)} disabled={busy} className="btn btn-ghost px-3 py-1 text-xs">Cancel</button>
                    </div>
                  </td>
                </tr>
              ) : (
                <tr key={entryId(e)} className="rowhover border-t border-line first:border-t-0">
                  <td className="px-3 py-1.5">
                    <input type="checkbox" checked={lockedSel.has(entryId(e))} readOnly
                      onClick={(ev) => rowCheck(ev, entryId(e), shownIds, setLockedSel, lastLocked, setLastLocked)}
                      aria-label={`Select ${e.korean || e.english}`} style={{ accentColor: 'var(--accent)' }} />
                  </td>
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
  )
}
