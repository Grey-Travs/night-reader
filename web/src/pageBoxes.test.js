import { describe, expect, it } from 'vitest'

import {
  applyBoxOrder, applyMove, checkDrop, groupBoxes, hasText, locate, neighbourId, nextSeam,
  planBoxMove, planDrop, planPhotoMove, planSplit, seamPrev, sortFilesForUpload,
  stoppedMessage, uploadReport,
} from './pageBoxes'

// Page order is chapter order, so a move that lands one place off quietly reorders
// someone's novel, and nothing downstream notices: the Korean still reads as Korean.
// The flat rail these replaced pinned three lessons that still hold — a downward drag
// landed one place short, a reorder must always be a permutation, and an id the list
// no longer has must refuse the move rather than leave a hole. Boxes add three more: a
// photo is in exactly one box, boxes stay grouped in order, and a seam resets exactly
// where the photo it joins to changed, never anywhere else.

const SEAM = { join_prev: 'sentence', join_prev_source: 'model', join_reason: 'r', join_glue: '' }
const NONE = { join_prev: '', join_prev_source: '', join_reason: '', join_glue: 'space' }

// groups: [[boxId, [pageIds]], ...]. Every photo but a box's first carries a decided
// seam, so a reset shows up as a seam going back to undecided.
function makeRail(groups, { chapters = {}, legacy = [], skipped = [], chapterCount = 0 } = {}) {
  const pages = []
  const batches = []
  let unbuilt = 0
  for (const [id, ids] of groups) {
    const chapter = chapters[id] ?? null
    if (chapter == null && !legacy.includes(id)) unbuilt += 1
    batches.push({
      id, label: '', count: ids.length, chapter, legacy: legacy.includes(id) || undefined,
      number: legacy.includes(id) ? null : (chapter ?? chapterCount + unbuilt),
    })
    ids.forEach((pid, i) => pages.push({
      id: pid, batch: id, status: skipped.includes(pid) ? 'skipped' : 'ok',
      ...(i === 0 ? NONE : SEAM),
    }))
  }
  return { pages, batches, chapter_count: chapterCount }
}

const boxesOf = (rail) => groupBoxes(rail.pages, rail.batches)
const order = (rail, box) => rail.pages.filter((p) => p.batch === box).map((p) => p.id)

// The photo each seam joins to, worked out independently of the code under test.
function joinsTo(pages) {
  const out = new Map()
  for (const [i, p] of pages.entries()) {
    if (p.status === 'skipped') continue
    const prev = pages.slice(0, i).reverse().find((q) => q.batch === p.batch && q.status !== 'skipped')
    out.set(p.id, prev?.id ?? null)
  }
  return out
}

const file = (name, { size = 1000, type = 'image/jpeg' } = {}) => ({ name, size, type })

describe('sortFilesForUpload', () => {
  it('puts a numbered series in number order, IMG_9 before IMG_10', () => {
    const picked = [file('IMG_10.jpg'), file('IMG_9.jpg'), file('IMG_1.jpg')]
    expect(sortFilesForUpload(picked).map((f) => f.name)).toEqual(['IMG_1.jpg', 'IMG_9.jpg', 'IMG_10.jpg'])
  })

  it('ignores case when deciding the names are one series', () => {
    const picked = [file('img_2.JPG'), file('IMG_1.jpg')]
    expect(sortFilesForUpload(picked).map((f) => f.name)).toEqual(['IMG_1.jpg', 'img_2.JPG'])
  })

  it('keeps the picked order when the names are not one series', () => {
    // Renamed or converted phone files: sorting these would scramble a selection that
    // was already right.
    const picked = [file('scan b.jpg'), file('cover.jpg'), file('page 2.jpg')]
    expect(sortFilesForUpload(picked)).toEqual(picked)
  })

  it('keeps the picked order between identical names, and returns every file once', () => {
    const one = file('IMG_1.jpg')
    const two = file('IMG_1.jpg')
    const sorted = sortFilesForUpload([file('IMG_3.jpg'), one, two])
    expect(sorted[0]).toBe(one)
    expect(sorted[1]).toBe(two)
    expect(sorted).toHaveLength(3)
  })
})

