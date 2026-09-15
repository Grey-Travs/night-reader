import { Children, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { api } from '../api'
import { blockIndexAt, isPlainParagraph, splitBlocks } from '../blocks'
// One converter, mirrored in translation_bot/mdhtml.py, so what Copy puts on the
// clipboard is byte-for-byte what the posting payload sends.
import { markdownToHtml, stripLeadingHeading } from '../mdhtml'
import { Badge } from './ui'
import Hint from './Hint'
import ParagraphPanel from './ParagraphPanel'
import ShortcutsHelp from './ShortcutsHelp'
import { useHints } from '../hints'
import { getReadingPrefs, getScrollPos, markChapterRead, setLastRead, setReadingPrefs, setScrollPos } from '../prefs'

// Reading themes (background + ink). `default` tracks the app surface so it follows
// light/dark; the rest are fixed reading palettes (paper, sepia, true-black OLED).
const THEME = {
  default: { bg: 'var(--reading)', ink: 'var(--ink)', label: 'Default' },
  paper: { bg: '#F8F3E7', ink: '#33302A', label: 'Paper' },
  sepia: { bg: '#f4ecd8', ink: '#43361f', label: 'Sepia' },
  oled: { bg: '#000000', ink: '#C9C4BC', label: 'OLED' },
}
const FONT = { serif: 'var(--font-reading)', sans: 'var(--font-ui)' }

// Shown in the flagged banner while a repair for this chapter is queued or running.
const TASK_RUNNING_NOTE = {
  translate: 'Re-translating this chapter',
  resolve: 'AI resolve is running on this chapter',
  pronouns: 'Fixing the pronouns in this chapter',
}

// --- inline glossary tooltips ------------------------------------------------
function escapeRegExp(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') }

// Keep only informative locked entries (those with a note or pronoun), keyed by
// their exact English spelling — so hovering a name actually tells you something.
function buildGlossLookup(terms) {
  const map = new Map()
  for (const t of terms || []) {
    if (t.english && (t.note || t.pronoun)) map.set(t.english, t)
  }
  return map
}

function GlossMark({ term, children }) {
  const [show, setShow] = useState(false)
  const meta = [term.note, term.pronoun && `(${term.pronoun})`].filter(Boolean).join(' · ')
  return (
    <span className="relative" onMouseEnter={() => setShow(true)} onMouseLeave={() => setShow(false)}>
      <span style={{ borderBottom: '1px dotted var(--accent-text)', cursor: 'help' }}>{children}</span>
      {show && (
        <span className="absolute left-1/2 top-full z-30 block w-56 -translate-x-1/2 rounded-card border border-line p-2 text-xs font-normal not-italic leading-snug shadow-lg" style={{ background: 'var(--elevated)', color: 'var(--ink)', fontFamily: 'var(--font-ui)' }}>
          <strong>{term.english}</strong>{meta ? ` — ${meta}` : ''}
        </span>
      )}
    </span>
  )
}

// Wrap exact glossary-name matches inside a text node with a GlossMark; non-string
// children (already-rendered elements) pass through untouched.
function highlightChildren(children, regex, lookup) {
  return Children.map(children, (child) => {
    if (typeof child !== 'string') return child
    const parts = child.split(regex)
    if (parts.length === 1) return child
    return parts.map((part, i) => {
      const term = lookup.get(part)
      return term ? <GlossMark key={i} term={term}>{part}</GlossMark> : part
    })
  })
}

// Render raw source text (paragraphs separated by blank lines) as <p> blocks.
function SourceProse({ text, lang, style }) {
  const paras = (text || '').split(/\n{2,}/).map((p) => p.trim()).filter(Boolean)
  if (!paras.length) {
    return <div className="sunken p-4 font-ui text-sm text-muted">This tab has no text.</div>
  }
  if (lang === 'english') {
    return <article className="reading mx-auto" style={style}>{paras.map((p, i) => <p key={i}>{p}</p>)}</article>
  }
  return <article className="korean mx-auto" style={style}>{paras.map((p, i) => <p key={i} className="mb-4">{p}</p>)}</article>
}

function downloadText(filename, text, type = 'text/markdown') {
  const url = URL.createObjectURL(new Blob([text], { type }))
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

export default function ChapterReader({ pid, index, chapters, glossary = [], onClose, onNavigate,
                                       onChanged, onRetranslate, onResolve, onFixPronouns,
                                       taskRunning = false, taskKind = 'translate', onGuide }) {
  const { on: hintsOn } = useHints()
  const [showTip, setShowTip] = useState(true)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [showSource, setShowSource] = useState(false)
  // For a scanned novel the truest source is the photograph itself, so it leads.
  const [sourceMode, setSourceMode] = useState('photo')
  // Which paragraph the rewrite drawer is open on, if any.
  const [paraPanel, setParaPanel] = useState(null)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)
  const [prefs, setPrefs] = useState(getReadingPrefs)
  const [showType, setShowType] = useState(false)
  const [showShortcuts, setShowShortcuts] = useState(false)
  const [copied, setCopied] = useState(false)
  const [scan, setScan] = useState(null)        // scan result {problems, auto_fixable} | null
  const [scanning, setScanning] = useState(false)
  const [deepScanning, setDeepScanning] = useState(false)
  const [fixing, setFixing] = useState(false)
  const [showCompare, setShowCompare] = useState(false) // old-vs-new previous-version view
  const [prevText, setPrevText] = useState(null)
  const [reverting, setReverting] = useState(false)
  const [resolving, setResolving] = useState(false)
  const [fixingPronouns, setFixingPronouns] = useState(false)
  const [accepting, setAccepting] = useState(false)
  const scrollerRef = useRef(null)              // the outer scroll container
  const lastSave = useRef(0)                    // throttle scroll-position writes
  const advancedRef = useRef(false)             // auto-advance fires once per chapter

  // Copy the chapter as rich text (italics, bold and the *** scene break survive a
  // paste into Docs/Word), keeping the raw text as the plain-text fallback.
  async function copyChapter(rawMd) {
    const md = stripLeadingHeading(rawMd)
    const html = markdownToHtml(md)
    let ok = false
    try {
      if (navigator.clipboard?.write && window.ClipboardItem) {
        await navigator.clipboard.write([new ClipboardItem({
          'text/html': new Blob([html], { type: 'text/html' }),
          'text/plain': new Blob([md], { type: 'text/plain' }),
        })])
        ok = true
      }
    } catch { /* fall through to plain copy */ }
    if (!ok) {
      try { await navigator.clipboard.writeText(md); ok = true } catch { /* ignore */ }
    }
    if (!ok) {
      const ta = document.createElement('textarea')
      ta.value = md
      document.body.appendChild(ta)
      ta.select()
      try { document.execCommand('copy') } catch { /* ignore */ }
      ta.remove()
    }
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  // A monotonic token, not an `alive` flag: this component stays MOUNTED across an
  // :idx change, so there is no unmount to hang a flag on. Holding the arrow key to
  // skim returns responses out of order, and the last one to arrive used to win —
  // the URL said chapter 12 while the prose, and `blocks`, were chapter 9's. A
  // rewrite then POSTed chapter 9's paragraph text against chapter 12.
  const loadToken = useRef(0)

  const load = useCallback(() => {
    const mine = ++loadToken.current
    setData(null)
    setError(null)  // otherwise a failed chapter's banner outlived it for the session
    api.chapter(pid, index)
      .then((d) => { if (mine === loadToken.current) setData(d) })
      .catch((e) => { if (mine === loadToken.current) setError(String(e.message || e)) })
  }, [pid, index])

  useEffect(() => { setEditing(false); setShowCompare(false); setPrevText(null); load() }, [load])
  useEffect(() => { setLastRead(pid, index); markChapterRead(pid, index) }, [pid, index])

  // Compare with / revert to the previous translation (retained on each re-translate).
  async function toggleCompare() {
    if (showCompare) { setShowCompare(false); return }
    setError(null)
    try {
      if (prevText == null) setPrevText((await api.previousChapter(pid, index)).translation || '')
      setShowCompare(true)
    } catch (e) { setError(String(e.message || e)) }
  }
  async function revertToPrevious() {
    if (!prevText) return
    setReverting(true); setError(null)
    try {
      await api.saveChapter(pid, index, prevText)
      setShowCompare(false); setPrevText(null)
      onChanged?.(); load() // the now-current becomes the new "previous", so it stays reversible
    } catch (e) { setError(String(e.message || e)) }
    finally { setReverting(false) }
  }

  // Both repairs are QUEUED on the novel's worker rather than awaited here, so they
  // show up in Activity, keep streaming if this view is closed, and ride out a rate
  // limit. `taskRunning` (below) is what tells us the result has landed.
  async function runResolve() {
    setResolving(true); setError(null)
    try { await onResolve?.(index) } finally { setResolving(false) }
  }
  async function runFixPronouns() {
    setFixingPronouns(true); setError(null)
    try { await onFixPronouns?.(index) } finally { setFixingPronouns(false) }
  }

  // When the queued repair finishes, reload the chapter and — if a prior version was
  // kept — drop straight into the before/after so the change can be judged or reverted.
  // The ref holds WHICH chapter was busy, so navigating away mid-repair doesn't make
  // the next chapter you open pop into compare view.
  const wasBusy = useRef(null)
  useEffect(() => {
    if (taskRunning) { wasBusy.current = index; return }
    if (wasBusy.current !== index) return
    wasBusy.current = null
    onChanged?.()
    api.chapter(pid, index).then(async (d) => {
      setData(d)
      if (!d.has_previous) return
      try { setPrevText((await api.previousChapter(pid, index)).translation || '') } catch { /* ignore */ }
      setShowCompare(true)
    }).catch(() => load())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskRunning, pid, index])
  async function runAccept() {
    setAccepting(true); setError(null)
    try { await api.acceptChapter(pid, index); onChanged?.(); load() }
    catch (e) { setError(String(e.message || e)) }
    finally { setAccepting(false) }
  }

  // Paragraph spans of the translation as it currently stands. The server addresses a
  // rewrite by this same ordinal (see web/src/blocks.js — it mirrors paragraphs.py).
  //
  // These two MUST stay above the keydown effect below: its dependency array names
  // `openParagraph`, and a dependency array is an ordinary expression evaluated during
  // render. Declaring it after the effect put it in the temporal dead zone, so every
  // render threw ReferenceError and the reader never displayed at all.
  const blocks = useMemo(() => splitBlocks(data?.translation || ''), [data?.translation])

  const openParagraph = useCallback((k) => {
    const block = blocks[k]
    if (block) setParaPanel({ paragraph: k, text: block.text })
  }, [blocks])

  // Neighbour chapters for prev/next, as {pid, index} refs.
  //
  // Within one novel that is just its chapter list. When the novel is part of a series
  // the server resolves the neighbours instead, and the one either side of a boundary
  // lives in a DIFFERENT Google Doc — which is what lets chapter 100 flow straight into
  // the next document's chapter 1. The in-novel calculation stays as the fallback, so a
  // novel in no series behaves exactly as it always did.
  const order = (chapters || []).map((c) => c.index)
  const pos = order.indexOf(index)
  const prevInNovel = pos > 0 ? order[pos - 1] : null
  const nextInNovel = pos >= 0 && pos < order.length - 1 ? order[pos + 1] : null
  // `project_id`, not `pid` — the same field name the server uses for a neighbour, so the
  // in-novel fallback and the server's cross-document ref are the same shape. They were
  // not: the server sent `project_id` while the navigator read `pid`, so crossing a
  // boundary kept the CURRENT novel's id and used the other novel's index. Chapter 100
  // led to chapter 1 of the same document, and chapter 101's Prev asked part 2 for a
  // chapter 100 it does not have.
  const prevRef = data?.prev
    || (prevInNovel != null ? { project_id: pid, index: prevInNovel } : null)
  const nextRef = data?.next
    || (nextInNovel != null ? { project_id: pid, index: nextInNovel } : null)

  // Esc backs out one layer at a time (popover → edit mode → close), so it never
  // throws away an in-progress edit. ←/→ flip chapters (unless typing in a field).
  useEffect(() => {
    const h = (e) => {
      if (e.key === 'Escape') {
        if (showShortcuts) { setShowShortcuts(false); return }
        if (showType) { setShowType(false); return }
        // Inserted, not appended: Esc from inside the rewrite drawer must close the
        // drawer, not the whole reader.
        if (paraPanel) { setParaPanel(null); return }
        if (editing) { setEditing(false); return }
        onClose()
        return
      }
      const t = e.target.tagName
      if (editing || t === 'INPUT' || t === 'TEXTAREA' || t === 'SELECT') return
      if (e.key === '?') { setShowShortcuts((v) => !v); return }
      // `r` rewrites whatever you're actually looking at — the paragraph nearest the
      // middle of the viewport.
      if (e.key === 'r' || e.key === 'R') {
        const nodes = [...document.querySelectorAll('.reading p.para[data-para]')]
        if (!nodes.length) return
        const middle = window.innerHeight / 2
        let best = null
        let bestDist = Infinity
        for (const node of nodes) {
          const box = node.getBoundingClientRect()
          const dist = Math.abs((box.top + box.bottom) / 2 - middle)
          if (dist < bestDist) { bestDist = dist; best = node }
        }
        if (best) openParagraph(Number(best.dataset.para))
        return
      }
      if (e.key === 'ArrowLeft' && prevRef) onNavigate(prevRef)
      if (e.key === 'ArrowRight' && nextRef) onNavigate(nextRef)
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose, onNavigate, prevRef, nextRef, editing, showType, showShortcuts,
      paraPanel, openParagraph])

  function updatePrefs(patch) {
    const next = { ...prefs, ...patch }
    setPrefs(next)
    setReadingPrefs(next)
  }

  // Restore the saved scroll position once the chapter content has painted, and arm
  // auto-advance fresh for this chapter.
  useEffect(() => {
    if (!data || editing) return
    const el = scrollerRef.current
    if (!el) return
    advancedRef.current = false
    const ratio = getScrollPos(pid, index)
    const id = requestAnimationFrame(() => {
      const max = el.scrollHeight - el.clientHeight
      if (max > 0 && ratio > 0) el.scrollTop = ratio * max
    })
    return () => cancelAnimationFrame(id)
  }, [data, editing, pid, index])

  // Save scroll position (throttled). When auto-advance is on, scrolling to the very
  // bottom of a scrollable chapter moves to the next one (fires once per chapter; a
  // short, non-scrollable chapter never auto-skips).
  function onScroll(e) {
    const el = e.currentTarget
    const max = el.scrollHeight - el.clientHeight
    if (max <= 0) return
    const ratio = el.scrollTop / max
    const now = Date.now()
    if (now - lastSave.current > 350) { lastSave.current = now; setScrollPos(pid, index, ratio) }
    // `!paraPanel`: applying a shorter version can push the ratio past the threshold,
    // which would flip to the next chapter out from under an open rewrite.
    if (prefs.autoAdvance && !editing && !paraPanel && nextRef && !advancedRef.current && ratio >= 0.992) {
      advancedRef.current = true
      onNavigate(nextRef)
    }
  }

  async function saveEdit() {
    setSaving(true)
    setError(null)
    try {
      await api.saveChapter(pid, index, draft)
      setEditing(false)
      onChanged?.()
      load()
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setSaving(false)
    }
  }

  // Normalize a scan response so `problems` is always an array — a missing/odd shape
  // from the API must never crash the popup render.
  const normScan = (r) => ({ ...(r || {}), problems: (r && r.problems) || [] })

  // Fast (free, instant) check — always opens the popup so the deep-check is one
  // click away even when the quick scan finds nothing.
  async function runScan() {
    setScanning(true)
    setError(null)
    try {
      setScan(normScan(await api.scanChapter(pid, index)))
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setScanning(false)
    }
  }

  // Deep check — Claude reads the whole chapter and flags non-story text anywhere,
  // catching phrasings the regex can't anticipate. Merges into the popup.
  async function runDeepScan() {
    setDeepScanning(true)
    setError(null)
    try {
      setScan(normScan(await api.deepScanChapter(pid, index)))
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setDeepScanning(false)
    }
  }

  async function autoFix() {
    setFixing(true)
    setError(null)
    try {
      const snippets = (scan?.problems || []).filter((p) => p.snippet).map((p) => p.snippet)
      const r = await api.fixChapter(pid, index, { remove: snippets })
      onChanged?.()
      load() // refresh the reader with the cleaned text
      setScan(normScan(r)) // show whatever still needs a re-translate (empty list = all fixed)
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setFixing(false)
    }
  }

  const hasTranslation = !!data?.translation
  const failures = data?.failures || []
  // A mis-gendered character is flagged separately from every other kind of problem,
  // because it is the one with a cheap targeted repair rather than a re-translation.
  const hasPronounIssue = (data?.flags || []).includes('pronoun')
  // Any repair queued for THIS chapter locks the buttons — a second one would just
  // queue behind the first and act on text the first is about to replace.
  const busyHere = taskRunning || resolving || fixingPronouns
  const th = THEME[prefs.theme] || THEME.default
  const fontFam = FONT[prefs.font] || FONT.serif
  // Glossary-name highlighting for the rendered translation (toggleable).
  const glossLookup = useMemo(() => buildGlossLookup(glossary), [glossary])
  // A scanned chapter knows which photos it was built from (batch builds only — a
  // whole-novel split can't attribute pages to a chapter, so it sends none).
  const hasPhotos = (data?.page_ids || []).length > 0
  const sourceParas = useMemo(
    () => (data?.source || '').split(/\n{2,}/).map((p) => p.trim()).filter(Boolean),
    [data?.source],
  )

  const glossRegex = useMemo(() => {
    if (!prefs.glossaryTips || glossLookup.size === 0) return null
    const names = [...glossLookup.keys()].sort((a, b) => b.length - a.length).map(escapeRegExp)
    return new RegExp(`(${names.join('|')})`, 'g')
  }, [prefs.glossaryTips, glossLookup])

  // Glossary tooltips. Shared by both components objects below, and it must ALWAYS
  // exist — it used to be undefined when glossary tips were switched off, which would
  // have silently disabled rewriting with them.
  const decorate = useMemo(() => (glossRegex
    ? (kids) => highlightChildren(kids, glossRegex, glossLookup)
    : (kids) => kids), [glossRegex, glossLookup])

  const inlineComponents = useMemo(() => {
    const wrap = (Tag) => function GlossTag({ node, children, ...props }) {
      return <Tag {...props}>{decorate(children)}</Tag>
    }
    return { li: wrap('li'), em: wrap('em'), strong: wrap('strong') }
  }, [decorate])

  // THE CHAPTER BODY: tooltips plus a rewrite handle in the margin.
  const glossComponents = useMemo(() => {
    const Paragraph = function ReaderParagraph({ node, children, ...props }) {
      const k = blockIndexAt(blocks, node?.position?.start?.offset)
      // Only offer the handle where a Markdown paragraph and a blank-line block are
      // the same thing — they disagree for rules, headings, quotes and part markers,
      // and the address is always block-based.
      const plain = k != null && isPlainParagraph(blocks[k]?.text)
      return (
        <p {...props} data-para={k ?? undefined}
           className={`para${paraPanel?.paragraph === k ? ' para-active' : ''}`}>
          {decorate(children)}
          {plain && !editing && (
            <button type="button" className="para-handle" tabIndex={-1}
                    title="Rewrite this paragraph (r)"
                    aria-label={`Rewrite paragraph ${k + 1}`}
                    onClick={() => openParagraph(k)}>✎</button>
          )}
        </p>
      )
    }
    return { ...inlineComponents, p: Paragraph }
  }, [inlineComponents, decorate, blocks, paraPanel, editing, openParagraph])

  // THE DRAWER'S VERSION CARDS: tooltips only, never a handle.
  //
  // `blocks` describes the CHAPTER, but a preview's markdown is one paragraph on its
  // own — so every offset in it is near 0 and blockIndexAt resolves them all to block
  // 0. Reusing the chapter's components put a ✎ in the margin of each version card,
  // and clicking it called openParagraph(0): the drawer silently jumped to the
  // chapter's first paragraph, dropping the versions being compared.
  const previewComponents = useMemo(() => {
    const Paragraph = function PreviewParagraph({ node, children, ...props }) {
      return <p {...props}>{decorate(children)}</p>
    }
    return { ...inlineComponents, p: Paragraph }
  }, [inlineComponents, decorate])
  const readStyle = { fontSize: prefs.fontSize, maxWidth: `${prefs.width}ch`, color: th.ink }
  // Side-by-side columns: honour font size + theme ink, but let the grid govern width.
  const dualStyle = { fontSize: prefs.fontSize, color: th.ink }
  const rootStyle = { background: th.bg }
  // Raw Markdown translation (or plain source) — copyChapter renders it to rich text.
  const copyableText = data?.translation || data?.source || ''

  return (
    <div ref={scrollerRef} onScroll={onScroll} className="fixed inset-0 z-50 overflow-y-auto" style={rootStyle}>
      {/* slim top bar — recedes while reading */}
      <div
        className="sticky top-0 z-10 flex items-center justify-between gap-2 border-b border-line px-4 py-3 backdrop-blur"
        style={{ background: `color-mix(in oklab, ${th.bg} 88%, transparent)` }}
      >
        <button onClick={onClose} className="btn btn-quiet text-sm" aria-label="Back">← Back</button>
        <div className="flex min-w-0 items-center gap-2">
          <span className="hidden truncate text-sm text-muted sm:inline">Chapter {data?.global ?? data?.number ?? index}</span>
          <span className="text-sm text-muted sm:hidden">Ch {data?.global ?? data?.number ?? index}</span>
          {data && <Badge status={data.status} />}
        </div>
        <div className="flex items-center gap-1">
          {onGuide && <button onClick={onGuide} className="btn btn-ghost px-2.5 py-1.5 text-xs" title="How to use this app" aria-label="Guide">❓</button>}
          {/* Typography */}
          <div className="relative">
            <button onClick={() => setShowType((v) => !v)} className="btn btn-ghost px-2.5 py-1.5 text-xs" title="Reading options" aria-label="Reading options">Aa</button>
            {showType && (
              <div className="absolute right-0 top-full z-20 mt-1 w-52 rounded-card border border-line p-3 text-sm shadow-lg" style={{ background: 'var(--elevated)', color: 'var(--ink)' }}>
                <div className="mb-2 flex items-center justify-between">
                  <span className="text-muted">Text size</span>
                  <span className="flex items-center gap-1">
                    <button onClick={() => updatePrefs({ fontSize: Math.max(13, prefs.fontSize - 1) })} className="btn btn-ghost h-7 w-7 !p-0">−</button>
                    <span className="w-8 text-center tabular-nums">{prefs.fontSize}</span>
                    <button onClick={() => updatePrefs({ fontSize: Math.min(28, prefs.fontSize + 1) })} className="btn btn-ghost h-7 w-7 !p-0">+</button>
                  </span>
                </div>
                <div className="mb-2 flex items-center justify-between">
                  <span className="text-muted">Width</span>
                  <span className="flex items-center gap-1">
                    <button onClick={() => updatePrefs({ width: Math.max(48, prefs.width - 4) })} className="btn btn-ghost h-7 w-7 !p-0">−</button>
                    <span className="w-8 text-center tabular-nums">{prefs.width}</span>
                    <button onClick={() => updatePrefs({ width: Math.min(96, prefs.width + 4) })} className="btn btn-ghost h-7 w-7 !p-0">+</button>
                  </span>
                </div>
                <div className="mb-2">
                  <div className="mb-1.5 text-muted">Theme</div>
                  <div className="flex gap-2">
                    {Object.keys(THEME).map((t) => (
                      <button
                        key={t}
                        onClick={() => updatePrefs({ theme: t })}
                        title={THEME[t].label}
                        aria-label={THEME[t].label}
                        className="h-7 w-7 rounded-full"
                        style={{ background: THEME[t].bg, border: `${prefs.theme === t ? 2 : 1}px solid ${prefs.theme === t ? 'var(--accent)' : 'var(--border-strong)'}` }}
                      />
                    ))}
                  </div>
                </div>
                <div className="mb-2 flex items-center justify-between">
                  <span className="text-muted">Font</span>
                  <span className="flex items-center gap-1">
                    <button onClick={() => updatePrefs({ font: 'serif' })} className={`btn px-2.5 py-1 text-xs ${prefs.font === 'serif' ? 'btn-primary' : 'btn-ghost'}`} style={{ fontFamily: 'var(--font-reading)' }}>Serif</button>
                    <button onClick={() => updatePrefs({ font: 'sans' })} className={`btn px-2.5 py-1 text-xs ${prefs.font === 'sans' ? 'btn-primary' : 'btn-ghost'}`} style={{ fontFamily: 'var(--font-ui)' }}>Sans</button>
                  </span>
                </div>
                <label className="mb-2 flex items-center justify-between">
                  <span className="text-muted">Auto-advance</span>
                  <input type="checkbox" checked={prefs.autoAdvance} onChange={(e) => updatePrefs({ autoAdvance: e.target.checked })} style={{ accentColor: 'var(--accent)' }} title="Scroll to the bottom to flip to the next chapter" />
                </label>
                <label className="flex items-center justify-between">
                  <span className="text-muted">Glossary tips</span>
                  <input type="checkbox" checked={prefs.glossaryTips} onChange={(e) => updatePrefs({ glossaryTips: e.target.checked })} style={{ accentColor: 'var(--accent)' }} title="Underline known names; hover for their glossary note" />
                </label>
              </div>
            )}
          </div>
          {hasTranslation && !editing && !!data?.source && (
            <button onClick={() => setShowSource((v) => !v)} className="btn btn-ghost px-2.5 py-1.5 text-xs">
              {showSource ? 'Hide original' : 'Show original'}
            </button>
          )}
        </div>
      </div>

      {/* action row */}
      {data && !editing && (
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-2 px-5 pt-4 sm:px-8">
          <div className="flex items-center gap-1.5">
            <button onClick={() => prevRef && onNavigate(prevRef)} disabled={!prevRef} className="btn btn-ghost px-3 py-1.5 text-xs">← Prev</button>
            <button onClick={() => nextRef && onNavigate(nextRef)} disabled={!nextRef} className="btn btn-ghost px-3 py-1.5 text-xs">Next →</button>
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            {copyableText && (
              <>
                <button onClick={() => copyChapter(copyableText)} className="btn btn-ghost px-3 py-1.5 text-xs">{copied ? 'Copied ✓' : 'Copy text'}</button>
                <Hint text="Copies just the chapter text, with its formatting. The tips and buttons on this page are never copied." />
              </>
            )}
            {hasTranslation && (
              <>
                <button onClick={runScan} disabled={scanning} className="btn btn-ghost px-3 py-1.5 text-xs">
                  {scanning ? 'Checking…' : 'Check chapter'}
                </button>
                <Hint text="Scans this chapter for problems — leftover AI notes, untranslated bits, or odd length — and offers to fix them." />
                <button onClick={() => { setDraft(data.translation || ''); setEditing(true); setShowSource(false) }} className="btn btn-ghost px-3 py-1.5 text-xs">Edit</button>
                {data.has_previous && (
                  <button onClick={toggleCompare} className="btn btn-ghost px-3 py-1.5 text-xs">{showCompare ? 'Hide compare' : 'Compare previous'}</button>
                )}
                <button onClick={() => downloadText(`chapter-${index}.md`, data.translation)} className="btn btn-ghost px-3 py-1.5 text-xs">Download</button>
                {data.language === 'korean' && onRetranslate && !data.offline && (
                  <button onClick={() => { onRetranslate(index); onClose() }} className="btn btn-ghost px-3 py-1.5 text-xs">Re-translate</button>
                )}
              </>
            )}
          </div>
        </div>
      )}

      {/* In-chapter tip — lives in the page chrome, NOT inside the prose, so it is
          never part of the text that "Copy text" copies. */}
      {hintsOn && showTip && data && !editing && (
        <div className="mx-auto mt-3 max-w-6xl px-5 sm:px-8">
          <div className="flex items-center gap-2 rounded-card border border-line px-3 py-1.5 text-xs text-muted" style={{ background: 'var(--surface)' }}>
            <span aria-hidden>💡</span>
            <span className="flex-1">
              {hasTranslation
                ? 'Tip: “Check chapter” scans this chapter for problems. ← → flip chapters · press ? for shortcuts · Aa for themes & fonts.'
                : 'Tip: this is the original — translate it from the chapter list to read it in English. Open the Guide (❓) for help.'}
            </span>
            <button onClick={() => setShowTip(false)} className="btn btn-quiet text-xs" aria-label="Dismiss tip">Dismiss</button>
          </div>
        </div>
      )}

      <div className="mx-auto w-full max-w-6xl px-5 py-6 sm:px-8">
        {error && <div className="mb-4 rounded-btn px-3 py-2 text-sm pill-review">{error}</div>}
        {!data && !error && <div className="text-hint">Loading…</div>}

        {/* Prominent chapter heading — page chrome from the chapter's own number, NOT
            part of the prose, so "Copy text" never includes it. */}
        {data && (data.global ?? data.number) && !editing && (
          <h1 className="reading mx-auto mb-6 text-center font-semibold" style={{ maxWidth: '68ch', fontSize: `${Math.round(prefs.fontSize * 1.5)}px`, color: th.ink, fontFamily: fontFam }}>
            Chapter {data.global ?? data.number}
          </h1>
        )}

        {/* The Korean moved out from under the English.
            Rebuilding a scanned novel, or correcting a page that was already built
            into a chapter, replaces this chapter's source while the translation file
            stays exactly where it was. Both panes below then show real text that was
            never a translation of each other — and nothing said so, because the state
            record still read "validated". The translation is still shown: it is work
            the reader paid for, and it is theirs to keep or redo. */}
        {data?.source_changed && hasTranslation && !editing && (
          <div className="mx-auto mb-6 rounded-card border border-line px-4 py-3 text-sm"
               style={{ maxWidth: '68ch', background: 'var(--b-review-bg)', color: 'var(--b-review-tx)' }}>
            <div className="font-medium">The original changed after this was translated</div>
            <p className="mt-1">
              This English was translated from different Korean than the text shown
              here — the pages behind this chapter were rebuilt or corrected since.
              Re-translate the chapter to bring them back in line.
            </p>
            {onRetranslate && !data.offline && (
              <button onClick={() => { onRetranslate(index); onClose() }}
                      className="btn btn-ghost mt-2 px-3 py-1 text-xs">
                Re-translate this chapter
              </button>
            )}
          </div>
        )}

        {/* review reasons */}
        {data && data.status === 'needs-review' && !editing && (failures.length > 0 || data.diagnosis?.length > 0) && (
          <div className="mx-auto mb-6 rounded-card border border-line px-4 py-3 text-sm" style={{ maxWidth: '68ch', background: 'var(--b-review-bg)', color: 'var(--b-review-tx)' }}>
            <div className="font-medium">{hasPronounIssue ? 'Wrong gender' : 'Flagged for review'}</div>
            {data.diagnosis?.length > 0 ? (
              <ul className="mt-1 space-y-1">{data.diagnosis.map((d, i) => <li key={i}>• {d.message}</li>)}</ul>
            ) : (
              <ul className="mt-1 list-disc pl-5">{failures.map((f, i) => <li key={i}>{f}</li>)}</ul>
            )}
            {data.validation && (
              <div className="mt-1 text-xs opacity-80">
                length ratio {data.validation.length_ratio} · paragraphs {data.validation.output_paragraphs}/{data.validation.source_paragraphs}
              </div>
            )}
            <div className="mt-3 flex flex-wrap gap-2">
              {/* A wrong pronoun has its own repair: the glossary already knows the right
                  one, so rewriting just the pronouns beats re-translating the chapter and
                  re-rolling every other decision. When that's the problem it leads. */}
              {hasPronounIssue && !data.offline && (
                <button onClick={runFixPronouns} disabled={busyHere} className="btn btn-primary px-3 py-1.5 text-xs"
                  title="Rewrite only the pronouns of the mis-gendered characters, keeping the rest of the chapter exactly as it is">
                  {fixingPronouns ? 'Starting…' : '⚥ Fix pronouns'}
                </button>
              )}
              {data.language === 'korean' && !data.offline && (
                <button onClick={runResolve} disabled={busyHere} className={`btn ${hasPronounIssue ? 'btn-ghost' : 'btn-primary'} px-3 py-1.5 text-xs`} title="Re-translate this chapter with a correction aimed at the problem, then compare">{resolving ? 'Starting…' : '✨ AI resolve'}</button>
              )}
              <button onClick={runScan} disabled={scanning || busyHere} className="btn btn-ghost px-3 py-1.5 text-xs" title="Scan for stray text / Korean and auto-fix what's safe">{scanning ? 'Checking…' : 'Scan & fix'}</button>
              <button onClick={runAccept} disabled={accepting || busyHere} className="btn btn-ghost px-3 py-1.5 text-xs" title="It's actually fine — clear the flag">{accepting ? '…' : 'Mark as fine'}</button>
            </div>
            {taskRunning ? (
              <div className="mt-2 flex items-center gap-2 text-xs">
                <span className="inline-block h-1.5 w-1.5 shrink-0 rounded-full animate-pulse" style={{ background: 'currentColor' }} />
                {TASK_RUNNING_NOTE[taskKind] || TASK_RUNNING_NOTE.translate} — follow it on the novel's Activity tab. You can close this.
              </div>
            ) : (
              <div className="mt-2 text-xs opacity-80">Fixing uses your Claude plan. The version before the fix is kept, so you can compare and revert.</div>
            )}
          </div>
        )}

        {data && editing ? (
          <div className="mx-auto" style={{ maxWidth: '80ch' }}>
            <div className="mb-2 text-sm text-muted">Editing chapter {index} — Markdown. Saving marks it reviewed.</div>
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              className="input min-h-[55vh] w-full font-mono text-sm"
              style={{ lineHeight: 1.6 }}
            />
            <div className="mt-3 flex gap-2">
              <button onClick={saveEdit} disabled={saving} className="btn btn-primary px-4 py-2 text-sm">{saving ? 'Saving…' : 'Save'}</button>
              <button onClick={() => setEditing(false)} disabled={saving} className="btn btn-ghost px-4 py-2 text-sm">Cancel</button>
            </div>
          </div>
        ) : data && showCompare ? (
          <div>
            <div className="mx-auto mb-3 flex max-w-6xl flex-wrap items-center justify-between gap-2">
              <span className="text-sm text-muted">Previous translation (left) vs current (right). Revert is reversible — the current is kept as the new “previous.”</span>
              <button onClick={revertToPrevious} disabled={reverting} className="btn btn-primary px-3 py-1.5 text-xs">{reverting ? 'Reverting…' : 'Revert to previous'}</button>
            </div>
            <div className="grid gap-8 md:grid-cols-2 md:divide-x md:divide-line">
              <article className="reading md:pr-8" style={{ ...dualStyle, fontFamily: fontFam }}>
                <div className="mb-3 font-ui text-xs font-medium uppercase tracking-wide text-hint">Previous</div>
                <ReactMarkdown>{prevText || '*(empty)*'}</ReactMarkdown>
              </article>
              <article className="reading md:pl-8" style={{ ...dualStyle, fontFamily: fontFam }}>
                <div className="mb-3 font-ui text-xs font-medium uppercase tracking-wide text-hint">Current</div>
                <ReactMarkdown>{data.translation}</ReactMarkdown>
              </article>
            </div>
          </div>
        ) : data ? (
          hasTranslation ? (
            showSource ? (
              <div className="grid gap-8 md:grid-cols-2 md:divide-x md:divide-line">
                <div className="md:pr-8">
                  <div className="mb-3 flex items-center justify-between gap-2">
                    <span className="font-ui text-xs font-medium uppercase tracking-wide text-hint">
                      {hasPhotos && sourceMode === 'photo' ? 'The page' : 'Korean'}
                    </span>
                    {/* A scanned chapter can show the actual photograph, which makes
                        any translation error traceable back to the page it came from. */}
                    {hasPhotos && (
                      <div className="flex gap-1">
                        {['photo', 'text'].map((mode) => (
                          <button
                            key={mode}
                            onClick={() => setSourceMode(mode)}
                            className={`btn px-2 py-0.5 text-[11px] ${sourceMode === mode ? 'btn-primary' : 'btn-ghost'}`}
                          >
                            {mode === 'photo' ? 'Photo' : 'Text'}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                  {hasPhotos && sourceMode === 'photo' ? (
                    <div className="space-y-3">
                      {data.page_ids.map((id) => (
                        <img
                          key={id} src={api.pageImageUrl(pid, id)} alt=""
                          loading="lazy" decoding="async"
                          className="w-full rounded-card border border-line"
                        />
                      ))}
                    </div>
                  ) : (
                    <article className="korean" style={dualStyle}>
                      {sourceParas.length
                        ? sourceParas.map((p, i) => <p key={i} className="mb-4">{p}</p>)
                        : <div className="sunken p-4 font-ui text-sm text-muted">No source text.</div>}
                    </article>
                  )}
                </div>
                <article className="reading md:pl-8" style={{ ...dualStyle, fontFamily: fontFam }}>
                  <div className="mb-3 font-ui text-xs font-medium uppercase tracking-wide text-hint">English</div>
                  <ReactMarkdown components={glossComponents}>{data.translation}</ReactMarkdown>
                </article>
              </div>
            ) : (
              <article className="reading mx-auto" style={{ ...readStyle, fontFamily: fontFam }}>
                <ReactMarkdown components={glossComponents}>{data.translation}</ReactMarkdown>
              </article>
            )
          ) : data.language === 'empty' ? (
            <article className="reading mx-auto" style={readStyle}>
              <div className="sunken p-4 font-ui text-sm text-muted">This tab is empty.</div>
            </article>
          ) : (
            <>
              <div className="mx-auto mb-6 sunken px-4 py-2 text-center font-ui text-sm text-muted" style={{ maxWidth: data.language === 'english' ? '68ch' : '72ch' }}>
                {data.language === 'english'
                  ? 'This tab is already in English in your document — shown below.'
                  : 'Not translated yet — showing the original. Translate it from the chapter list to read it in English.'}
              </div>
              <SourceProse text={data.source} lang={data.language} style={readStyle} />
            </>
          )
        ) : null}

        {paraPanel && (
          <ParagraphPanel
            pid={pid}
            index={index}
            paragraph={paraPanel.paragraph}
            expectedText={paraPanel.text}
            components={previewComponents}
            onClose={() => setParaPanel(null)}
            onApplied={(res) => {
              // Patch in place rather than reloading: a remount re-fires the
              // scroll-restore effect and jumps to a stale saved position.
              setData((d) => d && {
                ...d,
                translation: res.translation,
                status: res.status ?? d.status,
                validation: res.validation ?? d.validation,
              })
              setParaPanel((p) => p && { ...p, text: res.group?.variants?.find(
                (v) => v.id === res.group.current_id)?.text ?? p.text })
              onChanged?.()
            }}
          />
        )}

        {/* Bottom navigation — so you don't have to scroll back up to flip chapters. */}
        {data && !editing && (
          <div className="mx-auto mt-12 flex max-w-3xl items-center justify-between gap-2 pb-10">
            <button onClick={() => prevRef && onNavigate(prevRef)} disabled={!prevRef} className="btn btn-ghost px-4 py-2 text-sm">◂ Prev</button>
            <span className="text-xs text-hint">
              {(data.global ?? data.number) ? `Chapter ${data.global ?? data.number}` : `Ch ${index}`}
            </span>
            <button onClick={() => nextRef && onNavigate(nextRef)} disabled={!nextRef} className="btn btn-primary px-5 py-2.5 text-sm">Next chapter ▸</button>
          </div>
        )}
      </div>

      {/* Chapter-check results popup */}
      {scan && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center p-4" style={{ background: 'var(--scrim)' }} onClick={() => setScan(null)}>
          <div className="w-full max-w-lg overflow-hidden rounded-card border border-line" style={{ background: 'var(--elevated)', color: 'var(--ink)' }} onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between border-b border-line px-5 py-3">
              <h3 className="font-medium">
                Chapter {index} — {scan.problems.length ? `${scan.problems.length} issue${scan.problems.length === 1 ? '' : 's'}` : 'no issues found'}
                {scan.deep && <span className="ml-2 text-xs text-hint">(deep AI check)</span>}
              </h3>
              <button onClick={() => setScan(null)} className="btn btn-quiet text-lg leading-none">✕</button>
            </div>
            <div className="max-h-[55vh] overflow-y-auto px-5 py-4">
              {scan.problems.length === 0 ? (
                <div className="sunken px-3 py-3 text-sm text-muted">
                  {scan.deep
                    ? 'The deep AI check found no stray text — this chapter looks clean.'
                    : 'No obvious issues. For full confidence, run the deep AI check below — it reads the whole chapter for anything the quick check might miss.'}
                </div>
              ) : (
                <ul className="space-y-2">
                  {scan.problems.map((p, i) => (
                    <li key={i} className="flex items-start gap-2 rounded-card border border-line p-3 text-sm">
                      <span className={`pill ${p.severity === 'high' ? 'pill-review' : p.severity === 'medium' ? 'pill-queued' : 'pill-muted'}`}>{p.severity}</span>
                      <span className="flex-1">{p.message}{p.auto_fixable && <span className="ml-1 text-xs text-hint">· auto-fixable</span>}</span>
                    </li>
                  ))}
                </ul>
              )}
              {scan.problems.length > 0 && (
                <p className="mt-3 text-xs text-hint">
                  {scan.auto_fixable
                    ? 'Auto-fix removes the flagged stray text in place (the original is backed up). Anything it can’t fix needs a re-translate.'
                    : 'These need a re-translate to fix.'}
                </p>
              )}
            </div>
            <div className="flex flex-wrap justify-end gap-2 border-t border-line px-5 py-3">
              {!scan.deep && (
                <button onClick={runDeepScan} disabled={deepScanning} className="btn btn-ghost mr-auto px-4 py-2 text-sm">{deepScanning ? 'Deep checking…' : 'Deep check with AI'}</button>
              )}
              {scan.auto_fixable && (
                <button onClick={autoFix} disabled={fixing} className="btn btn-primary px-4 py-2 text-sm">{fixing ? 'Fixing…' : 'Fix automatically'}</button>
              )}
              {data?.language === 'korean' && onRetranslate && !data?.offline && (
                <button onClick={() => { onRetranslate(index); setScan(null); onClose() }} className="btn btn-ghost px-4 py-2 text-sm">Re-translate chapter</button>
              )}
              <button onClick={() => setScan(null)} className="btn btn-ghost px-4 py-2 text-sm">Close</button>
            </div>
          </div>
        </div>
      )}
      {showShortcuts && <ShortcutsHelp onClose={() => setShowShortcuts(false)} />}
    </div>
  )
}
