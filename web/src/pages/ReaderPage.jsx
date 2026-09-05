import { Navigate, useNavigate, useOutletContext, useParams } from 'react-router-dom'
import ChapterReader from '../components/ChapterReader'

// The reader as a deep-linkable route (/novel/:pid/chapter/:idx). It renders full
// screen over the shell; chapters + the job's re-translate come from ProjectLayout,
// which stays mounted underneath so a running translation is never interrupted.
export default function ReaderPage() {
  const { pid, chapters, reload, enqueue, glossary, queue, resolveChapter, fixPronouns } = useOutletContext()
  const { idx } = useParams()
  const navigate = useNavigate()
  const index = Number(idx)

  // A non-numeric / junk URL (stale bookmark, typo) would otherwise become NaN and
  // poison localStorage (last-read, read-set) and the chapter fetch. Bounce home.
  if (!Number.isInteger(index) || index < 1) return <Navigate to={`/novel/${pid}`} replace />

  // A repair is queued on the novel's shared worker, so "is this chapter busy?" is a
  // question about that queue, not about local state inside the reader.
  const busy = queue?.current === index || (queue?.pending || []).includes(index)

  return (
    <ChapterReader
      pid={pid}
      index={index}
      chapters={chapters}
      glossary={glossary}
      onClose={() => navigate(`/novel/${pid}`)}
      onNavigate={(i) => navigate(`/novel/${pid}/chapter/${i}`)}
      onChanged={reload}
      onRetranslate={(i) => enqueue([i], true)}
      onResolve={resolveChapter}
      onFixPronouns={fixPronouns}
      taskRunning={busy}
      taskKind={queue?.current === index ? queue?.kind : 'translate'}
      onGuide={() => navigate('/guide')}
    />
  )
}
