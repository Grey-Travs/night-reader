import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { api } from '../api'
import { reorderIds } from '../reorder'
import { useToast } from '../toast'
import { useConfirm } from '../confirm'
import { Modal, PageBadge, SkeletonRows } from '../components/ui'

// The scanned-page workbench: photo on one side, the Korean read out of it on the
// other, and the seams between pages made visible in the middle.
//
// The seams are the part that matters. Photographing a print book, a sentence
// routinely runs across a page break; if each page is split into paragraphs on its
// own, every one of those becomes a false paragraph break, and the translator
// faithfully preserves it into the English. So the joins live IN the page rail as
// editable chips rather than hidden in a dialog.

const JOIN_CHIP = {
  sentence: { label: '→ continues', cls: 'pill-translated', hint: 'The sentence runs straight across this break — no paragraph break here.' },
  paragraph: { label: '↵ new paragraph', cls: 'pill-muted', hint: 'A paragraph ends on the previous page.' },
  chapter: { label: '📖 new chapter', cls: 'pill-english', hint: 'A new chapter starts on this page.' },
  gap: { label: '⚠ page missing?', cls: 'pill-review', hint: "The text doesn't follow on — a page may never have been photographed." },
}
const JOIN_ORDER = ['sentence', 'paragraph', 'chapter', 'gap']

// Below this the built chapter is treated as already-English and silently skipped by
// the translator, so it has to be visible per page, before that ever happens.
const MIN_HANGUL = 0.15

const CONFIDENCE_CLS = { high: 'pill-translated', medium: 'pill-queued', low: 'pill-review' }

