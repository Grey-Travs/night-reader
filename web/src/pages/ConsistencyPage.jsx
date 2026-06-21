import { useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { api } from '../api'
import { useToast } from '../toast'
import { useConfirm } from '../confirm'

// Name-consistency checker: find proper nouns spelled inconsistently across a novel's
// chapters, and frequent ones missing from the glossary. Fixes are instant + revertible
// (whole-word replace in the saved chapters, snapshotted to previous/).
export default function ConsistencyPage() {
  const { pid, loadPending } = useOutletContext()
  const toast = useToast()
  const confirm = useConfirm()
  const [report, setReport] = useState(null)
  const [scanning, setScanning] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [canon, setCanon] = useState({}) // variant index -> chosen canonical spelling

  async function run() {
    setScanning(true); setError(null)
    try {
      const r = await api.consistencyScan(pid)
      setReport(r)
      const c = {}
      r.variants.forEach((v, i) => { c[i] = v.glossary_spelling || v.options[0].spelling })
      setCanon(c)
    } catch (e) { setError(String(e.message || e)) }
    finally { setScanning(false) }
  }

  async function unify(v, i) {
    const target = canon[i] || v.glossary_spelling || v.options[0].spelling
    const others = v.options.filter((o) => o.spelling !== target)
    if (!others.length) { toast('Already consistent'); return }
    const totalCh = new Set(others.flatMap((o) => o.chapters)).size
    const ok = await confirm({
      title: 'Unify spelling',
      body: `Replace ${others.map((o) => `“${o.spelling}”`).join(', ')} with “${target}” across ${totalCh} chapter${totalCh === 1 ? '' : 's'}?\n\nThis edits the saved translations (revertible per chapter from the reader).`,
      confirmLabel: 'Replace',
    })
    if (!ok) return
    setBusy(true); setError(null)
    try {
      // One request applies every variant in a single pass per chapter, so each chapter
      // is snapshotted to previous/ once and the unify stays cleanly revertible.
      const chapters = [...new Set(others.flatMap((o) => o.chapters))].sort((a, b) => a - b)
      const res = await api.consistencyReplace(pid, { from: others.map((o) => o.spelling), to: target, chapters })
      toast(`Replaced ${res.replaced} occurrence${res.replaced === 1 ? '' : 's'}`)
      await run()
    } catch (e) { setError(String(e.message || e)) }
    finally { setBusy(false) }
  }

  async function addTerm(spelling) {
    setBusy(true); setError(null)
    try {
      await api.saveGlossaryTerm(pid, { english: spelling, type: 'name' })
      loadPending?.()
      toast(`Added “${spelling}” to glossary`)
      await run()
    } catch (e) { setError(String(e.message || e)) }
    finally { setBusy(false) }
  }

  const chList = (chapters, n = 8) => `ch ${chapters.slice(0, n).join(', ')}${chapters.length > n ? '…' : ''}`

  return (
    <div className="page">
      <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="font-reading text-2xl font-medium">Name consistency</h1>
          <p className="text-sm text-hint">Find characters/terms spelled different ways across chapters, and frequent names missing from the glossary.</p>
        </div>
        <button onClick={run} disabled={scanning} className="btn btn-primary px-4 py-2">{scanning ? 'Scanning…' : (report ? 'Re-scan' : 'Run check')}</button>
      </div>
      {error && <div className="mb-4 rounded-btn px-3 py-2 text-sm pill-review">{error}</div>}

      {report == null ? (
        <div className="rounded-card border border-dashed border-line-strong p-10 text-center text-muted">
          Run a check to scan this novel's translations for spelling inconsistencies. It's instant and free (no Claude usage).
        </div>
      ) : (
        <>
          <div className="mb-3 text-xs text-hint">Scanned {report.scanned} translated chapter{report.scanned === 1 ? '' : 's'}.</div>

          <h2 className="mb-2 text-sm font-medium text-muted">Inconsistent spellings ({report.variants.length})</h2>
          {report.variants.length === 0 ? (
            <div className="sunken px-3 py-2 text-sm text-muted">No inconsistencies found — every name is spelled one way.</div>
          ) : (
            <div className="space-y-3">
              {report.variants.map((v, i) => (
                <div key={i} className="card p-4">
                  <div className="flex flex-wrap items-center gap-2 text-sm">
                    {v.options.map((o) => (
                      <span key={o.spelling} className={`pill ${o.spelling === (canon[i] || v.glossary_spelling) ? 'pill-translated' : 'pill-muted'}`}>
                        {o.spelling} · {o.count}× · {chList(o.chapters)}
                      </span>
                    ))}
                  </div>
                  {v.glossary_spelling && <div className="mt-1.5 text-xs text-hint">Glossary spelling: “{v.glossary_spelling}”.</div>}
                  <div className="mt-3 flex flex-wrap items-center gap-2">
                    <span className="text-xs text-muted">Make canonical:</span>
                    <select value={canon[i] || ''} onChange={(e) => setCanon((c) => ({ ...c, [i]: e.target.value }))} className="input !py-1 text-xs">
                      {v.options.map((o) => <option key={o.spelling} value={o.spelling}>{o.spelling}</option>)}
                    </select>
                    <button onClick={() => unify(v, i)} disabled={busy} className="btn btn-primary px-3 py-1.5 text-xs">Unify spelling</button>
                    {!v.glossary_spelling && <button onClick={() => addTerm(canon[i] || v.options[0].spelling)} disabled={busy} className="btn btn-ghost px-3 py-1.5 text-xs">Add to glossary</button>}
                  </div>
                </div>
              ))}
            </div>
          )}

          <h2 className="mb-2 mt-6 text-sm font-medium text-muted">Frequent names not in the glossary ({report.missing.length})</h2>
          {report.missing.length === 0 ? (
            <div className="sunken px-3 py-2 text-sm text-muted">Nothing notable missing.</div>
          ) : (
            <div className="grid gap-2 sm:grid-cols-2">
              {report.missing.map((m) => (
                <div key={m.spelling} className="flex items-center justify-between gap-2 rounded-card border border-line p-3 text-sm">
                  <span><strong>{m.spelling}</strong> <span className="text-xs text-hint">{m.count}× · {chList(m.chapters, 6)}</span></span>
                  <button onClick={() => addTerm(m.spelling)} disabled={busy} className="btn btn-ghost px-3 py-1.5 text-xs">Add</button>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}
