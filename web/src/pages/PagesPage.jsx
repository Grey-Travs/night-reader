import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useOutletContext, useSearchParams } from 'react-router-dom'
import { api } from '../api'
import { useToast } from '../toast'
import { useConfirm } from '../confirm'
import { SkeletonRows } from '../components/ui'
import ChapterBoxes from '../components/ChapterBoxes'
import PagePhotoView from '../components/PagePhotoView'
import BuildChaptersDialog from '../components/BuildChaptersDialog'
import {
  BUSY, NEW_BOX, REFUSED, applyBoxOrder, applyMove, boxName, checkDrop, groupBoxes, hasText,
  isBuilt, locate, neighbourId, planBoxMove, planPhotoMove, planSplit, sortFilesForUpload,
  stoppedMessage, uploadReport,
} from '../pageBoxes'

// The scanned-page workbench. The overview is the novel's chapters as boxes, in reading
// order, each holding its photos in order; a photo opens beside the Korean read out of
// it, with the seam to the photo before it.
//
// The seams are the part that matters. Photographing a print book, a sentence
// routinely runs across a page break; if each page is split into paragraphs on its
// own, every one of those becomes a false paragraph break, and the translator
// faithfully preserves it into the English. So the joins sit between the photos, where
// they can be seen and changed, rather than hidden in a dialog.

