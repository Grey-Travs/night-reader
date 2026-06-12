import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { Dot, ProgressBar } from './ui'
import ThemeToggle from './ThemeToggle'
import { getLastRead } from '../prefs'

export default function ProjectLibrary({ status, onOpen, onSettings, onSetup }) {
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
      if (e.status === 409 && e.detail?.project_id) onOpen(e.detail.project_id)
      else setError(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }

  async function addText(e) {
    e.preventDefault()
    if (!text.trim()) { setError('Paste some text (or load a .txt file) first.'); return }
    setBusy(true)
    setError(null)
    try {
      const project = await api.createTextProject({
        name: textName.trim(), text, split_mode: splitMode, separator,
      })
      setText(''); setTextName('')
      await load()
      onOpen(project.id)
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }

  async function onFile(e) {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    setText(await file.text())
    if (!textName.trim()) setTextName(file.name.replace(/\.[^.]+$/, ''))
  }

  async function remove(pid, name) {
    if (!confirm(`Remove "${name}" from your library?\n\nThis deletes its translations and glossary on this computer. Your source is untouched.`))
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

        <h2 className="mb-3 text-sm font-medium text-muted">Your novels</h2>
        {loading ? (
          <div className="card p-8 text-center text-hint">Loading…</div>
        ) : projects.length === 0 ? (
          <div className="rounded-card border border-dashed border-line-strong p-10 text-center text-muted">
            No novels yet. Add one above to get started.
          </div>
        ) : (
          <div className="grid gap-4 sm:grid-cols-2">
            {projects.map((p) => {
              const total = p.chapter_count
              const cont = getLastRead(p.id)
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
                  <div className="mt-4 flex gap-2">
                    <button onClick={() => onOpen(p.id)} className="btn btn-ghost flex-1 px-3 py-2">Open</button>
                    {cont != null && <button onClick={() => onOpen(p.id, cont)} className="btn btn-ghost px-3 py-2" title={`Continue chapter ${cont}`}>Continue · Ch {cont}</button>}
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </main>
    </div>
  )
}
