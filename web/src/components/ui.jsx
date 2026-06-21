// Shared presentational bits, styled against the Night Reader tokens.
import { useEffect, useRef } from 'react'

// status -> pill class (semantic, stable across light/dark). 'failed' shares the
// needs-review coral; 'empty' is a quiet neutral.
export const STATUS_STYLES = {
  validated: 'pill-translated',
  translated: 'pill-translated',
  translating: 'pill-translating',
  pending: 'pill-queued',
  'needs-review': 'pill-review',
  failed: 'pill-review',
  empty: 'pill-muted',
  'english-source': 'pill-english',
}

export const STATUS_LABEL = {
  validated: 'Translated',
  translated: 'Translated',
  translating: 'Translating',
  pending: 'Queued',
  'needs-review': 'Needs review',
  failed: 'Failed',
  empty: 'Empty',
  'english-source': 'Already English',
}

export function Badge({ status }) {
  return (
    <span className={`pill ${STATUS_STYLES[status] || 'pill-muted'} ${status === 'translating' ? 'animate-pulse' : ''}`}>
      {STATUS_LABEL[status] || status}
    </span>
  )
}

export function Dot({ ok }) {
  return (
    <span
      className="inline-block h-2 w-2 rounded-full"
      style={{ background: ok ? 'var(--accent)' : 'var(--hint)' }}
    />
  )
}

export function StatCard({ label, value, sub, accent }) {
  return (
    <div className="card p-4">
      <div className="text-sm text-muted">{label}</div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums ${accent || 'text-ink'}`}>{value}</div>
      {sub && <div className="mt-0.5 text-xs text-hint">{sub}</div>}
    </div>
  )
}

export function Skeleton({ className = '' }) {
  return <div className={`animate-pulse rounded ${className}`} style={{ background: 'var(--b-muted-bg)' }} />
}

export function SkeletonRows({ rows = 6, className = 'h-9' }) {
  return (
    <div className="space-y-2 p-4">
      {Array.from({ length: rows }).map((_, i) => <Skeleton key={i} className={`w-full ${className}`} />)}
    </div>
  )
}

export function SkeletonCards({ count = 4 }) {
  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} className="card space-y-3 p-5">
          <Skeleton className="h-5 w-2/3" />
          <Skeleton className="h-3 w-1/2" />
          <Skeleton className="h-2 w-full" />
          <Skeleton className="h-9 w-full" />
        </div>
      ))}
    </div>
  )
}

export function ProgressBar({ value, total }) {
  const pct = total ? Math.round((value / total) * 100) : 0
  return (
    <div className="h-2.5 w-full overflow-hidden rounded-full" style={{ background: 'var(--border)' }}>
      <div
        className="h-full rounded-full transition-all duration-500"
        style={{ width: `${pct}%`, background: 'var(--accent)' }}
      />
    </div>
  )
}

export function Modal({ onClose, children, wide }) {
  const ref = useRef(null)
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    ref.current?.focus()  // move focus into the dialog
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto p-4 sm:p-8"
      style={{ background: 'var(--scrim)' }}
      onClick={onClose}
    >
      <div
        ref={ref}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        className={`w-full ${wide ? 'max-w-4xl' : 'max-w-2xl'} overflow-hidden rounded-card border border-line outline-none`}
        style={{ background: 'var(--elevated)' }}
        onClick={(e) => e.stopPropagation()}
      >
        {children}
      </div>
    </div>
  )
}
