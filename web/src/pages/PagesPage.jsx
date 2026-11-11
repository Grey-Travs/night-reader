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
  BUSY, HEIC_ADVICE, NEW_BOX, REFUSED, addingInto, applyBoxOrder, applyMove, boxName, checkDrop, groupBoxes,
  hasText, isBuilt, leftNothing, locate, neighbourId, planBoxMove, planPhotoMove, planSplit,
  removeBox, sortFilesForUpload, stillAdding, stoppedMessage, toRead, uploadReport,
} from '../pageBoxes'
import {
  boxMade, boxTurn, cancelUpload, getSnapshot as uploadsNow, onUploadAnswer, queueUpload,
  retryUpload, takeFinished, useUploads,
} from '../uploadQueue'

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
// answers with that page alone — { rev, patch } to fold the page into the rail, or
// { rev, reload: true } to fetch the rail after it.
const RELOAD = { reload: true }

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
 * already shows is dropped — or fetched again, when all it's older than is a page saved
 * since (see apply).
 */
function useRail(pid, showError) {
  const [rail, setRail] = useState(null)
  const [railError, setRailError] = useState(null)
  const railRef = useRef(null)
  const pidRef = useRef(pid)
  const base = useRef(-1)        // the rev of the summary on screen
  const shown = useRef(-1)       // the newest rev on screen: that, or a page saved since
  const missed = useRef(-1)      // the newest rev passed over while edits were pending
  const newest = useRef(null)    // the newest summary handed over, shown or not
  const pending = useRef(0)      // edits queued or in flight
  const behind = useRef(false)   // an earlier answer was passed over, or an edit failed
  const chain = useRef(Promise.resolve())
  const generation = useRef(0)
  const fetchAgain = useRef(null)

  const show = useCallback((next) => { railRef.current = next; setRail(next) }, [])

  // A new novel starts from nothing: its revs have nothing to do with the last one's, and
  // edits still finishing there must not hold this one's rail back.
  useEffect(() => {
    pidRef.current = pid
    generation.current += 1
    base.current = -1
    shown.current = -1
    missed.current = -1
    newest.current = null
    pending.current = 0
    behind.current = false
    show(null)
    setRailError(null)
  }, [pid, show])

  const saw = useCallback((payload) => {
    if (payload?.batches && !(payload.rev < newest.current?.rev)) newest.current = payload
  }, [])

  // A summary older than the one on screen is dropped. One newer than that, but older than
  // a page saved since, is fetched again instead: shown, it would put the page back as it
  // was before the save, and dropped, everything else it carried was lost — the last read
  // results of a job that ended during the save, read just before the save landed, and
  // the photo sat on "Claude is reading" for good. Fetched again, the answer has both.
  const apply = useCallback((payload) => {
    if (!payload) return
    if (payload.rev != null) {
      if (payload.rev < shown.current) {
        if (payload.rev > base.current) fetchAgain.current()
        return
      }
      base.current = payload.rev
      shown.current = payload.rev
    }
    show(payload)
  }, [show])

  // A poll's or an upload's answer. Never shown over edits still on their way — the
  // screen already has them and this payload may not — but remembered, so the rail is
  // fetched again if the edits' own answer turns out to be older.
  const offer = useCallback((payload, forPid) => {
    if (!payload || forPid !== pidRef.current) return
    saw(payload)
    if (pending.current > 0) missed.current = Math.max(missed.current, payload.rev ?? Infinity)
    else apply(payload)
  }, [saw, apply])

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

  // A refresh that fails leaves the screen short of what it was meant to fetch, so the next
  // edit fetches it again rather than trusting the rail as it is.
  const refresh = useCallback(() => loadRail({ quiet: true })
    .then((loaded) => { if (!loaded) behind.current = true }), [loadRail])
  fetchAgain.current = refresh

  // `send` makes the request and says what to show for it (see RELOAD). It is told whether
  // the screen it was queued from is still the one showing, so it knows whether what the
  // server last said here is about its novel. `optimistic` changes the screen now;
  // returning null from it means "can't say, wait for the answer". Resolves true once the
  // edit has been sent and accepted.
  const write = useCallback((send, { context, optimistic } = {}) => {
    // A write for a novel no longer on screen — the box of a drop there whose turn came
    // after the tab moved on, or the autosave flushed as it moves on — still takes its
    // place in the queue, but nothing it says reaches this novel's screen: its answer would
    // show that novel's boxes in place of this one's. Sent on its own instead, ahead of the
    // queue, an autosave could overtake an earlier save of the same photo and be
    // overwritten by it on the server. The queue is one for every novel, so the edits made
    // here do wait for it to go.
    const here = pid === pidRef.current
    const gen = here ? generation.current : -1
    if (here && optimistic && railRef.current) {
      const next = optimistic(railRef.current)
      if (next) show(next)
    }
    if (here) pending.current += 1
    const step = async () => {
      let result = null
      let ok = true
      try {
        result = await send(gen === generation.current)
      } catch (e) {
        ok = false
        // Shown over whatever novel is on screen by now, so it says when the edit was made
        // somewhere else: unqualified, one novel's failed move read as the other novel's.
        showError(e, { context: gen === generation.current ? context
          : `${context} (begun before you changed novels)` })
      }
      if (gen !== generation.current) {
        // Another novel is on screen now — or this one again, after a visit elsewhere. Its
        // boxes were fetched on the way back, maybe before this edit landed: this answer is
        // newer, so a move tapped since is planned from it (see sentBoxes) — planned from
        // the boxes fetched on the way back, it put back an order from before this edit —
        // and the screen is fetched again, or it would keep the order the server no longer has.
        if (pid === pidRef.current) {
          saw(result)
          refresh()
        }
        return ok
      }
      pending.current -= 1
      if (!ok) behind.current = true
      saw(result)
      if (pending.current > 0) {
        // Only the last answer is shown, so what this one carried comes with a fetch after
        // it — a page save's own page too: folded into the rail the last answer left, it
        // went missing from the screen, its photo still "Not read yet" once typed into.
        if (result) behind.current = true
        return ok
      }
      const reload = !!result?.reload || behind.current
      behind.current = false
      if (result?.patch || result?.reload) {
        // Its rev still counts: a refresh sent before this save and answered after it
        // carries the screen from before, and put back a seam or a status just changed.
        // But only as a page saved since the summary on screen (see apply).
        const fresh = result.rev == null || result.rev >= shown.current
        if (fresh && result.rev != null) shown.current = result.rev
        if (fresh && result.patch) show(result.patch(railRef.current))
      } else if (result) apply(result)
      // Whatever was passed over meanwhile is fetched again if it's newer than the summary
      // on screen. Compared with a page save's rev instead, the last read results of a job
      // that ended during the save never reached the screen.
      if (reload || missed.current > base.current) refresh()
      missed.current = -1
      return ok
    }
    const run = chain.current.then(step)
    chain.current = run.catch(() => false)   // one broken step must never stall the rest
    return run
  }, [pid, show, saw, apply, refresh, showError])

  // Resolves once every edit queued so far has been sent. A build reads the photos the
  // server has, so it waits for these first.
  const whenWritten = useCallback(() => chain.current, [])

  // The boxes as the server last said it has them, edits sent so far included. A box order
  // is planned from these when its turn in the queue comes (see moveBox).
  const sentBoxes = useCallback(() => newest.current?.batches || railRef.current?.batches || [], [])

  return { rail, railError, railRef, loadRail, offer, write, whenWritten, sentBoxes }
}

