import { useCallback, useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { api } from '../api'

// Rewrite one paragraph, keeping every version so you can compare and pick.
//
// A drawer rather than a modal, deliberately: the paragraph stays visible behind it
// (highlighted), so you can see the sentence you're changing while you change it.
// Versions stack full-width rather than sitting side by side — a 68ch reading measure
// cut in half is unreadable at reading size.
//
// Nothing here writes to the chapter. Generating only appends to the paragraph's
// history; only "Use this" changes what you're reading.

const PRESETS = [
  ['More natural', 'make it read more naturally in English'],
  ['Less stiff', 'less stiff and formal, more like spoken English'],
  ['Shorter', 'tighter and shorter, without losing anything'],
  ['Punchier', 'punchier, with more momentum'],
  ['Simpler words', 'plainer, simpler word choices'],
]

const KIND_LABEL = { original: 'Original', retranslate: 'Re-translated', rephrase: 'Rephrased' }

function words(s) {
  return (s || '').trim().split(/\s+/).filter(Boolean).length
}

export default function ParagraphPanel({
  pid, index, paragraph, expectedText, components, onClose, onApplied,
}) {
  const [group, setGroup] = useState(null)
  const [korean, setKorean] = useState(null)
  const [showKorean, setShowKorean] = useState(false)
  const [instruction, setInstruction] = useState('')
  const [busy, setBusy] = useState('')
  const [problem, setProblem] = useState(null)

  const ref = useCallback(() => ({ paragraph, expected_text: expectedText }), [paragraph, expectedText])

  // What Korean this paragraph came from, and whether retranslating is possible here
  // at all (a saved-copy novel, an already-English chapter, or paragraphs that don't
  // line up all rule it out — rephrase still works in every one of those cases).
  useEffect(() => {
    let alive = true
    setGroup(null); setKorean(null); setProblem(null); setInstruction('')
    api.paragraphSource(pid, index, { paragraph, expected_text: expectedText })
      .then((r) => { if (alive) setKorean(r) })
      .catch(() => { if (alive) setKorean({ available: false, reason: null, korean: [] }) })
    api.chapterVariants(pid, index)
      .then((r) => {
        if (!alive) return
        const found = (r.groups || []).find((g) => g.paragraph === paragraph && !g.stale)
        if (found) setGroup(found)
      })
      .catch(() => {})
    return () => { alive = false }
  }, [pid, index, paragraph, expectedText])

  async function generate(mode) {
    setBusy(mode); setProblem(null)
    try {
      const body = { ...ref(), group_id: group?.id || null }
      if (mode === 'rephrase') body.instruction = instruction
      const res = mode === 'retranslate'
        ? await api.retranslateParagraph(pid, index, body)
        : await api.rephraseParagraph(pid, index, body)
      if (!res.ok) {
        setProblem(res.reasons?.join(' · ') || 'That attempt came back unusable.')
        return
      }
      setGroup(res.group)
      if (res.duplicate) setProblem('That came back the same as a version you already have.')
    } catch (e) {
      setProblem(e.message || String(e))
    } finally { setBusy('') }
  }

  async function use(variantId) {
    setBusy('apply'); setProblem(null)
    try {
      const res = await api.applyParagraph(pid, index, { group_id: group.id, variant_id: variantId })
      setGroup(res.group)
      onApplied?.(res)
    } catch (e) {
      setProblem(e.message || String(e))
    } finally { setBusy('') }
  }

  async function discard(variantId) {
    try {
      await api.discardParagraph(pid, index, { group_id: group.id, variant_id: variantId })
      setGroup((g) => g && { ...g, variants: g.variants.filter((v) => v.id !== variantId) })
    } catch (e) { setProblem(e.message || String(e)) }
  }

  const variants = group?.variants || [
    { id: 'v0', kind: 'original', text: expectedText, warnings: [] },
  ]
  const currentId = group?.current_id || 'v0'
  const currentText = (variants.find((v) => v.id === currentId) || variants[0]).text
  const canRetranslate = korean?.available === true
  const working = !!busy

  return (
    <aside
      className="fixed inset-y-0 right-0 z-40 flex w-full max-w-xl flex-col border-l border-line shadow-xl"
      style={{ background: 'var(--elevated)' }}
      role="dialog"
      aria-label="Rewrite this paragraph"
    >
      <header className="flex items-center justify-between gap-2 border-b border-line px-4 py-3">
        <div className="min-w-0">
          <div className="font-ui text-sm font-medium">Rewrite this paragraph</div>
          <div className="text-xs text-hint">
            Paragraph {paragraph + 1}
            {group && variants.length > 1 && ` · ${variants.length - 1} version${variants.length === 2 ? '' : 's'}`}
          </div>
        </div>
        <button onClick={onClose} className="btn btn-ghost px-2 py-1 text-sm" title="Close (Esc)">✕</button>
      </header>

      <div className="flex-1 overflow-y-auto px-4 py-3">
        {/* The Korean this came from, so a retranslation can be judged for fidelity. */}
        {korean?.available && (
          <div className="mb-3">
            <button
              onClick={() => setShowKorean((v) => !v)}
              className="text-xs text-hint underline hover:no-underline"
            >
              {showKorean ? 'Hide the Korean' : 'Show the Korean this came from'}
            </button>
            {showKorean && (
              <div className="korean sunken mt-2 rounded-card p-3 text-sm">
                {korean.korean.map((para, i) => (
                  <p key={i} className={i === korean.focus ? '' : 'opacity-45'}>{para}</p>
                ))}
              </div>
            )}
          </div>
        )}

        {problem && (
          <div className="mb-3 rounded-card px-3 py-2 text-sm pill-review">{problem}</div>
        )}

        <div className="space-y-3">
          {variants.map((variant) => {
            const isCurrent = variant.id === currentId
            const delta = words(variant.text) - words(currentText)
            return (
              <div
                key={variant.id}
                className={`rounded-card border p-3 ${isCurrent ? 'border-line-strong' : 'border-line'}`}
              >
                <div className="mb-2 flex flex-wrap items-center gap-2">
                  <span className="font-ui text-xs font-medium text-muted">
                    {KIND_LABEL[variant.kind] || variant.kind}
                    {variant.instruction ? ` — “${variant.instruction}”` : ''}
                  </span>
                  {isCurrent && <span className="pill pill-translated !px-1.5 !py-0 text-[11px]">In the chapter</span>}
                  {!isCurrent && delta !== 0 && (
                    <span className="text-[11px] text-hint">{delta > 0 ? `+${delta}` : delta} words</span>
                  )}
                </div>

                <div className="reading text-sm" style={{ fontSize: '0.95rem' }}>
                  <ReactMarkdown components={components}>{variant.text || ''}</ReactMarkdown>
                </div>

                {(variant.warnings || []).map((w, i) => (
                  <div key={i} className="mt-2 rounded-card px-2 py-1 text-[11px] pill-review">{w}</div>
                ))}

                <div className="mt-2 flex items-center gap-2">
                  {!isCurrent && (
                    <button
                      className="btn btn-primary px-2.5 py-1 text-xs"
                      disabled={working || !group}
                      onClick={() => use(variant.id)}
                    >
                      {variant.kind === 'original' ? 'Revert to this' : 'Use this'}
                    </button>
                  )}
                  <button
                    className="btn btn-quiet px-2 py-1 text-xs"
                    onClick={() => navigator.clipboard?.writeText(variant.text || '')}
                  >
                    Copy
                  </button>
                  {variant.id !== 'v0' && !isCurrent && group && (
                    <button className="btn btn-ghost px-2 py-1 text-xs" onClick={() => discard(variant.id)}>
                      Discard
                    </button>
                  )}
                  {variant.cost_usd > 0 && (
                    <span className="ml-auto text-[11px] text-hint">${variant.cost_usd.toFixed(4)}</span>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      </div>

      <footer className="border-t border-line px-4 py-3">
        <div className="mb-2 flex flex-wrap gap-1">
          {PRESETS.map(([label, text]) => (
            <button
              key={label}
              onClick={() => setInstruction(text)}
              className="btn btn-quiet px-2 py-0.5 text-[11px]"
            >
              {label}
            </button>
          ))}
        </div>
        <input
          className="input w-full text-sm"
          placeholder="How should it read? (optional)"
          value={instruction}
          maxLength={500}
          onChange={(e) => setInstruction(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !working) generate('rephrase') }}
        />
        <div className="mt-2 flex items-center gap-2">
          <button
            className="btn btn-primary flex-1 px-3 py-1.5 text-sm"
            disabled={working}
            onClick={() => generate('rephrase')}
          >
            {busy === 'rephrase' ? 'Rewriting…' : 'Rephrase'}
          </button>
          <button
            className="btn btn-quiet flex-1 px-3 py-1.5 text-sm"
            disabled={working || !canRetranslate}
            title={canRetranslate ? 'Translate this paragraph again from the Korean'
              : (korean?.reason || 'The Korean source for this paragraph isn’t available.')}
            onClick={() => generate('retranslate')}
          >
            {busy === 'retranslate' ? 'Translating…' : 'Re-translate'}
          </button>
        </div>
        {korean && !canRetranslate && korean.reason && (
          <p className="mt-2 text-[11px] text-hint">{korean.reason}</p>
        )}
      </footer>
    </aside>
  )
}
