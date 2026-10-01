import { useEffect } from 'react'
import { api } from '../api'
import { PageBadge, Skeleton } from './ui'
import { BUSY, SEAMS, boxName, boxTitle, isBuilt, isLocked, planSplit, seamPrev } from '../pageBoxes'

// One photo beside the Korean read out of it. ← → step through its own chapter and
// nowhere else, because stepping off the end of a chapter used to carry straight on
// into the next one, and a correction meant for the last page of chapter 4 went onto
// the first page of chapter 5 without anything on screen saying so.

// Below this the built chapter is treated as already-English and silently skipped by
// the translator, so it has to be visible per page, before that ever happens.
const MIN_HANGUL = 0.15

const CONFIDENCE_CLS = { high: 'pill-translated', medium: 'pill-queued', low: 'pill-review' }

export default function PagePhotoView({
  pid, boxes, box, index, row, page, draft, onDraft, onPersist, zoom, setZoom,
  busy, running, onBack, onStep, onReread, onVerify, onDelete, onSetSeam, onMove, onSplit,
}) {
  const total = box.pages.length
  const title = boxTitle(box)

  // ←/→ step within the chapter. Never while typing (the caret owns them), with a
  // modifier held (Alt+← is the browser's Back), or under a dialog that has the keyboard.
  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
      if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return
      if (e.target?.closest?.('input, textarea, select, [contenteditable]')) return
      if (document.querySelector('[aria-modal="true"]')) return
      e.preventDefault()
      onStep(e.key === 'ArrowLeft' ? -1 : 1)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onStep])

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <button className="btn btn-ghost px-3 py-1.5 text-sm" onClick={onBack}>← Back to chapters</button>
        <h1 className="min-w-0 flex-1 font-reading text-lg font-medium">
          {boxName(box)}{title ? ` · ${title}` : ''}
          <span className="text-hint"> — photo {index + 1} of {total}</span>
        </h1>
        <div className="flex items-center gap-1">
          <button
            className="btn btn-quiet tap px-2 py-1 text-sm" aria-label="Previous photo"
            disabled={index <= 0} onClick={() => onStep(-1)}
          >◀</button>
          <button
            className="btn btn-quiet tap px-2 py-1 text-sm" aria-label="Next photo"
            disabled={index >= total - 1} onClick={() => onStep(1)}
          >▶</button>
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_26rem]">
        <section className="card flex flex-col self-start overflow-hidden lg:sticky lg:top-4">
          <div className="flex items-center justify-between gap-2 border-b border-line px-3 py-2">
            <span className="min-w-0 truncate text-xs text-hint">{row.name || `Page ${row.seq}`}</span>
            <div className="flex items-center gap-1">
              <button className="btn btn-quiet px-2 py-1 text-sm" aria-label="Zoom out" onClick={() => setZoom((z) => Math.max(0.25, z - 0.25))}>−</button>
              <button className="btn btn-quiet px-2 py-1 text-xs" onClick={() => setZoom(1)}>{Math.round(zoom * 100)}%</button>
              <button className="btn btn-quiet px-2 py-1 text-sm" aria-label="Zoom in" onClick={() => setZoom((z) => Math.min(5, z + 0.25))}>+</button>
            </div>
          </div>
          <div className="max-h-[55vh] overflow-auto p-3 lg:max-h-[75vh]" style={{ background: 'var(--b-muted-bg)' }}>
            <img
              src={api.pageImageUrl(pid, row.id)} alt={row.name || `Page ${row.seq}`}
              className="mx-auto block origin-top"
              style={{ transform: `scale(${zoom})`, maxWidth: '100%' }}
            />
          </div>
        </section>

        <section className="card flex flex-col p-4">
          {/* The record is fetched per photo. Until it's this photo's, nothing is
              editable: typing into the previous photo's text while the next one loads
              would save that text onto the wrong page. */}
          {page && page.id === row.id ? (
            <PageEditor
              row={row} page={page} draft={draft} onDraft={onDraft} onPersist={onPersist}
              busy={busy} running={running} onReread={onReread} onVerify={onVerify} onDelete={onDelete}
            />
          ) : (
            <div className="space-y-2">
              <Skeleton className="h-5 w-1/3" />
              <Skeleton className="h-64 w-full" />
            </div>
          )}
          <SeamControl box={box} index={index} row={row} onSetSeam={onSetSeam} />
          <MoveControl boxes={boxes} box={box} index={index} row={row} onMove={onMove} onSplit={onSplit} />
        </section>
      </div>
    </div>
  )
}

// ---- the text read out of it -----------------------------------------------------------

