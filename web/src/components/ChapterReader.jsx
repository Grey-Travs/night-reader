import { Children, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { api } from '../api'
import { Badge } from './ui'
import Hint from './Hint'
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

function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

// Inline Markdown emphasis -> HTML, so it pastes as actual italic/bold.
function inlineHtml(s) {
  return escapeHtml(s)
    .replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/(?<!\w)_(.+?)_(?!\w)/g, '<em>$1</em>')
    .replace(/`([^`]+)`/g, '$1')
}

// Drop a leading chapter-number/title heading (e.g. "# 12") so Copy gives just the
// prose. Only the very first block is removed, and only when it's a heading.
function stripLeadingHeading(md) {
  return (md || '').replace(/^﻿?\s*#{1,6}[ \t]+[^\n]*(?:\n+|$)/, '')
}

// Render the chapter Markdown to HTML so copying preserves italics, bold, and the
// *** thematic break (as a real <hr> line) when pasted into Docs/Word/email.
function mdToHtml(md) {
  return (md || '').replace(/\r\n/g, '\n').trim().split(/\n\s*\n/).map((b) => {
    b = b.trim()
    if (!b) return ''
    if (/^(?:[-*_] *){3,}$/.test(b)) return '<hr>'
    const h = b.match(/^(#{1,6})\s+(.*)$/)
    if (h) { const lv = Math.min(h[1].length, 6); return `<h${lv}>${inlineHtml(h[2].trim())}</h${lv}>` }
    if (b.startsWith('>')) return `<blockquote><p>${inlineHtml(b.replace(/^[ \t]{0,3}>[ \t]?/gm, '')).replace(/\n/g, '<br>')}</p></blockquote>`
    return `<p>${b.split('\n').map(inlineHtml).join('<br>')}</p>`
  }).filter(Boolean).join('\n')
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

export default function ChapterReader({ pid, index, chapters, glossary = [], onClose, onNavigate, onChanged, onRetranslate, onGuide }) {
  const { on: hintsOn } = useHints()
  const [showTip, setShowTip] = useState(true)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [showSource, setShowSource] = useState(false)
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
  const [accepting, setAccepting] = useState(false)
  const scrollerRef = useRef(null)              // the outer scroll container
  const lastSave = useRef(0)                    // throttle scroll-position writes
  const advancedRef = useRef(false)             // auto-advance fires once per chapter

  // Copy the chapter as rich text (italics, bold and the *** scene break survive a
  // paste into Docs/Word), keeping the raw text as the plain-text fallback.
  async function copyChapter(rawMd) {
    const md = stripLeadingHeading(rawMd)
    const html = mdToHtml(md)
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

  const load = useCallback(() => {
    setData(null)
    api.chapter(pid, index).then(setData).catch((e) => setError(String(e.message || e)))
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

  // AI resolve: re-translate targeting the chapter's specific failures, then show the
  // before/after so the user can keep it or revert.
  async function runResolve() {
    setResolving(true); setError(null)
    try {
      const r = await api.resolveChapter(pid, index)
      onChanged?.()
      load()
      if (r.has_previous) {
        try { setPrevText((await api.previousChapter(pid, index)).translation || '') } catch { /* ignore */ }
        setShowCompare(true)
      }
    } catch (e) { setError(String(e.message || e)) }
    finally { setResolving(false) }
  }
  async function runAccept() {
    setAccepting(true); setError(null)
    try { await api.acceptChapter(pid, index); onChanged?.(); load() }
    catch (e) { setError(String(e.message || e)) }
    finally { setAccepting(false) }
  }

  // Neighbour chapters for prev/next (across the whole novel, in order).
  const order = (chapters || []).map((c) => c.index)
  const pos = order.indexOf(index)
  const prevIndex = pos > 0 ? order[pos - 1] : null
  const nextIndex = pos >= 0 && pos < order.length - 1 ? order[pos + 1] : null

  // Esc backs out one layer at a time (popover → edit mode → close), so it never
  // throws away an in-progress edit. ←/→ flip chapters (unless typing in a field).
  useEffect(() => {
    const h = (e) => {
      if (e.key === 'Escape') {
        if (showShortcuts) { setShowShortcuts(false); return }
        if (showType) { setShowType(false); return }
        if (editing) { setEditing(false); return }
        onClose()
        return
      }
      const t = e.target.tagName
      if (editing || t === 'INPUT' || t === 'TEXTAREA' || t === 'SELECT') return
      if (e.key === '?') { setShowShortcuts((v) => !v); return }
      if (e.key === 'ArrowLeft' && prevIndex != null) onNavigate(prevIndex)
      if (e.key === 'ArrowRight' && nextIndex != null) onNavigate(nextIndex)
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose, onNavigate, prevIndex, nextIndex, editing, showType, showShortcuts])

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
    if (prefs.autoAdvance && !editing && nextIndex != null && !advancedRef.current && ratio >= 0.992) {
      advancedRef.current = true
      onNavigate(nextIndex)
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

  // Fast (free, instant) check — always opens the popup so the deep-check is one
  // click away even when the quick scan finds nothing.
  async function runScan() {
    setScanning(true)
    setError(null)
    try {
      setScan(await api.scanChapter(pid, index))
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
      setScan(await api.deepScanChapter(pid, index))
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
      setScan(r) // show whatever still needs a re-translate (empty list = all fixed)
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setFixing(false)
    }
  }

  const hasTranslation = !!data?.translation
  const failures = data?.failures || []
  const th = THEME[prefs.theme] || THEME.default
  const fontFam = FONT[prefs.font] || FONT.serif
  // Glossary-name highlighting for the rendered translation (toggleable).
  const glossLookup = useMemo(() => buildGlossLookup(glossary), [glossary])
  const glossComponents = useMemo(() => {
    if (!prefs.glossaryTips || glossLookup.size === 0) return undefined
    const names = [...glossLookup.keys()].sort((a, b) => b.length - a.length).map(escapeRegExp)
    const regex = new RegExp(`(${names.join('|')})`, 'g')
    const wrap = (Tag) => function GlossTag({ node, children, ...props }) {
      return <Tag {...props}>{highlightChildren(children, regex, glossLookup)}</Tag>
    }
    return { p: wrap('p'), li: wrap('li'), em: wrap('em'), strong: wrap('strong') }
  }, [prefs.glossaryTips, glossLookup])
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
          <span className="hidden truncate text-sm text-muted sm:inline">Chapter {data?.number || index}</span>
          <span className="text-sm text-muted sm:hidden">Ch {data?.number || index}</span>
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
            <button onClick={() => prevIndex != null && onNavigate(prevIndex)} disabled={prevIndex == null} className="btn btn-ghost px-3 py-1.5 text-xs">← Prev</button>
            <button onClick={() => nextIndex != null && onNavigate(nextIndex)} disabled={nextIndex == null} className="btn btn-ghost px-3 py-1.5 text-xs">Next →</button>
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
        {data && data.number && !editing && (
          <h1 className="reading mx-auto mb-6 text-center font-semibold" style={{ maxWidth: '68ch', fontSize: `${Math.round(prefs.fontSize * 1.5)}px`, color: th.ink, fontFamily: fontFam }}>
            Chapter {data.number}
          </h1>
        )}

        {/* review reasons */}
        {data && data.status === 'needs-review' && !editing && (failures.length > 0 || data.diagnosis?.length > 0) && (
          <div className="mx-auto mb-6 rounded-card border border-line px-4 py-3 text-sm" style={{ maxWidth: '68ch', background: 'var(--b-review-bg)', color: 'var(--b-review-tx)' }}>
            <div className="font-medium">Flagged for review</div>
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
              {data.language === 'korean' && !data.offline && (
                <button onClick={runResolve} disabled={resolving} className="btn btn-primary px-3 py-1.5 text-xs" title="Re-translate this chapter with a correction aimed at the problem, then compare">{resolving ? 'Resolving with AI…' : '✨ AI resolve'}</button>
              )}
              <button onClick={runScan} disabled={scanning} className="btn btn-ghost px-3 py-1.5 text-xs" title="Scan for stray text / Korean and auto-fix what's safe">{scanning ? 'Checking…' : 'Scan & fix'}</button>
              <button onClick={runAccept} disabled={accepting} className="btn btn-ghost px-3 py-1.5 text-xs" title="It's actually fine — clear the flag">{accepting ? '…' : 'Mark as fine'}</button>
            </div>
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
                <article className="korean md:pr-8" style={dualStyle}>
                  <div className="mb-3 font-ui text-xs font-medium uppercase tracking-wide text-hint">Korean</div>
                  {data.source}
                </article>
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

        {/* Bottom navigation — so you don't have to scroll back up to flip chapters. */}
        {data && !editing && (
          <div className="mx-auto mt-12 flex max-w-3xl items-center justify-between gap-2 pb-10">
            <button onClick={() => prevIndex != null && onNavigate(prevIndex)} disabled={prevIndex == null} className="btn btn-ghost px-4 py-2 text-sm">◂ Prev</button>
            <span className="text-xs text-hint">{data.number ? `Chapter ${data.number}` : `Ch ${index}`}</span>
            <button onClick={() => nextIndex != null && onNavigate(nextIndex)} disabled={nextIndex == null} className="btn btn-primary px-5 py-2.5 text-sm">Next chapter ▸</button>
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