export default function PagesPage() {
  const { pid, running, enqueue: _enqueue, showError, reload, setProjectMeta } = useOutletContext()
  const toast = useToast()
  const confirm = useConfirm()

  const [rail, setRail] = useState(null)
  const [railError, setRailError] = useState(null)
  const [selectedId, setSelectedId] = useState(null)
  const [current, setCurrent] = useState(null)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState('')
  const [uploading, setUploading] = useState(null)
  const [zoom, setZoom] = useState(1)
  const [showBuild, setShowBuild] = useState(false)
  const fileRef = useRef(null)
  const saveTimer = useRef(null)
  // The edit waiting on the debounce, so it can be FLUSHED rather than dropped when
  // the reader navigates away inside the 700ms window.
  const pendingSave = useRef(null)

  const pages = rail?.pages || []
  const counts = rail?.counts || {}

  // `quiet` refreshes never open the modal. The background poll used to call
  // showError on every failure, so a backend that died mid-run reopened an
  // undismissable dialog every 2.5 seconds — the scrim blocked the sidebar, so the
  // only way out was reloading the browser. Poll failures show inline instead.
  const loadRail = useCallback(async ({ quiet = false } = {}) => {
    try {
      setRail(await api.pages(pid))
      setRailError(null)
      return true
    } catch (e) {
      setRailError(e)
      if (!quiet) showError(e, { context: 'loading the pages' })
      return false
    }
  }, [pid, showError])

  useEffect(() => { loadRail() }, [loadRail])

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

  // Select the first page that still wants attention, once.
  useEffect(() => {
    if (selectedId || !pages.length) return
    const wanted = pages.find((p) => p.status === 'needs-check') || pages[0]
    setSelectedId(wanted.id)
  }, [pages, selectedId])

  useEffect(() => {
    if (!selectedId) { setCurrent(null); return undefined }
    let alive = true
    api.page(pid, selectedId)
      .then((p) => { if (alive) { setCurrent(p); setDraft(p.text || ''); setZoom(1) } })
      .catch(() => { if (alive) setCurrent(null) })
    return () => { alive = false }
  }, [pid, selectedId])

  const selectedIndex = pages.findIndex((p) => p.id === selectedId)

  function step(delta) {
    const next = pages[selectedIndex + delta]
    if (next) setSelectedId(next.id)
  }

  // ---- saving --------------------------------------------------------------
  async function persist(fields, { quiet = false, pageId = null } = {}) {
    const target = pageId || selectedId
    if (!target) return
    try {
      const updated = await api.savePage(pid, target, fields)
      // Only touch the editor if that page is STILL the one on screen. A debounced
      // save can land after the reader has moved on, and applying it then put page
      // A's confidence, notes and verify issues beside page B's photo.
      setCurrent((cur) => (cur && cur.id === updated.id ? updated : cur))
      setRail((r) => r && {
        ...r,
        pages: r.pages.map((p) => (p.id === updated.id
          ? {
            ...p,
            ...updated,
            // The rail row and the detail record are different shapes: `has_text`
            // exists only on the row, so the spread leaves it STALE. Recompute it
            // with `chars`, or a page that just gained text keeps has_text false
            // and the Build / Work-out-the-joins buttons stay hidden.
            text: undefined,
            raw_text: undefined,
            chars: (updated.text || '').length,
            has_text: Boolean((updated.text || '').trim()),
          }
          : p)),
      })
      if (!quiet) toast('Saved ✓')
    } catch (e) { showError(e, { context: 'saving that page' }) }
  }

  function onDraft(value) {
    setDraft(value)
    pendingSave.current = { pageId: selectedId, text: value }
    clearTimeout(saveTimer.current)
    saveTimer.current = setTimeout(() => {
      pendingSave.current = null
      persist({ text: value, pageId: selectedId }, { quiet: true })
    }, 700)
  }

  // FLUSH a pending autosave, don't cancel it — on unmount, when the novel changes,
  // and when the reader moves to another page. This cleanup used to call
  // clearTimeout despite claiming to flush, so a correction typed within the 700ms
  // window and then navigated away from was discarded silently: no toast, no
  // warning, no dirty indicator, and the typo was still there on return.
  useEffect(() => () => {
    clearTimeout(saveTimer.current)
    const p = pendingSave.current
    pendingSave.current = null
    if (p) api.savePage(pid, p.pageId, { text: p.text }).catch(() => {})
  }, [pid, selectedId])

  // ---- uploading -----------------------------------------------------------
  async function upload(files) {
    const list = Array.from(files || []).filter((f) => f.size)
    if (!list.length) return
    // One batch per drop: a batch is how you say "these photos are one chapter".
    let batch = ''
    let added = 0
    let duplicates = 0
    setUploading({ done: 0, total: list.length })
    for (const [i, file] of list.entries()) {
      try {
        const res = await api.uploadPage(pid, file, { batch, label: '' })
        batch = batch || res.batch || ''
        if (res.duplicate) duplicates += 1
        else added += 1
      } catch (e) {
        showError(e, { context: `uploading ${file.name || 'that image'}` })
        break
      }
      setUploading({ done: i + 1, total: list.length })
    }
    setUploading(null)
    await loadRail()
    const parts = []
    if (added) parts.push(`${added} page${added === 1 ? '' : 's'} added`)
    if (duplicates) parts.push(`${duplicates} already here`)
    if (parts.length) toast(parts.join(' · '))
  }

  const [dragOver, setDragOver] = useState(false)

  function onDrop(e) {
    e.preventDefault()
    setDragOver(false)
    upload(e.dataTransfer?.files)
  }

  // ---- work ----------------------------------------------------------------
  async function run(what, fn, context) {
    setBusy(what)
    try { return await fn() } catch (e) { showError(e, { context }); return null } finally { setBusy('') }
  }

  const unread = (counts.new || 0) + (counts.failed || 0)
  const flagged = counts['needs-check'] || 0

  const readAll = () => run('read', async () => {
    await api.readPages(pid, [])
    await loadRail()
    toast(`Reading ${unread} page${unread === 1 ? '' : 's'}…`)
  }, 'reading the pages')

  const verifyFlagged = () => run('verify', async () => {
    await api.verifyPages(pid, [])
    await loadRail()
    toast(`Double-checking ${flagged} page${flagged === 1 ? '' : 's'}…`)
  }, 'double-checking the pages')

  const stitch = () => run('stitch', async () => {
    const res = await api.stitchPages(pid, { use_model: true })
    setRail(res.pages)
    toast(res.gaps
      ? `${res.seams} seams · ${res.gaps} possible missing page${res.gaps === 1 ? '' : 's'}`
      : `${res.seams} seam${res.seams === 1 ? '' : 's'} worked out`)
  }, 'working out how the pages join')

  const rereadOne = () => run('reread', async () => {
    await api.readPages(pid, [selectedId])
    await loadRail()
    toast('Re-reading that page…')
  }, 'reading that page')

  const verifyOne = () => run('verify1', async () => {
    await api.verifyPages(pid, [selectedId])
    await loadRail()
    toast('Checking that page against its photo…')
  }, 'checking that page')

  async function acceptAllGood() {
    const good = pages.filter((p) => p.status === 'needs-check' && p.confidence === 'high')
    if (!good.length) { toast('Nothing to accept'); return }
    await run('accept', async () => {
      for (const page of good) await api.savePage(pid, page.id, { status: 'ok' })
      await loadRail()
      toast(`${good.length} page${good.length === 1 ? '' : 's'} accepted`)
    }, 'accepting those pages')
  }

  async function removeSelected() {
    if (!current) return
    const ok = await confirm({
      title: 'Delete this page?',
      body: 'The photo and everything read from it are removed. This cannot be undone.',
      confirmLabel: 'Delete', danger: true,
    })
    if (!ok) return
    await run('delete', async () => {
      await api.deletePages(pid, [current.id])
      setSelectedId(null)
      await loadRail()
      toast('Page deleted')
    }, 'deleting that page')
  }

  async function setJoin(pageId, kind) {
    try {
      await api.savePage(pid, pageId, { join_prev: kind })
      setRail((r) => r && {
        ...r,
        pages: r.pages.map((p) => (p.id === pageId
          ? { ...p, join_prev: kind, join_prev_source: 'user', join_reason: 'set by you' } : p)),
      })
    } catch (e) { showError(e, { context: 'changing how those pages join' }) }
  }

  // ---- reorder -------------------------------------------------------------
  const dragId = useRef(null)

  async function dropOn(targetId) {
    const from = dragId.current
    dragId.current = null
    if (!from || from === targetId) return

    // Built from the list as it stands NOW. Deriving the order from the render
    // closure and then resolving ids against fresh state was a mismatch: the 2.5s
    // poll can add or drop a page mid-drag, and the stale id list then produced
    // `undefined` rows — a page silently missing from the rail, or a crash on
    // page.id, and a reorder request that omitted a real page.
    const next = reorderIds(pages.map((p) => p.id), from, targetId)
    if (!next) return

    setRail((r) => {
      if (!r) return r
      const byId = new Map(r.pages.map((p) => [p.id, p]))
      const rows = next.map((i) => byId.get(i)).filter(Boolean)
      // If a poll changed the set underneath us, skip the optimistic update and let
      // the server's answer settle it rather than rendering a rail with holes.
      return rows.length === r.pages.length ? { ...r, pages: rows } : r
    })

    try { await api.reorderPages(pid, next) } catch (e) {
      showError(e, { context: 'reordering the pages' })
      loadRail()
    }
  }

  const anyText = pages.some((p) => p.has_text)

  return (
    <div className="page">
      <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-reading text-2xl font-medium">Pages</h1>
          <p className="text-sm text-hint">
            {rail == null && railError ? "Couldn't load this novel's pages."
              : rail == null ? 'Loading…'
              : pages.length === 0 ? 'Add photos or screenshots of the pages, and Claude reads the Korean out of them.'
              : `${counts.total} page${counts.total === 1 ? '' : 's'} · ${counts.ok || 0} good · ${flagged} to check · ${counts.new || 0} not read yet`}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <input
            ref={fileRef} type="file" accept="image/jpeg,image/png,image/webp" multiple hidden
            onChange={(e) => { upload(e.target.files); e.target.value = '' }}
          />
          <button className="btn btn-quiet px-3 py-1.5 text-sm" onClick={() => fileRef.current?.click()}>
            Add photos
          </button>
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
          {anyText && (
            <button className="btn btn-quiet px-3 py-1.5 text-sm" disabled={!!busy || running} onClick={stitch}>
              {busy === 'stitch' ? 'Working…' : 'Work out the joins'}
            </button>
          )}
          {anyText && (
            <button className="btn btn-primary px-3 py-1.5 text-sm" disabled={!!busy || running} onClick={() => setShowBuild(true)}>
              Build chapters →
            </button>
          )}
        </div>
      </div>

      {uploading && (
        <div className="mb-4 rounded-card px-3 py-2 text-sm pill-queued">
          Uploading {uploading.done} of {uploading.total}…
        </div>
      )}

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
      ) : pages.length === 0 ? (
        <div
          onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
          onDragLeave={() => setDragOver(false)}
          onDrop={onDrop}
          className={`rounded-card border border-dashed p-10 text-center ${dragOver ? 'border-line-strong' : 'border-line'}`}
        >
          <p className="text-sm text-muted">Drop page photos here, or</p>
          <button className="btn btn-primary mt-3 px-4 py-2 text-sm" onClick={() => fileRef.current?.click()}>
            Choose images
          </button>
          <p className="mt-4 text-xs text-hint">
            JPEG, PNG or WebP. Photos of print pages, screenshots and scans all work.
            <br />
            iPhone photos need Settings → Camera → Formats → Most Compatible.
          </p>
        </div>
      ) : (
        <div
          className="grid gap-4 lg:grid-cols-[15rem_minmax(0,1fr)_22rem]"
          onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
          onDragLeave={() => setDragOver(false)}
          onDrop={onDrop}
        >
          <PageRail
            pid={pid} pages={pages} selectedId={selectedId} onSelect={setSelectedId}
            onSetJoin={setJoin} dragId={dragId} onDropOn={dropOn}
          />

          <PageViewer
            pid={pid} page={current} zoom={zoom} setZoom={setZoom}
            index={selectedIndex} total={pages.length} onStep={step}
          />

          <PageEditor
            page={current} draft={draft} onDraft={onDraft} onPersist={persist}
            busy={busy} running={running}
            onReread={rereadOne} onVerify={verifyOne} onDelete={removeSelected}
            onAcceptAllGood={acceptAllGood}
          />
        </div>
      )}

      {showBuild && (
        <BuildDialog
          pid={pid} counts={counts} build={rail?.build}
          onClose={() => setShowBuild(false)}
          onBuilt={(res) => {
            setShowBuild(false)
            setProjectMeta?.(res.project || {})
            reload?.(true)
            loadRail()
            toast(`${res.added} chapter${res.added === 1 ? '' : 's'} built`)
          }}
          onError={(e) => showError(e, { context: 'building the chapters' })}
        />
      )}
    </div>
  )
}