describe('checkDrop', () => {
  it('refuses the whole drop over one iPhone HEIC photo', () => {
    const res = checkDrop([file('IMG_1.jpg'), file('IMG_2.HEIC', { type: '' })])
    expect(res.refused).toEqual([{ name: 'IMG_2.HEIC', reason: 'heic' }])
  })

  it('refuses anything that is not a JPEG, PNG or WebP, and anything too big', () => {
    const res = checkDrop([
      file('notes.pdf', { type: 'application/pdf' }),
      file('anim.gif', { type: 'image/gif' }),
      file('raw', { type: '' }),
      file('huge.jpg', { size: 26 * 1024 * 1024 }),
    ])
    expect(res.refused.map((r) => r.reason)).toEqual(['type', 'type', 'type', 'size'])
    expect(res.files).toEqual([])
  })

  it('trusts the name when the browser gives no type', () => {
    expect(checkDrop([file('page.png', { type: '' })]).files).toHaveLength(1)
  })

  it('leaves out empty and system files without refusing the drop', () => {
    const res = checkDrop([file('IMG_1.jpg'), file('IMG_2.jpg', { size: 0 }), file('Thumbs.db', { type: '' })])
    expect(res.refused).toEqual([])
    expect(res.files.map((f) => f.name)).toEqual(['IMG_1.jpg'])
    expect(res.skipped.map((s) => s.name)).toEqual(['IMG_2.jpg', 'Thumbs.db'])
  })
})

describe('reading the rail', () => {
  it('groups photos into their boxes in box order, keeping an empty box', () => {
    const boxes = boxesOf(makeRail([['b1', ['a', 'b']], ['b2', []], ['b3', ['c']]]))
    expect(boxes.map((b) => [b.id, b.pages.map((p) => p.id)])).toEqual([
      ['b1', ['a', 'b']], ['b2', []], ['b3', ['c']],
    ])
  })

  it('shows a photo whose box it does not know, rather than hiding it', () => {
    const boxes = groupBoxes([{ id: 'a', batch: 'b1' }, { id: 'z', batch: 'gone' }], [{ id: 'b1' }])
    expect(boxes.at(-1)).toMatchObject({ orphan: true, pages: [{ id: 'z' }] })
  })

  it('steps only within a chapter', () => {
    const boxes = boxesOf(makeRail([['b1', ['a', 'b']], ['b2', ['c']]]))
    expect(neighbourId(boxes, 'a', 1)).toBe('b')
    expect(neighbourId(boxes, 'b', 1)).toBeNull()
    expect(neighbourId(boxes, 'c', -1)).toBeNull()
    expect(locate(boxes, 'c')).toMatchObject({ boxIndex: 1, index: 0 })
  })

  it('joins a seam to the previous photo that is not skipped', () => {
    const { pages } = boxesOf(makeRail([['b1', ['a', 'b', 'c']]], { skipped: ['b'] }))[0]
    expect(seamPrev(pages, 0)).toBe(-1)
    expect(seamPrev(pages, 1)).toBe(-1)   // a skipped photo has no seam of its own
    expect(seamPrev(pages, 2)).toBe(0)
  })

  it('counts text the way the server does, ignoring invisible characters', () => {
    expect(hasText('​﻿ \n')).toBe(false)
    expect(hasText(' 문을 열었다 ')).toBe(true)
  })

  it('cycles a seam through the three choices', () => {
    expect(['', 'sentence', 'paragraph', 'gap', 'chapter'].map(nextSeam))
      .toEqual(['sentence', 'paragraph', 'gap', 'sentence', 'sentence'])
  })
})