function PageEditor({ row, page, draft, onDraft, onPersist, busy, running, onReread, onVerify, onDelete }) {
  const issues = page.verify?.issues || []
  const lowKorean = (page.text || '').trim() && (page.hangul_fraction ?? 1) < MIN_HANGUL
  // The server overwrites the text when the reading finishes, so anything typed now
  // would be lost without a word. The container fetches the text again at that point.
  const reading = BUSY.has(row.status)

  function applyIssue(issue) {
    if (!issue.where || !draft.includes(issue.where)) return
    onDraft(draft.replace(issue.where, issue.suggest))
  }

  return (
    <>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <PageBadge status={row.status} />
        {page.confidence && (
          <span className={`pill ${CONFIDENCE_CLS[page.confidence] || 'pill-muted'}`}>
            {page.confidence} confidence
          </span>
        )}
      </div>

      {page.notes?.length > 0 && (
        <ul className="mb-2 space-y-1 text-xs text-hint">
          {page.notes.map((n, i) => <li key={i}>· {n}</li>)}
        </ul>
      )}

      {issues.length > 0 && (
        <div className="mb-3 space-y-2">
          {issues.map((issue, i) => (
            <div key={i} className="rounded-card border border-line p-2 text-xs">
              <div className="mb-1 font-medium">{issue.kind}</div>
              {issue.page_says && <div className="text-muted">The photo shows: {issue.page_says}</div>}
              <button
                className="btn btn-quiet mt-1 px-2 py-0.5 text-xs"
                disabled={reading || !issue.where || !draft.includes(issue.where)}
                title={issue.where ? 'Replace that text' : "Couldn't locate this exactly — fix it by hand"}
                onClick={() => applyIssue(issue)}
              >
                Apply
              </button>
            </div>
          ))}
        </div>
      )}

      <textarea
        className="input font-korean min-h-[16rem] flex-1 resize-y text-sm lg:min-h-[50vh]"
        aria-label="Korean text read from this photo"
        value={draft}
        readOnly={reading}
        placeholder={reading ? 'Claude is reading this photo…' : 'Nothing read from this photo yet.'}
        onChange={(e) => onDraft(e.target.value)}
      />
      {reading && (
        <p className="mt-1 text-xs text-hint">
          Claude is reading this photo. Its text appears here when that’s done.
        </p>
      )}

      <div className="mt-1 flex items-center justify-between text-xs text-hint">
        <span>{(draft || '').length} characters</span>
        <span>{Math.round((page.hangul_fraction || 0) * 100)}% Korean</span>
      </div>

      {lowKorean && (
        <div className="mt-2 rounded-card px-2 py-1.5 text-xs pill-review">
          Very little Korean on this page. If it stays this way, the chapter built from
          it will be treated as already-English and skipped by the translator.
        </div>
      )}

      {row.status === 'skipped' && (
        <p className="mt-2 text-xs text-hint">
          Skipped photos are left out of the chapter. “Looks good” puts this one back.
        </p>
      )}

      <div className="mt-3 flex flex-wrap gap-2">
        <button className="btn btn-quiet px-2 py-1 text-xs" disabled={!!busy || running} onClick={onReread}>
          Re-read from scratch
        </button>
        <button className="btn btn-quiet px-2 py-1 text-xs" disabled={!!busy || running || !draft.trim()} onClick={onVerify}>
          Check against the photo
        </button>
        <button className="btn btn-quiet px-2 py-1 text-xs" disabled={reading} onClick={() => onPersist({ status: 'ok' })}>
          Looks good
        </button>
        <button className="btn btn-quiet px-2 py-1 text-xs" disabled={reading} onClick={() => onPersist({ status: 'skipped' })}>
          Skip (cover / blank)
        </button>
        <button className="btn btn-ghost px-2 py-1 text-xs" onClick={onDelete}>Delete</button>
      </div>

      <label className="mt-3 block text-xs text-hint">
        Note for a re-read (optional)
        {/* key={page.id} remounts the input when the reader moves to another photo.
            defaultValue is read once on mount, and the editor is NOT remounted by a
            prop change — so without this the box still held page A's note, and
            clicking into the textarea blurred it straight onto page B. */}
        <input
          key={page.id}
          className="input mt-1 w-full text-xs"
          placeholder="e.g. the bottom two lines are cut off"
          defaultValue={page.hint || ''}
          onBlur={(e) => e.target.value !== (page.hint || '') && onPersist({ hint: e.target.value }, { quiet: true })}
        />
      </label>
    </>
  )
}

// ---- how it joins the photo before -------------------------------------------------------

