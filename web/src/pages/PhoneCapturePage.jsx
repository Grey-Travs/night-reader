import { useEffect, useRef, useState } from 'react'
import { Navigate, useNavigate, useSearchParams } from 'react-router-dom'
import { api } from '../api'
import { encodePicked } from '../camera'
import { usePhoneOutbox } from '../outbox'
import { HEIC_ADVICE, REFUSED, checkDrop, sortFilesForUpload } from '../pageBoxes'
import PhoneBar from '../components/PhoneBar'
import PhoneCamera, { RetakeCamera } from '../components/PhoneCamera'

// "Capture a chapter": choose the novel, then photograph its next chapter page by page.
//
// Where you are lives in the address — no novel, a novel, a chapter being shot — so the
// back gesture walks back through it, and a reload lands where it was.
//
// The chapter number shown is only what the computer said last; the number its box gets
// replaces it. No label is ever sent, because a label becomes the chapter's title ahead
// of the heading Claude reads off the first photo.

const LAST_NOVEL = 'nr.phone.novel'

function remembered() {
  try { return localStorage.getItem(LAST_NOVEL) || '' } catch { return '' }
}

function remember(pid) {
  try { localStorage.setItem(LAST_NOVEL, pid) } catch { /* only a convenience */ }
}

/** The photographed novels: from the computer when it answers, else as last seen. */
function useNovels(box) {
  const [state, setState] = useState({ list: null, offline: false, tick: 0 })
  useEffect(() => {
    if (!box) return undefined
    let alive = true
    api.scans().then((res) => {
      const list = res?.novels || []
      if (!alive) return
      setState((s) => ({ ...s, list, offline: false }))
      box.cacheNovels(list).catch?.(() => {})
    }).catch(async () => {
      const cached = await box.cachedNovels()
      if (alive) setState((s) => ({ ...s, list: cached || [], offline: true }))
    })
    return () => { alive = false }
  }, [box, state.tick])
  return { ...state, refresh: () => setState((s) => ({ ...s, tick: s.tick + 1 })) }
}

/** The number the next chapter of this novel will most likely get. */
export function nextNumber(novel, chapters, pid) {
  const base = novel?.next_number || (novel?.chapter_count ?? 0) + 1
  const waiting = chapters.filter((c) => c.pid === pid && !c.box && c.state !== 'discarding').length
  return base + waiting
}

export default function PhoneCapturePage() {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const pid = params.get('pid') || ''
  const shoot = params.get('shoot') || ''
  const prevId = params.get('prev') || ''
  const retake = params.get('retake') || ''
  // Where a retake goes back to: only ever a phone screen, never off the app.
  const back = (params.get('back') || '').startsWith('/phone') ? params.get('back') : '/phone'
  const [box, outbox] = usePhoneOutbox()
  const novels = useNovels(box)
  const novel = novels.list?.find((n) => n.id === pid) || null
  // Where a button just sent the screen. The address catches up a moment later — after
  // the outbox has already said the chapter being left is closed — and read on its own,
  // that moment sent the camera back to the start and opened it again.
  const heading = useRef(null)

  if (!pid) {
    return (
      <ChooseNovel
        novels={novels}
        onChoose={(id) => { remember(id); setParams({ pid: id }) }}
      />
    )
  }
  if (!outbox.ready) {
    return <div className="phone-shell"><PhoneBar title="Capture" /><p className="phone-body text-sm text-hint">Loading…</p></div>
  }

  if (retake) {
    return (
      <RetakeCamera
        box={box} pid={pid} pageId={retake} label={params.get('label') || 'Taking a photo again'}
        onDone={() => navigate(back, { replace: true })}
      />
    )
  }

  if (heading.current?.leaving) return null
  if (heading.current && heading.current.shoot === shoot) heading.current = null
  const showing = heading.current?.shoot || shoot
  if (showing) {
    const chapter = outbox.chapters.find((c) => c.id === showing)
    if (!chapter || chapter.state !== 'open') return <Navigate to={`/phone/capture?pid=${pid}`} replace />
    const wasId = heading.current ? heading.current.prev : prevId
    const prev = wasId ? outbox.chapters.find((c) => c.id === wasId && c.held) : null
    const name = novel?.name || ''
    const go = (to) => {
      heading.current = to
      setParams(to.prev ? { pid, shoot: to.shoot, prev: to.prev } : { pid, shoot: to.shoot }, { replace: true })
    }
    const next = async () => {
      const n = (chapter.number ?? chapter.shown ?? 0) + 1
      const made = await box.startChapter({ pid, novel: name, shown: n, expect: n })
      go({ shoot: made.id, prev: chapter.id })
      await box.closeChapter(chapter.id, { hold: true })
    }
    const backTo = async () => {
      if (!prev || !(await box.reopen(prev.id))) return
      go({ shoot: prev.id })
      await box.discard(chapter.id)
    }
    const away = () => {
      heading.current = { leaving: true }
      navigate('/phone')
    }
    const done = async () => {
      away()
      if (chapter.photos.length) await box.closeChapter(chapter.id)
      else await box.discard(chapter.id)
      if (prev) box.release(prev.id)
    }
    const leave = () => {
      away()
      if (prev) box.release(prev.id)
    }
    return (
      <PhoneCamera
        key={pid} box={box} chapter={chapter} prev={prev}
        onNext={next} onBackTo={backTo} onDone={done} onLeave={leave}
        onShot={() => { if (prev) box.release(prev.id) }}
      />
    )
  }

  return (
    <Ready
      box={box} outbox={outbox} pid={pid} novel={novel} offline={novels.offline}
      onShoot={(id) => setParams({ pid, shoot: id })}
      onSent={() => navigate('/phone')}
    />
  )
}

