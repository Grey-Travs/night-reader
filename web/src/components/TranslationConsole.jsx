import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { fmtCost, fmtDuration, fmtTokens } from '../format'

// The live translation view: the Korean the model was actually given on the left, the
// English it streams back on the right.
//
// The text here is real — `Translator._aquery` already received it in streamed chunks,
// and the worker forwards those over SSE (see server/app.py `_build_hooks`). Nothing on
// this screen is simulated, so the pace you see is the model's real pace.

// Counts up from an epoch-seconds start, mirroring the Countdown in ProjectActivityPage.
function Elapsed({ since }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [since])
  if (!since) return null
  return <>{fmtDuration((now - since * 1000) / 1000)}</>
}

// Keeps a scroller pinned to the bottom, but lets go the moment the user scrolls up to
// re-read something — nothing is more annoying than being yanked back down mid-sentence.
function useStickyScroll(dep) {
  const ref = useRef(null)
  const stuck = useRef(true)
  const onScroll = () => {
    const el = ref.current
    if (!el) return
    stuck.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24
  }
  useLayoutEffect(() => {
    const el = ref.current
    if (el && stuck.current) el.scrollTop = el.scrollHeight
  }, [dep])
  return { ref, onScroll }
}

const splitParagraphs = (text) => (text || '').split(/\n\s*\n/).filter((p) => p.trim())

export default function TranslationConsole({ live, log, running, totals }) {
  const english = live?.english || ''
  const source = live?.source || []
  const donePara = splitParagraphs(english).length
  const [chunkI, chunkN] = live?.chunk || [1, 1]

  const src = useStickyScroll(donePara)
  const eng = useStickyScroll(english.length)
  const logBox = useStickyScroll(log?.length)

  // Progress from paragraphs, not characters: Korean→English expands unevenly, so a
  // character ratio would race ahead and then stall. Held below 100% until the chapter
  // actually lands so it never claims to be finished early.
  const pct = source.length
    ? Math.min(99, Math.round((donePara / source.length) * 100))
    : null

  const tok = totals?.tokens || {}
  const used = (Number(tok.input_tokens) || 0) + (Number(tok.output_tokens) || 0)

  return (
    <section className="sunken mb-6 overflow-hidden">
      {/* header */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line px-4 py-2.5 text-xs">
        {running && (
          <span className="inline-block h-2 w-2 shrink-0 rounded-full motion-safe:animate-pulse" style={{ background: 'var(--accent)' }} />
        )}
        {live ? (
          <>
            <strong className="text-sm font-medium">ch {live.index}</strong>
            {live.title && <span className="min-w-0 truncate text-muted">{live.title}</span>}
            <span className="text-hint">{(live.chars || 0).toLocaleString()} chars</span>
            {chunkN > 1 && <span className="text-hint">part {chunkI}/{chunkN}</span>}
          </>
        ) : (
          <span className="text-sm font-medium text-muted">{running ? 'Starting…' : 'Last run'}</span>
        )}
        <span className="ml-auto flex items-center gap-3 tabular-nums text-hint">
          {live?.started_at && <Elapsed since={live.started_at} />}
        </span>
      </div>

      {/* the two panes */}
      {live ? (
        <div className="grid gap-px sm:grid-cols-2" style={{ background: 'var(--border)' }}>
          <div style={{ background: 'var(--surface)' }}>
            <div className="px-4 pt-3 text-[11px] font-medium uppercase tracking-wide text-hint">
              한국어 source
            </div>
            <div ref={src.ref} onScroll={src.onScroll} className="max-h-64 overflow-y-auto px-4 py-2">
              {source.length === 0 ? (
                <p className="text-xs text-hint">Loading the chapter…</p>
              ) : (
                source.map((p, i) => (
                  <p
                    key={i}
                    className="border-l-2 py-1 pl-2.5 text-xs leading-relaxed transition-colors"
                    style={{
                      borderColor: i === donePara ? 'var(--accent)' : 'transparent',
                      color: i < donePara ? 'var(--muted)' : 'var(--hint)',
                      opacity: i > donePara + 2 ? 0.5 : 1,
                    }}
                  >
                    {p}
                  </p>
                ))
              )}
            </div>
          </div>

          <div style={{ background: 'var(--surface)' }}>
            <div className="px-4 pt-3 text-[11px] font-medium uppercase tracking-wide text-hint">
              english
            </div>
            <div ref={eng.ref} onScroll={eng.onScroll} className="max-h-64 overflow-y-auto px-4 py-2">
              {english ? (
                splitParagraphs(english).map((p, i) => (
                  <p key={i} className="py-1 text-xs leading-relaxed">{p}</p>
                ))
              ) : (
                <p className="text-xs text-hint">Waiting for the first words…</p>
              )}
              {running && (
                <span className="motion-safe:animate-pulse text-xs" style={{ color: 'var(--accent-text)' }}>▋</span>
              )}
            </div>
          </div>
        </div>
      ) : null}

      {/* footer: progress + spend */}
      {live && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-line px-4 py-2 text-[11px] tabular-nums text-hint">
          <span>{donePara} / {source.length || '?'} ¶</span>
          <div className="h-1 min-w-24 flex-1 overflow-hidden rounded-full" style={{ background: 'var(--border)' }}>
            <div
              className="h-full rounded-full transition-[width] duration-500"
              style={{ width: `${pct ?? 0}%`, background: 'var(--accent)' }}
            />
          </div>
          {pct != null && <span>{pct}%</span>}
          {used > 0 && <span>· {fmtTokens(used)} tok</span>}
          {totals?.cost_usd != null && <span>· {fmtCost(totals.cost_usd)}</span>}
        </div>
      )}

      {/* the event log, unchanged in substance from before */}
      {log?.length > 0 && (
        <div
          ref={logBox.ref}
          onScroll={logBox.onScroll}
          className="max-h-40 overflow-y-auto border-t border-line px-4 py-2.5 font-mono text-[11px] leading-relaxed text-muted"
        >
          {log.map((line, i) => {
            const text = typeof line === 'string' ? line : line.text
            const kind = typeof line === 'string' ? null : line.kind
            const colour =
              kind === 'error' ? 'var(--danger)'
              : kind === 'warn' ? 'var(--b-review-tx)'
              : kind === 'good' ? 'var(--b-translated-tx)'
              : undefined
            return (
              <div key={i} className="row-in" style={colour ? { color: colour } : undefined}>
                {text}
              </div>
            )
          })}
          {running && !live && (
            <div className="motion-safe:animate-pulse" style={{ color: 'var(--accent-text)' }}>▋</div>
          )}
        </div>
      )}
    </section>
  )
}
