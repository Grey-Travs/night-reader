import { useEffect, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { ProgressBar, StatCard } from '../components/ui'
import TranslationConsole from '../components/TranslationConsole'
import { useConfirm } from '../confirm'
import { cacheHitRate, fmtCost, fmtPercent, fmtTokens } from '../format'

// What the worker is doing to the chapter in flight. A repair rides the same queue as a
// translation, so this bar has to name the operation or an AI resolve and a pronoun fix
// both read as "Translating". Mirrors TASK_LABEL in server/app.py.
const TASK_LABEL = { translate: 'Translating', resolve: 'AI resolve on', pronouns: 'Fixing pronouns in' }
const TASK_NOUN = { translate: 'translating', resolve: 'the AI resolve on', pronouns: 'the pronoun fix on' }

// Live countdown to a target epoch-seconds timestamp, e.g. "in 42:10".
function Countdown({ until }) {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [])
  const left = Math.max(0, Math.floor(until * 1000 - now) / 1000)
  if (left <= 0) return <>any moment now…</>
  const h = Math.floor(left / 3600), m = Math.floor((left % 3600) / 60), s = Math.floor(left % 60)
  const pad = (n) => String(n).padStart(2, '0')
  return <>in {h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`}</>
}

// Per-novel monitoring: the live translation console, progress, pause/auto-resume
// state and the headline stats. The job state itself lives in ProjectLayout, so this
// page keeps showing live updates even though it isn't the one holding the stream.
export default function ProjectActivityPage() {
  const {
    data, counts, koreanTotal, done,
    running, submitting, log, live, totals, paused, waiting, queue, totalQueued,
    cancelQueue, stopAll, resumeJob,
  } = useOutletContext()
  const confirm = useConfirm()

  async function stop() {
    // Only worth a confirm when a chapter is genuinely mid-flight — that's the case
    // where stopping throws away work already paid for.
    if (queue.current != null) {
      const ok = await confirm({
        title: `Stop ${TASK_NOUN[queue.kind] || TASK_NOUN.translate} chapter ${queue.current}?`,
        body: `It stops as soon as Claude sends its next update, usually within a second or two.\n\n`
          + `The chapter won't be marked failed and nothing already saved is overwritten — but the `
          + `work done on it so far is discarded, and re-running it spends your plan allowance again.`
          + (queue.pending.length ? `\n\nThe ${queue.pending.length} chapter(s) still queued are dropped too.` : ''),
        confirmLabel: 'Stop it',
      })
      if (!ok) return
    }
    stopAll()
  }

  const tok = totals?.tokens || {}
  const usedTokens = (Number(tok.input_tokens) || 0) + (Number(tok.output_tokens) || 0)
  const hitRate = cacheHitRate(tok)

  return (
    <div className="page">
      {/* Queue bar */}
      {totalQueued > 0 ? (
        <div className="mb-5 flex flex-wrap items-center justify-between gap-2 rounded-card border border-line p-3 text-sm" style={{ background: 'var(--b-translating-bg)', color: 'var(--b-translating-tx)' }}>
          <span className="flex items-center gap-2">
            <span className="inline-block h-2 w-2 rounded-full animate-pulse" style={{ background: 'var(--accent)' }} />
            {waiting ? 'Waiting for Claude to refresh'
              : queue.current != null
                ? <>{TASK_LABEL[queue.kind] || TASK_LABEL.translate} <strong>chapter {queue.current}</strong></>
                : 'Queued'}
            {queue.pending.length > 0 && ` · ${queue.pending.length} waiting in queue`}
          </span>
          {/* Stop must stay reachable while ONE chapter is running — gating this on a
              non-empty backlog used to hide the only control at exactly that moment. */}
          <span className="flex shrink-0 items-center gap-2">
            {queue.pending.length > 0 && (
              <button onClick={() => cancelQueue()} className="btn btn-ghost px-3 py-1 text-xs" title="Drop the queued chapters; let the running one finish">
                Clear queue
              </button>
            )}
            {(queue.current != null || queue.pending.length > 0) && (
              <button onClick={stop} className="btn btn-ghost px-3 py-1 text-xs" title="Stop the chapter being translated right now">
                ■ Stop
              </button>
            )}
          </span>
        </div>
      ) : !running && (
        <div className="mb-5 rounded-card border border-line p-4 text-sm text-muted" style={{ background: 'var(--surface)' }}>
          Nothing running right now. Start a translation from the <strong>Chapters</strong> tab, or run an
          {' '}<strong>AI resolve</strong> or <strong>Fix pronouns</strong> on a flagged chapter — whichever it
          is, progress shows here live.
        </div>
      )}

      {waiting && (
        <div className="mb-5 flex flex-col gap-2 rounded-card border border-line p-3 text-sm sm:flex-row sm:items-center sm:justify-between" style={{ background: 'var(--b-queued-bg)', color: 'var(--b-queued-tx)' }}>
          <span className="flex items-center gap-2">
            <span className="inline-block h-2 w-2 shrink-0 rounded-full animate-pulse" style={{ background: 'var(--accent)' }} />
            <span>
              Waiting for Claude to refresh — resuming automatically{' '}
              {waiting.resume_at ? <>around <strong>{new Date(waiting.resume_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</strong> (<Countdown until={waiting.resume_at} />)</> : 'soon'}
              . Progress is saved — you can close this tab.
            </span>
          </span>
          <button onClick={() => resumeJob()} disabled={submitting} className="btn btn-ghost shrink-0 px-3 py-1.5 text-xs">Resume now</button>
        </div>
      )}

      {paused && (
        <div className="mb-5 flex flex-col gap-2 rounded-card border border-line p-3 text-sm sm:flex-row sm:items-center sm:justify-between" style={{ background: 'var(--b-queued-bg)', color: 'var(--b-queued-tx)' }}>
          <span>
            Your plan's limit was reached — progress is saved{paused.pending?.length ? ` (${paused.pending.length} left to do)` : ''}.
            {paused.resets_at ? ` Picks back up automatically around ${new Date(paused.resets_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}.` : ' Resume when your plan resets.'}
          </span>
          <button onClick={() => resumeJob()} disabled={running} className="btn btn-ghost shrink-0 px-3 py-1.5 text-xs">Resume now</button>
        </div>
      )}

      {/* Live console: the Korean the model was given, and the English it streams back */}
      {(running || log.length > 0) && (
        <TranslationConsole live={live} log={log} running={running} totals={totals} />
      )}

      {/* Progress */}
      <section className="mb-6">
        <div className="mb-2 flex items-end justify-between">
          <h2 className="text-sm font-medium text-muted">Korean chapters translated</h2>
          <span className="text-sm tabular-nums text-hint">{done} / {koreanTotal}</span>
        </div>
        <ProgressBar value={done} total={koreanTotal} />
      </section>

      {/* Stat cards */}
      <section className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
        <StatCard label="Total tabs" value={data?.total ?? '—'} />
        <StatCard label="Translated" value={counts.validated || 0} />
        <StatCard label="To translate" value={counts.pending || 0} />
        <StatCard label="Needs review" value={counts['needs-review'] || 0} />
        <StatCard label="Already English" value={counts['english-source'] || 0} />
        <StatCard label="Plan usage" value={fmtCost(totals?.cost_usd ?? 0)} sub="equivalent" />
      </section>

      {/* Usage detail. Every one of these numbers was already being recorded per chapter
          in state.json and thrown away — only the dollar figure was ever shown. */}
      {usedTokens > 0 && (
        <section className="mt-4 grid grid-cols-2 gap-4 sm:grid-cols-4">
          <StatCard label="Tokens in" value={fmtTokens(tok.input_tokens)} sub="sent to Claude" />
          <StatCard label="Tokens out" value={fmtTokens(tok.output_tokens)} sub="translated text" />
          <StatCard label="Cached" value={fmtTokens(tok.cache_read_input_tokens)} sub="reused, not re-sent" />
          <StatCard
            label="Cache hit rate"
            value={fmtPercent(hitRate)}
            sub={hitRate != null && hitRate < 0.5
              ? 'low — the glossary is being re-sent'
              : 'higher is cheaper'}
          />
        </section>
      )}
    </div>
  )
}
