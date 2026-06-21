import { useEffect, useState } from 'react'
import { useHints } from '../hints'
import { COST_NOTE, FAQ, LEGEND, QUICK_START, STEPS, TERMS } from '../guide'

function Flow({ steps }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {steps.map((s, i) => (
        <span key={i} className="flex items-center gap-1.5">
          <span className="rounded-btn border border-line px-2 py-1 text-xs" style={{ background: 'var(--surface)' }}>{s}</span>
          {i < steps.length - 1 && <span className="text-hint">→</span>}
        </span>
      ))}
    </div>
  )
}

// Full dedicated guide page (covers the whole screen; "← Back" returns you).
export default function GuidePanel({ onClose, onGo }) {
  const { on, setOn } = useHints()
  const [open, setOpen] = useState(null) // index of the expanded "How it works"

  useEffect(() => {
    const h = (e) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose])

  return (
    <div className="fixed inset-0 z-[70] overflow-y-auto bg-page text-ink">
      <header className="sticky top-0 z-10 border-b border-line backdrop-blur" style={{ background: 'color-mix(in oklab, var(--page) 88%, transparent)' }}>
        <div className="mx-auto flex max-w-3xl items-center justify-between gap-3 px-6 py-4">
          <div className="flex items-center gap-3">
            <button onClick={onClose} className="btn btn-ghost px-2.5 py-1 text-sm">← Back</button>
            <h1 className="font-reading text-xl font-medium">How to use this app</h1>
          </div>
          <label className="flex items-center gap-2 text-sm text-muted">
            <span className="hidden sm:inline">Show hints</span>
            <input type="checkbox" checked={on} onChange={(e) => setOn(e.target.checked)} style={{ accentColor: 'var(--accent)' }} />
          </label>
        </div>
      </header>

      <main className="mx-auto max-w-3xl space-y-10 px-6 py-8 text-sm">
        {/* Quick start */}
        <section>
          <h2 className="mb-3 text-base font-medium">Quick start</h2>
          <ol className="space-y-2">
            {QUICK_START.map((t, i) => (
              <li key={i} className="flex gap-3">
                <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-sm font-semibold" style={{ background: 'var(--accent)', color: 'var(--accent-ink)' }}>{i + 1}</span>
                <span className="pt-0.5">{t}</span>
              </li>
            ))}
          </ol>
        </section>

        {/* Step by step */}
        <section>
          <h2 className="mb-3 text-base font-medium">Step by step</h2>
          <div className="space-y-3">
            {STEPS.map((s, i) => (
              <div key={i} className="card p-4">
                <div className="flex items-start gap-3">
                  <span className="text-2xl leading-none">{s.icon}</span>
                  <div className="min-w-0 flex-1">
                    <div className="font-medium">{i + 1}. {s.title}</div>
                    <div className="mt-1 text-muted">{s.instruction}</div>
                    {s.flow && <div className="mt-3">{<Flow steps={s.flow} />}</div>}
                    <div className="mt-3 flex flex-wrap gap-2">
                      <button onClick={() => setOpen(open === i ? null : i)} className="btn btn-ghost px-3 py-1.5 text-xs">
                        {open === i ? 'Hide' : 'How it works'}
                      </button>
                      {s.go && onGo && (
                        <button onClick={() => onGo(s.go)} className="btn btn-ghost px-3 py-1.5 text-xs">{s.goLabel || 'Take me there'} →</button>
                      )}
                    </div>
                    {open === i && (
                      <ul className="mt-3 list-disc space-y-1.5 pl-5 text-muted">
                        {s.how.map((h, j) => <li key={j}>{h}</li>)}
                      </ul>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </section>

        {/* Legend + Terms side by side on wide screens */}
        <div className="grid gap-10 sm:grid-cols-2">
          <section>
            <h2 className="mb-3 text-base font-medium">What the tags mean</h2>
            <ul className="space-y-2">
              {LEGEND.map((l) => (
                <li key={l.label} className="flex items-start gap-2">
                  <span className={`pill ${l.cls} shrink-0`}>{l.label}</span>
                  <span className="text-muted">{l.meaning}</span>
                </li>
              ))}
            </ul>
          </section>

          <section>
            <h2 className="mb-3 text-base font-medium">Word meanings</h2>
            <dl className="space-y-1.5">
              {TERMS.map(([t, m]) => (
                <div key={t}><dt className="inline font-medium">{t}: </dt><dd className="inline text-muted">{m}</dd></div>
              ))}
            </dl>
          </section>
        </div>

        {/* Troubleshooting */}
        <section>
          <h2 className="mb-3 text-base font-medium">If something goes wrong</h2>
          <div className="space-y-2">
            {FAQ.map(([q, a]) => (
              <div key={q} className="card p-4">
                <div className="font-medium">{q}</div>
                <div className="mt-1 text-muted">{a}</div>
              </div>
            ))}
          </div>
        </section>

        {/* Cost note */}
        <section className="rounded-card border border-line p-4" style={{ background: 'var(--b-queued-bg)', color: 'var(--b-queued-tx)' }}>
          {COST_NOTE}
        </section>

        <div className="border-t border-line pt-6 text-center">
          <button onClick={onClose} className="btn btn-primary px-6 py-2.5">Got it — back to the app</button>
        </div>
      </main>
    </div>
  )
}