// ---- the rail: pages, and the seams between them ---------------------------

function PageRail({ pid, pages, selectedId, onSelect, onSetJoin, dragId, onDropOn }) {
  return (
    <section className="card max-h-[70vh] overflow-y-auto p-2">
      {pages.map((page, i) => (
        <div key={page.id}>
          {i > 0 && <JoinChip page={page} onSet={(kind) => onSetJoin(page.id, kind)} />}
          <button
            draggable
            onDragStart={() => { dragId.current = page.id }}
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => { e.stopPropagation(); onDropOn(page.id) }}
            onClick={() => onSelect(page.id)}
            className={`rowhover flex w-full items-center gap-2 rounded-card p-2 text-left ${selectedId === page.id ? 'sunken' : ''}`}
          >
            <img
              src={api.pageImageUrl(pid, page.id)} alt="" loading="lazy" decoding="async"
              className="h-12 w-9 shrink-0 rounded object-cover"
              style={{ background: 'var(--b-muted-bg)' }}
            />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm">{page.name || `Page ${page.seq}`}</span>
              <span className="mt-0.5 block"><PageBadge status={page.status} /></span>
            </span>
          </button>
        </div>
      ))}
    </section>
  )
}

function JoinChip({ page, onSet }) {
  const kind = page.join_prev || 'paragraph'
  const chip = JOIN_CHIP[kind] || JOIN_CHIP.paragraph
  const next = JOIN_ORDER[(JOIN_ORDER.indexOf(kind) + 1) % JOIN_ORDER.length]
  return (
    <div className="flex items-center gap-1 px-2 py-1">
      <span className="h-px flex-1" style={{ background: 'var(--border)' }} />
      <button
        onClick={() => onSet(next)}
        title={`${chip.hint}${page.join_reason ? `\n\n(${page.join_reason})` : ''}\n\nClick to change.`}
        className={`pill ${chip.cls} !px-1.5 !py-0 text-[11px]`}
      >
        {chip.label}
      </button>
      <span className="h-px flex-1" style={{ background: 'var(--border)' }} />
    </div>
  )
}

