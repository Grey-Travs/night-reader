import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api'
import { useConfirm } from '../confirm'
import { applyIssue } from '../pageBoxes'
import { usePhoneOutbox } from '../outbox'
import { pagesNeedingYou, usePhoneProgress } from '../phoneProgress'
import { useToast } from '../toast'
import PhoneBar from '../components/PhoneBar'

// The pages that need a person, on the phone: each photo beside the Korean read from it,
// with Claude's notes, and the one tap that lets its chapter carry on by itself.
//
// Edits stay on the phone until a button is pressed. A typed page counts as approved, so
// saving as you type would let a chapter build from half a correction.

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`
const draftKey = (pageId) => `nr.phone.draft.${pageId}`
const reading = (page) => `${page?.ocr?.at || ''}|${page?.verify?.at || ''}`

export default function PhoneCheckPage() {
  const { pid, pageId } = useParams()
  return pid && pageId ? <CheckOne key={pageId} pid={pid} pageId={pageId} /> : <CheckList />
}

function CheckList() {
  const { novels, refresh } = usePhoneProgress()
  const [, outbox] = usePhoneOutbox()
  const toast = useToast()
  const confirm = useConfirm()
  const pages = pagesNeedingYou(novels)
  const rows = (novels || []).flatMap((n) => (n.chapters || []).map((row) => ({ ...row, pid: n.pid })))
  // A chapter still open on the computer that this phone isn't shooting: its Done was
  // lost with a phone, or a Telegram /done forgotten. It holds back every chapter after it.
  const here = new Set(outbox.chapters.filter((c) => c.state === 'open').map((c) => c.origin))
  const forgotten = rows.filter((row) => row.step === 'capturing' && !here.has(row.origin))
  const others = rows.flatMap((row) => (row.needs_you || []).filter((item) => !item.page_id)
    .map((item) => ({ ...item, number: row.number, batch: row.batch })))

  async function finish(row) {
    try {
      await api.markCaptured(row.pid, row.batch)
      toast(`Chapter ${row.number} finished`)
    } catch (e) { toast(e.message) }
    refresh()
  }

  async function remove(row) {
    const ok = await confirm({
      title: `Delete chapter ${row.number}?`,
      body: `Its ${plural(row.photos, 'photo')} and the text read from them are removed from the novel.`,
      confirmLabel: 'Delete', danger: true,
    })
    if (!ok) return
    try {
      await api.deletePageBatch(row.pid, row.batch)
      toast(`Chapter ${row.number} deleted`)
    } catch (e) { toast(e.message) }
    refresh()
  }

  return (
    <div className="phone-shell">
      <PhoneBar title="Pages to check" />
      <main className="phone-body">
        {novels == null ? (
          <p className="py-6 text-center text-sm text-hint">Loading…</p>
        ) : !pages.length && !forgotten.length && !others.length ? (
          <p className="py-6 text-center text-sm text-hint">Nothing needs you.</p>
        ) : null}
        {pages.length > 0 && (
          <ul className="space-y-2" aria-label="Pages that need you">
            {pages.map((item) => (
              <li key={item.page_id}>
                <Link className="card block px-4 py-3" to={`/phone/check/${item.pid}/${item.page_id}`}>
                  <span className="block text-sm font-medium">Chapter {item.number} · photo {item.photo}</span>
                  <span className="block text-xs text-hint">{item.message}</span>
                </Link>
              </li>
            ))}
          </ul>
        )}
        {forgotten.map((row) => (
          <div key={row.batch} className="card space-y-2 p-3 text-sm">
            <p>
              Chapter {row.number} is still open, with {plural(row.photos, 'photo')}, and holds
              back every chapter after it.
            </p>
            <div className="flex gap-2">
              <button className="btn btn-quiet px-3 py-1.5 text-xs" onClick={() => finish(row)}>Finish it</button>
              <button className="btn btn-quiet px-3 py-1.5 text-xs" onClick={() => remove(row)}>Delete it</button>
            </div>
          </div>
        ))}
        {others.map((item, i) => (
          <p key={`${item.batch}-${i}`} className="rounded-card px-3 py-2 text-xs pill-review">
            {item.message}
            {item.kind === 'hold' || item.kind === 'refused' ? ' This needs the computer.' : ''}
          </p>
        ))}
      </main>
    </div>
  )
}

/** The unsaved edit of one page, kept on the phone and dropped if the page is read again. */
function useDraft(pageId, page) {
  const [draft, setDraft] = useState(null)
  const [dropped, setDropped] = useState(false)
  useEffect(() => {
    if (!page) return
    let kept = null
    try { kept = JSON.parse(localStorage.getItem(draftKey(pageId)) || 'null') } catch { kept = null }
    if (kept && kept.base === reading(page)) setDraft(kept.text)
    else {
      if (kept) setDropped(true)
      try { localStorage.removeItem(draftKey(pageId)) } catch { /* nothing kept */ }
      setDraft(page.text || '')
    }
  }, [pageId, page])
  const change = (text) => {
    setDraft(text)
    try { localStorage.setItem(draftKey(pageId), JSON.stringify({ base: reading(page), text })) } catch { /* kept in memory */ }
  }
  const clear = () => { try { localStorage.removeItem(draftKey(pageId)) } catch { /* none */ } }
  return { draft, change, clear, dropped }
}

const ZOOMS = [1, 2, 4]

function CheckOne({ pid, pageId }) {
  const navigate = useNavigate()
  const toast = useToast()
  const { novels } = usePhoneProgress({ pid })
  const items = pagesNeedingYou(novels).filter((item) => item.pid === pid)
  const item = items.find((x) => x.page_id === pageId) || null
  const [page, setPage] = useState(null)
  const [error, setError] = useState(null)
  const [zoom, setZoom] = useState(1)
  const [busy, setBusy] = useState(false)
  const { draft, change, clear, dropped } = useDraft(pageId, page)

  useEffect(() => {
    api.page(pid, pageId).then(setPage).catch(setError)
  }, [pid, pageId])

  // The next page that needs you, or the list. Replaces this one, so Back is the list.
  function next() {
    const at = items.findIndex((x) => x.page_id === pageId)
    const after = [...items.slice(at + 1), ...items.slice(0, Math.max(0, at))].find((x) => x.page_id !== pageId)
    navigate(after ? `/phone/check/${after.pid}/${after.page_id}` : '/phone/check', { replace: true })
  }

  async function act(call, said) {
    setBusy(true)
    try {
      await call()
      clear()
      toast(said)
      next()
    } catch (e) {
      toast(e.message || 'The computer couldn’t be reached.')
    } finally {
      setBusy(false)
    }
  }

  // A chapter heading part way through: split the chapter at this photo.
  async function splitHere() {
    const res = await api.phoneProgress({ pid, box: page.batch })
    const ids = (res?.novels?.[0]?.photos || []).map((p) => p.id)
    const from = ids.indexOf(pageId)
    if (from < 1) throw new Error('This photo is already the first of its chapter.')
    await api.createPageBatch(pid, { after: page.batch, ids: ids.slice(from) })
  }

  const label = item ? `Chapter ${item.number} · photo ${item.photo}` : 'A page'
  const issues = page?.verify?.issues || []
  const changed = page && draft != null && draft !== (page.text || '')
  const retakeAt = `/phone/capture?pid=${encodeURIComponent(pid)}&retake=${pageId}`
    + `&label=${encodeURIComponent(`Taking ${label.toLowerCase()} again`)}&back=/phone/check`

  return (
    <div className="phone-shell">
      <PhoneBar title={label} back="/phone/check" />
      <main className="phone-body">
        {error && <p className="rounded-card px-3 py-2 text-sm pill-review">{error.message || 'That page couldn’t be loaded.'}</p>}
        {item && <p className="rounded-card px-3 py-2 text-sm pill-review">{item.message}</p>}
        <div className="flex gap-1" role="group" aria-label="Zoom">
          {ZOOMS.map((z) => (
            <button key={z} className="btn btn-quiet px-2 py-1 text-xs" aria-pressed={zoom === z} onClick={() => setZoom(z)}>
              {z === 1 ? 'Fit' : `${z}×`}
            </button>
          ))}
        </div>
        <div className="overflow-auto rounded-card border border-line" style={{ maxHeight: '45svh' }}
          onDoubleClick={() => setZoom((z) => (z === 1 ? 2 : 1))}>
          <img src={api.pageImageUrl(pid, pageId)} alt={label} style={{ width: `${zoom * 100}%`, maxWidth: 'none' }} />
        </div>
        {page && (
          <>
            {page.look?.verdict === 'retake' && page.look.note && (
              <p className="text-xs text-hint">The quick look said: {page.look.note}</p>
            )}
            {page.notes?.length > 0 && (
              <ul className="space-y-1 text-xs text-hint">{page.notes.map((n, i) => <li key={i}>· {n}</li>)}</ul>
            )}
            {issues.map((issue, i) => (
              <div key={i} className="rounded-card border border-line p-2 text-xs">
                <div className="font-medium">{issue.kind}</div>
                {issue.page_says && <div className="text-muted">The photo shows: {issue.page_says}</div>}
                <button
                  className="btn btn-quiet mt-1 px-2 py-0.5 text-xs"
                  disabled={applyIssue(draft || '', issue) == null}
                  onClick={() => change(applyIssue(draft || '', issue))}
                >Apply</button>
              </div>
            ))}
            {dropped && (
              <p className="text-xs text-hint">This page was read again, so the edit you hadn’t saved was dropped.</p>
            )}
            <textarea
              className="input font-korean min-h-[12rem] text-base" lang="ko" spellCheck={false}
              aria-label="Korean text read from this photo"
              value={draft ?? ''} onChange={(e) => change(e.target.value)}
            />
            <div className="grid grid-cols-2 gap-2">
              <button
                className="btn btn-primary py-3 text-sm" disabled={busy || !(draft || '').trim()}
                onClick={() => act(() => api.savePage(pid, pageId,
                  changed ? { text: draft, status: 'edited' } : { status: 'ok' }), 'Saved')}
              >Looks good</button>
              <Link className="btn btn-quiet py-3 text-center text-sm" to={retakeAt}>Retake</Link>
              <button className="btn btn-quiet py-3 text-sm" disabled={busy}
                onClick={() => act(() => api.readPages(pid, [pageId]), 'Reading it again')}>Read again</button>
              <button className="btn btn-quiet py-3 text-sm" disabled={busy}
                onClick={() => act(() => api.savePage(pid, pageId, { status: 'skipped' }), 'Skipped')}>Skip page</button>
            </div>
            {item?.kind === 'heading' && (
              <div className="grid grid-cols-2 gap-2">
                <button className="btn btn-quiet py-3 text-sm" disabled={busy}
                  onClick={() => act(splitHere, 'Split into two chapters')}>Split here</button>
                <button className="btn btn-quiet py-3 text-sm" disabled={busy}
                  onClick={() => act(() => api.savePage(pid, pageId, { join_prev: 'paragraph' }), 'Kept as one chapter')}>
                  Keep together
                </button>
              </div>
            )}
            <button className="btn btn-ghost py-2 text-sm" onClick={next}>Later</button>
          </>
        )}
      </main>
    </div>
  )
}
