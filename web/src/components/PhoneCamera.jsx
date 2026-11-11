import { useEffect, useMemo, useRef, useState } from 'react'
import { captureStill, encodePage, openCamera, sample, wakeLock } from '../camera'
import { flagLine, judge, measure } from '../photoCheck'
import { chapterTally, photoBadge } from '../photoBadge'
import { usePhoneProgress } from '../phoneProgress'
import { AUTO, autoOn, createAutoShutter, grey, rememberAuto } from '../autoShutter'
import BeforeYouGo from './BeforeYouGo'
import PhotoBadge from './PhotoBadge'

// The camera screen: tap the shutter for each page, in order. The photos are saved on
// the phone as they are taken and sent from the outbox, so nothing here waits for the
// computer.
//
// Each shot is checked on the spot for blur, darkness and glare, and a flagged one says
// so at once with a Retake, while the book is still open at that page. Retake puts the
// new shot in the same place, sent or not.
//
// After that each photo's badge follows the computer every couple of seconds: Claude
// Haiku's quick look, then the full read, and when Claude isn't sure, its double-check
// and what it found. Asking also tells the computer the chapter is on screen, so it
// leaves these problems to the badges rather than pushing them.

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`
const SHOWN = 3
const WATCH_MS = 2000

function photoName(n, at = new Date()) {
  const pad = (v, w = 2) => String(v).padStart(w, '0')
  return `NR_${at.getFullYear()}${pad(at.getMonth() + 1)}${pad(at.getDate())}_`
    + `${pad(at.getHours())}${pad(at.getMinutes())}${pad(at.getSeconds())}_${pad(n, 3)}.jpg`
}

/** Object URLs for the photos shown, made and let go of as they change. */
function useThumbs(box, photos) {
  const [urls, setUrls] = useState({})
  const key = photos.map((p) => `${p.id}:${p.version}`).join(',')
  useEffect(() => {
    const made = {}
    for (const p of photos) {
      const blob = box?.blobOf(p.id)
      if (blob && typeof URL !== 'undefined' && URL.createObjectURL) made[p.id] = URL.createObjectURL(blob)
    }
    setUrls(made)
    return () => { for (const url of Object.values(made)) URL.revokeObjectURL?.(url) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [box, key])
  return urls
}

/** The back camera, open while the screen is: closed when the app goes to the
 *  background — a camera left running behind the lock screen keeps it busy for every
 *  other app — and opened again when it comes back. */
function useCameraStream(video, secure) {
  const [cam, setCam] = useState(null)
  const [error, setError] = useState(secure ? null : { code: 'insecure' })
  const lock = useRef(wakeLock())
  useEffect(() => {
    if (!secure) return undefined
    let alive = true
    let opened = null
    const start = async () => {
      try {
        const got = await openCamera()
        if (!alive) { got.stop(); return }
        opened = got
        if (video.current) {
          video.current.srcObject = got.stream
          await video.current.play?.()?.catch?.(() => {})
        }
        setCam(got)
        setError(null)
        lock.current.acquire()
      } catch (e) {
        if (alive) setError(e)
      }
    }
    const stop = () => {
      opened?.stop()
      opened = null
      setCam(null)
      lock.current.release()
    }
    const visibility = () => {
      if (document.visibilityState === 'hidden') stop()
      else if (!opened) start()
    }
    start()
    document.addEventListener('visibilitychange', visibility)
    return () => {
      alive = false
      document.removeEventListener('visibilitychange', visibility)
      stop()
    }
  }, [secure, video])
  return { cam, error }
}

export default function PhoneCamera({
  box, chapter, prev = null, onNext, onBackTo, onDone, onLeave, onShot,
  secure = globalThis.isSecureContext !== false,
}) {
  const video = useRef(null)
  const { cam, error } = useCameraStream(video, secure)
  const [taking, setTaking] = useState(false)
  const [flash, setFlash] = useState(false)
  const [banner, setBanner] = useState(null)     // { text, photoId }
  const [retaking, setRetaking] = useState(null)  // the photo the next shot replaces
  const [sheet, setSheet] = useState(null)        // a photo opened from the strip
  const [torch, setTorch] = useState(false)
  const [leaving, setLeaving] = useState(false)
  const [going, setGoing] = useState(false)       // "Before you go" is open
  const [listing, setListing] = useState(false)   // the flagged photos are open
  const [auto, setAuto] = useState(() => autoOn())
  const [autoPaused, setAutoPaused] = useState(false)
  const [ring, setRing] = useState(0)
  const machine = useRef(null)
  if (!machine.current) machine.current = createAutoShutter()
  const busy = useRef(false)
  const queue = useRef(Promise.resolve())
  const sharpness = useRef(new Map())             // chapter id -> its photos' sharpness
  const shots = useRef(0)                         // for the photos' file names only

  const photos = chapter.photos.filter((p) => p.state !== 'remove')
  const number = chapter.number ?? chapter.shown
  const strip = photos.slice(-SHOWN)
  const urls = useThumbs(box, strip.concat(sheet ? [sheet] : []))

  const progress = usePhoneProgress({ pid: chapter.pid, box: chapter.box || '', every: WATCH_MS,
    enabled: !!chapter.box })
  const rows = useMemo(() => {
    const novel = (progress.novels || []).find((n) => n.pid === chapter.pid)
    return new Map((novel?.photos || []).map((row) => [row.id, row]))
  }, [progress.novels, chapter.pid])
  const badges = photos.map((photo) => ({ photo, badge: photoBadge(photo, rows.get(photo.pageId)) }))
  const badgeOf = new Map(badges.map((b) => [b.photo.id, b.badge]))
  const flagged = badges.filter((b) => b.badge.flag)
  const tally = chapterTally(photos, rows)
  const unsettled = flagged.length || tally.sending || tally.reading || tally.checking

  function finish() {
    if (photos.length && unsettled) setGoing(true)
    else onDone()
  }

  // Saved one at a time, in shutter order: encoding takes longer than tapping.
  function save(source, { replace = null } = {}) {
    const chapterId = chapter.id
    shots.current += 1
    const name = photoName(shots.current)
    queue.current = queue.current.then(async () => {
      const m = measure(sample(source))
      const earlier = sharpness.current.get(chapterId) || []
      const flags = judge(m, earlier)
      sharpness.current.set(chapterId, [...earlier, m.sharpness])
      const { blob } = await encodePage(source)
      source.close?.()
      const check = { flags, ...m }
      const saved = replace
        ? await box.retakePhoto(replace.id, { blob, name, check })
        : await box.addPhoto(chapterId, { blob, name, check })
      if (saved && flags.length) setBanner({ text: flagLine(flags, saved.order), photoId: saved.id })
      onShot?.(saved)
    }).catch((e) => setBanner({ text: `That photo didn’t save: ${e.message || e}. Take it again.`, photoId: null }))
    return queue.current
  }

  async function shoot() {
    if (!cam || busy.current || !video.current) return
    busy.current = true
    // A photo taken by hand counts too: Auto waits for the next page.
    machine.current.shot()
    setRing(0)
    setTaking(true)
    setBanner(null)
    setFlash(true)
    setTimeout(() => setFlash(false), 120)
    navigator.vibrate?.(30)
    const replace = retaking
    setRetaking(null)
    try {
      const source = await captureStill(cam, video.current)
      save(source, { replace })
    } catch (e) {
      setBanner({ text: `The camera didn’t take that one: ${e.message || e}`, photoId: null })
    } finally {
      setTaking(false)
      busy.current = false
    }
  }

  // Auto: sample the preview several times a second, and shoot once a turned page has
  // settled and looks sharp (see autoShutter.js).
  const shootRef = useRef(shoot)
  shootRef.current = shoot
  useEffect(() => {
    if (!cam || !auto || autoPaused || sheet || going || listing || leaving) {
      setRing(0)
      return undefined
    }
    const timer = setInterval(() => {
      const view = video.current
      if (!view || busy.current) return
      let state
      try {
        state = machine.current.feed(grey(sample(view, { edge: AUTO.edge })), Date.now())
      } catch {
        return   // no frame yet
      }
      setRing(state.progress)
      if (!state.ready) return
      const earlier = sharpness.current.get(chapter.id) || []
      if (judge(measure(sample(view)), earlier).includes('blurry')) return   // wait for it to sharpen
      shootRef.current()
    }, AUTO.everyMs)
    return () => clearInterval(timer)
  }, [cam, auto, autoPaused, sheet, going, listing, leaving, chapter.id])

  function toggleAuto() {
    const on = !auto
    setAuto(on)
    rememberAuto(on)
    setAutoPaused(false)
    machine.current.reset()
  }

  // A photo from the phone's own camera app: checked and encoded like a shot when the
  // phone can decode it, and sent as it is when it can't.
  async function picked(files) {
    const replace = retaking
    setRetaking(null)
    for (const file of files) {
      let image = null
      try {
        image = await globalThis.createImageBitmap(file, { imageOrientation: 'from-image' })
      } catch { image = null }
      if (image) {
        await save(image, { replace })
        continue
      }
      const type = file.type || 'image/jpeg'
      const saved = replace
        ? await box.retakePhoto(replace.id, { blob: file, name: file.name, type })
        : await box.addPhoto(chapter.id, { blob: file, name: file.name || photoName(shots.current + 1), type })
      onShot?.(saved)
    }
  }

  async function toggleTorch() {
    const on = !torch
    setTorch(on)
    await cam?.setTorch(on).catch(() => setTorch(!on))
  }

  function retake(photo) {
    if (!photo) return
    setSheet(null)
    setBanner(null)
    setGoing(false)
    setListing(false)
    setRetaking(photo)
  }

  async function remove(photo) {
    setSheet(null)
    if (banner?.photoId === photo.id) setBanner(null)
    await box.removePhoto(photo.id)
  }

  const head = (
    <div className="camera-top">
      <span className="text-sm font-medium">Chapter {number ?? '?'} · {plural(photos.length, 'page')}</span>
      <span className="flex items-center gap-2">
        {flagged.length > 0 && (
          <button className="camera-btn camera-flagged" onClick={() => setListing(true)}>
            {flagged.length} flagged
          </button>
        )}
        <button className="camera-btn" aria-pressed={auto} onClick={toggleAuto}
          title="Takes each page by itself once it is still after a turn">
          {auto ? (autoPaused ? 'Auto paused' : 'Auto on') : 'Auto'}
        </button>
        {cam?.canTorch && (
          <button className="camera-btn" aria-pressed={torch} onClick={toggleTorch}>
            {torch ? 'Torch on' : 'Torch'}
          </button>
        )}
        <button className="camera-btn" aria-label="Close the camera"
          onClick={() => (photos.length ? setLeaving(true) : onLeave())}>✕</button>
      </span>
    </div>
  )

  const strip_ = (
    <div className="camera-thumbs">
      {strip.map((p) => (
        <button key={p.id} className="camera-thumb" aria-label={`Photo ${p.order}: ${badgeOf.get(p.id)?.long || ''}`}
          onClick={() => setSheet(p)}>
          {urls[p.id] ? <img src={urls[p.id]} alt="" /> : <span>{p.order}</span>}
          <PhotoBadge badge={badgeOf.get(p.id)} />
        </button>
      ))}
    </div>
  )

  if (error) {
    return (
      <Fallback
        error={error} chapter={chapter} number={number} photos={photos} head={head}
        onPick={picked} onDone={finish} strip={strip_} banner={banner}
      />
    )
  }

  return (
    <div className="camera" role="dialog" aria-label={`Camera, chapter ${number ?? ''}`}>
      {head}
      <div className="camera-view">
        <video
          ref={video} playsInline muted autoPlay
          onClick={() => { if (auto) setAutoPaused((p) => !p) }}
          aria-label={auto ? (autoPaused ? 'Tap to let Auto carry on' : 'Tap to pause Auto') : undefined}
        />
        <div className={`camera-flash ${flash ? 'on' : ''}`} />
        {retaking && (
          <div className="camera-banner" role="status">
            <span>Retaking photo {retaking.order}</span>
            <button className="camera-btn" onClick={() => setRetaking(null)}>Cancel</button>
          </div>
        )}
        {!retaking && banner && (
          <div className="camera-banner" role="alert">
            <span>{banner.text}</span>
            {banner.photoId && (
              <button className="camera-btn" onClick={() => retake(photos.find((p) => p.id === banner.photoId))}>
                Retake
              </button>
            )}
            <button className="camera-btn" aria-label="Dismiss" onClick={() => setBanner(null)}>✕</button>
          </div>
        )}
        {prev && photos.length === 0 && (
          <button className="camera-btn camera-back" onClick={onBackTo}>
            ← Back to Chapter {prev.number ?? prev.shown}
          </button>
        )}
      </div>
      <div className="camera-bottom">
        {strip_}
        <button
          className="camera-shutter" disabled={!cam || taking}
          style={auto && !autoPaused ? { '--ring': `${Math.round(ring * 360)}deg` } : undefined}
          data-auto={auto && !autoPaused ? 'on' : undefined}
          aria-label={retaking ? `Retake photo ${retaking.order}` : `Take page ${photos.length + 1}`}
          onClick={shoot}
        />
        <span className="flex flex-col items-end gap-2">
          <button className="camera-btn" disabled={!photos.length} onClick={onNext}>
            Chapter {(number ?? 0) + 1} →
          </button>
          <button className="camera-btn" onClick={finish}>Done</button>
        </span>
      </div>
      {going && (
        <BeforeYouGo
          number={number} flagged={flagged} tally={tally} onRetake={retake}
          onFinish={() => { setGoing(false); onDone() }} onKeepShooting={() => setGoing(false)}
        />
      )}
      {listing && (
        <div className="camera-sheet" role="dialog" aria-label="Flagged photos">
          <ul className="space-y-2">
            {flagged.map(({ photo, badge }) => (
              <li key={photo.id} className="flex items-center gap-2 text-sm">
                <span className="flex-1">{badge.long}</span>
                <button className="camera-btn" onClick={() => retake(photo)}>Retake</button>
              </li>
            ))}
          </ul>
          <button className="camera-btn self-center" onClick={() => setListing(false)}>Close</button>
        </div>
      )}
      {sheet && (
        <div className="camera-sheet" role="dialog" aria-label={`Photo ${sheet.order}`}>
          {urls[sheet.id] && <img src={urls[sheet.id]} alt={`Photo ${sheet.order}`} />}
          {badgeOf.get(sheet.id) && <p className="text-center text-sm">{badgeOf.get(sheet.id).long}</p>}
          <div className="flex flex-wrap justify-center gap-2">
            <button className="camera-btn" onClick={() => retake(sheet)}>Retake</button>
            <button className="camera-btn" onClick={() => remove(sheet)}>Delete</button>
            <button className="camera-btn" onClick={() => setSheet(null)}>Close</button>
          </div>
        </div>
      )}
      {leaving && (
        <div className="camera-sheet" role="dialog" aria-label="Leave the camera">
          <p className="text-center text-sm">Chapter {number} has {plural(photos.length, 'page')}.</p>
          <div className="flex flex-wrap justify-center gap-2">
            <button className="camera-btn" onClick={onDone}>Finish chapter</button>
            <button className="camera-btn" onClick={onLeave}>Keep it open</button>
            <button className="camera-btn" onClick={() => setLeaving(false)}>Cancel</button>
          </div>
        </div>
      )}
    </div>
  )
}

/**
 * One photo taken again, from the check screen or the hub after Done: the new shot
 * takes the old one's place on the computer, through the outbox, so it waits on the
 * phone if the computer can't be reached.
 */
export function RetakeCamera({ box, pid, pageId, label, onDone,
  secure = globalThis.isSecureContext !== false }) {
  const video = useRef(null)
  const { cam, error } = useCameraStream(video, secure)
  const [taking, setTaking] = useState(false)
  const [problem, setProblem] = useState('')

  async function keep(source) {
    const m = measure(sample(source))
    const { blob } = await encodePage(source)
    source.close?.()
    await box.retakePage(pid, pageId, { blob, name: photoName(1), check: { flags: judge(m, []), ...m } })
    onDone()
  }

  async function shoot() {
    if (!cam || taking || !video.current) return
    setTaking(true)
    setProblem('')
    navigator.vibrate?.(30)
    try {
      await keep(await captureStill(cam, video.current))
    } catch (e) {
      setProblem(`That didn’t work: ${e.message || e}. Try again.`)
      setTaking(false)
    }
  }

  async function picked(files) {
    const file = files[0]
    if (!file) return
    try {
      await keep(await globalThis.createImageBitmap(file, { imageOrientation: 'from-image' }))
    } catch {
      await box.retakePage(pid, pageId, { blob: file, name: file.name, type: file.type || 'image/jpeg' })
      onDone()
    }
  }

  const head = (
    <div className="camera-top">
      <span className="text-sm font-medium">{label}</span>
      <button className="camera-btn" aria-label="Cancel the retake" onClick={onDone}>✕</button>
    </div>
  )
  if (error) {
    return (
      <Fallback
        error={error} number={null} photos={[]} head={head} onPick={picked} onDone={onDone}
        strip={null} banner={problem ? { text: problem } : null} single
      />
    )
  }
  return (
    <div className="camera" role="dialog" aria-label={label}>
      {head}
      <div className="camera-view">
        <video ref={video} playsInline muted autoPlay />
        {problem && <div className="camera-banner" role="alert"><span>{problem}</span></div>}
      </div>
      <div className="camera-bottom">
        <span />
        <button className="camera-shutter" disabled={!cam || taking} aria-label="Take it again" onClick={shoot} />
        <span />
      </div>
    </div>
  )
}

// Over plain http, with no camera API, or with the camera refused: the phone's own camera
// app, one shot per tap, into the same chapter.
function Fallback({ error, number, photos, head, onPick, onDone, strip, banner, single = false }) {
  const why = error.code === 'denied'
    ? 'The camera isn’t allowed for Night Reader. In Chrome, tap ⋮ → Settings → Site settings → Camera, allow it, then open the camera again.'
    : error.code === 'insecure'
      ? 'The fast camera needs the installed app: open Night Reader from its home-screen icon (installed from the https link in Settings on the computer).'
      : 'The camera couldn’t be opened here, so each page goes through the phone’s camera app instead.'
  return (
    <div className="camera" role="dialog" aria-label={`Camera, chapter ${number ?? ''}`}>
      {head}
      <div className="camera-view camera-fallback">
        <p className="text-sm">{why}</p>
        {banner && <p className="text-sm">{banner.text}</p>}
        <label className="camera-btn camera-big">
          {single ? 'Take it again' : `Take page ${photos.length + 1}`}
          <input
            type="file" accept="image/*" capture="environment" hidden
            onChange={(e) => { onPick(Array.from(e.target.files || [])); e.target.value = '' }}
          />
        </label>
      </div>
      <div className="camera-bottom">
        {strip}
        <span />
        <button className="camera-btn" onClick={onDone}>Done</button>
      </div>
    </div>
  )
}
