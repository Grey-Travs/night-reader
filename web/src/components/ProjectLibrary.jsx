import { useEffect, useState } from 'react'
import { api } from '../api'
import { Dot, ProgressBar } from './ui'
import ThemeToggle from './ThemeToggle'

export default function ProjectLibrary({ status, onOpen, onSettings, onSetup }) {
  const [projects, setProjects] = useState([])
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

  async function load() {
    setLoading(true)
    try {
      const d = await api.listProjects()
      setProjects(d.projects)
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { load() }, [])

  async function addNovel(e) {
    e.preventDefault()
    if (!url.trim()) return
    setBusy(true)
    setError(null)
    try {
      const project = await api.createProject(url.trim())
      setUrl('')
      await load()
      onOpen(project.id)
    } catch (e) {
      if (e.status === 409 && e.detail?.project_id) {
        onOpen(e.detail.project_id)
      } else {
        setError(String(e.message || e))
      }
    } finally {
      setBusy(false)
    }
  }

  async function remove(pid, name) {
    if (!confirm(`Remove "${name}" from your library?\n\nThis deletes its translations and glossary on this computer. Your Google Doc is untouched.`))
      return
    await api.deleteProject(pid)
    load()
  }

  return (
    <div className="min-h-screen bg-page text-ink">
      <header className="border-b border-line">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-4">
          <div>
            <h1 className="font-reading text-xl font-medium">Web-Novel Translator</h1>
            <p className="text-sm text-hint">Your library · runs on your Claude plan</p>
          </div>
          <div className="flex items-center gap-3 text-sm text-muted">
            <span className="hidden items-center gap-1.5 sm:flex"><Dot ok={status?.google_logged_in} /> Google</span>
            <span className="hidden items-center gap-1.5 sm:flex"><Dot ok={status?.claude_logged_in} /> Claude</span>
            <button onClick={onSetup} className="btn btn-quiet text-sm">Setup</button>
            <button onClick={onSettings} className="btn btn-ghost px-3 py-1.5">Settings</button>
            <ThemeToggle />
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-5xl px-6 py-8">
        {/* Add a novel */}
        <section className="card mb-8 p-6">
          <h2 className="text-base font-medium">Add a novel</h2>
          <p className="mt-1 text-sm text-muted">
            Paste the Google Docs link (one chapter per tab). We'll read it and name the project from the doc's title.
          </p>
          <form onSubmit={addNovel} className="mt-4 flex flex-col gap-2 sm:flex-row">
            <input
              type="text"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://docs.google.com/document/d/…"
              className="input flex-1"
            />
            <button type="submit" disabled={busy} className="btn btn-primary px-5 py-2">
              {busy ? 'Reading…' : 'Add novel'}
            </button>
          </form>
          {error && <div className="mt-3 rounded-btn px-3 py-2 text-sm pill-review">{error}</div>}
        </section>

        <h2 className="mb-3 text-sm font-medium text-muted">Your novels</h2>
        {loading ? (
          <div className="card p-8 text-center text-hint">Loading…</div>
        ) : projects.length === 0 ? (
          <div className="rounded-card border border-dashed border-line-strong p-10 text-center text-muted">
            No novels yet. Paste a Google Doc link above to add your first.
          </div>
        ) : (
          <div className="grid gap-4 sm:grid-cols-2">
            {projects.map((p) => {
              const total = p.chapter_count
              return (
                <div key={p.id} className="card p-5">
                  <div className="flex items-start justify-between gap-3">
                    <h3 className="font-reading text-lg font-medium leading-snug">{p.name}</h3>
                    <button onClick={() => remove(p.id, p.name)} className="tap -m-2 shrink-0 p-2 text-hint hover:text-danger" title="Remove from library">✕</button>
                  </div>
                  <div className="mt-3 text-sm text-muted">
                    {p.translated} translated{total ? ` · ${total} tabs` : ''}
                    {p.needs_review ? ` · ${p.needs_review} to review` : ''}
                  </div>
                  {total ? <div className="mt-2"><ProgressBar value={p.translated} total={total} /></div> : null}
                  <button onClick={() => onOpen(p.id)} className="btn btn-ghost mt-4 w-full px-3 py-2">Open</button>
                </div>
              )
            })}
          </div>
        )}
      </main>
    </div>
  )
}