// What a queued write hands back once it has been sent: the fresh summary (every upload
// and box endpoint answers with one), RELOAD to fetch one, or — for a page save, which
// answers with that page alone — { rev, patch } to fold the page into the rail.
const RELOAD = Symbol('reload')

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`

// The rail row and the page record are different shapes: `has_text` exists only on the
// row, so a plain spread leaves it STALE. Recompute it with `chars`, or a page that just
// gained text keeps has_text false and the Build / Work-out-the-joins buttons stay hidden.
function withRecord(rail, record) {
  if (!rail) return rail
  const { text, raw_text: _raw, rev: _rev, ...fields } = record
  return {
    ...rail,
    pages: rail.pages.map((p) => (p.id === record.id
      ? { ...p, ...fields, chars: (text || '').length, has_text: hasText(text) } : p)),
  }
}

const patchPage = (rail, pageId, fields) => rail && {
  ...rail,
  pages: rail.pages.map((p) => (p.id === pageId ? { ...p, ...fields } : p)),
}

/**
 * The boxes on screen, and every way they change.
 *
 * Three things write to the rail: the poll, the answers to uploads, and this tab's own
 * edits, which show at once and reach the server later. Left to race, a poll that left
 * before a drag and answered after it put the old order back, and a second drag planned
 * from that screen silently undid the first. And edits sent side by side could be
 * applied in any order — the server serves requests from a thread pool on a lock that
 * keeps no queue, and a phone link reorders them on its own — so three quick "Later"
 * taps could land the photo somewhere the screen never showed.
 *
 * So edits go out one at a time, in order, and only the last one's answer is shown
 * (each earlier one would undo the moves queued after it). Polls and upload answers wait
 * while any edit is pending, and anything stamped with an older `rev` than the screen
 * already shows is dropped.
 */
function useRail(pid, showError) {
  const [rail, setRail] = useState(null)
  const [railError, setRailError] = useState(null)
  const railRef = useRef(null)
  const pidRef = useRef(pid)
  const shown = useRef(-1)       // the newest rev on screen
  const missed = useRef(-1)      // the newest rev passed over while edits were pending
  const pending = useRef(0)      // edits queued or in flight
  const behind = useRef(false)   // an earlier answer was passed over, or an edit failed
  const chain = useRef(Promise.resolve())
  const generation = useRef(0)

  const show = useCallback((next) => { railRef.current = next; setRail(next) }, [])

  // A new novel starts from nothing: its revs have nothing to do with the last one's, and
  // edits still finishing there must not hold this one's rail back.
  useEffect(() => {
    pidRef.current = pid
    generation.current += 1
    shown.current = -1
    missed.current = -1
    pending.current = 0
    behind.current = false
    show(null)
    setRailError(null)
  }, [pid, show])

  const apply = useCallback((payload) => {
    if (!payload) return
    if (payload.rev != null) {
      if (payload.rev < shown.current) return
      shown.current = payload.rev
    }
    show(payload)
  }, [show])

  // A poll's or an upload's answer. Never shown over edits still on their way — the
  // screen already has them and this payload may not — but remembered, so the rail is
  // fetched again if the edits' own answer turns out to be older.
  const offer = useCallback((payload, forPid) => {
    if (!payload || forPid !== pidRef.current) return
    if (pending.current > 0) missed.current = Math.max(missed.current, payload.rev ?? Infinity)
    else apply(payload)
  }, [apply])

  // `quiet` refreshes never open the modal. The background poll used to call
  // showError on every failure, so a backend that died mid-run reopened an
  // undismissable dialog every 2.5 seconds — the scrim blocked the sidebar, so the
  // only way out was reloading the browser. Poll failures show inline instead.
  const loadRail = useCallback(async ({ quiet = false } = {}) => {
    try {
      offer(await api.pages(pid), pid)
      if (pid === pidRef.current) setRailError(null)
      return true
    } catch (e) {
      if (pid !== pidRef.current) return false
      setRailError(e)
      if (!quiet) showError(e, { context: 'loading the pages' })
      return false
    }
  }, [pid, offer, showError])

  // `send` makes the request and says what to show for it (see RELOAD). `optimistic`
  // changes the screen now; returning null from it means "can't say, wait for the answer".
  // Resolves true once the edit has been sent and accepted.
  const write = useCallback((send, { context, optimistic } = {}) => {
    const gen = generation.current
    if (optimistic && railRef.current) {
      const next = optimistic(railRef.current)
      if (next) show(next)
    }
    pending.current += 1
    const step = async () => {
      let result = null
      let ok = true
      try {
        result = await send()
      } catch (e) {
        ok = false
        showError(e, { context })
      }
      if (gen !== generation.current) return ok   // another novel is on screen now
      pending.current -= 1
      if (!ok) behind.current = true
      if (pending.current > 0) {
        // A page save's answer is one record; anything else passed over here only
        // reaches the screen with the last answer if that one is a summary too.
        if (result && !result.patch) behind.current = true
        return ok
      }
      const reload = result === RELOAD || behind.current
      behind.current = false
      if (result?.patch) {
        if (result.rev == null || result.rev >= shown.current) {
          if (result.rev != null) shown.current = result.rev
          show(result.patch(railRef.current))
        }
      } else if (result && result !== RELOAD) apply(result)
      if (reload || missed.current > shown.current) loadRail({ quiet: true })
      missed.current = -1
      return ok
    }
    const run = chain.current.then(step)
    chain.current = run.catch(() => false)   // one broken step must never stall the rest
    return run
  }, [show, apply, loadRail, showError])

  // Resolves once every edit queued so far has been sent. A build reads the photos the
  // server has, so it waits for these first.
  const whenWritten = useCallback(() => chain.current, [])

  return { rail, railError, railRef, pidRef, loadRail, offer, write, whenWritten }
}

export default function PagesPage() {
  const { pid, running, submitTask, showError, reload, setProjectMeta, chapters = [] } = useOutletContext()
  const toast = useToast()
  const confirm = useConfirm()
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const photoId = params.get('photo')

  const { rail, railError, railRef, pidRef, loadRail, offer, write, whenWritten } = useRail(pid, showError)
  const boxes = useMemo(() => (rail ? groupBoxes(rail.pages, rail.batches) : []), [rail])
  // Moves are planned from the rail as it is NOW, optimistic changes included. A plan
  // made from the last render could be a tap behind.
  const boxesNow = () => groupBoxes(railRef.current?.pages, railRef.current?.batches)

  const [current, setCurrent] = useState(null)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState('')
  const [zoom, setZoom] = useState(1)
  const [showBuild, setShowBuild] = useState(false)
  const [expanded, setExpanded] = useState({})
  const [revealBox, setRevealBox] = useState(null)
  const [notices, setNotices] = useState([])
  const fileRef = useRef(null)
  const pickTarget = useRef(NEW_BOX)
  const saveTimer = useRef(null)
  // The edit waiting on the debounce, so it can be FLUSHED rather than dropped when
  // the reader navigates away inside the 700ms window.
  const pendingSave = useRef(null)
  const openedHere = useRef(false)
  const returnTo = useRef(null)
  const noticeSeq = useRef(0)

  const pages = rail?.pages || []
  const counts = rail?.counts || {}

  useEffect(() => { loadRail() }, [loadRail])
  useEffect(() => { setExpanded({}); openedHere.current = false }, [pid])

  // While the worker is reading pages, keep the rail honest without a stream of its
  // own — the SSE console already shows the detail. Give up after a few consecutive
  // failures rather than hammering a backend that is plainly gone.
  useEffect(() => {
    if (!running) return undefined
    let misses = 0
    const id = setInterval(async () => {
      const ok = await loadRail({ quiet: true })
      misses = ok ? 0 : misses + 1
      if (misses >= 3) clearInterval(id)
    }, 2500)
    return () => clearInterval(id)
  }, [running, loadRail])

  // One more look when the job ends: the last page often finishes between two ticks,
  // and with the poll stopped it would sit on "Reading" until something else reloaded.
  const wasRunning = useRef(running)
  useEffect(() => {
    if (wasRunning.current && !running) loadRail({ quiet: true })
    wasRunning.current = running
  }, [running, loadRail])

  const notice = useCallback((text, forPid) => {
    noticeSeq.current += 1
    const id = noticeSeq.current
    setNotices((list) => [...list, { id, pid: forPid, text }])
  }, [])

  // ---- the open photo ---------------------------------------------------------------
  const at = photoId ? locate(boxes, photoId) : null
  const row = at ? at.box.pages[at.index] : null

  useEffect(() => {
    if (!photoId) { setCurrent(null); return undefined }
    let alive = true
    api.page(pid, photoId)
      .then((p) => { if (alive) { setCurrent(p); setDraft(p.text || ''); setZoom(1) } })
      .catch(() => { if (alive) setCurrent(null) })
    return () => { alive = false }
  }, [pid, photoId])

  // Reading a photo replaces its text on the server, but the record on screen was
  // fetched before that. Fetch it again the moment its row stops being busy, or the
  // reader sits looking at an empty box beside a page that has long been read.
  const rowId = row?.id
  const rowStatus = row?.status
  const seen = useRef(null)
  useEffect(() => {
    const was = seen.current
    seen.current = rowId ? { id: rowId, status: rowStatus } : null
    return undefined
    let alive = true
    api.page(pid, rowId)
      .then((p) => { if (alive) { setCurrent(p); setDraft(p.text || '') } })
      .catch(() => {})
    return () => { alive = false }
  }, [pid, rowId, rowStatus])

  // ---- saving ------------------------------------------------------------------------
  // Every page save goes through the write queue like a move does, the autosave flush
  // included: sent beside a move, it could reach the server first or last, and its
  // answer could put back a row the move had already changed.
  function saveFields(forPid, pageId, fields, {
    quiet = true, refresh = false, optimistic = null, context = 'saving that page',
  } = {}) {
    return write(async () => {
      const updated = await api.savePage(forPid, pageId, fields)
      // Only touch the editor if that page is STILL the one on screen. A debounced
      // save can land after the reader has moved on, and applying it then put page
      // A's confidence, notes and verify issues beside page B's photo.
      setCurrent((cur) => (cur && cur.id === updated.id ? updated : cur))
      if (!quiet) toast('Saved ✓')
      return refresh ? RELOAD : { rev: updated.rev, patch: (r) => withRecord(r, updated) }
    }, { context, optimistic })
  }

  // A status change moves box pills and the Build button, which the page's own answer
  // can't show, so it fetches the boxes again once the queue is clear.
  function persist(fields, { quiet = false } = {}) {
    if (!photoId) return null
    const status = 'status' in fields
    return saveFields(pid, photoId, fields, {
      quiet,
      refresh: status,
      optimistic: status ? (r) => patchPage(r, photoId, { status: fields.status }) : null,
    })
  }

  function onDraft(value) {
    setDraft(value)
    // The id of the record being edited, not the URL's: the two differ for the moment
    // between stepping to a photo and its text arriving.
    if (!current) return
    pendingSave.current = { pid, pageId: current.id, text: value }
    clearTimeout(saveTimer.current)
    saveTimer.current = setTimeout(() => flushSave(), 700)
  }

  function flushSave({ refresh = false } = {}) {
    clearTimeout(saveTimer.current)
    const p = pendingSave.current
    pendingSave.current = null
    if (p) saveFields(p.pid, p.pageId, { text: p.text }, { refresh })
    return !!p
  }
  const flushRef = useRef(flushSave)
  flushRef.current = flushSave

  // FLUSH a pending autosave, don't cancel it — on unmount, when the novel changes,
  // and when the reader moves to another photo. This cleanup used to call
  // clearTimeout despite claiming to flush, so a correction typed within the 700ms
  // window and then navigated away from was discarded silently: no toast, no
  // warning, no dirty indicator, and the typo was still there on return.
  useEffect(() => () => flushRef.current(), [pid, photoId])

  const setSeam = (pageId, kind) => saveFields(pid, pageId, { join_prev: kind }, {
    refresh: true,
    context: 'changing how those photos join',
    optimistic: (r) => patchPage(r, pageId, { join_prev: kind, join_prev_source: 'user', join_reason: 'set by you' }),
  })

  // ---- moving between the boxes and a photo ------------------------------------------
  function showPhoto(id, { replace = false } = {}) {
    returnTo.current = id
    setParams((prev) => {
      const next = new URLSearchParams(prev)
      next.set('photo', id)
      return next
    }, { replace })
  }

  function openPhoto(id) {
    openedHere.current = true
    showPhoto(id)
    window.scrollTo(0, 0)   // the router keeps no scroll position of its own
  }

  // Steps replace the history entry, so the browser's Back button returns to the boxes
  // instead of walking back through every photo looked at.
  const stepPhoto = (delta) => {
    const id = neighbourId(boxesNow(), photoId, delta)
    if (id) showPhoto(id, { replace: true })
  }

  function leavePhoto() {
    if (openedHere.current) {
      openedHere.current = false
      navigate(-1)
    } else {
      setParams((prev) => {
        const next = new URLSearchParams(prev)
        next.delete('photo')
        return next
      }, { replace: true })
    }
  }

  function backToBoxes() {
    // The boxes are fetched after the last save lands, so a pill doesn't come back
    // showing the state from before the edit. A write with nothing to send does that
    // when there's no save to flush.
    if (!flushSave({ refresh: true })) write(async () => RELOAD)
    if (at) setExpanded((e) => ({ ...e, [at.box.id]: true }))
    leavePhoto()
  }

  // Back on the boxes, bring the photo just looked at into view (its box was opened above).
  useEffect(() => {
    if (photoId || !returnTo.current || !rail) return
    const el = document.querySelector(`[data-page-id="${returnTo.current}"]`)
    returnTo.current = null
    el?.scrollIntoView({ block: 'center' })
    el?.focus()
  }, [photoId, rail])

  // A box just made: scroll to it, ready for photos. Its + tile gets focus, not the file
  // picker — iOS refuses to open one that isn't the direct result of a tap.
  useEffect(() => {
    if (!revealBox || photoId) return
    const box = document.getElementById(`box-${revealBox}`)
    if (!box) return   // not on screen yet; the next rail update brings it
    setRevealBox(null)
    box.scrollIntoView({ block: 'center' })
    box.querySelector('[data-add-photos]')?.focus()
  }, [revealBox, rail, photoId])

  // ---- uploading ---------------------------------------------------------------------
  // One queue for every drop, so two drops can't interleave page by page. A drop's box
  // is fixed when it's queued, and every job remembers its novel: switching novels
  // mid-upload must not send the rest of one novel's photos into another.
  const uploadQueue = useRef([])
  const uploading = useRef(false)
  const jobSeq = useRef(0)
  const [uploads, setUploads] = useState([])
  const showUploads = () => setUploads(uploadQueue.current.map((job) => ({ ...job })))

  // The next job to send. A stopped drop holds back every later drop for the same novel
  // until it's retried or cancelled: going on would put the next drop's photos ahead of
  // the ones that never went in, or create its new chapter first.
  const nextJob = () => uploadQueue.current.find((job, i, all) => !job.stopped
    && !all.slice(0, i).some((j) => j.pid === job.pid && j.stopped))

  function enqueueUpload(fileList, target) {
    const { files, refused, skipped } = checkDrop(Array.from(fileList || []))
    if (refused.length) {
      notice(`Nothing was added — ${refused.length === 1 ? 'this photo' : 'these'} can’t go in:\n`
        + refused.map((r) => `· ${r.name} — ${REFUSED[r.reason]}`).join('\n')
        + (refused.some((r) => r.reason === 'heic')
          ? '\n\nOn an iPhone: Settings → Camera → Formats → Most Compatible, then take or '
            + 'export the photos again as JPEG.' : ''), pid)
      return
    }
    if (!files.length) {
      if (skipped.length) notice(`Nothing to add: ${skipped.map((s) => `${s.name} (${s.reason})`).join(', ')}.`, pid)
      return
    }
    uploadQueue.current.push({
      id: ++jobSeq.current, pid, target, batch: target === NEW_BOX ? '' : target,
      files: sortFilesForUpload(files), next: 0, added: 0, here: 0, elsewhere: [], skipped,
      number: null, stopped: null,
    })
    showUploads()
    pumpUploads()
  }

  async function pumpUploads() {
    if (uploading.current) return
    uploading.current = true
    try {
      for (let job = nextJob(); job; job = nextJob()) {
        await sendUpload(job)
        if (!job.stopped) {
          uploadQueue.current = uploadQueue.current.filter((j) => j !== job)
          reportUpload(job)
        }
        showUploads()
      }
    } finally {
      uploading.current = false
    }
  }

  async function sendUpload(job) {
    while (job.next < job.files.length) {
      const file = job.files[job.next]
      try {
        const res = await api.uploadPage(job.pid, file, { batch: job.batch })
        if (res.duplicate) {
          if (job.batch && res.existing_batch === job.batch) job.here += 1
          else {
            job.elsewhere.push({ name: file.name, batch: res.existing_batch,
                                 number: res.existing_number, pageId: res.page?.id })
          }
        } else {
          job.added += 1
          // Only a photo that really went in names the drop's box. A duplicate's box is
          // some other chapter, and adopting it sent the rest of a new chapter there.
          if (!job.batch) job.batch = res.batch
          if (res.number != null) job.number = res.number
        }
        offer(res.pages, job.pid)
      } catch (e) {
        job.stopped = { index: job.next, name: file.name, error: e }
        return
      }
      job.next += 1
      showUploads()
    }
  }

  function reportUpload(job) {
    if (job.pid !== pidRef.current) return   // the reader has moved to another novel
    const now = railRef.current
    const { toast: message, notices: lines } = uploadReport(job, groupBoxes(now?.pages, now?.batches))
    if (message) toast(message)
    for (const line of lines) notice(line, job.pid)
  }

  function resumeUpload(jobId, { skip = false, asNewChapter = false } = {}) {
    const job = uploadQueue.current.find((j) => j.id === jobId)
    if (!job?.stopped) return
    if (skip) {
      job.skipped = [...job.skipped, { name: job.stopped.name, reason: 'skipped after it failed' }]
      job.next += 1
    }
    // The box this drop was going into is gone; the rest becomes a chapter of its own.
    if (asNewChapter) { job.batch = ''; job.target = NEW_BOX }
    job.stopped = null
    showUploads()
    pumpUploads()
  }

  // Cancelling the rest also drops whatever was queued behind it for this novel — sent
  // now, those photos would go in ahead of the ones that were never sent.
  function cancelUploads(jobId) {
    const all = uploadQueue.current
    const from = all.findIndex((j) => j.id === jobId)
    if (from === -1) return
    const job = all[from]
    uploadQueue.current = all.filter((j, i) => i < from || j.pid !== job.pid)
    reportUpload(job)
    showUploads()
    pumpUploads()
  }

  function pickPhotos(target) {
    pickTarget.current = target
    fileRef.current?.click()
  }

  // A drop that misses every box would make the browser open the photo in place of the
  // app, and whatever was typed and not yet saved went with it.
  useEffect(() => {
    const files = (e) => Array.from(e.dataTransfer?.types || []).includes('Files')
    const over = (e) => { if (files(e)) e.preventDefault() }
    const drop = (e) => {
      if (!files(e) || e.defaultPrevented) return
      e.preventDefault()
      toast(photoId
        ? 'To add photos, go back to the chapters and drop them onto a chapter box.'
        : 'Drop photos onto a chapter box, or onto “New chapter” below the last one.')
    }
    window.addEventListener('dragover', over)
    window.addEventListener('drop', drop)
    return () => {
      window.removeEventListener('dragover', over)
      window.removeEventListener('drop', drop)
    }
  }, [photoId, toast])

  // ---- work ----------------------------------------------------------------
  async function run(what, fn, context) {
    setBusy(what)
    try { return await fn() } catch (e) { showError(e, { context }); return null } finally { setBusy('') }
  }

  // Reading pages is queued on the novel's shared worker exactly like a translation,
  // so it has to be STARTED like one: submitTask attaches the SSE stream and flips
  // `running`. Calling api.readPages directly threw the returned job_id away, so
  // nothing ever attached — the rail's 2.5s poll is gated on `running`, so every row
  // sat on "Queued" forever, the Activity console stayed empty, and the transcribed
  // Korean never appeared however long you waited. The only way to see progress was
  // to reload the browser.
  const startPageWork = (what, call, context, done) => run(what, async () => {
    const res = await submitTask(call, { context })
    if (!res) return null   // submitTask already surfaced the error
    await loadRail()
    done(res)
    return res
  }, context)

  const unread = (counts.new || 0) + (counts.failed || 0)
  const flagged = counts['needs-check'] || 0
  const confident = pages.filter((p) => p.status === 'needs-check' && p.confidence === 'high')

  const readAll = () => startPageWork(
    'read', () => api.readPages(pid, []), 'reading the pages',
    () => toast(`Reading ${unread} page${unread === 1 ? '' : 's'}…`),
  )

  const verifyFlagged = () => startPageWork(
    'verify', () => api.verifyPages(pid, []), 'double-checking the pages',
    () => toast(`Double-checking ${flagged} page${flagged === 1 ? '' : 's'}…`),
  )

  const readBox = (box) => {
    const ids = box.pages.filter((p) => p.status === 'new' || p.status === 'failed').map((p) => p.id)
    return startPageWork(
      'read', () => api.readPages(pid, ids), `reading ${boxName(box)}`,
      () => toast(`Reading ${plural(ids.length, 'photo')}…`),
    )
  }

  // Not queued with the edits: its model call can take many seconds, and every move
  // behind it would wait. The server checks each seam it writes against the photos as
  // they are by then, so its answer is offered like a poll's.
  const stitch = () => run('stitch', async () => {
    const res = await api.stitchPages(pid, { use_model: true })
    offer(res.pages, pid)
    let message = res.gaps
      ? `${res.seams} seams · ${res.gaps} possible missing page${res.gaps === 1 ? '' : 's'}`
      : `${res.seams} seam${res.seams === 1 ? '' : 's'} worked out`
    if (res.remaining > 0) message += ` · ${res.remaining} more unsure — press again`
    toast(message)
  }, 'working out how the pages join')

  const rereadOne = () => startPageWork(
    'reread', () => api.readPages(pid, [photoId]), 'reading that page',
    () => toast('Re-reading that page…'),
  )

  const verifyOne = () => startPageWork(
    'verify1', () => api.verifyPages(pid, [photoId]), 'checking that page',
    () => toast('Checking that page against its photo…'),
  )

  async function acceptAllGood() {
    if (!confident.length) { toast('Nothing to accept'); return }
    await run('accept', async () => {
      const done = await Promise.all(confident.map((p) => saveFields(pid, p.id, { status: 'ok' }, {
        refresh: true,
        context: 'accepting those pages',
        optimistic: (r) => patchPage(r, p.id, { status: 'ok' }),
      })))
      const n = done.filter(Boolean).length
      if (n) toast(`${n} page${n === 1 ? '' : 's'} accepted`)
    }, 'accepting those pages')
  }

  async function deletePhoto() {
    if (!row) return
    const ok = await confirm({
      title: 'Delete this photo?',
      body: 'The photo and everything read from it are removed. This cannot be undone.',
      confirmLabel: 'Delete', danger: true,
    })
    if (!ok) return
    // A save still waiting on the debounce must never be sent for a page that's gone.
    clearTimeout(saveTimer.current)
    pendingSave.current = null
    const gone = row.id
    const now = boxesNow()
    const next = neighbourId(now, gone, 1) || neighbourId(now, gone, -1)
    if (next) showPhoto(next, { replace: true })
    else leavePhoto()
    const done = await write(async () => (await api.deletePages(pid, [gone])).pages,
                             { context: 'deleting that photo' })
    if (done) toast('Photo deleted')
  }

  const moveTo = (plan) => plan && write(
    async () => (await api.movePages(pid, plan)).pages,
    { context: 'moving that photo', optimistic: (r) => applyMove(r, plan) },
  )

  async function splitHere() {
    const plan = planSplit(boxesNow(), photoId)
    if (!plan) return
    let made = null
    const done = await write(async () => {
      const res = await api.createPageBatch(pid, plan)
      made = res.batch
      return res.pages
    }, { context: 'splitting the chapter' })
    if (done && made) toast(`${boxName(made)} now starts at this photo`)
  }

  // ---- the boxes themselves ------------------------------------------------------------
  function addBox() {
    const last = boxesNow().filter((b) => !b.orphan).at(-1)
    // An empty box at the end already is the new chapter. A second one would only hold
    // up every box after it at Build time.
    if (last && !isBuilt(last) && !last.pages.length) { setRevealBox(last.id); return }
    let made = null
    write(async () => {
      const res = await api.createPageBatch(pid, {})
      made = res.batch
      return res.pages
    }, { context: 'adding a chapter' }).then(() => { if (made) setRevealBox(made.id) })
  }

  const newBoxFrom = (pageId) => write(
    async () => (await api.createPageBatch(pid, { ids: [pageId] })).pages,
    { context: 'starting a new chapter' },
  )

  function moveBox(id, delta) {
    const ids = planBoxMove(boxesNow(), id, delta)
    if (!ids) return
    write(async () => (await api.reorderPageBatches(pid, ids)).pages, {
      context: 'moving that chapter',
      optimistic: (r) => applyBoxOrder(r, ids),
    })
  }

  const renameBox = (id, label) => write(async () => (await api.renamePageBatch(pid, id, label)).pages, {
    context: 'renaming that chapter',
    optimistic: (r) => ({ ...r, batches: r.batches.map((b) => (b.id === id ? { ...b, label } : b)) }),
  })

  async function deleteBox(box) {
    // The photos the confirm names are the ones the server is told to expect, so one
    // that arrives while the dialog is open makes it refuse instead of going with them.
    const ids = box.pages.map((p) => p.id)
    if (ids.length) {
      const ok = await confirm({
        title: `Delete ${boxName(box)}?`,
        body: `${boxName(box)} and its ${plural(ids.length, 'photo')} are removed, with the `
          + 'text read from them. This can’t be undone.',
        confirmLabel: 'Delete', danger: true,
      })
      if (!ok) return
    }
    const done = await write(
      async () => (await api.deletePageBatch(pid, box.id, { expect_page_ids: ids })).pages,
      { context: 'deleting that chapter' },
    )
    if (done && ids.length) toast(`${boxName(box)} deleted`)
  }

  // A stable onClose: Modal refocuses itself whenever it changes, and an inline one
  // changed on every 2.5s poll, pulling focus off whatever was being clicked.
  const closeBuild = useCallback(() => setShowBuild(false), [])

  const afterBuild = (res) => {
    setProjectMeta?.(res.project || {})
    reload?.(true)
    loadRail({ quiet: true })
  }

  // ---- render ----------------------------------------------------------------------
  const myUploads = uploads.filter((job) => job.pid === pid)
  const uploadingInto = new Set(myUploads.map((job) => job.batch).filter(Boolean))
  const myNotices = notices.filter((n) => n.pid === pid)
  const anyText = pages.some((p) => p.has_text)
  const unbuilt = boxes.filter((b) => !b.orphan && !isBuilt(b)).length
  const builtCount = rail?.chapter_count || 0

  const banners = (
    <>
      <UploadStatus
        jobs={myUploads} boxes={boxes}
        onRetry={(id, opts) => resumeUpload(id, opts)} onCancel={cancelUploads}
        onDetails={(e) => showError(e, { context: 'adding photos' })}
      />
      {myNotices.map((n) => (
        <div key={n.id} className="mb-3 flex items-start gap-3 rounded-card px-3 py-2 text-sm pill-review">
          <p className="flex-1 whitespace-pre-line">{n.text}</p>
          <button
            className="shrink-0 text-xs underline hover:no-underline"
            onClick={() => setNotices((list) => list.filter((x) => x.id !== n.id))}
          >Dismiss</button>
        </div>
      ))}
    </>
  )

  if (photoId) {
    return (
      <div className="page">
        {banners}
        {rail == null ? (
          <SkeletonRows rows={8} />
        ) : !at ? (
          <div className="rounded-card border border-dashed border-line p-10 text-center">
            <p className="text-sm text-muted">That photo isn’t in this novel any more.</p>
            <button className="btn btn-primary mt-3 px-4 py-2 text-sm" onClick={backToBoxes}>
              Back to chapters
            </button>
          </div>
        ) : (
          <PagePhotoView
            pid={pid} boxes={boxes} box={at.box} index={at.index} row={row} page={current}
            draft={draft} onDraft={onDraft} onPersist={persist} zoom={zoom} setZoom={setZoom}
            busy={busy} running={running}
            onBack={backToBoxes} onStep={stepPhoto}
            onReread={rereadOne} onVerify={verifyOne} onDelete={deletePhoto}
            onSetSeam={(kind) => setSeam(photoId, kind)}
            onMove={(how) => moveTo(planPhotoMove(boxesNow(), photoId, how))}
            onSplit={splitHere}
          />
        )}
      </div>
    )
  }

  return (
    <div className="page">
      <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-reading text-2xl font-medium">Pages</h1>
          <p className="text-sm text-hint">
            {rail == null && railError ? "Couldn't load this novel's pages."
              : rail == null ? 'Loading…'
              : boxes.length === 0 ? 'Add photos or screenshots of the pages, and Claude reads the Korean out of them.'
              : `${plural(builtCount + unbuilt, 'chapter')} · ${builtCount} built · `
                + `${plural(counts.total || 0, 'photo')} · ${(counts.ok || 0) + (counts.edited || 0)} good · `
                + `${flagged} to check · ${counts.new || 0} not read yet`}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <input
            ref={fileRef} type="file" accept="image/jpeg,image/png,image/webp" multiple hidden
            onChange={(e) => { enqueueUpload(e.target.files, pickTarget.current); e.target.value = '' }}
          />
          {rail != null && (
            <button
              className="btn btn-quiet px-3 py-1.5 text-sm" onClick={addBox}
              onDragOver={(e) => { if (Array.from(e.dataTransfer?.types || []).includes('Files')) e.preventDefault() }}
              onDrop={(e) => {
                if (!Array.from(e.dataTransfer?.types || []).includes('Files')) return
                e.preventDefault()
                enqueueUpload(e.dataTransfer.files, NEW_BOX)
              }}
            >
              Add chapter
            </button>
          )}
          {unread > 0 && (
            <button className="btn btn-primary px-3 py-1.5 text-sm" disabled={!!busy || running} onClick={readAll}>
              Read {unread} page{unread === 1 ? '' : 's'}
            </button>
          )}
          {flagged > 0 && (
            <button className="btn btn-quiet px-3 py-1.5 text-sm" disabled={!!busy || running} onClick={verifyFlagged}>
              Double-check {flagged}
            </button>
          )}
          {confident.length > 0 && (
            <button className="btn btn-quiet px-3 py-1.5 text-sm" disabled={!!busy} onClick={acceptAllGood}>
              Accept {confident.length} confident
            </button>
          )}
          {anyText && (
            <button className="btn btn-quiet px-3 py-1.5 text-sm" disabled={!!busy || running} onClick={stitch}>
              {busy === 'stitch' ? 'Working…' : 'Work out the joins'}
            </button>
          )}
          {(anyText || builtCount > 0) && (
            <button className="btn btn-primary px-3 py-1.5 text-sm" disabled={!!busy || running} onClick={() => setShowBuild(true)}>
              Build chapters →
            </button>
          )}
        </div>
      </div>

      {banners}

      {rail == null && railError ? (
        // Without this the screen sat on a skeleton forever while an error dialog
        // reopened behind it. An inline message with a retry is dismissable.
        <div className="rounded-card border border-dashed border-line p-10 text-center">
          <p className="text-sm pill-review mx-auto inline-block rounded-card px-3 py-2">
            {railError.message || String(railError)}
          </p>
          <p className="mt-3 text-xs text-hint">
            The app may have stopped. Check the window it started in, then try again.
          </p>
          <button className="btn btn-primary mt-3 px-4 py-2 text-sm" onClick={() => loadRail()}>
            Try again
          </button>
        </div>
      ) : rail == null ? (
        <SkeletonRows rows={8} />
      ) : (
        <ChapterBoxes
          pid={pid} boxes={boxes} chapters={chapters} expanded={expanded}
          busy={busy} running={running} uploadingInto={uploadingInto}
          onToggle={(id, open) => setExpanded((e) => ({ ...e, [id]: open }))}
          onOpenPhoto={openPhoto} onAddPhotos={pickPhotos} onDropFiles={enqueueUpload}
          onMove={moveTo} onNewBoxFrom={newBoxFrom} onAddBox={addBox}
          onMoveBox={moveBox} onRenameBox={renameBox} onDeleteBox={deleteBox}
          onReadBox={readBox} onSetSeam={setSeam}
          onOpenChapter={(index) => navigate(`/novel/${pid}/chapter/${index}`)}
        />
      )}

      {showBuild && (
        <BuildChaptersDialog
          pid={pid} boxes={boxes} chapters={chapters} whenWritten={whenWritten}
          onClose={closeBuild} onBuilt={afterBuild} showError={showError}
        />
      )}
    </div>
  )
}

// What the upload queue is doing, for this novel: the drop going in now, the ones
// waiting behind it, and a drop that stopped — which says exactly where.
function UploadStatus({ jobs, boxes, onRetry, onCancel, onDetails }) {
  if (!jobs.length) return null
  const stopped = jobs.find((job) => job.stopped)
  const going = jobs.find((job) => !job.stopped)
  const waiting = jobs.filter((job) => job !== stopped && job !== going)
  const dest = (job) => {
    const box = job.batch && boxes.find((b) => b.id === job.batch)
    return box ? boxName(box) : job.batch ? 'its chapter' : 'a new chapter'
  }
  return (
    <div className="mb-3 space-y-2">
      {stopped && (
        <div className="rounded-card px-3 py-2 text-sm pill-review">
          <p>{stoppedMessage(stopped, boxes)}</p>
          {jobs.length > 1 && (
            <p className="mt-1 text-xs">
              {plural(jobs.length - 1, 'more drop')} {jobs.length === 2 ? 'is' : 'are'} waiting behind it.
            </p>
          )}
          <div className="mt-2 flex flex-wrap gap-2">
            {stopped.stopped.error?.status === 404 ? (
              <button className="btn btn-primary px-3 py-1 text-xs" onClick={() => onRetry(stopped.id, { asNewChapter: true })}>
                Add the rest as a new chapter
              </button>
            ) : (
              <button className="btn btn-primary px-3 py-1 text-xs" onClick={() => onRetry(stopped.id)}>
                Try the rest again
              </button>
            )}
            <button className="btn btn-ghost px-3 py-1 text-xs" onClick={() => onRetry(stopped.id, { skip: true })}>
              Skip this photo
            </button>
            <button className="btn btn-ghost px-3 py-1 text-xs" onClick={() => onCancel(stopped.id)}>
              Cancel the rest
            </button>
            <button className="btn btn-quiet px-3 py-1 text-xs" onClick={() => onDetails(stopped.stopped.error)}>
              Details
            </button>
          </div>
        </div>
      )}
      {going && !stopped && (
        <div className="rounded-card px-3 py-2 text-sm pill-queued">
          Adding {Math.min(going.next + 1, going.files.length)} of {going.files.length} to {dest(going)}…
          {waiting.length > 0 && ` Then ${plural(waiting.length, 'more drop')}.`}
        </div>
      )}
    </div>
  )
}
