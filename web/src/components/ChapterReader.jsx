import { useCallback, useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { api } from '../api'
import { Badge } from './ui'
import { getReadingPrefs, setLastRead, setReadingPrefs } from '../prefs'

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

export default function ChapterReader({ pid, index, chapters, onClose, onNavigate, onChanged, onRetranslate }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [showSource, setShowSource] = useState(false)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)
  const [prefs, setPrefs] = useState(getReadingPrefs)
  const [showType, setShowType] = useState(false)

  const load = useCallback(() => {
    setData(null)
    api.chapter(pid, index).then(setData).catch((e) => setError(String(e.message || e)))
  }, [pid, index])

  useEffect(() => { setEditing(false); load() }, [load])
  useEffect(() => { setLastRead(pid, index) }, [pid, index])

  // Neighbour chapters for prev/next (across the whole novel, in order).
  const order = (chapters || []).map((c) => c.index)
  const pos = order.indexOf(index)
  const prevIndex = pos > 0 ? order[pos - 1] : null
  const nextIndex = pos >= 0 && pos < order.length - 1 ? order[pos + 1] : null

  // Esc closes; ←/→ flip chapters (unless typing in a field).
  useEffect(() => {
    const h = (e) => {
      if (e.key === 'Escape') { onClose(); return }
      const t = e.target.tagName
      if (editing || t === 'INPUT' || t === 'TEXTAREA' || t === 'SELECT') return
      if (e.key === 'ArrowLeft' && prevIndex != null) onNavigate(prevIndex)
      if (e.key === 'ArrowRight' && nextIndex != null) onNavigate(nextIndex)
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose, onNavigate, prevIndex, nextIndex, editing])

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

  const hasTranslation = !!data?.translation
  const failures = data?.failures || []
  const readStyle = { fontSize: prefs.fontSize, maxWidth: `${prefs.width}ch`, ...(prefs.sepia ? { color: SEPIA_INK } : {}) }
  const rootStyle = { background: prefs.sepia ? SEPIA_BG : 'var(--reading)' }

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
          {hasTranslation && !editing && (
            <button onClick={() => setShowSource((v) => !v)} className="btn btn-ghost px-2.5 py-1.5 text-xs">
              {showSource ? 'Hide original' : 'Show original'}
            </button>
          )}
        </div>
      </div>

      {/* action row */}
      {data && (
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-2 px-5 pt-4 sm:px-8">
          <div className="flex items-center gap-1.5">
            <button onClick={() => prevIndex != null && onNavigate(prevIndex)} disabled={prevIndex == null} className="btn btn-ghost px-3 py-1.5 text-xs">← Prev</button>
            <button onClick={() => nextIndex != null && onNavigate(nextIndex)} disabled={nextIndex == null} className="btn btn-ghost px-3 py-1.5 text-xs">Next →</button>
          </div>
          {hasTranslation && !editing && (
            <div className="flex flex-wrap items-center gap-1.5">
              <button onClick={() => { setDraft(data.translation || ''); setEditing(true); setShowSource(false) }} className="btn btn-ghost px-3 py-1.5 text-xs">Edit</button>
              <button onClick={() => downloadText(`chapter-${index}.md`, data.translation)} className="btn btn-ghost px-3 py-1.5 text-xs">Download</button>
              {data.language === 'korean' && onRetranslate && (
                <button onClick={() => { onRetranslate(index); onClose() }} className="btn btn-ghost px-3 py-1.5 text-xs">Re-translate</button>
              )}
            </div>
          )}
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
                <article className="korean md:pr-8" style={prefs.sepia ? { color: SEPIA_INK } : undefined}>
                  <div className="mb-3 font-ui text-xs font-medium uppercase tracking-wide text-hint">Korean</div>
                  {data.source}
                </article>
                <article className="reading md:pl-8" style={prefs.sepia ? { color: SEPIA_INK } : undefined}>
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
    </div>
  )
}