describe('planPhotoMove', () => {
  const boxes = boxesOf(makeRail([['b1', ['a', 'b', 'c', 'd']], ['b2', ['e', 'f']], ['b3', []]]))

  it('moves a photo one place earlier or later', () => {
    expect(planPhotoMove(boxes, 'c', 'earlier')).toEqual({ ids: ['c'], batch: 'b1', before: 'b' })
    expect(planPhotoMove(boxes, 'b', 'later')).toEqual({ ids: ['b'], batch: 'b1', before: 'd' })
    expect(planPhotoMove(boxes, 'c', 'later')).toEqual({ ids: ['c'], batch: 'b1', before: null })
  })

  it('does nothing past either end of the chapter', () => {
    expect(planPhotoMove(boxes, 'a', 'earlier')).toBeNull()
    expect(planPhotoMove(boxes, 'd', 'later')).toBeNull()
  })

  it('lands "Later" exactly one place on, not one short', () => {
    const rail = makeRail([['b1', ['a', 'b', 'c', 'd']]])
    const moved = applyMove(rail, planPhotoMove(boxesOf(rail), 'b', 'later'))
    expect(order(moved, 'b1')).toEqual(['a', 'c', 'b', 'd'])
  })

  it('sends a photo to the end of an earlier chapter and the start of a later one', () => {
    expect(planPhotoMove(boxes, 'e', { toBox: 'b1' })).toEqual({ ids: ['e'], batch: 'b1', before: null })
    expect(planPhotoMove(boxes, 'd', { toBox: 'b2' })).toEqual({ ids: ['d'], batch: 'b2', before: 'e' })
    expect(planPhotoMove(boxes, 'd', { toBox: 'b3' })).toEqual({ ids: ['d'], batch: 'b3', before: null })
  })

  it('refuses its own chapter, an unknown one and the locked pile', () => {
    const locked = boxesOf(makeRail([['old', ['x']], ['b1', ['a', 'b']]], { legacy: ['old'] }))
    expect(planPhotoMove(boxes, 'a', { toBox: 'b1' })).toBeNull()
    expect(planPhotoMove(boxes, 'a', { toBox: 'nope' })).toBeNull()
    expect(planPhotoMove(locked, 'a', { toBox: 'old' })).toBeNull()
    expect(planPhotoMove(locked, 'x', 'later')).toBeNull()
  })
})

describe('planDrop', () => {
  const boxes = boxesOf(makeRail([['b1', ['a', 'b', 'c', 'd', 'e']], ['b2', ['f']]]))

  it('drops on the left half before a photo and on the right half after it', () => {
    expect(planDrop(boxes, 'a', { overId: 'c', side: 'before' })).toEqual({ ids: ['a'], batch: 'b1', before: 'c' })
    expect(planDrop(boxes, 'a', { overId: 'c', side: 'after' })).toEqual({ ids: ['a'], batch: 'b1', before: 'd' })
    expect(planDrop(boxes, 'a', { overId: 'e', side: 'after' })).toEqual({ ids: ['a'], batch: 'b1', before: null })
  })

  it('lands a downward drag where it was dropped — the old off-by-one', () => {
    const rail = makeRail([['b1', ['a', 'b', 'c', 'd', 'e']]])
    const moved = applyMove(rail, planDrop(boxesOf(rail), 'b', { overId: 'd', side: 'after' }))
    expect(order(moved, 'b1')).toEqual(['a', 'c', 'd', 'b', 'e'])
  })

  it('sends nothing for a drop onto itself or onto either edge of its own gap', () => {
    expect(planDrop(boxes, 'c', { overId: 'c', side: 'after' })).toBeNull()
    expect(planDrop(boxes, 'c', { overId: 'b', side: 'after' })).toBeNull()
    expect(planDrop(boxes, 'c', { overId: 'd', side: 'before' })).toBeNull()
    expect(planDrop(boxes, 'e', { boxId: 'b1' })).toBeNull()
  })

  it('drops onto a box at its end, across chapters too', () => {
    expect(planDrop(boxes, 'a', { boxId: 'b2' })).toEqual({ ids: ['a'], batch: 'b2', before: null })
    expect(planDrop(boxes, 'f', { overId: 'a', side: 'before' })).toEqual({ ids: ['f'], batch: 'b1', before: 'a' })
  })

  it('refuses the locked pile in either direction, and a photo that is gone', () => {
    const locked = boxesOf(makeRail([['old', ['x', 'y']], ['b1', ['a']]], { legacy: ['old'] }))
    expect(planDrop(locked, 'a', { overId: 'x', side: 'before' })).toBeNull()
    expect(planDrop(locked, 'x', { boxId: 'b1' })).toBeNull()
    expect(planDrop(boxes, 'gone', { boxId: 'b1' })).toBeNull()
  })
})