function ChooseNovel({ novels, onChoose }) {
  const [name, setName] = useState('')
  const [making, setMaking] = useState(false)
  const [error, setError] = useState('')
  const last = remembered()
  const list = [...(novels.list || [])].sort((a, b) => (b.id === last) - (a.id === last))

  async function create() {
    setMaking(true)
    setError('')
    try {
      const made = await api.createImagesProject(name.trim() || 'Untitled novel')
      onChoose(made.id)
    } catch (e) {
      setError(e.message || 'The computer couldn’t be reached, so a new novel can’t be made now.')
    } finally {
      setMaking(false)
    }
  }

  return (
    <div className="phone-shell">
      <PhoneBar title="Capture a chapter" />
      <main className="phone-body">
        {novels.offline && (
          <p className="rounded-card px-3 py-2 text-xs pill-review">
            The computer can’t be reached. These are the novels as last seen; photos wait on
            this phone until it can.
          </p>
        )}
        {novels.list == null ? (
          <p className="py-6 text-center text-sm text-hint">Loading…</p>
        ) : (
          <ul className="space-y-2" aria-label="Novels">
            {list.map((n) => (
              <li key={n.id}>
                <button className="card w-full px-4 py-3 text-left" onClick={() => onChoose(n.id)}>
                  <span className="block text-sm font-medium">{n.name}</span>
                  <span className="block text-xs text-hint">
                    Next: Chapter {n.next_number || (n.chapter_count ?? 0) + 1}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
        <section className="card space-y-2 p-4">
          <h2 className="text-sm font-medium">A new novel</h2>
          <input
            className="input w-full py-3 text-base" placeholder="Its name"
            value={name} onChange={(e) => setName(e.target.value)} aria-label="New novel name"
          />
          <button className="btn btn-quiet w-full py-3 text-sm" disabled={making} onClick={create}>
            {making ? 'Making it…' : 'Start a new novel'}
          </button>
          {error && <p className="text-xs pill-review rounded-card px-2 py-1">{error}</p>}
        </section>
      </main>
    </div>
  )
}

function Ready({ box, outbox, pid, novel, offline, onShoot, onSent }) {
  const [saving, setSaving] = useState(null)   // "Saving 3 of 10…"
  const [notice, setNotice] = useState('')
  const open = outbox.chapters.find((c) => c.pid === pid && c.state === 'open')
  const n = nextNumber(novel, outbox.chapters, pid)

  async function start() {
    const made = await box.startChapter({ pid, novel: novel?.name || '', shown: n, expect: n })
    onShoot(made.id)
  }

  // Photos from the gallery, in the order their names give, as one new chapter.
  async function pick(fileList) {
    const { files, refused } = checkDrop(Array.from(fileList || []))
    if (refused.length) {
      setNotice(`Nothing was added — ${refused.length === 1 ? 'this photo' : 'these'} can’t go in:\n`
        + refused.map((r) => `· ${r.name} — ${REFUSED[r.reason]}`).join('\n')
        + (refused.some((r) => r.reason === 'heic') ? `\n\n${HEIC_ADVICE}` : ''))
      return
    }
    if (!files.length) return
    const ordered = sortFilesForUpload(files)
    const made = await box.startChapter({ pid, novel: novel?.name || '', shown: n, expect: n })
    for (const [i, file] of ordered.entries()) {
      setSaving(`Saving ${i + 1} of ${ordered.length}…`)
      const { blob } = await encodePicked(file)
      await box.addPhoto(made.id, { blob, name: file.name, type: blob.type || 'image/jpeg' })
    }
    await box.closeChapter(made.id)
    setSaving(null)
    onSent()
  }

  const number = open ? (open.number ?? open.shown) : n
  return (
    <div className="phone-shell">
      <PhoneBar title={novel?.name || 'Capture a chapter'} back="/phone/capture" />
      <main className="phone-body">
        {offline && (
          <p className="rounded-card px-3 py-2 text-xs pill-review">
            The computer can’t be reached. Shoot anyway: the photos wait on this phone.
          </p>
        )}
        {open ? (
          <>
            <button className="btn btn-primary w-full py-4 text-base" onClick={() => onShoot(open.id)}>
              Continue Chapter {number}
            </button>
            <p className="text-xs text-hint">
              Chapter {number} isn’t finished yet. Finish it before starting the next one.
            </p>
          </>
        ) : (
          <button className="btn btn-primary w-full py-4 text-base" onClick={start}>
            Capture Chapter {number}
          </button>
        )}
        <label className={`btn btn-quiet w-full py-3 text-center text-sm ${open || saving ? 'pointer-events-none opacity-60' : ''}`}>
          {saving || 'Choose photos'}
          <input
            type="file" accept="image/*" multiple hidden disabled={!!open || !!saving}
            onChange={(e) => { pick(e.target.files); e.target.value = '' }}
          />
        </label>
        {notice && <p className="whitespace-pre-line rounded-card px-3 py-2 text-xs pill-review">{notice}</p>}
        <p className="text-xs text-hint">
          Photos are saved on this phone first and sent in order, so it doesn’t matter if
          the computer is asleep or the signal drops.
        </p>
      </main>
    </div>
  )
}
