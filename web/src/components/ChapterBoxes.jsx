import { useRef, useState } from 'react'
import { api } from '../api'
import { PAGE_STATUS_LABEL, PageBadge, STATUS_LABEL, STATUS_STYLES } from './ui'
import {
  NEW_BOX, SEAMS, TILE_MIME, boxName, boxTitle, isBuilt, isLocked, nextSeam,
  planBoxMove, planDrop, seamPrev,
} from '../pageBoxes'

// The Pages overview: one box per chapter, in reading order, each holding its photos
// in order. Boxes used to exist only on the server, as invisible upload "batches", so
// nothing on screen said which photos made which chapter — and a photo dragged across
// what looked like one long list quietly split a chapter in two.
//
// Every drag here has a tap equivalent in the photo view (Earlier / Later / Move to
// chapter / Split), because a phone can't drag at all.

const dragTypes = (e) => Array.from(e.dataTransfer?.types || [])
const hasFiles = (e) => dragTypes(e).includes('Files')
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`
const capital = (s) => s.charAt(0).toUpperCase() + s.slice(1)

// What holds a box back, in the server's own order (empty, all-skipped, reading, unread,
// failed, no-text, to-check). Counted by the server: a box held back only by a page
// with no text used to read "0 to check" when the pill counted statuses itself.
const BLOCKER_TEXT = {
  empty: () => 'no photos left',
  'all-skipped': () => 'all photos skipped',
  reading: (n) => `${n} being read`,
  unread: (n) => `${n} not read`,
  failed: (n) => `${n} couldn’t be read`,
  'no-text': (n) => `${n} with no text`,
  'to-check': (n) => `${n} to check`,
}

const holdUps = (box) => (box.blockers || [])
  .map((b) => (BLOCKER_TEXT[b.kind] || (() => b.kind))(b.count))
  .join(' · ')

// A built chapter's pill speaks for its translation. A chapter rebuilt from changed
// photos reads "pending" in the chapter list, which would look like it was simply never
// translated; source_changed is what says its English is now out of date.
function builtPill(chapter) {
  if (!chapter) return { label: 'Built', cls: 'pill-translated' }
  if (chapter.source_changed) {
    return { label: 'Changed since translated — translate again', cls: 'pill-review' }
  }
  if (chapter.status === 'pending') return { label: 'Built · not translated yet', cls: 'pill-muted' }
  return {
    label: `Built · ${STATUS_LABEL[chapter.status] || chapter.status}`,
    cls: STATUS_STYLES[chapter.status] || 'pill-muted',
  }
}

export function boxPill(box, chapter) {
  switch (box.state) {
    case 'empty': return { label: 'No photos yet', cls: 'pill-muted' }
    case 'skipped': return { label: 'All photos skipped', cls: 'pill-muted' }
    case 'reading': return { label: 'Reading…', cls: 'pill-translating animate-pulse' }
    case 'unread': return { label: capital(holdUps(box) || 'not read yet'), cls: 'pill-queued' }
    case 'to-check': return { label: capital(holdUps(box) || 'to check'), cls: 'pill-review' }
    case 'ready': return { label: 'Ready to build', cls: 'pill-translated' }
    case 'built': return builtPill(chapter)
    case 'changed': return { label: 'Changed — build to update', cls: 'pill-english' }
    case 'changed-waiting': return { label: `Changed — ${holdUps(box) || 'waiting'}`, cls: 'pill-review' }
    case 'legacy': return { label: 'Built before chapter boxes', cls: 'pill-muted' }
    default: return { label: box.orphan ? 'Not in a chapter' : (box.state || '…'), cls: 'pill-muted' }
  }
}

export default function ChapterBoxes({
  pid, boxes, chapters, expanded, busy, running, uploadingInto,
  onToggle, onOpenPhoto, onAddPhotos, onDropFiles, onMove, onNewBoxFrom, onAddBox,
  onMoveBox, onRenameBox, onDeleteBox, onReadBox, onSetSeam, onOpenChapter,
}) {
  // The photo being dragged. Cleared on dragend whatever happened to the drag, so a
  // drag let go of outside any box can never move that photo later.
  const dragId = useRef(null)
  // Where a drop would land: { id, side } on a photo, { box } on a box, or 'new'.
  const [hint, setHint] = useState(null)
  const tileDrag = (e) => !!dragId.current && dragTypes(e).includes(TILE_MIME)
  const point = (next) => setHint((h) => (JSON.stringify(h) === JSON.stringify(next) ? h : next))
  const endDrag = () => { dragId.current = null; setHint(null) }

  const tiles = {
    start(e, pageId) {
      dragId.current = pageId
      // Firefox starts no drag at all without data set.
      e.dataTransfer.setData(TILE_MIME, pageId)
      e.dataTransfer.effectAllowed = 'move'
    },
    end: endDrag,
    over(e, pageId) {
      if (!tileDrag(e)) return   // files go on to the box underneath
      e.preventDefault()
      e.stopPropagation()
      point({ id: pageId, side: sideOf(e) })
    },
    drop(e, pageId) {
      if (!tileDrag(e)) return
      e.preventDefault()
      e.stopPropagation()
      const plan = planDrop(boxes, dragId.current, { overId: pageId, side: sideOf(e) })
      endDrag()
      if (plan) onMove(plan)
    },
  }

  if (!boxes.length) {
    return (
      <div
        onDragOver={(e) => { if (hasFiles(e)) { e.preventDefault(); point('new') } }}
        onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget)) setHint(null) }}
        onDrop={(e) => {
          if (!hasFiles(e)) return
          e.preventDefault()
          setHint(null)
          onDropFiles(e.dataTransfer.files, NEW_BOX)
        }}
        className={`rounded-card border border-dashed p-10 text-center ${hint === 'new' ? 'border-line-strong' : 'border-line'}`}
      >
        <p className="text-sm text-muted">
          Each chapter gets its own box, in reading order. Drop chapter 1’s photos here, or
        </p>
        <button className="btn btn-primary mt-3 px-4 py-2 text-sm" onClick={() => onAddPhotos(NEW_BOX)}>
          Choose photos
        </button>
        <div>
          <button className="btn btn-quiet mt-2 px-3 py-1.5 text-sm" onClick={onAddBox}>
            Add an empty chapter
          </button>
        </div>
        <p className="mt-4 text-xs text-hint">
          JPEG, PNG or WebP. Photos of print pages, screenshots and scans all work.
          <br />
          iPhone photos need Settings → Camera → Formats → Most Compatible.
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {boxes.map((box) => (
        <Box
          key={box.id ?? 'orphans'} pid={pid} box={box} boxes={boxes}
          chapter={box.chapter != null ? chapters.find((c) => c.index === box.chapter) : null}
          open={expanded[box.id] ?? !(box.legacy || box.state === 'built')}
          busy={busy} running={running} uploading={uploadingInto.has(box.id)}
          hint={hint} point={point} tiles={tiles} tileDrag={tileDrag} dragId={dragId} endDrag={endDrag}
          onToggle={onToggle} onOpenPhoto={onOpenPhoto} onAddPhotos={onAddPhotos}
          onDropFiles={onDropFiles} onMove={onMove} onMoveBox={onMoveBox}
          onRenameBox={onRenameBox} onDeleteBox={onDeleteBox} onReadBox={onReadBox}
          onSetSeam={onSetSeam} onOpenChapter={onOpenChapter}
        />
      ))}

      <button
        type="button"
        onClick={onAddBox}
        onDragOver={(e) => {
          if (hasFiles(e) || tileDrag(e)) { e.preventDefault(); point('new') }
        }}
        onDragLeave={() => setHint(null)}
        onDrop={(e) => {
          if (tileDrag(e)) {
            e.preventDefault()
            const id = dragId.current
            endDrag()
            onNewBoxFrom(id)
          } else if (hasFiles(e)) {
            e.preventDefault()
            setHint(null)
            onDropFiles(e.dataTransfer.files, NEW_BOX)
          }
        }}
        className={`w-full rounded-card border border-dashed p-4 text-sm text-muted hover:text-ink ${hint === 'new' ? 'border-line-strong' : 'border-line'}`}
      >
        ＋ Add chapter — or drop photos here to start a new one
      </button>
    </div>
  )
}

// The half of a photo the pointer is over decides the side, read from the drop itself
// rather than the last dragover, which can be a frame behind.
function sideOf(e) {
  const rect = e.currentTarget.getBoundingClientRect()
  return e.clientX < rect.left + rect.width / 2 ? 'before' : 'after'
}

function Box({
  pid, box, boxes, chapter, open, busy, running, uploading, hint, point, tiles, tileDrag,
  dragId, endDrag, onToggle, onOpenPhoto, onAddPhotos, onDropFiles, onMove, onMoveBox,
  onRenameBox, onDeleteBox, onReadBox, onSetSeam, onOpenChapter,
}) {
  const name = boxName(box)
  const locked = isLocked(box)
  const built = isBuilt(box)
  const pill = boxPill(box, chapter)
  const unread = box.pages.filter((p) => p.status === 'new' || p.status === 'failed')
  const up = planBoxMove(boxes, box.id, -1)
  const down = planBoxMove(boxes, box.id, 1)
  const above = boxes[boxes.findIndex((b) => b.id === box.id) - 1]
  // "No photos yet" already says it for a new box, and a box being read says so in its pill.
  const messages = box.state === 'empty' || box.state === 'reading'
    ? [] : (box.blockers || []).map((b) => b.message).filter(Boolean)
  const dropping = hint?.box === box.id

  return (
    <section
      aria-label={name}
      id={box.id ? `box-${box.id}` : undefined}
      className="card p-3"
      style={dropping ? { outline: '2px dashed var(--accent)', outlineOffset: '2px' } : undefined}
      onDragOver={(e) => {
        if (locked || !(hasFiles(e) || tileDrag(e))) return
        e.preventDefault()
        point({ box: box.id })
      }}
      onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget)) point(null) }}
      onDrop={(e) => {
        if (locked) return
        if (tileDrag(e)) {
          e.preventDefault()
          const plan = planDrop(boxes, dragId.current, { boxId: box.id })
          endDrag()
          if (plan) onMove(plan)
        } else if (hasFiles(e)) {
          e.preventDefault()
          point(null)
          onDropFiles(e.dataTransfer.files, box.id)
        }
      }}
    >
      <header className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <h2 className="font-reading text-lg font-medium">{box.legacy ? '🔒 ' : ''}{name}</h2>
        <BoxTitle box={box} name={name} onRename={onRenameBox} />
        <span className={`pill ${pill.cls}`} title={messages.join('\n') || undefined}>{pill.label}</span>
        <span className="text-xs text-hint">{plural(box.pages.length, 'photo')}</span>
        {!locked && unread.length > 0 && (
          <button className="btn btn-quiet px-2 py-1 text-xs" disabled={!!busy || running} onClick={() => onReadBox(box)}>
            Read {unread.length}
          </button>
        )}
        <div className="flex-1" />
        {box.chapter != null && (
          <button className="btn btn-quiet px-2 py-1 text-xs" onClick={() => onOpenChapter(box.chapter)}>
            Read chapter {box.chapter} →
          </button>
        )}
        {!built && !locked && (
          <>
            <button
              className="btn btn-quiet tap px-2 py-1 text-sm" aria-label={`Move ${name} up`}
              disabled={!up} onClick={() => onMoveBox(box.id, -1)}
              title={!up && isBuilt(above) ? `Built chapters keep their place, so this can’t go above ${boxName(above)}` : undefined}
            >↑</button>
            <button
              className="btn btn-quiet tap px-2 py-1 text-sm" aria-label={`Move ${name} down`}
              disabled={!down} onClick={() => onMoveBox(box.id, 1)}
            >↓</button>
            <button
              className="btn btn-quiet tap px-2 py-1 text-xs" aria-label={`Delete ${name}`}
              disabled={uploading} title={uploading ? 'Photos are still being added to this chapter' : undefined}
              onClick={() => onDeleteBox(box)}
            >Delete</button>
          </>
        )}
        <button
          className="btn btn-quiet tap px-2 py-1 text-xs" aria-expanded={open}
          aria-label={`${open ? 'Hide' : 'Show'} the photos in ${name}`}
          onClick={() => onToggle(box.id, !open)}
        >{open ? 'Hide' : 'Show'}</button>
      </header>

      {open && messages.length > 0 && (
        <ul className="mt-1 space-y-0.5 text-xs text-hint">
          {messages.map((m, i) => <li key={i}>· {m}</li>)}
        </ul>
      )}
      {open && box.legacy && (
        <p className="mt-1 text-xs text-hint">
          These chapters were built from one pile of photos, before chapter boxes, so they
          stay as they are. Start over (in Build chapters) turns this pile into one chapter.
        </p>
      )}

      {open && (
        <ul className="mt-3 flex flex-wrap gap-x-1 gap-y-3">
          {box.pages.map((page, i) => (
            <li key={page.id} className="flex items-start">
              {!locked && seamPrev(box.pages, i) !== -1 && (
                <SeamMarker
                  page={page} pos={i + 1} prevPos={seamPrev(box.pages, i) + 1}
                  onSet={(kind) => onSetSeam(page.id, kind)} onOpen={() => onOpenPhoto(page.id)}
                />
              )}
              <div
                role="button" tabIndex={0} draggable={!locked} data-page-id={page.id}
                aria-label={`Photo ${i + 1} of ${name}: ${PAGE_STATUS_LABEL[page.status] || page.status}`}
                onClick={() => onOpenPhoto(page.id)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpenPhoto(page.id) }
                }}
                onDragStart={locked ? undefined : (e) => tiles.start(e, page.id)}
                onDragEnd={tiles.end}
                onDragOver={locked ? undefined : (e) => tiles.over(e, page.id)}
                onDrop={locked ? undefined : (e) => tiles.drop(e, page.id)}
                className={`rowhover relative w-28 cursor-pointer rounded-card p-1 ${page.status === 'skipped' ? 'opacity-60' : ''}`}
                style={hint?.id === page.id
                  ? { boxShadow: `inset ${hint.side === 'before' ? 3 : -3}px 0 0 var(--accent)` } : undefined}
              >
                <img
                  src={api.pageImageUrl(pid, page.id)} alt="" draggable={false} loading="lazy" decoding="async"
                  className="aspect-[3/4] w-full rounded-card border border-line object-cover"
                  style={{ background: 'var(--b-muted-bg)' }}
                />
                <span className="pill pill-muted absolute left-2 top-2 !px-1.5 !py-0 text-[11px] tabular-nums">{i + 1}</span>
                <span className="mt-1 block"><PageBadge status={page.status} /></span>
                <span className="block truncate text-[11px] text-hint" title={page.name || undefined}>
                  {page.name || `Page ${page.seq}`}
                </span>
              </div>
            </li>
          ))}
          {!locked && (
            <li>
              <button
                type="button" data-add-photos={box.id} aria-label={`Add photos to ${name}`}
                onClick={() => onAddPhotos(box.id)}
                className="m-1 flex aspect-[3/4] w-28 flex-col items-center justify-center rounded-card border border-dashed border-line text-muted hover:text-ink"
              >
                <span className="text-2xl leading-none">＋</span>
                <span className="mt-1 text-xs">Add photos</span>
              </button>
            </li>
          )}
        </ul>
      )}
    </section>
  )
}

// The join between a photo and the one before it. A click cycles it; a heading Claude
// reported opens the photo instead, because what it usually wants is a split.
function SeamMarker({ page, pos, prevPos, onSet, onOpen }) {
  const kind = page.join_prev || ''
  const seam = SEAMS[kind]
  const skipped = prevPos !== pos - 1 ? `\n\n(It joins photo ${prevPos}: photo ${pos - 1} is skipped.)` : ''
  const title = !seam
    ? `Not worked out yet — “Work out the joins” decides it. Click to set it yourself.${skipped}`
    : `${seam.hint}${page.join_reason ? `\n\n(${page.join_reason})` : ''}${skipped}\n\n`
      + (kind === 'chapter' ? 'Click to open this photo.' : 'Click to change.')
  return (
    <button
      type="button" title={title}
      aria-label={`Join before photo ${pos}: ${seam ? seam.label : 'not worked out yet'}`}
      onClick={() => (kind === 'chapter' ? onOpen() : onSet(nextSeam(kind)))}
      className={`pill mx-0.5 mt-[4.5rem] !px-1.5 !py-0 text-[11px] ${seam ? seam.cls : 'pill-muted border border-dashed border-line'}`}
    >
      {seam ? seam.icon : '·'}
    </button>
  )
}

// The box's title becomes the chapter's title. Without one, the heading Claude read off
// the first photo is used, which is shown so it can be corrected before it's built.
function BoxTitle({ box, name, onRename }) {
  const [editing, setEditing] = useState(false)
  const cancelled = useRef(false)
  const title = boxTitle(box)
  if (isLocked(box)) return title ? <span className="text-sm text-muted">{title}</span> : null
  if (editing) {
    return (
      <input
        autoFocus
        className="input min-w-[12rem] flex-1 py-1 text-sm"
        aria-label={`Title of ${name}`}
        defaultValue={box.label || ''}
        maxLength={120}
        placeholder="Add a title (optional) — it becomes the chapter title"
        onKeyDown={(e) => {
          if (e.key === 'Enter') e.currentTarget.blur()
          if (e.key === 'Escape') { cancelled.current = true; e.currentTarget.blur() }
        }}
        onBlur={(e) => {
          const value = e.target.value.trim()
          setEditing(false)
          if (!cancelled.current && value !== (box.label || '')) onRename(box.id, value)
          cancelled.current = false
        }}
      />
    )
  }
  return (
    <button
      type="button" aria-label={`Rename ${name}`} onClick={() => setEditing(true)}
      className="min-w-0 truncate text-left text-sm hover:text-ink"
      title="The title becomes the chapter title"
    >
      {box.label ? <span>{box.label}</span>
        : title ? <span className="text-hint">{title} <span className="text-[11px]">(from the first photo)</span></span>
        : <span className="text-hint">Add a title</span>}
      <span className="ml-1 text-hint" aria-hidden="true">✎</span>
    </button>
  )
}
