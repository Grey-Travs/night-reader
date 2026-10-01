// Chapter boxes: the ordering rules behind the Pages tab, kept out of the components so
// every move can be tested directly.
//
// A box is a chapter. Its photos, in order, become that chapter's text when it's built,
// so a photo that lands one place off reorders someone's novel, and nothing downstream
// can notice: the Korean still reads like Korean. The flat page rail this replaced
// learned three rules the hard way, and they all carry over here:
//   * plan a move from the list as it stands NOW, never from a stale render;
//   * a move that would change nothing sends nothing;
//   * a page or box that is no longer there refuses the move instead of leaving a hole.

/** Upload target meaning "start a new chapter at the end with these photos". */
export const NEW_BOX = '__new__'

// Tile drags carry this type as well as a ref. Files dragged in from the desktop never
// do, so a drag that was abandoned without a dragend can't turn the next file drop into
// a page move.
export const TILE_MIME = 'application/x-night-reader-page'

/** Statuses during which the server is about to overwrite a page's text. */
export const BUSY = new Set(['queued', 'ocr-running'])

// The server's limit (server/pages.py MAX_IMAGE_BYTES), checked here first so one
// oversized photo can't stop a drop halfway through a chapter.
export const MAX_IMAGE_MB = 25
const MAX_IMAGE_BYTES = MAX_IMAGE_MB * 1024 * 1024

export const SEAMS = {
  sentence: {
    icon: '→', label: 'Sentence continues', cls: 'pill-translated',
    hint: 'The sentence runs straight across this break — no paragraph break here.',
  },
  paragraph: {
    icon: '¶', label: 'New paragraph', cls: 'pill-muted',
    hint: 'A paragraph ends on the photo before.',
  },
  gap: {
    icon: '⚠', label: 'Page missing?', cls: 'pill-review',
    hint: "The text doesn't follow on — a page may never have been photographed.",
  },
  // Not a choice any more: inside a box a heading is only a paragraph break, and the
  // box decides where the chapter ends. Claude can still REPORT one, which is worth
  // showing, because it usually means the next chapter starts on this photo.
  chapter: {
    icon: '📖', label: 'Heading here', cls: 'pill-english',
    hint: 'Claude saw a chapter heading on this photo. If the next chapter starts here, split the chapter at this photo.',
  },
}

const SEAM_CYCLE = ['sentence', 'paragraph', 'gap']

/** The seam a click moves to. An undecided seam and a reported heading start the cycle. */
export function nextSeam(kind) {
  return SEAM_CYCLE[(SEAM_CYCLE.indexOf(kind) + 1) % SEAM_CYCLE.length]
}

const NO_SEAM = { join_prev: '', join_prev_source: '', join_reason: '', join_glue: 'space' }

/** Built boxes (and the locked pile from before boxes) keep their chapter numbers. */
export const isBuilt = (box) => box?.chapter != null || !!box?.legacy

