import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api'
import NovelCard from '../components/NovelCard'
import { SkeletonCards } from '../components/ui'
import { useToast } from '../toast'
import { useConfirm } from '../confirm'

// The library / bookshelf: add a novel, search every translation, open a novel.
// The live cross-novel queue lives on its own Activity page now; here we just show
// a compact "N translating — view activity" pointer so the shelf stays uncluttered.
export default function LibraryPage() {
  const navigate = useNavigate()
  const toast = useToast()
  const confirm = useConfirm()
  const open = (id, chapter) => navigate(chapter != null ? `/novel/${id}/chapter/${chapter}` : `/novel/${id}`)

  const [projects, setProjects] = useState([])
  const [tab, setTab] = useState('gdoc') // gdoc | text
  const [url, setUrl] = useState('')
  const [text, setText] = useState('')
  const [textName, setTextName] = useState('')
  const [splitMode, setSplitMode] = useState('separator')
  const [separator, setSeparator] = useState('---')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const fileRef = useRef(null)
  const importRef = useRef(null)
  const [importing, setImporting] = useState(false)
  const [importMsg, setImportMsg] = useState(null)
  const [q, setQ] = useState('')
  const [results, setResults] = useState(null)
  const [searching, setSearching] = useState(false)
  const [sortBy, setSortBy] = useState('default')
  const [nameFilter, setNameFilter] = useState('')
  const [queued, setQueued] = useState(0)
  const [queueNovels, setQueueNovels] = useState(0)

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

  // Light poll just for the "translating now" pointer (full dashboard is on /activity).
  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const d = await api.queueOverview()
        if (!alive) return
        const jobs = d.jobs || []
        setQueued(jobs.reduce((n, j) => n + (j.current != null ? 1 : 0) + j.pending.length, 0))
        setQueueNovels(jobs.length)
      } catch { /* ignore */ }
    }
    tick()
    const id = setInterval(tick, 5000)
    return () => { alive = false; clearInterval(id) }
  }, [])

  async function addNovel(e) {
    e.preventDefault()
    if (!url.trim()) return
    setBusy(true); setError(null)
    try {
      const project = await api.createProject(url.trim())
      setUrl(''); await load(); open(project.id)
    } catch (e) {
      if (e.status === 409 && e.detail?.project_id) open(e.detail.project_id)
      else setError(String(e.message || e))
    } finally { setBusy(false) }
  }

  async function addText(e) {
    e.preventDefault()
    if (!text.trim()) { setError('Paste some text (or load a .txt file) first.'); return }
    setBusy(true); setError(null)
    try {
      const project = await api.createTextProject({ name: textName.trim(), text, split_mode: splitMode, separator })
      setText(''); setTextName(''); await load(); open(project.id)
    } catch (e) {
      setError(String(e.message || e))
    } finally { setBusy(false) }
  }

  async function onFile(e) {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    setText(await file.text())
    if (!textName.trim()) setTextName(file.name.replace(/\.[^.]+$/, ''))
  }

  async function onImportBundle(e) {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    setImporting(true); setError(null); setImportMsg(null)
    try {
      const d = await api.importBundle(file)
      const names = (d.imported || []).map((p) => p.name)
      setImportMsg(`Imported ${names.length} novel${names.length === 1 ? '' : 's'}${names.length ? ': ' + names.join(', ') : ''}.`)
      await load()
    } catch (err) {
      setError('Import failed: ' + String(err.message || err))
    } finally { setImporting(false) }
  }

  async function doGlobalSearch(e) {
    e?.preventDefault()
    const needle = q.trim()
    if (!needle) { setResults(null); return }
    setSearching(true); setError(null)
    try {
      setResults((await api.searchAll(needle)).results)
    } catch (err) {
      setError(String(err.message || err))
    } finally { setSearching(false) }
  }

  const active = projects.filter((p) => !p.archived)
  const archivedCount = projects.length - active.length

  const shownProjects = (() => {
    const nf = nameFilter.trim().toLowerCase()
    let list = nf ? active.filter((p) => (p.name || '').toLowerCase().includes(nf)) : active.slice()
    if (sortBy === 'name') list.sort((a, b) => (a.name || '').localeCompare(b.name || ''))
    else if (sortBy === 'progress') list.sort((a, b) => (b.translated || 0) - (a.translated || 0))
    else if (sortBy === 'review') list.sort((a, b) => (b.needs_review || 0) - (a.needs_review || 0))
    return list
  })()

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

  return (
    <div className="page">
      <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-reading text-2xl font-medium">Your library</h1>
          <p className="text-sm text-hint">Translate Korean web novels on your Claude plan.</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button onClick={() => importRef.current?.click()} disabled={importing} className="btn btn-ghost px-3 py-1.5" title="Add a novel from a .zip backup made on another device">{importing ? 'Importing…' : 'Import'}</button>
          <input ref={importRef} type="file" accept=".zip,application/zip" onChange={onImportBundle} className="hidden" />
          {projects.length > 0 && <a href={api.backupAllUrl()} className="btn btn-ghost px-3 py-1.5" title="Download every novel as one .zip backup">Back up all</a>}
        </div>
      </div>

      {queued > 0 && (
        <Link to="/activity" className="mb-6 flex items-center gap-2 rounded-card border border-line p-3 text-sm rowhover" style={{ background: 'var(--b-translating-bg)', color: 'var(--b-translating-tx)' }}>
          <span className="inline-block h-2 w-2 rounded-full animate-pulse" style={{ background: 'var(--accent)' }} />
          <span className="flex-1">Translating {queued} chapter{queued === 1 ? '' : 's'} across {queueNovels} novel{queueNovels === 1 ? '' : 's'}.</span>
          <span className="font-medium">View activity →</span>
        </Link>
      )}

      {/* Add a novel */}
      <section className="card mb-8 p-6">
        <div className="mb-3 flex items-center gap-2">
          <h2 className="text-base font-medium">Add a novel</h2>
          <div className="ml-auto flex gap-1">
            <button onClick={() => setTab('gdoc')} className={`btn px-3 py-1 text-xs ${tab === 'gdoc' ? 'btn-primary' : 'btn-ghost'}`}>Google Doc</button>
            <button onClick={() => setTab('text')} className={`btn px-3 py-1 text-xs ${tab === 'text' ? 'btn-primary' : 'btn-ghost'}`}>Paste / .txt</button>
          </div>
        </div>

        {tab === 'gdoc' ? (
          <>
            <p className="text-sm text-muted">Paste the Google Docs link (one chapter per tab). We'll read it and name the project from the doc's title.</p>
            <form onSubmit={addNovel} className="mt-4 flex flex-col gap-2 sm:flex-row">
              <input type="text" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://docs.google.com/document/d/…" className="input flex-1" />
              <button type="submit" disabled={busy} className="btn btn-primary px-5 py-2">{busy ? 'Reading…' : 'Add novel'}</button>
            </form>
          </>
        ) : (
          <form onSubmit={addText} className="mt-1">
            <p className="text-sm text-muted">Paste the novel text, or load a .txt file — no Google account needed.</p>
            <div className="mt-3 flex flex-col gap-2 sm:flex-row">
              <input value={textName} onChange={(e) => setTextName(e.target.value)} placeholder="Novel name" className="input flex-1" />
              <button type="button" onClick={() => fileRef.current?.click()} className="btn btn-ghost px-4 py-2">Load .txt</button>
              <input ref={fileRef} type="file" accept=".txt,text/plain" onChange={onFile} className="hidden" />
            </div>
            <textarea value={text} onChange={(e) => setText(e.target.value)} rows={6} placeholder="Paste the Korean (or mixed) novel text here…" className="input mt-2 w-full font-mono text-sm" />
            <div className="mt-2 flex flex-wrap items-center gap-2 text-sm">
              <span className="text-muted">Split chapters by:</span>
              <select value={splitMode} onChange={(e) => setSplitMode(e.target.value)} className="input !py-1">
                <option value="separator">A separator line</option>
                <option value="heading">Chapter headings (Chapter N / N화)</option>
                <option value="single">Don't split (one chapter)</option>
              </select>
              {splitMode === 'separator' && (
                <input value={separator} onChange={(e) => setSeparator(e.target.value)} className="input w-24 !py-1" title="Lines equal to this start a new chapter" />
              )}
              <button type="submit" disabled={busy} className="btn btn-primary ml-auto px-5 py-2">{busy ? 'Importing…' : 'Import novel'}</button>
            </div>
          </form>
        )}
        {error && <div className="mt-3 rounded-btn px-3 py-2 text-sm pill-review">{error}</div>}
      </section>

      {importMsg && (
        <div className="mb-4 rounded-card border border-line px-3 py-2 text-sm" style={{ background: 'var(--b-translated-bg)', color: 'var(--b-translated-tx)' }}>{importMsg}</div>
      )}

      {/* Search across every novel's translations */}
      {projects.length > 0 && (
        <section className="mb-6">
          <form onSubmit={doGlobalSearch} className="flex gap-2">
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search all your translations…" className="input flex-1" />
            <button type="submit" disabled={searching} className="btn btn-ghost px-4 py-2">{searching ? 'Searching…' : 'Search'}</button>
            {results != null && <button type="button" onClick={() => { setResults(null); setQ('') }} className="btn btn-ghost px-3 py-2">Clear</button>}
          </form>
          {results != null && (
            <div className="mt-3 max-h-[50vh] overflow-auto rounded-card border border-line">
              {results.length === 0 ? (
                <div className="p-6 text-center text-hint">No matches for “{q}”.</div>
              ) : (
                <div className="divide-y divide-line">
                  {results.map((r, i) => (
                    <button key={`${r.project_id}-${r.index}-${i}`} onClick={() => open(r.project_id, r.index)} className="block w-full p-3 text-left rowhover">
                      <div className="text-sm font-medium">{r.project_name} · Ch {r.index}{r.title ? ` — ${r.title}` : ''}</div>
                      <div className="mt-0.5 text-xs text-muted">{r.snippet}</div>
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}
        </section>
      )}

      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-3">
          <h2 className="text-sm font-medium text-muted">Your novels</h2>
          {archivedCount > 0 && (
            <Link to="/archive" className="text-xs text-hint hover:text-accent-text hover:underline" title="Finished novels you've archived">📦 {archivedCount} archived</Link>
          )}
        </div>
        {active.length > 1 && (
          <div className="flex flex-wrap items-center gap-2">
            <input value={nameFilter} onChange={(e) => setNameFilter(e.target.value)} placeholder="Filter…" className="input !py-1 text-xs" />
            <select value={sortBy} onChange={(e) => setSortBy(e.target.value)} className="input !py-1 text-xs" aria-label="Sort novels">
              <option value="default">Sort: default</option>
              <option value="name">Name (A–Z)</option>
              <option value="progress">Most translated</option>
              <option value="review">Needs review</option>
            </select>
          </div>
        )}
      </div>
      {loading ? (
        <SkeletonCards count={4} />
      ) : active.length === 0 ? (
        <div className="rounded-card border border-dashed border-line-strong p-10 text-center text-muted">
          {archivedCount > 0 ? (
            <>All your novels are archived. <Link to="/archive" className="text-accent-text hover:underline">View the archive</Link>, or add a new novel above.</>
          ) : (
            'No novels yet. Add one above to get started.'
          )}
        </div>
      ) : shownProjects.length === 0 ? (
        <div className="rounded-card border border-dashed border-line-strong p-10 text-center text-muted">
          No novels match “{nameFilter}”.
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {shownProjects.map((p) => (
            <NovelCard key={p.id} p={p} onOpen={open} onToggleArchive={toggleArchive} onRemove={remove} />
          ))}
        </div>
      )}
    </div>
  )
}
