import { useCallback, useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { api } from '../api'
import { Badge } from './ui'
import Hint from './Hint'
import { useHints } from '../hints'
import { getReadingPrefs, markChapterRead, setLastRead, setReadingPrefs } from '../prefs'

const SEPIA_BG = '#f4ecd8'
const SEPIA_INK = '#43361f'

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

export default function ChapterReader({ pid, index, chapters, onClose, onNavigate, onChanged, onRetranslate, onGuide }) {
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
  const [copied, setCopied] = useState(false)
  const [scan, setScan] = useState(null)        // scan result {problems, auto_fixable} | null
  const [scanning, setScanning] = useState(false)
  const [deepScanning, setDeepScanning] = useState(false)
  const [fixing, setFixing] = useState(false)

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

  useEffect(() => { setEditing(false); load() }, [load])
  useEffect(() => { setLastRead(pid, index); markChapterRead(pid, index) }, [pid, index])

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
        if (showType) { setShowType(false); return }
        if (editing) { setEditing(false); return }
        onClose()
        return
      }
      const t = e.target.tagName
      if (editing || t === 'INPUT' || t === 'TEXTAREA' || t === 'SELECT') return
      if (e.key === 'ArrowLeft' && prevIndex != null) onNavigate(prevIndex)
      if (e.key === 'ArrowRight' && nextIndex != null) onNavigate(nextIndex)
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose, onNavigate, prevIndex, nextIndex, editing, showType])

  function updatePrefs(patch) {
    const next = { ...prefs, ...patch }
    setPrefs(next)
    setReadingPrefs(next)
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
  const readStyle = { fontSize: prefs.fontSize, maxWidth: `${prefs.width}ch`, ...(prefs.sepia ? { color: SEPIA_INK } : {}) }
  // Side-by-side columns: honour font size + sepia, but let the grid govern width.
  const dualStyle = { fontSize: prefs.fontSize, ...(prefs.sepia ? { color: SEPIA_INK } : {}) }
  const rootStyle = { background: prefs.sepia ? SEPIA_BG : 'var(--reading)' }
  // Raw Markdown translation (or plain source) — copyChapter renders it to rich text.
  const copyableText = data?.translation || data?.source || ''

  return (
    <div className="fixed inset-0 z-50 overflow-y-auto" style={rootStyle}>
      {/* slim top bar — recedes while reading */}
      <div
        className="sticky top-0 z-10 flex items-center justify-between gap-2 border-b border-line px-4 py-3 backdrop-blur"
        style={{ background: prefs.sepia ? SEPIA_BG : 'color-mix(in oklab, var(--reading) 88%, transparent)' }}
      >
        <button onClick={onClose} className="btn btn-quiet text-sm" aria-label="Back">← Back</button>
        <div className="flex min-w-0 items-center gap-2">
          <span className="hidden truncate text-sm text-muted sm:inline">Chapter {index}{data?.title ? ` · ${data.title}` : ''}</span>
          <span className="text-sm text-muted sm:hidden">Ch {index}</span>
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
                <label className="flex items-center justify-between">
                  <span className="text-muted">Sepia</span>
                  <input type="checkbox" checked={prefs.sepia} onChange={(e) => updatePrefs({ sepia: e.target.checked })} style={{ accentColor: 'var(--accent)' }} />
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
                ? 'Tip: “Check chapter” scans this chapter for problems. Use ← → to flip chapters. Open the Guide (❓) for a walkthrough.'
                : 'Tip: this is the original — translate it from the chapter list to read it in English. Open the Guide (❓) for help.'}
            </span>
            <button onClick={() => setShowTip(false)} className="btn btn-quiet text-xs" aria-label="Dismiss tip">Dismiss</button>
          </div>
        </div>
      )}

      <div className="mx-auto w-full max-w-6xl px-5 py-6 sm:px-8">
        {error && <div className="mb-4 rounded-btn px-3 py-2 text-sm pill-review">{error}</div>}
        {!data && !error && <div className="text-hint">Loading…</div>}

        {/* review reasons */}
        {data && data.status === 'needs-review' && failures.length > 0 && !editing && (
          <div className="mx-auto mb-6 rounded-card border border-line px-4 py-3 text-sm" style={{ maxWidth: '68ch', background: 'var(--b-review-bg)', color: 'var(--b-review-tx)' }}>
            <div className="font-medium">Flagged for review</div>
            <ul className="mt-1 list-disc pl-5">{failures.map((f, i) => <li key={i}>{f}</li>)}</ul>
            {data.validation && (
              <div className="mt-1 text-xs opacity-80">
                length ratio {data.validation.length_ratio} · paragraphs {data.validation.output_paragraphs}/{data.validation.source_paragraphs}
              </div>
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
        ) : data ? (
          hasTranslation ? (
            showSource ? (
              <div className="grid gap-8 md:grid-cols-2 md:divide-x md:divide-line">
                <article className="korean md:pr-8" style={dualStyle}>
                  <div className="mb-3 font-ui text-xs font-medium uppercase tracking-wide text-hint">Korean</div>
                  {data.source}
                </article>
                <article className="reading md:pl-8" style={dualStyle}>
                  <div className="mb-3 font-ui text-xs font-medium uppercase tracking-wide text-hint">English</div>
                  <ReactMarkdown>{data.translation}</ReactMarkdown>
                </article>
              </div>
            ) : (
              <article className="reading mx-auto" style={readStyle}>
                <ReactMarkdown>{data.translation}</ReactMarkdown>
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
    </div>
  )
}
