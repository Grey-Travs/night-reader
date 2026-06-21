import { ProgressBar } from './ui'
import { getLastRead } from '../prefs'

// One novel in a grid. Shared by the Library and the Archive — `archived` only flips
// the move button's label (Archive ↔ Restore).
export default function NovelCard({ p, archived = false, onOpen, onToggleArchive, onRemove }) {
  const total = p.chapter_count
  const cont = getLastRead(p.id)
  return (
    <div className="card p-5">
      <div className="flex items-start justify-between gap-3">
        <button onClick={() => onOpen(p.id)} className="text-left font-reading text-lg font-medium leading-snug hover:text-accent-text hover:underline">{p.name}</button>
        <button onClick={() => onRemove(p.id, p.name)} className="tap -m-2 shrink-0 p-2 text-hint hover:text-danger" title="Remove from library">✕</button>
      </div>
      <div className="mt-3 text-sm text-muted">
        {p.translated} translated{total ? ` · ${total} tabs` : ''}
        {p.needs_review ? ` · ${p.needs_review} to review` : ''}
      </div>
      {total ? <div className="mt-2"><ProgressBar value={p.translated} total={total} /></div> : null}
      <div className="mt-4 flex flex-wrap gap-2">
        <button onClick={() => onOpen(p.id)} className="btn btn-ghost flex-1 px-3 py-2">Open</button>
        {cont != null && <button onClick={() => onOpen(p.id, cont)} className="btn btn-ghost px-3 py-2" title={`Continue chapter ${cont}`}>Continue · Ch {cont}</button>}
        <button onClick={() => onToggleArchive(p)} className="btn btn-ghost px-3 py-2" title={archived ? 'Move back to your library' : 'Move to the archive'}>
          {archived ? '↩ Restore' : '📦 Archive'}
        </button>
      </div>
    </div>
  )
}
