import { useSyncExternalStore } from 'react'
import { api } from './api'

// The upload queue: every drop, for every novel, going in one photo at a time.
//
// One queue, so two drops can't interleave page by page. A drop's box is fixed when it's
// queued, and every job remembers its novel: switching novels mid-upload must not send
// the rest of one novel's photos into another.
//
// It lives here rather than in the Pages tab because a drop outlives the tab. As the
// tab's own state, switching to Chapters mid-drop left its loop running in a page no
// longer on screen: a photo that failed there stopped the drop with nothing left to say
// so, and the rest of the chapter was never sent. Back on the tab, a fresh, empty queue
// sent the next drop into the same box alongside the old one, and their photos went in
// interleaved — the very mix-up one queue exists to prevent.

let jobs = []        // waiting, going in, or stopped
let finished = []    // done, until the Pages tab of their novel reports them
let snapshot = { jobs, finished }
let seq = 0
let pumping = false
// Bumped by resetUploads, so a drop still awaiting its server from before can't carry
// on into the fresh queue.
let generation = 0
const listeners = new Set()
const answerers = new Set()

// Closing or reloading the app mid-drop sends nothing more, and nothing would say how
// far it got: the rest of the chapter just never arrives. So the browser asks first
// while any drop is waiting, going in, or stopped.
const askFirst = (e) => { e.preventDefault(); e.returnValue = true }
let asking = false

function emit() {
  snapshot = { jobs: jobs.map((job) => ({ ...job })), finished }
  const ask = jobs.length > 0
  if (ask !== asking) {
    asking = ask
    if (ask) window.addEventListener('beforeunload', askFirst)
    else window.removeEventListener('beforeunload', askFirst)
  }
  for (const listener of listeners) listener()
}

