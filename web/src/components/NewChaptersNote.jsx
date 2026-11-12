// Chapters added to the end of a series' documents are numbered by the server on its own
// (server/series.py: number_new_chapters), so they show up for posting without anyone
// pressing "Re-read documents". This says when that happened, and names any new chapters
// it could not number safely, so they are never just quietly missing.

// "217–235", or "40, 52–53" for broken runs.
export function chapterRuns(numbers) {
  const sorted = [...new Set(numbers)].sort((a, b) => a - b)
  const runs = []
  for (const n of sorted) {
    const last = runs[runs.length - 1]
    if (last && n === last[1] + 1) last[1] = n
    else runs.push([n, n])
  }
  return runs.map(([a, b]) => (a === b ? String(a) : `${a}–${b}`)).join(', ')
}

export default function NewChaptersNote({ added = [], waiting = [], action = null, className = '' }) {
  if (!added.length && !waiting.length) return null
  return (
    <div role="status" className={`rounded-card border border-line p-3 text-sm ${className}`} style={{ background: 'var(--surface)' }}>
      {added.length > 0 && (
        <p>
          Numbered {added.length} new chapter{added.length === 1 ? '' : 's'} on its own:{' '}
          <strong className="tabular-nums">ch {chapterRuns(added)}</strong>.
        </p>
      )}
      {waiting.map((w) => (
        <p key={w.project_id} className={added.length ? 'mt-1' : ''}>
          {w.count} new chapter{w.count === 1 ? '' : 's'} in <strong>{w.name || 'one document'}</strong>{' '}
          {w.count === 1 ? 'needs' : 'need'} a look before {w.count === 1 ? 'it gets a number' : 'they get numbers'}:
          they were added somewhere other than the end, or their numbers aren’t certain.{' '}
          {action}
        </p>
      ))}
    </div>
  )
}
