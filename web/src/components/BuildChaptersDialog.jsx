import { useEffect, useState } from 'react'
import { api } from '../api'
import { useConfirm } from '../confirm'
import { useToast } from '../toast'
import { Modal, SkeletonRows } from './ui'
import { boxTitle } from '../pageBoxes'

// Each box becomes one chapter, in box order. The dialog shows the server's own preview
// of a build before anything is written, because the old dialog built blind: a page
// still marked "needs check" was silently left out of the middle of its chapter, and
// the warnings that said so came back in the response and were never shown.

// Statuses whose English was made from the text a refresh is about to replace.
const TRANSLATED = new Set(['translated', 'validated', 'needs-review'])

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`

/** "3", "3 and 5", "7–9" — chapter numbers as a reader would say them. */
function numbers(list) {
  const ns = [...list].sort((a, b) => a - b)
  if (ns.length > 2 && ns.every((n, i) => i === 0 || n === ns[i - 1] + 1)) return `${ns[0]}–${ns.at(-1)}`
  return ns.length <= 2 ? ns.join(' and ') : `${ns.slice(0, -1).join(', ')} and ${ns.at(-1)}`
}

function summarise(res) {
  const parts = []
  if (res.added?.length) parts.push(`${plural(res.added.length, 'chapter')} added`)
  const changed = [...(res.refreshed || []), ...(res.updated || [])]
  if (changed.length) parts.push(`chapter${changed.length === 1 ? '' : 's'} ${numbers(changed)} updated`)
  return parts.join(' · ') || 'Chapters built'
}

export default function BuildChaptersDialog({ pid, boxes, chapters, whenWritten, onClose, onBuilt, showError }) {
  const confirm = useConfirm()
  const toast = useToast()
  // Reset to the safe choice every time the dialog opens: building with photos still to
  // check is a decision for this build, not a setting to inherit.
  const [include, setInclude] = useState('approved')
  const [plan, setPlan] = useState(null)
  const [planError, setPlanError] = useState(null)
  const [attempt, setAttempt] = useState(0)
  const [working, setWorking] = useState(false)
  const [result, setResult] = useState(null)
  const [notReady, setNotReady] = useState(null)   // why Start over can't run yet

  useEffect(() => {
    let alive = true
    setPlan(null)
    setPlanError(null)
    ;(async () => {
      try {
        // Edits still on their way would make the preview describe the photos as they
        // were a moment ago.
        await whenWritten?.()
        const res = await api.buildChapters(pid, { include, append: true, preview: true })
        if (alive) setPlan(res)
      } catch (e) {
        if (alive) setPlanError(e)
      }
    })()
    return () => { alive = false }
  }, [pid, include, attempt, whenWritten])

  const titled = (n, title = '') => {
    const box = boxes.find((b) => !b.legacy && b.number === n)
    const t = title && title !== `Chapter ${n}` ? title : box ? boxTitle(box) : ''
    return t ? `Chapter ${n} · ${t}` : `Chapter ${n}`
  }
  const retranslate = (n) => {
    const row = chapters.find((c) => c.index === n)
    return row && (row.source_changed || TRANSLATED.has(row.status))
  }

  async function build({ append = true, force = false } = {}) {
    setWorking(true)
    try {
      await whenWritten?.()
      const res = await api.buildChapters(pid, { include, append, force })
      onBuilt(res)
      // Closing with a toast is only honest when there is nothing else to say.
      if (!res.warnings?.length && !res.waiting?.length) {
        toast(summarise(res))
        onClose()
      } else setResult(res)
    } catch (e) {
      // Starting over when chapters are already translated is refused until confirmed
      // a second time — offer that rather than just reporting the wall.
      if (!append && !force && e.status === 400 && /already translated|renumber/i.test(e.message || '')) {
        const again = await confirm({
          title: 'Translated chapters would change',
          body: `${e.message}\n\nStart over anyway?`,
          confirmLabel: 'Start over anyway',
          danger: true,
        })
        if (again) return await build({ append: false, force: true })
      } else showError(e, { context: 'building the chapters' })
    } finally {
      setWorking(false)
    }
    return null
  }

  async function startOver() {
    setWorking(true)
    let preview
    try {
      await whenWritten?.()
      preview = await api.buildChapters(pid, { include, append: false, preview: true })
    } catch (e) {
      showError(e, { context: 'checking what Start over would do' })
      return
    } finally {
      setWorking(false)
    }
    // Start over builds every box or none, so a box that isn't ready refuses it all.
    if (preview.waiting?.length) { setNotReady(preview.waiting); return }
    setNotReady(null)
    const pile = boxes.some((b) => b.legacy)
    const ok = await confirm({
      title: 'Start over?',
      body: `Every box is built again as a chapter, numbered from 1 in box order: `
        + `${plural(preview.chapters, 'chapter')}. Empty boxes are removed.`
        + (pile ? '\n\nThe photos built before chapter boxes become ONE chapter.' : '')
        + '\n\nA chapter whose number or text changes will need translating again.',
      confirmLabel: 'Start over',
      danger: true,
    })
    if (ok) await build({ append: false })
  }

  if (result) {
    return (
      <Modal onClose={onClose}>
        <div className="p-5">
          <h2 className="font-reading text-lg font-medium">Chapters built</h2>
          <p className="mt-1 text-sm">{summarise(result)}.</p>
          <Waiting waiting={result.waiting || []} titled={titled} />
          <Warnings warnings={result.warnings || []} />
          <div className="mt-5 flex justify-end">
            <button className="btn btn-primary px-3 py-1.5 text-sm" onClick={onClose}>Done</button>
          </div>
        </div>
      </Modal>
    )
  }

  const added = plan?.added || []
  const refreshed = plan?.refreshed || []
  const updated = plan?.updated || []
  const canBuild = added.length + refreshed.length + updated.length > 0
  const unchanged = boxes.filter((b) => b.state === 'built' && !b.legacy).length

  return (
    <Modal onClose={onClose}>
      <div className="p-5">
        <h2 className="font-reading text-lg font-medium">Build chapters</h2>
        <p className="mt-1 text-sm text-hint">
          Each chapter box becomes one chapter, in box order. Nothing about the photos
          changes, so you can always build again.
        </p>

        {planError ? (
          <div className="mt-4 rounded-card px-3 py-2 text-sm pill-review">
            {planError.message || String(planError)}
            <button className="ml-2 underline hover:no-underline" onClick={() => setAttempt((n) => n + 1)}>
              Try again
            </button>
          </div>
        ) : !plan ? (
          <SkeletonRows rows={3} />
        ) : (
          <div className="mt-4 space-y-3 text-sm">
            {added.length > 0 && (
              <PlanList title="Will be added" items={added.map((n) => titled(n))} />
            )}
            {(refreshed.length > 0 || updated.length > 0) && (
              <PlanList
                title="Will be updated"
                items={[
                  ...refreshed.map((n) => `${titled(n)} — its text changed since it was built`
                    + (retranslate(n) ? '. Its English was made from the old text, so translate it again afterwards.' : '')),
                  ...updated.map((n) => `${titled(n)} — its title or photo list changed; the text is the same`),
                ]}
              />
            )}
            <Waiting waiting={plan.waiting || []} titled={titled} />
            {unchanged > 0 && (
              <p className="text-xs text-hint">
                {unchanged === 1 ? '1 built chapter is' : `${unchanged} built chapters are`} up to date.
              </p>
            )}
            {plan.unworked_seams > 0 && (
              <p className="rounded-card px-2 py-1.5 text-xs pill-queued">
                {plural(plan.unworked_seams, 'join')} between photos {plan.unworked_seams === 1 ? 'hasn’t' : 'haven’t'} been
                worked out yet. A build treats an unclear one as a new paragraph — press
                “Work out the joins” first to have them checked.
              </p>
            )}
            <Warnings warnings={plan.warnings || []} />
          </div>
        )}

        <label className="mt-4 flex items-center gap-2 text-sm">
          <input
            type="checkbox" checked={include === 'all'}
            onChange={(e) => setInclude(e.target.checked ? 'all' : 'approved')}
          />
          Also use photos marked “Needs a check”
        </label>

        <div className="mt-5 flex flex-wrap items-center justify-end gap-2">
          {plan && !canBuild && (
            <span className="mr-auto text-xs text-hint">
              {plan.waiting?.length ? 'Nothing is ready to build yet.' : 'Every chapter is already up to date.'}
            </span>
          )}
          <button className="btn btn-quiet px-3 py-1.5 text-sm" onClick={onClose}>Cancel</button>
          <button
            className="btn btn-primary px-3 py-1.5 text-sm"
            disabled={working || !canBuild} onClick={() => build()}
          >
            {working ? 'Building…' : 'Build'}
          </button>
        </div>

        <details className="mt-5 rounded-card border border-line px-3 py-2">
          <summary className="cursor-pointer text-sm text-muted">Start over…</summary>
          <p className="mt-2 text-xs text-hint">
            Builds every chapter again from its box, numbered from 1 in box order. It’s
            the way to put a new chapter in front of one that’s already built.
            {boxes.some((b) => b.legacy) && ' The photos built before chapter boxes become one chapter.'}
          </p>
          {notReady && (
            <div className="mt-2 rounded-card px-2 py-1.5 text-xs pill-review">
              Start over needs every box with photos to be ready first.
              <ul className="mt-1 space-y-0.5">
                {notReady.map((w) => (
                  <li key={w.batch}>· {w.number != null ? titled(w.number, w.title) : w.title}: {(w.reasons || []).join(' ')}</li>
                ))}
              </ul>
            </div>
          )}
          <button className="btn btn-ghost mt-2 px-3 py-1.5 text-xs" disabled={working} onClick={startOver}>
            Start over
          </button>
        </details>
      </div>
    </Modal>
  )
}

function PlanList({ title, items }) {
  return (
    <div>
      <h3 className="text-xs font-medium text-muted">{title}</h3>
      <ul className="mt-1 space-y-0.5">
        {items.map((item, i) => <li key={i}>· {item}</li>)}
      </ul>
    </div>
  )
}

// A box waits either for its own reasons, or only because an earlier box isn't ready —
// box N has to be chapter N, so nothing builds past a box that waits.
function Waiting({ waiting, titled }) {
  if (!waiting.length) return null
  const own = waiting.filter((w) => w.blocked_by == null)
  const behind = new Map()
  for (const w of waiting.filter((x) => x.blocked_by != null)) {
    behind.set(w.blocked_by, [...(behind.get(w.blocked_by) || []), w.number])
  }
  return (
    <div>
      <h3 className="text-xs font-medium text-muted">Waiting</h3>
      <ul className="mt-1 space-y-1">
        {own.map((w) => (
          <li key={w.batch}>
            · {w.number != null ? titled(w.number, w.title) : w.title}
            {(w.reasons || []).map((r, i) => <span key={i} className="block pl-3 text-xs text-hint">{r}</span>)}
          </li>
        ))}
        {[...behind.entries()].map(([first, ns]) => (
          <li key={`after-${first}`} className="text-xs text-hint">
            · Chapter{ns.length === 1 ? '' : 's'} {numbers(ns)} {ns.length === 1 ? 'waits' : 'wait'} for
            chapter {first}, so the numbering stays in order.
          </li>
        ))}
      </ul>
    </div>
  )
}

function Warnings({ warnings }) {
  if (!warnings.length) return null
  return (
    <ul className="space-y-1">
      {warnings.map((w, i) => (
        <li key={i} className="rounded-card px-2 py-1.5 text-xs pill-review">{w}</li>
      ))}
    </ul>
  )
}