export function subscribe(listener) {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

export const getSnapshot = () => snapshot

/** `{ jobs, finished }`, kept current. */
export const useUploads = () => useSyncExternalStore(subscribe, getSnapshot)

/**
 * Each photo's answer carries the novel's fresh summary. Handed to whichever Pages tab
 * is on screen, which shows it if it's for the novel it's showing; with none open it's
 * dropped, and the tab fetches the boxes itself when it opens.
 */
export function onUploadAnswer(fn) {
  answerers.add(fn)
  return () => { answerers.delete(fn) }
}

// The next job to send. A stopped drop holds back every later drop for the same novel
// until it's retried or cancelled: going on would put the next drop's photos ahead of
// the ones that never went in. A drop whose box is still being made waits for it.
const nextJob = () => jobs.find((job, i) => job.batch && !job.stopped
  && !jobs.slice(0, i).some((j) => j.pid === job.pid && j.stopped))

// Every drop knows its box before its first photo goes. A new chapter's box used to be
// made by that first photo, wherever the end of the novel was when its turn came: a box
// made while it waited (Add chapter, a photo dragged to New chapter) landed ahead of it,
// and the two chapters swapped. And when the answer to that first photo was lost, the
// photo sent again came back a duplicate naming no box of the drop's, so the rest of the
// chapter made a second one.
//
// So the Pages tab makes the box when the photos are dropped, in its queue of box
// changes: a new chapter's drop hands over `makeBox`, which queues that, and the tab says
// how it went with boxTurn and boxMade. The boxes are made in the order the drops came. A
// box that can't be made stops its drop, and the new chapters dropped after it wait for
// it to be tried again. Such a drop used to vanish, and the next new chapter took its
// box's place: dropped again, the chapter that came first became the one after it.

function reserve(job) {
  job.reserving = true
  job.makeBox(job.id)
}

// The first drop of the novel with no box has one made, unless it stopped or one is on
// its way already.
function reserveNext(pid) {
  const job = jobs.find((j) => j.pid === pid && !j.batch)
  if (job && !job.stopped && !job.reserving) reserve(job)
}

/**
 * Queues a checked, sorted drop for `target`, a box id — or, for a new chapter, for the
 * box `makeBox(id)` makes (see boxMade).
 */
export function queueUpload({ pid, target = '', makeBox = null, files, skipped = [] }) {
  const job = {
    id: ++seq, pid, batch: target, made: false, makeBox, reserving: false,
    files, next: 0, added: 0, here: 0, elsewhere: [], skipped, number: null, stopped: null,
  }
  jobs = [...jobs, job]
  if (!target) reserve(job)
  emit()
  pump()
}

/**
 * Whether it's the drop `id`'s turn to have its new box made. Not once it has one or is
 * gone, and not while a drop before it in the same novel has none: that one's box comes
 * first, and this one waits until it's made (see boxMade).
 */
export function boxTurn(id) {
  const at = jobs.findIndex((j) => j.id === id)
  const job = jobs[at]
  if (!job || job.batch || job.stopped) return false
  if (jobs.slice(0, at).some((j) => j.pid === job.pid && !j.batch)) {
    job.reserving = false
    return false
  }
  return true
}

/**
 * The box made for the drop `id` — or the error that kept it from being made, which stops
 * the drop there. False when the drop is gone, cancelled while its box was on its way, so
 * the box was made for nothing.
 */
export function boxMade(id, { batch = '', error = null }) {
  const job = jobs.find((j) => j.id === id)
  if (!job) return false
  job.reserving = false
  if (batch) {
    Object.assign(job, { batch, made: true })
    reserveNext(job.pid)
  } else job.stopped = { index: job.next, name: job.files[job.next].name, error }
  emit()
  pump()
  return true
}

async function pump() {
  if (pumping) return
  pumping = true
  const gen = generation
  try {
    for (let job = nextJob(); job; job = nextJob()) {
      await send(job, gen)
      if (gen !== generation) return
      if (!job.stopped) {
        jobs = jobs.filter((j) => j !== job)
        finished = [...finished, job]
        if (job.reopened) refinish(job)
      }
      emit()
    }
  } finally {
    if (gen === generation) pumping = false
  }
}

async function send(job, gen) {
  while (job.next < job.files.length) {
    const file = job.files[job.next]
    let res
    try {
      res = await api.uploadPage(job.pid, file, { batch: job.batch })
    } catch (e) {
      if (gen === generation) job.stopped = { index: job.next, name: file.name, error: e }
      return
    }
    if (gen !== generation) return
    // A duplicate never changes the drop's box: one already in it is a photo sent again
    // after its answer was lost, and one anywhere else is reported, never followed —
    // adopting its box sent the rest of a new chapter into an old one.
    if (res.duplicate) {
      if (res.existing_batch === job.batch) job.here += 1
      else {
        job.elsewhere.push({ name: file.name, batch: res.existing_batch,
                             number: res.existing_number, pageId: res.page?.id })
      }
    } else {
      job.added += 1
      if (res.number != null) job.number = res.number
      // The photo went into a finished phone chapter, which now waits for Done again.
      if (res.reopened) job.reopened = true
    }
    for (const answer of answerers) answer(res.pages, job.pid)
    job.next += 1
    emit()
  }
}

// A drop that went into a finished phone chapter opened it again, since a photo arriving
// there usually means more are coming. This drop is all in, so the chapter is finished
// again, as it was. One that stopped part way leaves it open, to be finished by hand.
async function refinish(job) {
  try {
    const res = await api.markCaptured(job.pid, job.batch)
    for (const answer of answerers) answer(res.pages, job.pid)
  } catch { /* the chapter shows Finish, which does the same */ }
}

/**
 * Goes on with a stopped drop: from the photo that failed, or past it (`skip`). One with
 * no box to go on into — its box gone, or never made — goes on into a new chapter, whose
 * box `makeBox` makes (as for queueUpload).
 */
export function retryUpload(id, { skip = false, makeBox = null } = {}) {
  const job = jobs.find((j) => j.id === id)
  if (!job?.stopped || !(job.batch || makeBox)) return
  if (skip) {
    job.skipped = [...job.skipped, { name: job.stopped.name, reason: 'skipped after it failed' }]
    job.next += 1
  }
  job.stopped = null
  if (makeBox) {
    Object.assign(job, { batch: '', makeBox })
    reserveNext(job.pid)
  }
  emit()
  pump()
}

/**
 * Cancels the rest of a stopped drop, and whatever was queued behind it for that novel —
 * sent now, those photos would go in ahead of the ones that were never sent. The drop is
 * reported as finished, carrying the drops cancelled with it, so they can be named.
 */
export function cancelUpload(id) {
  const from = jobs.findIndex((j) => j.id === id)
  const job = jobs[from]
  if (!job?.stopped) return
  const cancelled = jobs.filter((j, i) => i > from && j.pid === job.pid)
  jobs = jobs.filter((j, i) => i < from || j.pid !== job.pid)
  finished = [...finished, { ...job, cancelled }]
  emit()
  pump()
}

/** The finished drops for this novel not reported yet. Each is handed out once. */
export function takeFinished(pid) {
  const mine = finished.filter((job) => job.pid === pid)
  if (mine.length) {
    finished = finished.filter((job) => job.pid !== pid)
    emit()
  }
  return mine
}

/** An empty queue, with any drop still waiting on its server abandoned. For tests. */
export function resetUploads() {
  generation += 1
  jobs = []
  finished = []
  pumping = false
  emit()
}