function SeamControl({ box, index, row, onSetSeam }) {
  if (box.legacy || box.orphan) return null
  const prev = seamPrev(box.pages, index)
  const kind = row.join_prev || ''
  let body
  if (row.status === 'skipped') {
    body = <p className="text-xs text-hint">A skipped photo is left out of the chapter, so it joins nothing.</p>
  } else if (prev === -1) {
    body = <p className="text-xs text-hint">First photo of the chapter — nothing to join to.</p>
  } else {
    body = (
      <>
        {kind === 'chapter' && (
          <p className="mb-2 rounded-card px-2 py-1.5 text-xs pill-english">
            Claude saw a chapter heading on this photo. If the next chapter starts here,
            use “Split chapter here” below.
          </p>
        )}
        <div className="flex flex-wrap gap-1" role="group" aria-label={`How this photo joins photo ${prev + 1}`}>
          {['sentence', 'paragraph', 'gap'].map((k) => (
            <button
              key={k} aria-pressed={kind === k}
              className={`btn px-2 py-1 text-xs ${kind === k ? 'btn-primary' : 'btn-ghost'}`}
              onClick={() => kind !== k && onSetSeam(k)}
            >
              {SEAMS[k].icon} {SEAMS[k].label}
            </button>
          ))}
        </div>
        <p className="mt-1 text-xs text-hint">
          {!kind ? 'Not worked out yet. “Work out the joins” decides it, or pick one.'
            : row.join_reason || SEAMS[kind]?.hint}
        </p>
      </>
    )
  }
  return (
    <div className="mt-4 border-t border-line pt-3">
      <h2 className="mb-1.5 text-xs font-medium text-muted">
        {prev === -1 || row.status === 'skipped' ? 'Join' : `Joins to photo ${prev + 1}`}
      </h2>
      {body}
    </div>
  )
}

// ---- where it sits ---------------------------------------------------------------------

function MoveControl({ boxes, box, index, row, onMove, onSplit }) {
  if (box.legacy) {
    return (
      <p className="mt-4 border-t border-line pt-3 text-xs text-hint">
        This photo is part of the chapters built before chapter boxes. It stays where it
        is, and fixing its text here doesn’t reach those chapters.
      </p>
    )
  }
  if (box.orphan) {
    return (
      <p className="mt-4 border-t border-line pt-3 text-xs text-hint">
        This photo isn’t in a chapter box. Reload the page to sort it into one.
      </p>
    )
  }
  const here = boxes.findIndex((b) => b.id === box.id)
  const others = boxes.map((b, i) => ({ b, i })).filter(({ b }) => b.id !== box.id && !isLocked(b))
  const split = planSplit(boxes, row.id)
  const builtAfter = boxes.slice(here + 1).find(isBuilt)
  const splitWhy = split ? null
    : index === 0 ? 'This is already the first photo of the chapter.'
    : builtAfter ? `${boxName(builtAfter)} after it is already built, so a new chapter can’t go in between. That needs Start over, in Build chapters.`
    : null

  return (
    <div className="mt-4 border-t border-line pt-3">
      <h2 className="mb-1.5 text-xs font-medium text-muted">Move this photo</h2>
      <div className="flex flex-wrap items-center gap-2">
        <button className="btn btn-ghost px-2 py-1 text-xs" disabled={index === 0} onClick={() => onMove('earlier')}>
          ◀ Earlier
        </button>
        <button className="btn btn-ghost px-2 py-1 text-xs" disabled={index >= box.pages.length - 1} onClick={() => onMove('later')}>
          Later ▶
        </button>
        {others.length > 0 && (
          <select
            className="input min-w-0 py-1 text-xs" aria-label="Move to another chapter" value=""
            onChange={(e) => e.target.value && onMove({ toBox: e.target.value })}
          >
            <option value="">Move to chapter…</option>
            {others.map(({ b, i }) => {
              const t = boxTitle(b)
              return (
                <option key={b.id} value={b.id}>
                  {boxName(b)}{t ? ` · ${t}` : ''} ({i < here ? 'at the end' : 'at the start'})
                </option>
              )
            })}
          </select>
        )}
        <button
          className={`btn px-2 py-1 text-xs ${row.join_prev === 'chapter' && split ? 'btn-primary' : 'btn-ghost'}`}
          disabled={!split} title={splitWhy || 'Start a new chapter with this photo and the ones after it'}
          onClick={onSplit}
        >
          Split chapter here
        </button>
      </div>
      {splitWhy && index > 0 && <p className="mt-1 text-xs text-hint">{splitWhy}</p>}
    </div>
  )
}