// ---- the photo -------------------------------------------------------------

function PageViewer({ pid, page, zoom, setZoom, index, total, onStep }) {
  if (!page) return <section className="card p-5 text-sm text-hint">Pick a page.</section>
  return (
    <section className="card flex flex-col overflow-hidden">
      <div className="flex items-center justify-between gap-2 border-b border-line px-3 py-2">
        <div className="flex items-center gap-1">
          <button className="btn btn-quiet px-2 py-1 text-sm" disabled={index <= 0} onClick={() => onStep(-1)}>←</button>
          <span className="text-xs text-hint">{index + 1} / {total}</span>
          <button className="btn btn-quiet px-2 py-1 text-sm" disabled={index >= total - 1} onClick={() => onStep(1)}>→</button>
        </div>
        <div className="flex items-center gap-1">
          <button className="btn btn-quiet px-2 py-1 text-sm" onClick={() => setZoom((z) => Math.max(0.25, z - 0.25))}>−</button>
          <button className="btn btn-quiet px-2 py-1 text-xs" onClick={() => setZoom(1)}>{Math.round(zoom * 100)}%</button>
          <button className="btn btn-quiet px-2 py-1 text-sm" onClick={() => setZoom((z) => Math.min(5, z + 0.25))}>+</button>
        </div>
      </div>
      <div className="max-h-[65vh] overflow-auto p-3" style={{ background: 'var(--b-muted-bg)' }}>
        <img
          src={api.pageImageUrl(pid, page.id)} alt={page.name || `Page ${page.seq}`}
          className="mx-auto block origin-top"
          style={{ transform: `scale(${zoom})`, maxWidth: '100%' }}
        />
      </div>
    </section>
  )
}