/** Boxes nothing can be moved into or out of. */
export const isLocked = (box) => !!box?.legacy || !!box?.orphan

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`

/** "Chapter 5", or the range a pre-box pile was built into. */
export function boxName(box) {
  if (box?.orphan) return 'Photos in no chapter'
  if (box?.legacy) {
    const [first, last] = box.chapters || []
    return first != null && last != null && first !== last
      ? `Chapters ${first}–${last}` : `Chapter ${first ?? '?'}`
  }
  return `Chapter ${box?.number ?? '?'}`
}

// The characters server/pages.py page_has_text ignores (translation_bot/docs_extract.py
// _INVISIBLE_RE). A page holding only these would build into an empty chapter, so the
// server doesn't count it as having text, and neither may the screen.
const INVISIBLE = /[­͏؜ᅟᅠ឴឵᠎​-‏‪-‮⁠-⁤⁦-⁯ㅤ﻿ﾠ]/g

export const hasText = (text) => Boolean((text || '').replace(INVISIBLE, '').trim())

/** What the chapter will be called: its label, else the heading on its first photo. */
export function boxTitle(box) {
  if (box?.label) return box.label
  const first = (box?.pages || []).find((p) => p.status !== 'skipped')
  return (first?.heading || '').trim()
}

// ---- uploads -------------------------------------------------------------------------

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' })

/**
 * The order to upload a drop in.
 *
 * Browsers hand files over in no promised order (Explorer often puts the one you
 * grabbed first), so IMG_10 could land before IMG_9. A natural sort fixes that, but
 * only when the names are one numbered series: renamed or converted phone files often
 * aren't, and sorting those would scramble a selection that was already right. So it
 * sorts only when every name is the same once each run of digits is masked.
 */
export function sortFilesForUpload(files) {
  const list = [...(files || [])]
  const shape = (f) => (f.name || '').toLowerCase().replace(/\d+/g, '#')
  if (list.length < 2 || !list.every((f) => shape(f) === shape(list[0]))) return list
  return list.sort((a, b) => collator.compare(a.name || '', b.name || ''))
}

const IMAGE_TYPES = new Set(['image/jpeg', 'image/jpg', 'image/pjpeg', 'image/png', 'image/webp'])
const IMAGE_NAME = /\.(jpe?g|jfif|png|webp)$/i
const HEIC_NAME = /\.(heic|heif)$/i
// What a folder dragged in from Windows or a Mac brings along besides the photos.
const JUNK_NAME = /^(\.ds_store|thumbs\.db|desktop\.ini)$|^\._/i

export const REFUSED = {
  heic: 'an iPhone HEIC photo, which browsers can’t show',
  type: 'not a JPEG, PNG or WebP image',
  size: `bigger than ${MAX_IMAGE_MB} MB`,
}

/**
 * Checks a whole drop before any of it is sent.
 *
 * The server refuses these too, but one file at a time — so a HEIC photo in the middle
 * of a drop used to stop it there, leaving half a chapter uploaded and the rest unsent.
 * Refusing the drop up front means only a network failure can stop one partway.
 * Empty files and system files are left out rather than refusing the drop over them.
 */
export function checkDrop(files) {
  const ok = []
  const refused = []
  const skipped = []
  for (const file of files || []) {
    const name = file.name || 'That file'
    const type = (file.type || '').toLowerCase()
    if (JUNK_NAME.test(name)) skipped.push({ name, reason: 'a system file' })
    else if (!file.size) skipped.push({ name, reason: 'empty' })
    else if (HEIC_NAME.test(name) || type === 'image/heic' || type === 'image/heif') {
      refused.push({ name, reason: 'heic' })
    } else if (!(IMAGE_TYPES.has(type) || (!type && IMAGE_NAME.test(name)))) {
      refused.push({ name, reason: 'type' })
    } else if (file.size > MAX_IMAGE_BYTES) refused.push({ name, reason: 'size' })
    else ok.push(file)
  }
  return { files: ok, refused, skipped }
}

// ---- reading the rail ----------------------------------------------------------------

/**
 * The boxes in order, each with its photos in order.
 *
 * The server keeps pages grouped by box, so this is a grouping, never a sort. A page
 * whose box it doesn't know still gets shown, in a box of its own at the end: hiding
 * it would make a photo vanish from the screen while it still sits in the novel.
 */
export function groupBoxes(pages, batches) {
  const boxes = (batches || []).map((b) => ({ ...b, pages: [] }))
  const byId = new Map(boxes.map((b) => [b.id, b]))
  let orphans = null
  for (const page of pages || []) {
    const box = byId.get(page.batch)
    if (box) box.pages.push(page)
    else {
      orphans ||= { id: null, orphan: true, label: '', chapter: null, number: null,
                    state: '', blockers: [], pages: [] }
      orphans.pages.push(page)
    }
  }
  return orphans ? [...boxes, orphans] : boxes
}

/** Where a photo is: its box, the box's place, and its own place in the box. */
export function locate(boxes, pageId) {
  for (const [boxIndex, box] of (boxes || []).entries()) {
    const index = box.pages.findIndex((p) => p.id === pageId)
    if (index !== -1) return { box, boxIndex, index }
  }
  return null
}

/**
 * The photo a seam joins to: the previous photo in the box that isn't skipped, or -1.
 * A skipped photo has no seam of its own, and the first photo of a box has none either —
 * the chapter starts there.
 */
export function seamPrev(pages, i) {
  if (!pages[i] || pages[i].status === 'skipped') return -1
  for (let j = i - 1; j >= 0; j -= 1) if (pages[j].status !== 'skipped') return j
  return -1
}

/** The photo before or after this one, within its own chapter only. */
export function neighbourId(boxes, pageId, delta) {
  const at = locate(boxes, pageId)
  return at?.box.pages[at.index + delta]?.id ?? null
}

// ---- planning moves ------------------------------------------------------------------
// A plan is exactly what POST /pages/move takes: { ids, batch, before }, where `before`
// is the photo to land in front of and null means the end of the box.

/**
 * Earlier / Later within the chapter, or into another chapter (`{ toBox }`).
 *
 * Into an EARLIER chapter a photo goes to its end, into a LATER one to its start. That
 * is the shape of the usual mistake — a photo on the wrong side of a chapter boundary —
 * so one move fixes it.
 */
export function planPhotoMove(boxes, pageId, how) {
  const at = locate(boxes, pageId)
  if (!at || isLocked(at.box)) return null
  const { box, index } = at
  if (how === 'earlier') {
    return index > 0 ? { ids: [pageId], batch: box.id, before: box.pages[index - 1].id } : null
  }
  if (how === 'later') {
    return index < box.pages.length - 1
      ? { ids: [pageId], batch: box.id, before: box.pages[index + 2]?.id ?? null } : null
  }
  const targetIndex = (boxes || []).findIndex((b) => b.id === how?.toBox)
  const target = boxes?.[targetIndex]
  if (!target || target.id === box.id || isLocked(target)) return null
  return {
    ids: [pageId],
    batch: target.id,
    before: targetIndex < at.boxIndex ? null : (target.pages[0]?.id ?? null),
  }
}

/**
 * A tile dropped on another tile (`overId`, on its `side`) or on a box (`boxId`, at its
 * end). Null when the drop would leave everything where it is — onto itself, or onto
 * either edge of its own gap — so a fumbled drag never resets a seam.
 */
export function planDrop(boxes, draggedId, { boxId = null, overId = null, side = 'before' } = {}) {
  const from = locate(boxes, draggedId)
  if (!from || isLocked(from.box) || overId === draggedId) return null
  let box
  let before = null
  if (overId) {
    const over = locate(boxes, overId)
    if (!over) return null
    box = over.box
    before = side === 'after' ? (box.pages[over.index + 1]?.id ?? null) : overId
  } else {
    box = (boxes || []).find((b) => b.id === boxId)
  }
  if (!box || isLocked(box)) return null
  if (box.id === from.box.id) {
    const next = box.pages[from.index + 1]?.id ?? null
    if (before === draggedId || before === next) return null
  }
  return { ids: [draggedId], batch: box.id, before }
}

/**
 * Where "Split chapter here" puts the new chapter: right after this one, holding this
 * photo and every photo after it. Null on a chapter's first photo, and when a built
 * chapter comes after this one — the new chapter would have to take that chapter's
 * number, and built chapters keep theirs. (The last built chapter can still be split.)
 */
export function planSplit(boxes, pageId) {
  const at = locate(boxes, pageId)
  if (!at || at.index === 0 || isLocked(at.box)) return null
  if ((boxes || []).slice(at.boxIndex + 1).some(isBuilt)) return null
  return { after: at.box.id, ids: at.box.pages.slice(at.index).map((p) => p.id) }
}

/**
 * The full box order after moving one box up or down. Only boxes that aren't built yet
 * move, and only among themselves: a built chapter's number is already its place.
 */
export function planBoxMove(boxes, boxId, delta) {
  const real = (boxes || []).filter((b) => !b.orphan)
  const i = real.findIndex((b) => b.id === boxId)
  const j = i + delta
  if (i === -1 || j < 0 || j >= real.length || isBuilt(real[i]) || isBuilt(real[j])) return null
  const ids = real.map((b) => b.id)
  ;[ids[i], ids[j]] = [ids[j], ids[i]]
  return ids
}

// ---- applying moves on screen before the server answers -------------------------------

// For each photo with a seam, the photo it joins to: the previous non-skipped photo in
// its own box. Mirrors the server, which resets a seam the moment this changes.
function seamNeighbours(pages) {
  const prev = new Map()
  const last = new Map()
  for (const page of pages) {
    if (page.status === 'skipped') continue
    prev.set(page.id, last.get(page.batch) ?? null)
    last.set(page.batch, page.id)
  }
  return prev
}

// Pages grouped in box order. Array sort is stable, so the order inside a box is kept.
function groupByBox(pages, batches) {
  const order = new Map(batches.map((b, i) => [b.id, i]))
  const rank = (p) => order.get(p.batch) ?? batches.length
  return [...pages].sort((a, b) => rank(a) - rank(b))
}

/**
 * The rail as it will be after a move, so the screen doesn't wait on the server.
 *
 * Null whenever the server would refuse the move (an unknown page or box, a locked box,
 * an anchor that isn't in the target box or is itself moving); the caller then sends
 * it without guessing. A seam is reset exactly where the photo it joins to changed —
 * the server does the same — because a "sentence continues" decided against one page
 * glues words from an unrelated one once that page has moved.
 */
export function applyMove(rail, { ids, batch, before = null } = {}) {
  const batches = rail?.batches || []
  const pages = rail?.pages || []
  const target = batches.find((b) => b.id === batch)
  if (!ids?.length || !target || target.legacy) return null
  const moving = new Set(ids)
  const byId = new Map(pages.map((p) => [p.id, p]))
  const locked = new Set(batches.filter((b) => b.legacy).map((b) => b.id))
  if ([...moving].some((id) => !byId.has(id) || locked.has(byId.get(id).batch))) return null
  if (before != null && (moving.has(before) || byId.get(before)?.batch !== batch)) return null

  const was = seamNeighbours(pages)
  const staying = pages.filter((p) => !moving.has(p.id))
  const moved = [...moving].map((id) => ({ ...byId.get(id), batch }))
  // No anchor: the end of the whole list, which the grouping turns into the end of the box.
  const at = before == null ? staying.length : staying.findIndex((p) => p.id === before)
  const placed = groupByBox([...staying.slice(0, at), ...moved, ...staying.slice(at)], batches)
  const now = seamNeighbours(placed)
  return {
    ...rail,
    pages: placed.map((p) => (now.has(p.id) && now.get(p.id) !== was.get(p.id)
      ? { ...p, ...NO_SEAM, join_prev_id: now.get(p.id) } : p)),
    batches: batches.map((b) => ({ ...b, count: placed.filter((p) => p.batch === b.id).length })),
  }
}

/**
 * The rail after reordering the boxes: a full permutation that leaves the built boxes
 * where they are. Boxes not built yet take their numbers from their new places, the
 * way the server numbers them (built chapters, then one more for each box in turn).
 */
export function applyBoxOrder(rail, ids) {
  const batches = rail?.batches || []
  const current = batches.map((b) => b.id)
  if (!ids || ids.length !== current.length || new Set(ids).size !== ids.length
      || ids.some((id) => !current.includes(id))) return null
  const fixed = batches.filter(isBuilt).length
  if (ids.slice(0, fixed).some((id, i) => id !== current[i])) return null
  const byId = new Map(batches.map((b) => [b.id, b]))
  let k = 0
  const ordered = ids.map((id) => {
    const box = byId.get(id)
    if (isBuilt(box)) return box
    k += 1
    return { ...box, number: (rail.chapter_count || 0) + k }
  })
  return { ...rail, batches: ordered, pages: groupByBox(rail.pages || [], ordered) }
}

// ---- what an upload says afterwards ---------------------------------------------------

// Where a drop went, named from the box as it is NOW: boxes can be reordered while a
// long drop is still going in, and the number it started with would then be wrong.
function destination(job, boxes) {
  const box = job.batch ? (boxes || []).find((b) => b.id === job.batch) : null
  if (box) return boxName(box)
  return job.number != null ? `Chapter ${job.number}` : 'a new chapter'
}

// Where a photo that was already in the novel sits; with its place in the box when
// there is just the one to name.
function existingPlace(dup, boxes, withPhoto) {
  const at = dup.pageId ? locate(boxes, dup.pageId) : null
  const box = at?.box || (boxes || []).find((b) => b.id === dup.batch)
  // existing_number is null for the locked pile built before chapter boxes.
  if (box?.legacy || (!box && dup.number == null)) {
    return box ? `the photos of ${boxName(box).toLowerCase()}, built before chapter boxes`
      : 'the chapters built before chapter boxes'
  }
  const name = box ? boxName(box) : `Chapter ${dup.number}`
  return at && withPhoto ? `${name} (photo ${at.index + 1})` : name
}

const SHOWN = 3

function elsewhereNotice(job, boxes, dest) {
  const byBox = new Map()
  for (const dup of job.elsewhere) {
    if (!byBox.has(dup.batch)) byBox.set(dup.batch, [])
    byBox.get(dup.batch).push(dup)
  }
  const parts = [...byBox.values()].map((dups) => {
    const more = dups.length > SHOWN ? ` and ${dups.length - SHOWN} more` : ''
    const names = dups.slice(0, SHOWN).map((d) => d.name).join(', ')
    return `${names}${more} ${dups.length === 1 ? 'is' : 'are'} already in `
      + existingPlace(dups[0], boxes, dups.length === 1)
  })
  const one = job.elsewhere.length === 1
  return `${parts.join('; ')}, so ${one ? 'it wasn’t' : 'they weren’t'} added `
    + `${job.batch ? `to ${dest}` : 'again'}.`
}

/**
 * The toast and the notices an upload leaves behind.
 *
 * A photo already in ANOTHER chapter is never moved by adding it again, so that has to
 * be said out loud: "1 already here", with no word of where, was how a photo stayed in
 * the wrong chapter unnoticed.
 */
export function uploadReport(job, boxes) {
  const dest = destination(job, boxes)
  let toast = null
  if (job.added) {
    toast = `${plural(job.added, 'photo')} added to ${dest}`
    if (job.here) toast += ` · ${job.here} already in it`
  } else if (job.here) {
    toast = job.here === job.files.length && job.here > 1
      ? `All ${job.here} photos are already in ${dest}.`
      : `${plural(job.here, 'photo')} already in ${dest}`
  }

  const notices = []
  if (job.elsewhere?.length) notices.push(elsewhereNotice(job, boxes, dest))
  if (job.skipped?.length) {
    notices.push(`Left out: ${job.skipped.map((s) => `${s.name} (${s.reason})`).join(', ')}.`)
  }
  return { toast, notices }
}

/** The sentence a stopped drop shows beside "Try the rest again". */
export function stoppedMessage(job, boxes) {
  const { index, name, error } = job.stopped
  const total = job.files.length
  const rest = total - index
  const sent = job.added
    ? `${plural(job.added, 'photo')} went into ${destination(job, boxes)} before it, and `
    : ''
  const unsent = `${rest === 1 ? 'that photo was' : `the ${rest} photos from there on were`} not sent`
  const why = (error?.message || 'it couldn’t be sent').replace(/[.\s]+$/, '')
  return `Stopped at ${name} (photo ${index + 1} of ${total}): ${why}. `
    + `${sent ? sent + unsent : unsent.charAt(0).toUpperCase() + unsent.slice(1)}.`
}