describe('applyMove', () => {
  it('moves within a box', () => {
    const rail = makeRail([['b1', ['a', 'b', 'c']]])
    expect(order(applyMove(rail, { ids: ['c'], batch: 'b1', before: 'a' }), 'b1')).toEqual(['c', 'a', 'b'])
  })

  it('moves across boxes, re-boxing the photo and recounting both', () => {
    const rail = makeRail([['b1', ['a', 'b']], ['b2', ['c']]])
    const next = applyMove(rail, { ids: ['b'], batch: 'b2', before: 'c' })
    expect(order(next, 'b1')).toEqual(['a'])
    expect(order(next, 'b2')).toEqual(['b', 'c'])
    expect(next.pages.find((p) => p.id === 'b').batch).toBe('b2')
    expect(next.batches.map((b) => b.count)).toEqual([1, 2])
  })

  it('keeps several photos in the order given', () => {
    const rail = makeRail([['b1', ['a', 'b', 'c']], ['b2', ['d']]])
    expect(order(applyMove(rail, { ids: ['c', 'a'], batch: 'b2', before: null }), 'b2')).toEqual(['d', 'c', 'a'])
  })

  it('resets exactly the seams whose photo before changed — user-set ones too', () => {
    const rail = makeRail([['b1', ['a', 'b', 'c', 'd']]])
    rail.pages[3] = { ...rail.pages[3], join_prev_source: 'user' }
    const next = applyMove(rail, { ids: ['b'], batch: 'b1', before: 'd' })   // a c b d
    const seam = (id) => next.pages.find((p) => p.id === id).join_prev
    expect(seam('c')).toBe('')   // used to follow b, now follows a
    expect(seam('b')).toBe('')   // moved
    expect(seam('d')).toBe('')   // used to follow c, now follows b
    expect(seam('a')).toBe('')   // first photo: never had one
    expect(next.pages.find((p) => p.id === 'b').join_prev_id).toBe('c')
  })

  it('leaves every seam alone when nothing moves', () => {
    const rail = makeRail([['b1', ['a', 'b', 'c']]])
    const next = applyMove(rail, { ids: ['b'], batch: 'b1', before: 'c' })
    expect(next.pages.map((p) => p.join_prev)).toEqual(rail.pages.map((p) => p.join_prev))
  })

  it('refuses what the server would refuse, so nothing is guessed on screen', () => {
    const rail = makeRail([['old', ['x']], ['b1', ['a', 'b']], ['b2', ['c']]], { legacy: ['old'] })
    expect(applyMove(rail, { ids: ['gone'], batch: 'b1' })).toBeNull()
    expect(applyMove(rail, { ids: ['a'], batch: 'nope' })).toBeNull()
    expect(applyMove(rail, { ids: ['a', 'b'], batch: 'b1', before: 'b' })).toBeNull()
    expect(applyMove(rail, { ids: ['a'], batch: 'b1', before: 'c' })).toBeNull()
    expect(applyMove(rail, { ids: ['a'], batch: 'old' })).toBeNull()
    expect(applyMove(rail, { ids: ['x'], batch: 'b1' })).toBeNull()
    expect(applyMove(rail, { ids: [], batch: 'b1' })).toBeNull()
  })

  it('always leaves a permutation, grouped in box order, with seams reset exactly where a neighbour changed', () => {
    // Every photo to every place in every box, with a skipped photo in the mix — the
    // seam after a skipped photo joins the one before it.
    const start = makeRail([['b1', ['a', 'b', 'c', 'd']], ['b2', ['e', 'f']], ['b3', []]], { skipped: ['c'] })
    const ids = start.pages.map((p) => p.id)
    const boxIds = start.batches.map((b) => b.id)
    const was = joinsTo(start.pages)
    for (const id of ids) {
      for (const batch of boxIds) {
        const anchors = [null, ...start.pages.filter((p) => p.batch === batch && p.id !== id).map((p) => p.id)]
        for (const before of anchors) {
          const next = applyMove(start, { ids: [id], batch, before })
          const where = `${id} → ${batch} before ${before}`
          expect(next, where).not.toBeNull()
          expect(next.pages.map((p) => p.id).sort(), where).toEqual([...ids].sort())
          const ranks = next.pages.map((p) => boxIds.indexOf(p.batch))
          expect(ranks, where).toEqual([...ranks].sort((x, y) => x - y))
          const inBox = order(next, batch)
          if (before) expect(inBox[inBox.indexOf(id) + 1], where).toBe(before)
          else expect(inBox.at(-1), where).toBe(id)
          for (const box of next.batches) expect(box.count, where).toBe(order(next, box.id).length)
          const now = joinsTo(next.pages)
          for (const p of next.pages) {
            const old = start.pages.find((q) => q.id === p.id)
            expect(p.join_prev, `${where}: ${p.id}`).toBe(now.get(p.id) !== was.get(p.id) ? '' : old.join_prev)
          }
        }
      }
    }
  })
})

