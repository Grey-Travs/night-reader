import { useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { api } from '../api'
import { Badge } from './ui'

export default function ChapterReader({ pid, index, onClose }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [showSource, setShowSource] = useState(false)

  useEffect(() => {
    api.chapter(pid, index).then(setData).catch((e) => setError(String(e.message || e)))
  }, [pid, index])

  // Esc closes the reader.
  useEffect(() => {
    const h = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose])

  return (
    <div className="fixed inset-0 z-50 overflow-y-auto" style={{ background: 'var(--reading)' }}>
      {/* slim top bar — recedes while reading */}
      <div
        className="sticky top-0 z-10 flex items-center justify-between gap-3 border-b border-line px-4 py-3 backdrop-blur"
        style={{ background: 'color-mix(in oklab, var(--reading) 88%, transparent)' }}
      >
        <button onClick={onClose} className="btn btn-quiet text-sm" aria-label="Back">← Back</button>
        <div className="flex min-w-0 items-center gap-2">
          <span className="hidden truncate text-sm text-muted sm:inline">Chapter {index}{data?.title ? ` · ${data.title}` : ''}</span>
          <span className="text-sm text-muted sm:hidden">Ch {index}</span>
          {data && <Badge status={data.status} />}
        </div>
        {data?.translation ? (
          <button onClick={() => setShowSource((v) => !v)} className="btn btn-ghost px-3 py-1.5 text-xs">
            {showSource ? 'Hide source' : 'Show source'}
          </button>
        ) : <span className="w-16" />}
      </div>

      <div className="mx-auto w-full max-w-6xl px-5 py-10 sm:px-8">
        {error && <div className="rounded-btn px-3 py-2 text-sm pill-review">{error}</div>}
        {!data && !error && <div className="text-hint">Loading…</div>}

        {data && (
          showSource && data.translation ? (
            <div className="grid gap-8 md:grid-cols-2 md:divide-x md:divide-line">
              <article className="korean md:pr-8">
                <div className="mb-3 font-ui text-xs font-medium uppercase tracking-wide text-hint">Korean</div>
                {data.source}
              </article>
              <article className="reading md:pl-8">
                <div className="mb-3 font-ui text-xs font-medium uppercase tracking-wide text-hint">English</div>
                <ReactMarkdown>{data.translation}</ReactMarkdown>
              </article>
            </div>
          ) : (
            <article className="reading mx-auto" style={{ maxWidth: '68ch' }}>
              {data.translation ? (
                <ReactMarkdown>{data.translation}</ReactMarkdown>
              ) : (
                <div className="sunken p-4 font-ui text-sm text-muted">
                  {data.language === 'english'
                    ? 'This tab is already in English in your document.'
                    : data.language === 'empty'
                    ? 'This tab is empty.'
                    : 'Not translated yet.'}
                </div>
              )}
            </article>
          )
        )}
      </div>
    </div>
  )
}