export default function PagesPage() {
  const {
    pid, running, submitTask, showError, reload, setProjectMeta, isShowing, chapters = [],
  } = useOutletContext()
  const toast = useToast()
  const confirm = useConfirm()
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const photoId = params.get('photo')

  const { rail, railError, railRef, loadRail, offer, write, whenWritten, sentBoxes } = useRail(pid, showError)
  const boxes = useMemo(() => (rail ? groupBoxes(rail.pages, rail.batches) : []), [rail])
  // Moves are planned from the rail as it is NOW, optimistic changes included. A plan
  // made from the last render could be a tap behind.
  const boxesNow = () => groupBoxes(railRef.current?.pages, railRef.current?.batches)

  const [current, setCurrent] = useState(null)
  const [recordError, setRecordError] = useState(null)
  const [recordTry, setRecordTry] = useState(0)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState('')
  const [zoom, setZoom] = useState(1)
  const [showBuild, setShowBuild] = useState(false)
  // A build carries on when its dialog is closed, so whether one is on its way is kept
  // here: the dialog opened again meanwhile would let a second start beside it. Kept per
  // novel, since this tab stays open when the novel changes: one flag held the next novel's
  // Build button on "Building…" until the last novel's build answered.
  const [building, setBuilding] = useState({})
  const [expanded, setExpanded] = useState({})
  const [revealBox, setRevealBox] = useState(null)
  const [notices, setNotices] = useState([])
  const fileRef = useRef(null)
  const pickTarget = useRef(NEW_BOX)
  const { jobs: uploadJobs, finished: uploadsDone } = useUploads()
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
  // The Build dialog belongs to the novel it was opened for; left open, it went on to
  // preview the next novel under the first one's choices.
  useEffect(() => { setExpanded({}); setShowBuild(false); openedHere.current = false }, [pid])

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

  // A failed fetch says so, with a retry. It used to leave the editor on its skeleton
  // for good, which on a flaky phone link looked like a photo still loading.
  useEffect(() => {
    if (!photoId) { setCurrent(null); return undefined }
    let alive = true
    setRecordError(null)
    api.page(pid, photoId)
      .then((p) => { if (alive) { setCurrent(p); setDraft(p.text || ''); setZoom(1) } })
      .catch((e) => { if (alive) { setCurrent(null); setRecordError(e) } })
    return () => { alive = false }
  }, [pid, photoId, recordTry])

  // Reading a photo replaces its text on the server, but the record on screen was
  // fetched before that. Fetch it again the moment its row stops being busy, or the
  // reader sits looking at an empty box beside a page that has long been read. The rail
  // doesn't always show the busy part — an answer that came while edits were pending is
  // passed over, and the next one can already be done — so a row whose reading or check
  // is newer than the record's counts too. A plain status change ("Looks good", Skip)
  // never does: fetching then put the server's text over a correction still being saved.
  const rowId = row?.id
  const rowBusy = !!row && BUSY.has(row.status)
  const rowRead = row ? `${row.ocr?.at ?? ''}|${row.verify?.at ?? ''}` : null
  const recordId = current?.id
  const recordRead = current ? `${current.ocr?.at ?? ''}|${current.verify?.at ?? ''}` : null
  const seen = useRef(null)
  useEffect(() => {
    const was = seen.current
    seen.current = rowId ? { id: rowId, busy: rowBusy } : null
    if (!rowId || rowBusy || recordId !== rowId) return undefined
    const finished = was?.id === rowId && was.busy
    if (!finished && rowRead === recordRead) return undefined
    let alive = true
    api.page(pid, rowId)
      .then((p) => {
        if (!alive) return
        setCurrent(p)
        // A correction typed meanwhile is still on its way to the server, and will
        // replace this text there; keep showing it rather than what it replaces.
        if (pendingSave.current?.pageId !== p.id) setDraft(p.text || '')
      })
      .catch(() => {})
    return () => { alive = false }
  }, [pid, rowId, rowBusy, rowRead, recordId, recordRead])

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
      return refresh ? { rev: updated.rev, reload: true }
        : { rev: updated.rev, patch: (r) => withRecord(r, updated) }
    }, { context, optimistic })
  }

  // A status change moves box pills and the Build button, which the page's own answer
  // can't show, so it fetches the boxes again once the queue is clear.
  //
  // A correction still waiting on the debounce goes in the same request. Sent on its own
  // afterwards, it reached the server last, and text saved without a status marks the
  // page edited: a cover whose junk text was cleared and then skipped at once came back
  // un-skipped, and went into the chapter at the next build.
  function persist(fields, { quiet = false } = {}) {
    if (!photoId) return null
    const status = 'status' in fields
    let body = fields
    const typed = pendingSave.current
    if (typed?.pid === pid && typed.pageId === photoId) {
      clearTimeout(saveTimer.current)
      pendingSave.current = null
      body = { text: typed.text, ...fields }
    } else flushSave()
    return saveFields(pid, photoId, body, {
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
  // The queue itself outlives this tab (see uploadQueue.js). The tab shows each photo's
  // answer while it's on screen, and reports each drop once it has gone in.
  useEffect(() => onUploadAnswer(offer), [offer])

  // Named from the boxes as they are now, so it waits for them to load — which also
  // covers a drop that finished while this tab was closed or showing another novel.
  useEffect(() => {
    const now = railRef.current
    if (!now) return
    for (const job of takeFinished(pid)) {
      const { toast: message, notices: lines } = uploadReport(job, groupBoxes(now.pages, now.batches))
      if (message) toast(message)
      for (const line of lines) notice(line, pid)
      // Only a box the drop left empty: one it put photos in can look empty to a screen
      // still behind its answers, and went from the screen with "2 photos added" just said.
      for (const each of [job, ...(job.cancelled || [])]) if (each.batch && leftNothing(each)) removeUnused(each.batch)
    }
  }, [uploadsDone, rail, railRef, pid, toast, notice])

  // The empty box at the end, unless photos are already on their way into it: that box
  // already IS the new chapter. Another one after it would leave it holding up every
  // later box at Build time with "No photos yet" — the natural way to get there being
  // "Add chapter", then dropping the photos on "New chapter".
  //
  // None while a drop for this novel has a box the screen doesn't show yet — one still
  // being made, or made with its answer still to come. That box is past this one, so
  // photos sent here would come ahead of the drop that made it.
  function emptyLastBox() {
    const now = boxesNow().filter((b) => !b.orphan)
    const mine = uploadsNow().jobs.filter((job) => job.pid === pid)
    if (mine.some((job) => !now.some((b) => b.id === job.batch))) return null
    const last = now.at(-1)
    // A hands-free box with no photos yet is a phone chapter about to arrive, not a box
    // waiting for this drop.
    if (!last || isBuilt(last) || last.pages.length || last.hands_free) return null
    return mine.some((job) => job.batch === last.id) ? null : last
  }

  // Makes the box for a new chapter's drop, in the write queue with every other box change,
  // so whatever is made after the drop comes after it (see uploadQueue.js). A box that
  // can't be made stops the drop, whose banner says why and offers to try again. The error
  // dialog it used to open named no photo, and the drop was gone by the time it closed.
  function makeBox(jobId) {
    write(async () => {
      if (!boxTurn(jobId)) return null
      let res
      try {
        res = await api.createPageBatch(pid, {})
      } catch (error) {
        boxMade(jobId, { error })
        return null
      }
      if (!boxMade(jobId, { batch: res.batch.id })) removeUnused(res.batch.id)
      return res.pages
    })
  }

  // The drops of this novel with no box yet. A box asked for now comes after theirs, so it
  // isn't made if, when its turn in the write queue comes, one of them still has none —
  // its box couldn't be made, or it waits behind one that couldn't. Made then, it went
  // ahead of that chapter, whose box is made at the end once it's tried again: pressed
  // while a new chapter's box was on its way, "Add chapter", and the next chapter dropped
  // into its box, came before the chapter dropped first.
  const boxless = () => uploadsNow().jobs.filter((job) => job.pid === pid && !job.batch).map((job) => job.id)
  function stillBoxless(ids) {
    if (!uploadsNow().jobs.some((job) => ids.includes(job.id) && !job.batch)) return false
    toast('Photos dropped before this are still waiting for their new chapter. Add them, '
      + 'or cancel them, first.')
    return true
  }

  // A box made for a drop that put nothing in it — every photo already in the novel, or
  // the drop cancelled — is removed again. Left, it sat empty at the end, and Build held
  // every box after it back with "No photos yet" for a chapter nobody asked for. The
  // server is told it must be empty, so a photo that got in after all (its answer lost)
  // keeps it.
  function removeUnused(id) {
    if (boxesNow().find((b) => b.id === id)?.pages.length) return
    if (uploadsNow().jobs.some((job) => job.pid === pid && job.batch === id)) return
    write(async () => {
      try {
        return (await api.deletePageBatch(pid, id, { expect_page_ids: [] })).pages
      } catch (e) {
        if (e.status === 409 || e.status === 404) return RELOAD
        throw e
      }
    }, { context: 'removing an unused chapter box', optimistic: (r) => removeBox(r, id) })
  }

  function enqueueUpload(fileList, target) {
    const { files, refused, skipped } = checkDrop(Array.from(fileList || []))
    if (refused.length) {
      notice(`Nothing was added — ${refused.length === 1 ? 'this photo' : 'these'} can’t go in:\n`
        + refused.map((r) => `· ${r.name} — ${REFUSED[r.reason]}`).join('\n')
        + (refused.some((r) => r.reason === 'heic') ? `\n\n${HEIC_ADVICE}` : ''), pid)
      return
    }
    if (!files.length) {
      if (skipped.length) notice(`Nothing to add: ${skipped.map((s) => `${s.name} (${s.reason})`).join(', ')}.`, pid)
      return
    }
    queueUpload({
      pid, target: target === NEW_BOX ? (emptyLastBox()?.id ?? '') : target, makeBox,
      files: sortFilesForUpload(files), skipped,
    })
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
  //
  // It reads the photos as the server has them, so it waits for edits still on their
  // way, as a build does.
  const startPageWork = (what, call, context, done) => run(what, async () => {
    await whenWritten()
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
    const ids = toRead(box).map((p) => p.id)
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

  // A correction still waiting on the debounce goes in first. Sent after, it marked the
  // page "edited" while it sat queued for reading — so the text box stopped being
  // read-only with Claude's text still to come — and a check compared the photo with
  // the text from before the correction.
  const rereadOne = () => {
    flushSave()
    return startPageWork(
      'reread', () => api.readPages(pid, [photoId]), 'reading that page',
      () => toast('Re-reading that page…'),
    )
  }

  const verifyOne = () => {
    flushSave()
    return startPageWork(
      'verify1', () => api.verifyPages(pid, [photoId]), 'checking that page',
      () => toast('Checking that page against its photo…'),
    )
  }

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
    // The confirm outlives a change of novel: answered on the next novel's screen, it
    // deleted a photo there was no longer any sign of, and put its neighbour in the URL.
    if (!ok || !onScreen(pid)) return
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

  // The controls for these are disabled already (see addingInto); this catches a tap
  // planned from a screen a moment old.
  function stillGoingIn(boxIds) {
    const into = addingInto(uploadsNow().jobs, pid)
    const busyBox = boxIds.find((id) => into.has(id))
    if (busyBox === undefined) return false
    toast(stillAdding(into.get(busyBox)))
    return true
  }

  const fromBoxes = (ids) => ids.map((id) => locate(boxesNow(), id)?.box.id)

  const moveTo = (plan) => plan && !stillGoingIn([plan.batch, ...fromBoxes(plan.ids)]) && write(
    async () => (await api.movePages(pid, plan)).pages,
    { context: 'moving that photo', optimistic: (r) => applyMove(r, plan) },
  )

  async function splitHere() {
    const plan = planSplit(boxesNow(), photoId)
    if (!plan || stillGoingIn([plan.after])) return
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
    const empty = emptyLastBox()
    if (empty) { setRevealBox(empty.id); return }
    const before = boxless()
    let made = null
    write(async () => {
      if (stillBoxless(before)) return null
      const res = await api.createPageBatch(pid, {})
      made = res.batch
      return res.pages
    }, { context: 'adding a chapter' }).then(() => { if (made) setRevealBox(made.id) })
  }

  function newBoxFrom(pageId) {
    if (stillGoingIn(fromBoxes([pageId]))) return null
    const empty = emptyLastBox()
    if (empty) return moveTo({ ids: [pageId], batch: empty.id, before: null })
    const before = boxless()
    return write(async () => {
      if (stillBoxless(before)) return null
      return (await api.createPageBatch(pid, { ids: [pageId] })).pages
    }, { context: 'starting a new chapter' })
  }

  // The order is planned again when its turn in the write queue comes, from the boxes the
  // server has by then. Planned at the click, it left out a box made after it — a new
  // chapter's, on its way ahead of this — and the server refused an order missing a box.
  function moveBox(id, delta) {
    const ids = planBoxMove(boxesNow(), id, delta)
    if (!ids) return
    write(async (current) => {
      // Once the tab has left this novel, even to come back, the boxes it holds weren't
      // fetched by this queue — the ones on the way back can predate a move still ahead of
      // this one — so the server is asked. Planned from those, a second tap on ↑ was taken
      // for one already made, and dropped.
      const sent = current ? sentBoxes() : (await api.pages(pid)).batches
      const order = planBoxMove(sent, id, delta)
      return order ? (await api.reorderPageBatches(pid, order)).pages : RELOAD
    }, {
      context: 'moving that chapter',
      optimistic: (r) => applyBoxOrder(r, ids),
    })
  }

  const renameBox = (id, label) => write(async () => (await api.renamePageBatch(pid, id, label)).pages, {
    context: 'renaming that chapter',
    optimistic: (r) => ({ ...r, batches: r.batches.map((b) => (b.id === id ? { ...b, label } : b)) }),
  })

  // A phone chapter whose Done never came holds back every chapter after it. Finishing
  // it here lets it build, as Done would have.
  const finishBox = (box) => write(
    async () => (await api.markCaptured(pid, box.id)).pages,
    { context: `finishing ${boxName(box)}` },
  )

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
      // Answered on another novel's screen, it deleted this one's chapter, and "Chapter N
      // deleted" read as that novel's.
      if (!ok || !onScreen(pid)) return
    }
    const done = await write(
      async () => (await api.deletePageBatch(pid, box.id, { expect_page_ids: ids })).pages,
      { context: 'deleting that chapter', optimistic: (r) => removeBox(r, box.id) },
    )
    if (done && ids.length) toast(`${boxName(box)} deleted`)
  }

  // A stable onClose: Modal refocuses itself whenever it changes, and an inline one
  // changed on every 2.5s poll, pulling focus off whatever was being clicked.
  const closeBuild = useCallback(() => setShowBuild(false), [])

  // A build can answer after its dialog was closed and the reader moved to another novel.
  // Its project and chapter list then belong to the novel it was started for: put into the
  // layout, they showed one novel's chapters under the other's name, with row actions
  // pointing at the wrong chapter numbers. Says whether that novel is still on screen.
  const pidNow = useRef(pid)
  pidNow.current = pid
  const onScreen = (forPid) => (isShowing ? isShowing(forPid) : forPid === pidNow.current)
  const afterBuild = (res, forPid) => {
    if (!onScreen(forPid)) return false
    setProjectMeta?.(res.project || {})
    reload?.(true)
    loadRail({ quiet: true })
    return true
  }

  // ---- render ----------------------------------------------------------------------
  const myUploads = uploadJobs.filter((job) => job.pid === pid)
  const uploadingInto = addingInto(uploadJobs, pid)
  const myNotices = notices.filter((n) => n.pid === pid)
  const anyText = pages.some((p) => p.has_text)
  const unbuilt = boxes.filter((b) => !b.orphan && !isBuilt(b)).length
  const builtCount = rail?.chapter_count || 0

  // Stop pauses the chapters sent from the phone too, until Resume — here or on the phone.
  const resumeHandsFree = async () => {
    try {
      await api.resumeHandsFree(pid)
    } catch (e) {
      showError(e, { context: 'letting the phone’s chapters carry on' })
      return
    }
    loadRail({ quiet: true })
  }

  const banners = (
    <>
      {rail?.hands_free_paused_at && (
        <div className="mb-3 flex items-start gap-3 rounded-card px-3 py-2 text-sm pill-review" role="status">
          <p className="flex-1">
            Hands-free is paused: chapters sent from the phone wait here until you resume.
          </p>
          <button className="btn btn-quiet shrink-0 px-2 py-0.5 text-xs" onClick={resumeHandsFree}>
            Resume
          </button>
        </div>
      )}
      <UploadStatus
        jobs={myUploads} boxes={boxes}
        onRetry={retryUpload} onCancel={cancelUpload}
        onNewChapter={(id) => retryUpload(id, { makeBox })}
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
        {rail == null && railError ? (
          <RailError error={railError} onRetry={() => loadRail()} />
        ) : rail == null ? (
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
            pageError={recordError} onRetryPage={() => setRecordTry((n) => n + 1)}
            draft={draft} onDraft={onDraft} onPersist={persist} zoom={zoom} setZoom={setZoom}
            busy={busy} running={running} uploadingInto={uploadingInto}
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
            <button
              className="btn btn-primary px-3 py-1.5 text-sm" disabled={!!busy || running || !!building[pid]}
              onClick={() => setShowBuild(true)}
            >
              {building[pid] ? 'Building…' : 'Build chapters →'}
            </button>
          )}
        </div>
      </div>

      {banners}

      {rail == null && railError ? (
        <RailError error={railError} onRetry={() => loadRail()} />
      ) : rail == null ? (
        <SkeletonRows rows={8} />
      ) : (
        <ChapterBoxes
          pid={pid} boxes={boxes} chapters={chapters} expanded={expanded}
          busy={busy} running={running} uploadingInto={uploadingInto}
          onToggle={(id, open) => setExpanded((e) => ({ ...e, [id]: open }))}
          onOpenPhoto={openPhoto} onAddPhotos={pickPhotos} onDropFiles={enqueueUpload}
          onMove={moveTo} onNewBoxFrom={newBoxFrom} onAddBox={addBox}
          onMoveBox={moveBox} onRenameBox={renameBox} onDeleteBox={deleteBox} onFinishBox={finishBox}
          onReadBox={readBox} onSetSeam={setSeam}
          onOpenChapter={(index) => navigate(`/novel/${pid}/chapter/${index}`)}
        />
      )}

      {showBuild && (
        <BuildChaptersDialog
          pid={pid} boxes={boxes} chapters={chapters} whenWritten={whenWritten}
          onClose={closeBuild} onBuilt={afterBuild}
          onWorking={(on) => setBuilding((all) => ({ ...all, [pid]: on }))}
          onNotice={(text) => notice(text, pid)} showError={showError}
        />
      )}
    </div>
  )
}

// Without this the screen sat on a skeleton forever while an error dialog reopened
// behind it — in the photo view too, where a photo opened from a link loads the boxes
// first. An inline message with a retry is dismissable.
function RailError({ error, onRetry }) {
  return (
    <div className="rounded-card border border-dashed border-line p-10 text-center">
      <p className="text-sm pill-review mx-auto inline-block rounded-card px-3 py-2">
        {error.message || String(error)}
      </p>
      <p className="mt-3 text-xs text-hint">
        The app may have stopped. Check the window it started in, then try again.
      </p>
      <button className="btn btn-primary mt-3 px-4 py-2 text-sm" onClick={onRetry}>
        Try again
      </button>
    </div>
  )
}

// What the upload queue is doing, for this novel: the drop going in now, the ones
// waiting behind it, and a drop that stopped — which says exactly where.
function UploadStatus({ jobs, boxes, onRetry, onCancel, onNewChapter, onDetails }) {
  if (!jobs.length) return null
  const stopped = jobs.find((job) => job.stopped)
  // A drop still waiting for its new box isn't sent, so the one going in is the first
  // that has its box.
  // A drop ahead of the stopped one is still going in, and says so: hidden behind the stop,
  // it looked as if every photo had stopped. The ones after the stop are counted there, and
  // can't be the one going in: they wait for the stop.
  const ahead = jobs.slice(0, stopped ? jobs.indexOf(stopped) : jobs.length)
  const going = ahead.find((job) => job.batch) || ahead[0]
  const waiting = ahead.filter((job) => job !== going)
  // Cancelling a stopped drop cancels the drops behind it too, so the button says so:
  // "Cancel the rest" alone read as the rest of this drop, and the next chapter's
  // photos went with it unannounced.
  const behind = stopped ? jobs.length - 1 - jobs.indexOf(stopped) : 0
  // A drop with no box to go on into — never made, or gone — can only go on into a new
  // one: tried again or skipped, it had nowhere to go.
  const noBox = stopped && (!stopped.batch || stopped.stopped.error?.status === 404)
  const dest = (job) => {
    const box = job.batch && boxes.find((b) => b.id === job.batch)
    return box ? boxName(box) : job.batch ? 'its chapter' : 'a new chapter'
  }
  return (
    <div className="mb-3 space-y-2">
      {stopped && (
        <div className="rounded-card px-3 py-2 text-sm pill-review">
          <p>{stoppedMessage(stopped, boxes)}</p>
          {behind > 0 && (
            <p className="mt-1 text-xs">
              {plural(behind, 'more drop')} {behind === 1 ? 'is' : 'are'} waiting behind it.
            </p>
          )}
          <div className="mt-2 flex flex-wrap gap-2">
            {noBox ? (
              <button className="btn btn-primary px-3 py-1 text-xs" onClick={() => onNewChapter(stopped.id)}>
                Add the rest as a new chapter
              </button>
            ) : (
              <>
                <button className="btn btn-primary px-3 py-1 text-xs" onClick={() => onRetry(stopped.id)}>
                  Try the rest again
                </button>
                <button className="btn btn-ghost px-3 py-1 text-xs" onClick={() => onRetry(stopped.id, { skip: true })}>
                  Skip this photo
                </button>
              </>
            )}
            <button className="btn btn-ghost px-3 py-1 text-xs" onClick={() => onCancel(stopped.id)}>
              {behind ? `Cancel the rest and the ${behind === 1 ? 'drop' : `${behind} drops`} behind it`
                : 'Cancel the rest'}
            </button>
            <button className="btn btn-quiet px-3 py-1 text-xs" onClick={() => onDetails(stopped.stopped.error)}>
              Details
            </button>
          </div>
        </div>
      )}
      {going && (
        <div className="rounded-card px-3 py-2 text-sm pill-queued">
          {going.batch
            ? `Adding ${Math.min(going.next + 1, going.files.length)} of ${going.files.length} to ${dest(going)}…`
            : `Starting a new chapter for ${plural(going.files.length - going.next, 'photo')}…`}
          {waiting.length > 0 && ` Then ${plural(waiting.length, 'more drop')}.`}
        </div>
      )}
    </div>
  )
}
