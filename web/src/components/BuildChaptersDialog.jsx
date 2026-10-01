import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { useConfirm } from '../confirm'
import { useToast } from '../toast'
import { Modal, SkeletonRows } from './ui'
import { boxName, boxTitle } from '../pageBoxes'

// Each box becomes one chapter, in box order. The dialog shows the server's own preview
// of a build before anything is written, because the old dialog built blind: a page
// still marked "needs check" was silently left out of the middle of its chapter, and
// the warnings that said so came back in the response and were never shown.

// Statuses whose English was made from the text a refresh is about to replace.
const TRANSLATED = new Set(['translated', 'validated', 'needs-review'])

// The server's refusal to start over past translated chapters without a second yes.
const RENUMBERS = /already translated|renumber/i

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`

// A yes to a Start over whose dialog closed under its confirm isn't acted on (the novel on
// screen may be another one), and says so: dropped without a word, the reader believed
// their novel had been started over.
const NOT_RUN = 'Start over wasn’t run: its dialog had closed. Open Build chapters to try again.'

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
  const said = parts.join(' · ') || 'Chapters built'
  // It opens a sentence, in a toast or under "Chapters built": "chapter 3 updated." didn't.
  return said.charAt(0).toUpperCase() + said.slice(1)
}

/** The renumbering a Start over preview warns of, said before the confirm that asks about it. */
function renumbered(refusal) {
  const n = Number(/renumber (\d+)/i.exec(refusal)?.[1])
  return `${n ? plural(n, 'chapter') : 'Chapters'} you’ve already translated would be renumbered. `
    + 'You’ll be asked once more before that happens.'
}

// The boxes waiting only because an earlier one isn't ready, said once per box they wait
// for — box N has to be chapter N, so nothing builds past a box that waits.
function waitingBehind(waiting) {
  const behind = new Map()
  for (const w of waiting.filter((x) => x.blocked_by != null)) {
    behind.set(w.blocked_by, [...(behind.get(w.blocked_by) || []), w.number])
  }
  return [...behind.entries()].map(([first, ns]) => ({
    first,
    text: `Chapter${ns.length === 1 ? '' : 's'} ${numbers(ns)} ${ns.length === 1 ? 'waits' : 'wait'} for `
      + `chapter ${first}, so the numbering stays in order.`,
  }))
}

export default function BuildChaptersDialog({
  pid, boxes, chapters, whenWritten, onClose, onBuilt, onWorking, onNotice, showError,
}) {
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

  // The dialog closes whenever it's asked to, and a build carries on without it. It used
  // to stay open until the build answered, so its warnings were seen, and a request that
  // hung — a phone changing networks mid-build — left it on "Building…" with no way out
  // but reloading the app. Closed first, the build is reported by the Pages tab instead
  // (see report), which also keeps a second build from starting meanwhile (onWorking).
  const open = useRef(false)
  useEffect(() => {
    open.current = true
    return () => { open.current = false }
  }, [])
  const work = (on) => { setWorking(on); onWorking?.(on) }

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
  // A waiting box is found by its id and called what the Pages tab calls it. Start over
  // numbers every box again from 1, so its numbers are the ones the boxes WOULD get, and
  // looking a box up by one named a different box — a built chapter with nothing wrong.
  const named = (w) => {
    const box = boxes.find((b) => b.id === w.batch)
    if (!box) return w.number != null ? titled(w.number, w.title) : w.title
    const t = boxTitle(box)
    const name = t ? `${boxName(box)} · ${t}` : boxName(box)
    return w.number != null && w.number !== box.number
      ? `${name} (would become chapter ${w.number})` : name
  }
  const retranslate = (n) => {
    const row = chapters.find((c) => c.index === n)
    return row && (row.source_changed || TRANSLATED.has(row.status))
  }

  // A build answered after the dialog was closed: the toast it would have closed with, and
  // a notice on the Pages tab with every warning and every chapter still waiting, which
  // went to a dialog no longer on screen. The notice waits on its own novel's tab; the toast
  // only shows while that novel is on screen, where "1 chapter added" means what it says.
  function report(res, here) {
    if (here) toast(summarise(res))
    const own = (res.waiting || []).filter((w) => w.blocked_by == null)
      .map((w) => `${named(w)}: ${(w.reasons || []).join(' ')}`)
    const lines = [...own, ...waitingBehind(res.waiting || []).map((b) => b.text), ...(res.warnings || [])]
    if (lines.length) onNotice?.(`Chapters built: ${summarise(res)}.\n${lines.map((line) => `· ${line}`).join('\n')}`)
  }

  async function build({ append = true, force = false } = {}) {
    work(true)
    try {
      await whenWritten?.()
      const res = await api.buildChapters(pid, { include, append, force })
      const here = onBuilt(res, pid) !== false
      if (!open.current) report(res, here)
      // Closing with a toast is only honest when there is nothing else to say.
      else if (!res.warnings?.length && !res.waiting?.length) {
        toast(summarise(res))
        onClose()
      } else setResult(res)
    } catch (e) {
      // Starting over when chapters are already translated is refused until confirmed
      // a second time — offer that rather than just reporting the wall. Not once the
      // dialog is closed: a confirm out of nowhere would start over a novel the reader
      // has left; the refusal says what to press when it's opened again.
      if (open.current && !append && !force && e.status === 400 && RENUMBERS.test(e.message || '')) {
        const again = await confirm({
          title: 'Translated chapters would change',
          body: `${e.message}\n\nStart over anyway?`,
          confirmLabel: 'Start over anyway',
          danger: true,
        })
        // Asked again after the answer: the confirm outlives this dialog, and answered once
        // the tab had moved to another novel, it renumbered a novel no longer on screen.
        if (again && open.current) return await build({ append: false, force: true })
        if (again) toast(NOT_RUN)
      } else showError(e, { context: 'building the chapters' })
    } finally {
      work(false)
    }
    return null
  }

  async function startOver() {
    work(true)
    let preview
    try {
      await whenWritten?.()
      preview = await api.buildChapters(pid, { include, append: false, preview: true })
    } catch (e) {
      if (open.current) showError(e, { context: 'checking what Start over would do' })
      return
    } finally {
      work(false)
    }
    // Closed while it looked: asked now, the reader would be starting over a novel they
    // had stopped asking about.
    if (!open.current) return
    // Start over builds every box or none, so a box that isn't ready refuses it all. Only
    // those are listed: a box waiting just because one before it isn't ready is ready.
    if (preview.waiting?.length) {
      setNotReady(preview.waiting.filter((w) => w.blocked_by == null))
      return
    }
    setNotReady(null)
    const pile = boxes.some((b) => b.legacy)
    // The preview's warnings are said here, before the yes: which translated chapters it
    // would renumber, a heading inside a box, a join it isn't sure of. This confirm used
    // to leave them out, and the first word of translations came after agreeing. The
    // renumbering is said in this dialog's own words: the server's sends the reader to
    // "Build" or "Start over anyway", neither of them here, and the confirm after this one
    // says it again, with the button it names.
    const warnings = (preview.warnings || []).map((w) => (RENUMBERS.test(w) ? renumbered(w) : w))
    const ok = await confirm({
      title: 'Start over?',
      body: `Every box is built again as a chapter, numbered from 1 in box order: `
        + `${plural(preview.chapters, 'chapter')}. Empty boxes are removed.`
        + (pile ? '\n\nThe photos built before chapter boxes become ONE chapter.' : '')
        + '\n\nA chapter whose number or text changes will need translating again.'
        + (warnings.length ? `\n\n${warnings.map((w) => `· ${w}`).join('\n')}` : ''),
      confirmLabel: 'Start over',
      danger: true,
    })
    // The same after this one: it stays up when the dialog closes under it.
    if (ok && open.current) await build({ append: false })
    else if (ok) toast(NOT_RUN)
  }

  if (result) {
    return (
      <Modal onClose={onClose}>
        <div className="p-5">
          <h2 className="font-reading text-lg font-medium">Chapters built</h2>
          <p className="mt-1 text-sm">{summarise(result)}.</p>
          <Waiting waiting={result.waiting || []} named={named} />
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
            <Waiting waiting={plan.waiting || []} named={named} />
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
          <button
            className="btn btn-quiet px-3 py-1.5 text-sm" onClick={onClose}
            title={working ? 'The build carries on, and the Pages tab says how it went.' : undefined}
          >
            {working ? 'Close' : 'Cancel'}
          </button>
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
                  <li key={w.batch}>· {named(w)}: {(w.reasons || []).join(' ')}</li>
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

// A box waits either for its own reasons, or only because an earlier box isn't ready.
function Waiting({ waiting, named }) {
  if (!waiting.length) return null
  const own = waiting.filter((w) => w.blocked_by == null)
  return (
    <div>
      <h3 className="text-xs font-medium text-muted">Waiting</h3>
      <ul className="mt-1 space-y-1">
        {own.map((w) => (
          <li key={w.batch}>
            · {named(w)}
            {(w.reasons || []).map((r, i) => <span key={i} className="block pl-3 text-xs text-hint">{r}</span>)}
          </li>
        ))}
        {waitingBehind(waiting).map(({ first, text }) => (
          <li key={`after-${first}`} className="text-xs text-hint">· {text}</li>
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
