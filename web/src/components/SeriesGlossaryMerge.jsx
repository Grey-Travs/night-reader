import { useState } from 'react'
import { api } from '../api'
import { useToast } from '../toast'
import { useConfirm } from '../confirm'

// Merging a series' per-document glossaries into one.
//
// This is the step that fixes a real bug rather than adding a convenience: while each
// document keeps its own glossary, a character's locked spelling resets when the story
// carries on into the next Google Doc, and can drift from there.
//
// Two deliberate choices in this UI. It always checks before it writes, because this is
// the only action here that changes what FUTURE translations read. And a conflict you
// leave alone still locks the suggested spelling — leaving a contested name unlocked
// until someone clicks would let the next chapter drift worse than before the merge.
export default function SeriesGlossaryMerge({ sid, merged, onMerged }) {
  const toast = useToast()
  const confirm = useConfirm()
  const [plan, setPlan] = useState(null)
  const [picks, setPicks] = useState({})
  const [busy, setBusy] = useState('')
  const [error, setError] = useState(null)
  const [done, setDone] = useState(null)

  async function check() {
    setBusy('check')
    setError(null)
    try {
      const p = await api.mergeSeriesGlossary(sid, {}, true)
      setPlan(p)
      setPicks({})
    } catch (e) {
      setError(e)
    } finally {
      setBusy('')
    }
  }

  async function merge() {
    const undecided = plan.conflicts.filter((c) => !picks[c.key]).length
    const ok = await confirm({
      title: 'Merge into one glossary?',
      body: 'Every document in this series will read and write one shared glossary from '
        + 'now on, so a name cannot drift when the story carries on into the next document.'
        + (undecided
          ? ` ${undecided} disagreement${undecided === 1 ? '' : 's'} you have not answered `
            + 'will take the suggested spelling.'
          : '')
        + ' Each novel keeps its own glossary file untouched, so unlinking the series puts '
        + 'everything back.',
      confirmLabel: 'Merge',
    })
    if (!ok) return
    setBusy('merge')
    try {
      const r = await api.mergeSeriesGlossary(sid, picks, false)
      setDone(r)
      setPlan(null)
      toast(`Merged — ${r.locked} terms locked for the whole series`)
      onMerged?.()
    } catch (e) {
      setError(e)
    } finally {
      setBusy('')
    }
  }

  const KIND_LABEL = {
    english: 'Two spellings for the same name',
    pronoun: 'Same name, different pronoun',
    casing: 'Same name written two ways',
  }

  return (
    <section className="mt-8 rounded-card border border-line p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h2 className="text-sm font-medium">Shared glossary</h2>
          <p className="mt-0.5 text-xs text-hint">
            {merged
              ? 'These documents already share one glossary.'
              : 'Each document still has its own, so a character’s spelling can reset '
                + 'where the story carries on into the next one.'}
          </p>
        </div>
        <button
          className="btn btn-ghost px-3 py-1.5 text-xs"
          disabled={!!busy}
          onClick={check}
        >
          {busy === 'check' ? 'Checking…' : plan ? 'Check again' : 'Check for conflicts'}
        </button>
      </div>

      {error && (
        <div className="mt-3 rounded-card px-3 py-2 text-sm pill-review">
          {error.message || String(error)}
        </div>
      )}

      {done && (
        <div className="mt-3 text-xs text-muted">
          <p>
            {done.locked} terms locked for the whole series
            {done.pending ? `, ${done.pending} suggestions waiting for approval` : ''}.
          </p>
          <p className="mt-1 text-hint">
            Near-duplicates filed under different Korean spellings are kept as they were —
            this merges the glossaries, it does not tidy them.
          </p>
        </div>
      )}

      {plan && (
        <div className="mt-3">
          <p className="text-xs text-muted">
            {plan.members.map((m) => `${m.name}: ${m.entries}`).join(' · ')}
            {' → '}
            {plan.locked} terms
            {plan.pending ? ` · ${plan.pending} suggestions` : ''}
          </p>

          {plan.conflicts.length === 0 ? (
            <p className="mt-2 text-xs text-hint">
              No disagreements — every document spells these the same way. Merging is safe.
            </p>
          ) : (
            <>
              <p className="mt-3 text-xs text-hint">
                {plan.conflicts.length} disagreement{plan.conflicts.length === 1 ? '' : 's'}.
                The suggested spelling is the one that appears in more of your translated
                chapters — the one readers have already seen most.
              </p>
              <div className="mt-2 space-y-2">
                {plan.conflicts.map((c) => (
                  <div key={c.key} className="rounded-card border border-line p-2">
                    <div className="flex flex-wrap items-baseline gap-2">
                      {c.korean && <span className="text-sm font-medium">{c.korean}</span>}
                      <span className="text-xs text-hint">{KIND_LABEL[c.kind] || c.kind}</span>
                    </div>
                    <div className="mt-1.5 space-y-1">
                      {c.candidates.map((cand) => {
                        const chosen = (picks[c.key] || c.suggested) === cand.english
                        return (
                          <label
                            key={`${cand.english}|${cand.pronoun}`}
                            className="flex cursor-pointer items-baseline gap-2 text-xs"
                          >
                            <input
                              type="radio"
                              name={c.key}
                              checked={chosen}
                              onChange={() => setPicks((p) => ({ ...p, [c.key]: cand.english }))}
                            />
                            <span className="font-medium">{cand.english}</span>
                            {cand.pronoun && <span className="text-hint">({cand.pronoun})</span>}
                            <span className="text-hint">
                              in {cand.in_chapters} chapter{cand.in_chapters === 1 ? '' : 's'}
                            </span>
                            <span className="min-w-0 flex-1 truncate text-hint">
                              · {cand.from.join(', ')}
                            </span>
                            {cand.english === c.suggested && (
                              <span className="shrink-0 text-hint">suggested</span>
                            )}
                          </label>
                        )
                      })}
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}

          <div className="mt-3 flex items-center gap-2">
            <button
              className="btn btn-primary px-3 py-1.5 text-xs"
              disabled={!!busy}
              onClick={merge}
            >
              {busy === 'merge' ? 'Merging…' : 'Merge into one glossary'}
            </button>
            <span className="text-xs text-hint">Nothing has been written yet.</span>
          </div>
        </div>
      )}
    </section>
  )
}