// ---- the text read out of it -----------------------------------------------

function PageEditor({ page, draft, onDraft, onPersist, busy, running,
                      onReread, onVerify, onDelete, onAcceptAllGood }) {
  if (!page) return <section className="card p-5 text-sm text-hint">Nothing selected.</section>

  const issues = page.verify?.issues || []
  const lowKorean = (page.text || '').trim() && (page.hangul_fraction ?? 1) < MIN_HANGUL

  function applyIssue(issue) {
    if (!issue.where || !draft.includes(issue.where)) return
    onDraft(draft.replace(issue.where, issue.suggest))
  }

  return (
    <section className="card flex max-h-[70vh] flex-col overflow-y-auto p-4">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <PageBadge status={page.status} />
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
                disabled={!issue.where || !draft.includes(issue.where)}
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
        className="input font-korean min-h-[16rem] flex-1 resize-y text-sm"
        value={draft}
        placeholder="Nothing read from this page yet."
        onChange={(e) => onDraft(e.target.value)}
      />

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

      <div className="mt-3 flex flex-wrap gap-2">
        <button className="btn btn-quiet px-2 py-1 text-xs" disabled={!!busy || running} onClick={onReread}>
          Re-read from scratch
        </button>
        <button className="btn btn-quiet px-2 py-1 text-xs" disabled={!!busy || running || !draft.trim()} onClick={onVerify}>
          Check against the photo
        </button>
        <button className="btn btn-quiet px-2 py-1 text-xs" onClick={() => onPersist({ status: 'ok' })}>
          Looks good
        </button>
        <button className="btn btn-quiet px-2 py-1 text-xs" onClick={() => onPersist({ status: 'skipped' })}>
          Skip (cover / blank)
        </button>
        <button className="btn btn-quiet px-2 py-1 text-xs" onClick={onAcceptAllGood}>
          Accept all confident
        </button>
        <button className="btn btn-ghost px-2 py-1 text-xs" onClick={onDelete}>Delete</button>
      </div>

      <label className="mt-3 block text-xs text-hint">
        Note for a re-read (optional)
        {/* key={page.id} remounts the input when the reader picks another page.
            defaultValue is read once on mount, and PageEditor is NOT remounted by a
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
    </section>
  )
}

// ---- turning pages into chapters -------------------------------------------

function BuildDialog({ pid, counts, build, onClose, onBuilt, onError }) {
  const [mode, setMode] = useState('batch')
  const [include, setInclude] = useState('approved')
  const [append, setAppend] = useState(true)
  const [working, setWorking] = useState(false)

  async function go(force = false) {
    setWorking(true)
    try {
      onBuilt(await api.buildChapters(pid, { mode, include, append, force }))
    } catch (e) {
      // A rebuild that would renumber already-translated chapters is refused until
      // it's confirmed — offer that rather than just reporting the wall.
      if (e.status === 400 && /already translated|renumber/i.test(e.message || '') && !force) {
        if (window.confirm(`${e.message}\n\nRebuild anyway?`)) return go(true)
      } else onError(e)
    } finally { setWorking(false) }
  }

  return (
    <Modal onClose={onClose}>
      <div className="p-5">
        <h2 className="font-reading text-lg font-medium">Build chapters</h2>
        <p className="mt-1 text-sm text-hint">
          Joins the pages into chapters and hands them to the translator. Nothing about
          the photos changes, so you can always build again.
        </p>

        <label className="mt-4 block text-sm">
          How the pages divide into chapters
          <select className="input mt-1 w-full" value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="batch">Each batch of photos I added is one chapter</option>
            <option value="heading">One pile — split where a chapter heading appears</option>
            <option value="separator">One pile — split on a --- separator line</option>
            <option value="single">All of it is one chapter</option>
          </select>
        </label>

        <label className="mt-3 block text-sm">
          Which pages to use
          <select className="input mt-1 w-full" value={include} onChange={(e) => setInclude(e.target.value)}>
            <option value="approved">Only pages I've accepted or edited ({(counts.ok || 0) + (counts.edited || 0)})</option>
            <option value="all">Every page that has text on it</option>
          </select>
        </label>

        <label className="mt-3 flex items-center gap-2 text-sm">
          <input type="checkbox" checked={append} onChange={(e) => setAppend(e.target.checked)} />
          Add these as new chapters, keeping the ones already built
        </label>
        {build && !append && (
          <p className="mt-1 text-xs pill-review rounded-card px-2 py-1">
            Rebuilding renumbers every chapter. Any you've already translated would be
            redone.
          </p>
        )}

        <div className="mt-5 flex justify-end gap-2">
          <button className="btn btn-quiet px-3 py-1.5 text-sm" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary px-3 py-1.5 text-sm" disabled={working} onClick={() => go(false)}>
            {working ? 'Building…' : 'Build'}
          </button>
        </div>
      </div>
    </Modal>
  )
}
