import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'

// Global quick-jump (Ctrl/⌘-K): type to filter novels and pages, Enter to go.
const STATIC = [
  { label: 'Library', sub: 'All your novels', to: '/' },
  { label: 'Activity', sub: 'Running translations', to: '/activity' },
  { label: 'Needs review', sub: 'Flagged chapters', to: '/review' },
  { label: 'Archive', sub: 'Finished novels', to: '/archive' },
  { label: 'Settings', sub: 'Model · effort · connections', to: '/settings' },
  { label: 'Guide', sub: 'How to use the app', to: '/guide' },
]

export default function CommandPalette() {
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const [active, setActive] = useState(0)
  const [projects, setProjects] = useState([])
  const inputRef = useRef(null)
  const listRef = useRef(null)

  useEffect(() => {
    const h = (e) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault()
        setOpen((v) => !v)
      }
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [])

  // Refresh the novel list each time it opens (cheap; keeps it current).
  useEffect(() => {
    if (!open) return
    setQ(''); setActive(0)
    api.listProjects().then((d) => setProjects(d.projects || [])).catch(() => {})
    const t = setTimeout(() => inputRef.current?.focus(), 0)
    return () => clearTimeout(t)
  }, [open])

  const items = useMemo(() => {
    const novels = projects.map((p) => ({ label: p.name, sub: 'Open novel', to: `/novel/${p.id}` }))
    const all = [...novels, ...STATIC]
    const needle = q.trim().toLowerCase()
    return needle ? all.filter((it) => `${it.label} ${it.sub}`.toLowerCase().includes(needle)) : all
  }, [projects, q])

  useEffect(() => { setActive(0) }, [q, projects])
  useEffect(() => {
    listRef.current?.querySelector(`[data-idx="${active}"]`)?.scrollIntoView({ block: 'nearest' })
  }, [active])

  if (!open) return null

  const go = (it) => { setOpen(false); navigate(it.to) }
  const onKey = (e) => {
    // Stop Escape from bubbling to other window-level keydown listeners (e.g. the
    // reader, which would otherwise also close on the same press).
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); setOpen(false); return }
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive((a) => Math.min(items.length - 1, a + 1)) }
    if (e.key === 'ArrowUp') { e.preventDefault(); setActive((a) => Math.max(0, a - 1)) }
    if (e.key === 'Enter') { e.preventDefault(); if (items[active]) go(items[active]) }
  }

  return (
    <div className="fixed inset-0 z-[80] flex items-start justify-center p-4 pt-[12vh]" style={{ background: 'var(--scrim)' }} onClick={() => setOpen(false)}>
      <div className="w-full max-w-xl overflow-hidden rounded-card border border-line shadow-lg" style={{ background: 'var(--elevated)' }} onClick={(e) => e.stopPropagation()}>
        <input
          ref={inputRef}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={onKey}
          placeholder="Jump to a novel or page…"
          className="w-full border-b border-line bg-transparent px-4 py-3 text-sm outline-none"
          style={{ color: 'var(--ink)' }}
        />
        <ul ref={listRef} className="max-h-[50vh] overflow-y-auto py-1">
          {items.length === 0 && <li className="px-4 py-3 text-sm text-hint">No matches.</li>}
          {items.map((it, i) => (
            <li key={`${it.to}-${i}`}>
              <button
                data-idx={i}
                onMouseEnter={() => setActive(i)}
                onClick={() => go(it)}
                className="flex w-full items-center justify-between gap-3 px-4 py-2.5 text-left text-sm"
                style={i === active ? { background: 'color-mix(in oklab, var(--accent) 16%, transparent)' } : undefined}
              >
                <span className="truncate font-medium">{it.label}</span>
                <span className="shrink-0 text-xs text-hint">{it.sub}</span>
              </button>
            </li>
          ))}
        </ul>
        <div className="flex items-center justify-between border-t border-line px-4 py-2 text-xs text-hint">
          <span>↑↓ move · Enter open · Esc close</span>
          <span>⌘/Ctrl-K</span>
        </div>
      </div>
    </div>
  )
}
