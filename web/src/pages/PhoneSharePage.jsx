import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api } from '../api'
import { encodePicked } from '../camera'
import { usePhoneOutbox } from '../outbox'
import { HEIC_ADVICE, REFUSED, checkDrop, sortFilesForUpload } from '../pageBoxes'
import { deleteShare, isDriveLink, readShare } from '../shareInbox'
import PhoneBar from '../components/PhoneBar'

// Photos or a scanner PDF shared to Night Reader from Gallery or Google Photos: in what
// order they go, into which novel, as a new chapter or onto the end of one. Then they go
// through the outbox like photos from the camera — saved on the phone first, sent in order
// — and the share is forgotten.
//
// A Google Drive "Share" sends a link, not the file, and a HEIC photo can't be read at
// all: both are said plainly here, with what to do instead.

const LAST_NOVEL = 'nr.phone.novel'
const isPdf = (file) => file.type === 'application/pdf' || /\.pdf$/i.test(file.name || '')

function remembered() {
  try { return localStorage.getItem(LAST_NOVEL) || '' } catch { return '' }
}

const MESSAGES = {
  missed: 'The share arrived before Night Reader was ready on this phone, so nothing was kept. Share it again.',
  empty: 'Nothing came with that share.',
  error: 'The phone couldn’t keep the shared files. Free some space, then share them again.',
}