describe('moving whole boxes', () => {
  const rail = makeRail([['b1', ['a']], ['b2', ['b']], ['b3', ['c']], ['b4', []]],
                        { chapters: { b1: 1 }, chapterCount: 1 })
  const boxes = boxesOf(rail)

  it('moves a box only among the boxes not built yet', () => {
    expect(planBoxMove(boxes, 'b3', -1)).toEqual(['b1', 'b3', 'b2', 'b4'])
    expect(planBoxMove(boxes, 'b2', 1)).toEqual(['b1', 'b3', 'b2', 'b4'])
    expect(planBoxMove(boxes, 'b2', -1)).toBeNull()   // a built chapter keeps its place
    expect(planBoxMove(boxes, 'b1', 1)).toBeNull()
    expect(planBoxMove(boxes, 'b4', 1)).toBeNull()
  })

  it('renumbers the boxes not built yet, and the photos follow their box', () => {
    const next = applyBoxOrder(rail, ['b1', 'b3', 'b2', 'b4'])
    expect(next.batches.map((b) => [b.id, b.number])).toEqual([['b1', 1], ['b3', 2], ['b2', 3], ['b4', 4]])
    expect(next.pages.map((p) => p.id)).toEqual(['a', 'c', 'b'])
  })

  it('refuses an order that moves a built box or is not every box once', () => {
    expect(applyBoxOrder(rail, ['b2', 'b1', 'b3', 'b4'])).toBeNull()
    expect(applyBoxOrder(rail, ['b1', 'b2', 'b3'])).toBeNull()
    expect(applyBoxOrder(rail, ['b1', 'b2', 'b2', 'b4'])).toBeNull()
  })
})

