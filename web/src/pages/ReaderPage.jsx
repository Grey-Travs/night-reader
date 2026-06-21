import { useNavigate, useOutletContext, useParams } from 'react-router-dom'
import ChapterReader from '../components/ChapterReader'

// The reader as a deep-linkable route (/novel/:pid/chapter/:idx). It renders full
// screen over the shell; chapters + the job's re-translate come from ProjectLayout,
// which stays mounted underneath so a running translation is never interrupted.
export default function ReaderPage() {
  const { pid, chapters, reload, enqueue, glossary } = useOutletContext()
  const { idx } = useParams()
  const navigate = useNavigate()
  const index = Number(idx)

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
      onGuide={() => navigate('/guide')}
    />
  )
}
