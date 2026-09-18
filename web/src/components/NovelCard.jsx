import { useState } from 'react'
import { ProgressBar } from './ui'
import { fmtCost } from '../format'
import { getLastRead } from '../prefs'
import { STATUS_PILL, STATUS_LABEL, STATUSES } from '../status'

// One novel in a grid. Shared by the Library and the Archive — `archived` only flips
// the move button's label (Archive ↔ Restore).
export default function NovelCard({ p, archived = false, onOpen, onToggleArchive, onRemove, onRetranslate, onSetStatus }) {
  const total = p.chapter_count
  const cont = getLastRead(p.id)
  const [menu, setMenu] = useState(false)
  // A novel read back off the publishing site rather than translated here. Worth saying on
  // the card, because such a novel genuinely behaves differently and all three differences
  // read as faults unless something explains them: its chapters cannot be translated (the
  // source is already English, so the queue skips them), Refresh has no document to fetch,
  // and the reader shows the same text as source and translation.
  const imported = !!p.imported_from
  const status = p.status || 'ongoing'
  // No pill for "ongoing". It is the default and it is what most of a shelf of fifty-odd
  // novels is, so badging it would put a label on nearly every card and make the two that
  // mean something harder to spot. Absence says ongoing; the menu says it out loud.
  const showStatus = status !== 'ongoing'

  return (
    <div className="card p-5">
      <div className="flex items-start justify-between gap-3">
        <button onClick={() => onOpen(p.id)} className="text-left font-reading text-lg font-medium leading-snug hover:text-accent-text hover:underline">{p.name}</button>
        <button onClick={() => onRemove(p.id, p.name)} className="tap -m-2 shrink-0 p-2 text-hint hover:text-danger" title="Remove from library">✕</button>
      </div>
      {(imported || showStatus) && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {showStatus && (
            <span
              className={`pill ${STATUS_PILL[status]}`}
              title={p.status_from === 'series'
                ? `Set for the whole of “${p.series_name}”`
                : undefined}
            >
              {STATUS_LABEL[status]}
            </span>
          )}
          {imported && (
            <span className="pill pill-muted" title={`Read off ${p.imported_from.site || 'the site'} — these chapters are already published, so they are not translated here`}>
              imported
            </span>
          )}
        </div>
      )}
      <div className="mt-3 text-sm text-muted">
        {/* "tabs" is a Google Docs word. An imported novel has no document, so it has
            chapters and nothing else. */}
        {p.translated} {imported ? 'chapters' : 'translated'}
        {total && !imported ? ` · ${total} tabs` : ''}
        {p.needs_review ? ` · ${p.needs_review} to review` : ''}
        {p.cost_usd > 0 ? ` · ${fmtCost(p.cost_usd)}` : ''}
      </div>
      {total ? <div className="mt-2"><ProgressBar value={p.translated} total={total} /></div> : null}
      <div className="mt-4 flex flex-wrap items-center gap-2">
        <button onClick={() => onOpen(p.id)} className="btn btn-ghost flex-1 px-3 py-2">Open</button>
        {cont != null && <button onClick={() => onOpen(p.id, cont)} className="btn btn-ghost px-3 py-2" title={`Continue chapter ${cont}`}>Continue · Ch {cont}</button>}
        <button onClick={() => onToggleArchive(p)} className="btn btn-ghost px-3 py-2" title={archived ? 'Move back to your library' : 'Move to the archive'}>
          {archived ? '↩ Restore' : '📦 Archive'}
        </button>
        {/* Overflow rather than more buttons — the row is already full at this width. */}
        {(onSetStatus || (onRetranslate && !imported)) && (
          <div className="relative">
            {/* aria-label, not just title: the only text in here is "⋯", which is what a
                screen reader would otherwise announce. */}
            <button onClick={() => setMenu((v) => !v)} className="btn btn-ghost px-2.5 py-2" title="More actions" aria-label="More actions" aria-haspopup="menu" aria-expanded={menu}>⋯</button>
            {menu && (
              <div
                className="absolute right-0 top-full z-20 mt-1 w-56 rounded-card border border-line p-1 text-sm shadow-lg"
                style={{ background: 'var(--elevated)' }}
                onMouseLeave={() => setMenu(false)}
              >
                {onSetStatus && (
                  <>
                    <div className="px-3 pb-1 pt-2 text-xs text-hint">
                      {p.status_from === 'series'
                        ? `Status · all of “${p.series_name}”`
                        : 'Status'}
                    </div>
                    {STATUSES.map((s) => (
                      <button
                        key={s}
                        onClick={() => { setMenu(false); if (s !== status) onSetStatus(p, s) }}
                        className="flex w-full items-center gap-2 rounded-btn px-3 py-2 text-left hover:bg-[color-mix(in_oklab,var(--ink)_6%,transparent)]"
                        aria-current={s === status}
                      >
                        {/* Decorative: aria-current already carries "this is the one",
                            and leaving the tick in the accessible name would make the
                            selected item announce differently from the others. */}
                        <span aria-hidden="true" className="w-3 text-accent-text">{s === status ? '✓' : ''}</span>
                        {STATUS_LABEL[s]}
                      </button>
                    ))}
                  </>
                )}
                {/* Never offered for an imported novel: its chapters are English, the
                    translate queue only accepts Korean, so it would queue zero and look
                    broken. */}
                {onRetranslate && !imported && (
                  <>
                    {onSetStatus && <div className="my-1 border-t border-line" />}
                    <button
                      onClick={() => { setMenu(false); onRetranslate(p) }}
                      className="block w-full rounded-btn px-3 py-2 text-left hover:bg-[color-mix(in_oklab,var(--ink)_6%,transparent)]"
                      title="Translate every Korean chapter again from scratch"
                    >
                      ↻ Re-translate whole novel
                    </button>
                  </>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
