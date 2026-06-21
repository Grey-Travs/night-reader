import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api'
import NovelCard from '../components/NovelCard'
import { SkeletonCards } from '../components/ui'
import { useToast } from '../toast'
import { useConfirm } from '../confirm'

// Finished novels the user has tucked away. They still translate, read and export
// like any other novel — archiving just keeps the main library shelf uncluttered.
export default function ArchivePage() {
  const navigate = useNavigate()
  const toast = useToast()
  const confirm = useConfirm()
  const open = (id, chapter) => navigate(chapter != null ? `/novel/${id}/chapter/${chapter}` : `/novel/${id}`)

  const [projects, setProjects] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  async function load() {
    setLoading(true)
    try {
      setProjects((await api.listProjects()).projects)
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { load() }, [])

  async function toggleArchive(p) {
    try {
      await api.updateProject(p.id, { archived: !p.archived })
      toast(p.archived ? 'Restored to library' : 'Moved to archive')
      await load()
    } catch (e) { setError(String(e.message || e)) }
  }

  async function remove(pid, name) {
    const ok = await confirm({
      title: `Remove “${name}”?`,
      body: 'This deletes its translations and glossary on this computer. Your source document is untouched.',
      confirmLabel: 'Remove', danger: true,
    })
    if (!ok) return
    await api.deleteProject(pid)
    toast('Removed from library')
    load()
  }

  const archived = projects.filter((p) => p.archived)

  return (
    <div className="page">
      <div className="mb-6">
        <h1 className="font-reading text-2xl font-medium">Archive</h1>
        <p className="text-sm text-hint">Finished novels you've tucked away. They still translate and read normally — open one any time, or restore it to the library.</p>
      </div>
      {error && <div className="mb-4 rounded-btn px-3 py-2 text-sm pill-review">{error}</div>}
      {loading ? (
        <SkeletonCards count={3} />
      ) : archived.length === 0 ? (
        <div className="rounded-card border border-dashed border-line-strong p-10 text-center text-muted">
          Nothing archived yet. On the <Link to="/" className="text-accent-text hover:underline">library</Link>, use a novel's “📦 Archive” button to move finished novels here.
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {archived.map((p) => (
            <NovelCard key={p.id} p={p} archived onOpen={open} onToggleArchive={toggleArchive} onRemove={remove} />
          ))}
        </div>
      )}
    </div>
  )
}