export default function PhoneSharePage() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const id = params.get('id') || ''
  const problem = params.get('missed') ? 'missed' : params.get('empty') ? 'empty'
    : params.get('error') ? 'error' : ''
  const [box, outbox] = usePhoneOutbox()
  const [share, setShare] = useState(undefined)
  const [order, setOrder] = useState([])
  const [novels, setNovels] = useState(null)
  const [pid, setPid] = useState(remembered())
  const [boxes, setBoxes] = useState([])
  const [target, setTarget] = useState('')
  const [sending, setSending] = useState(null)

  useEffect(() => {
    if (!id) { setShare(null); return }
    readShare(id).then((got) => {
      setShare(got)
      if (got) setOrder(sortFilesForUpload(got.files))
    }).catch(() => setShare(null))
  }, [id])

  useEffect(() => {
    api.scans().then((res) => setNovels(res?.novels || [])).catch(async () => {
      setNovels((await box?.cachedNovels?.()) || [])
    })
  }, [box])

  // "End of Chapter M": the chapters not built yet, which can still take photos.
  useEffect(() => {
    setTarget('')
    if (!pid) { setBoxes([]); return }
    api.pages(pid).then((rail) => setBoxes((rail?.batches || []).filter((b) => b.chapter == null && !b.legacy)))
      .catch(() => setBoxes([]))
  }, [pid])

  // PDFs go in as they are (the computer splits them); everything else is checked the way
  // a desktop drop is. The order stays the one on screen either way.
  const checked = useMemo(() => {
    const photos = checkDrop(order.filter((f) => !isPdf(f)))
    const fine = new Set(photos.files)
    return { files: order.filter((f) => isPdf(f) || fine.has(f)), refused: photos.refused }
  }, [order])
  const novel = (novels || []).find((n) => n.id === pid) || null
  const already = outbox.chapters.some((c) => c.origin === `share:${id}`)

  async function send() {
    const files = checked.files
    setSending(`Saving 0 of ${files.length}…`)
    try { localStorage.setItem(LAST_NOVEL, pid) } catch { /* only a convenience */ }
    // The share's own id names the chapter, so sending it twice — the app killed part
    // way, then the share opened again — never makes a second one.
    if (!already) {
      const made = await box.startChapter({
        pid, novel: novel?.name || '', shown: novel?.next_number ?? null,
        origin: `share:${id}`, box: target || null,
      })
      for (const [i, file] of files.entries()) {
        setSending(`Saving ${i + 1} of ${files.length}…`)
        if (isPdf(file)) {
          await box.addPhoto(made.id, { blob: file, name: file.name || 'scan.pdf', type: 'application/pdf' })
        } else {
          const { blob } = await encodePicked(file)
          await box.addPhoto(made.id, { blob, name: file.name, type: blob.type || 'image/jpeg' })
        }
      }
      await box.closeChapter(made.id)
    }
    await deleteShare(id)
    navigate('/phone', { replace: true })
  }

  const move = (from, to) => setOrder((list) => {
    const next = [...list]
    const [item] = next.splice(from, 1)
    next.splice(to, 0, item)
    return next
  })

  let body
  if (problem) {
    body = <p className="rounded-card px-3 py-2 text-sm pill-review">{MESSAGES[problem]}</p>
  } else if (share === undefined) {
    body = <p className="py-6 text-center text-sm text-hint">Loading…</p>
  } else if (!share) {
    body = <p className="rounded-card px-3 py-2 text-sm pill-review">That share is no longer on this phone. Share it again.</p>
  } else if (!share.files.length) {
    body = (
      <p className="rounded-card px-3 py-2 text-sm pill-review">
        {isDriveLink(share.text)
          ? 'That’s a link to a file in Google Drive, not the file itself. In Drive, use ⋮ → Send a copy, then choose Night Reader.'
          : 'Nothing that can be read came with that share — only text. Share the photos or the PDF themselves.'}
      </p>
    )
  } else {
    const heic = checked.refused.some((r) => r.reason === 'heic')
    body = (
      <>
        {checked.refused.length > 0 && (
          <p className="whitespace-pre-line rounded-card px-3 py-2 text-xs pill-review">
            {`These can’t go in:\n${checked.refused.map((r) => `· ${r.name} — ${REFUSED[r.reason]}`).join('\n')}`}
            {heic ? `\n\n${HEIC_ADVICE}` : ''}
          </p>
        )}
        <ol className="space-y-1" aria-label="Shared files, in the order they go in">
          {order.map((file, i) => (
            <li key={`${file.name}-${i}`} className="flex items-center gap-2 text-sm">
              <span className="w-6 text-right text-xs text-hint">{i + 1}</span>
              <span className="min-w-0 flex-1 truncate">{file.name}</span>
              <button className="btn btn-quiet px-2 py-0.5 text-xs" aria-label={`Move ${file.name} up`}
                disabled={i === 0} onClick={() => move(i, i - 1)}>↑</button>
              <button className="btn btn-quiet px-2 py-0.5 text-xs" aria-label={`Leave out ${file.name}`}
                onClick={() => setOrder((list) => list.filter((_, k) => k !== i))}>✕</button>
            </li>
          ))}
        </ol>
        <button className="btn btn-quiet px-3 py-1.5 text-xs" disabled={order.length < 2}
          onClick={() => setOrder((list) => [...list].reverse())}>Reverse the order</button>
        <label className="block">
          <span className="text-xs text-hint">Novel</span>
          <select className="input mt-1 w-full py-3 text-base" value={pid} onChange={(e) => setPid(e.target.value)}>
            <option value="">Choose a novel…</option>
            {(novels || []).map((n) => <option key={n.id} value={n.id}>{n.name}</option>)}
          </select>
        </label>
        {pid && (
          <label className="block">
            <span className="text-xs text-hint">Where</span>
            <select className="input mt-1 w-full py-3 text-base" value={target} onChange={(e) => setTarget(e.target.value)}>
              <option value="">New chapter at the end{novel?.next_number ? ` — Chapter ${novel.next_number}` : ''}</option>
              {boxes.map((b) => <option key={b.id} value={b.id}>End of Chapter {b.number}</option>)}
            </select>
          </label>
        )}
        <button
          className="btn btn-primary w-full py-4 text-base"
          disabled={!pid || !checked.files.length || checked.refused.length > 0 || !!sending || !box}
          onClick={send}
        >
          {sending || (already ? 'Already sent — done' : `Send ${checked.files.length} to Night Reader`)}
        </button>
      </>
    )
  }

  return (
    <div className="phone-shell">
      <PhoneBar title="Shared to Night Reader" />
      <main className="phone-body">{body}</main>
    </div>
  )
}