describe('planSplit', () => {
  it('starts a new chapter right after this one, from this photo on', () => {
    const boxes = boxesOf(makeRail([['b1', ['a', 'b', 'c']], ['b2', ['d']]]))
    expect(planSplit(boxes, 'b')).toEqual({ after: 'b1', ids: ['b', 'c'] })
  })

  it('refuses the first photo, and any chapter with a built one after it', () => {
    const boxes = boxesOf(makeRail([['b1', ['a', 'b']], ['b2', ['c', 'd']], ['b3', ['e', 'f']]],
                                   { chapters: { b1: 1, b2: 2 }, chapterCount: 2 }))
    expect(planSplit(boxes, 'c')).toBeNull()
    expect(planSplit(boxes, 'b')).toBeNull()
    expect(planSplit(boxes, 'd')).toEqual({ after: 'b2', ids: ['d'] })   // the last built one
    expect(planSplit(boxes, 'f')).toEqual({ after: 'b3', ids: ['f'] })
  })

  it('refuses the locked pile', () => {
    const boxes = boxesOf(makeRail([['old', ['x', 'y']]], { legacy: ['old'] }))
    expect(planSplit(boxes, 'y')).toBeNull()
  })
})

describe('what an upload says afterwards', () => {
  const rail = makeRail([['old', ['x']], ['b1', ['a', 'b']], ['b2', ['c']]],
                        { legacy: ['old'], chapterCount: 12 })
  rail.batches[0].chapters = [1, 12]
  const boxes = boxesOf(rail)
  const job = (over) => ({
    batch: 'b2', number: 14, files: Array(5).fill(file('x.jpg')), added: 0, here: 0,
    elsewhere: [], skipped: [], ...over,
  })

  it('counts what went in and what was already in that chapter', () => {
    expect(uploadReport(job({ added: 3, here: 1 }), boxes).toast)
      .toBe('3 photos added to Chapter 14 · 1 already in it')
    expect(uploadReport(job({ here: 5 }), boxes).toast).toBe('All 5 photos are already in Chapter 14.')
  })

  it('names the chapter and the place of a photo that was already elsewhere', () => {
    const { notices } = uploadReport(job({
      added: 1, elsewhere: [{ name: 'IMG_3.jpg', batch: 'b1', number: 13, pageId: 'b' }],
    }), boxes)
    expect(notices).toEqual(['IMG_3.jpg is already in Chapter 13 (photo 2), so it wasn’t added to Chapter 14.'])
  })

  it('says when that photo is in the pile built before chapter boxes', () => {
    const { notices } = uploadReport(job({
      batch: '', elsewhere: [{ name: 'IMG_1.jpg', batch: 'old', number: null, pageId: 'x' }],
    }), boxes)
    expect(notices[0]).toBe('IMG_1.jpg is already in the photos of chapters 1–12, built before chapter boxes, so it wasn’t added again.')
  })

  it('groups many by chapter instead of listing every one', () => {
    const elsewhere = ['1', '2', '3', '4', '5'].map((n) => ({ name: `IMG_${n}.jpg`, batch: 'b1', number: 13 }))
    const { notices } = uploadReport(job({ batch: '', elsewhere }), boxes)
    expect(notices[0]).toBe('IMG_1.jpg, IMG_2.jpg, IMG_3.jpg and 2 more are already in Chapter 13, so they weren’t added again.')
  })

  it('lists the files it left out', () => {
    const { notices } = uploadReport(job({ added: 1, skipped: [{ name: 'Thumbs.db', reason: 'a system file' }] }), boxes)
    expect(notices).toEqual(['Left out: Thumbs.db (a system file).'])
  })

  it('says exactly where a drop stopped and what was not sent', () => {
    const stopped = job({
      files: Array(10).fill(file('x.jpg')), added: 3,
      stopped: { index: 3, name: 'IMG_4.jpg', error: new Error('Failed to fetch') },
    })
    expect(stoppedMessage(stopped, boxes)).toBe(
      'Stopped at IMG_4.jpg (photo 4 of 10): Failed to fetch. 3 photos went into Chapter 14 '
      + 'before it, and the 7 photos from there on were not sent.')
    const first = job({ batch: '', number: null, files: Array(2).fill(file('x.jpg')),
                        stopped: { index: 0, name: 'IMG_1.jpg', error: new Error('Gone.') } })
    expect(stoppedMessage(first, boxes)).toBe('Stopped at IMG_1.jpg (photo 1 of 2): Gone. The 2 photos from there on were not sent.')
  })
})
