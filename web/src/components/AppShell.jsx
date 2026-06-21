import { NavLink, Outlet } from 'react-router-dom'
import { Dot } from './ui'
import ThemeToggle from './ThemeToggle'
import CommandPalette from './CommandPalette'

// Persistent app shell: a left sidebar with the top-level destinations, and the
// routed page in the content area. The sidebar stays put as you move around, so
// nothing is buried in per-screen header buttons anymore.
const NAV = [
  { to: '/', end: true, icon: '📚', label: 'Library' },
  { to: '/activity', icon: '⚡', label: 'Activity' },
  { to: '/review', icon: '🚩', label: 'Review' },
  { to: '/archive', icon: '📦', label: 'Archive' },
  { to: '/guide', icon: '❓', label: 'Guide' },
  { to: '/settings', icon: '⚙', label: 'Settings' },
]

const linkClass = ({ isActive }) => `navlink ${isActive ? 'navlink-active' : ''}`

export default function AppShell({ status, setStatus, onSetup }) {
  return (
    <div className="flex min-h-screen bg-page text-ink">
      {/* Desktop: vertical sidebar */}
      <aside
        className="sticky top-0 hidden h-screen w-60 shrink-0 flex-col border-r border-line px-3 py-5 md:flex"
        style={{ background: 'var(--surface)' }}
      >
        <div className="px-2 pb-5">
          <div className="font-reading text-lg font-medium leading-tight">Night Reader</div>
          <div className="text-xs text-hint">runs on your Claude plan</div>
        </div>
        <nav className="flex flex-col gap-1">
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.end} className={linkClass}>
              <span className="navicon" aria-hidden>{n.icon}</span>
              <span>{n.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="mt-auto flex flex-col gap-3 px-2 pt-4">
          <div className="flex items-center gap-3 text-xs text-muted">
            <span className="flex items-center gap-1.5"><Dot ok={status?.google_logged_in} /> Google</span>
            <span className="flex items-center gap-1.5"><Dot ok={status?.claude_logged_in} /> Claude</span>
          </div>
          <ThemeToggle />
        </div>
      </aside>

      {/* Mobile: top bar with the same destinations */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header
          className="sticky top-0 z-30 flex items-center gap-1 border-b border-line px-3 py-2 md:hidden"
          style={{ background: 'var(--surface)' }}
        >
          <span className="mr-1 shrink-0 font-reading text-sm font-medium">Night Reader</span>
          <nav className="flex flex-1 items-center gap-1 overflow-x-auto">
            {NAV.map((n) => (
              <NavLink key={n.to} to={n.to} end={n.end} className={({ isActive }) => `navlink shrink-0 !py-1.5 ${isActive ? 'navlink-active' : ''}`} title={n.label}>
                <span className="navicon" aria-hidden>{n.icon}</span>
                <span className="text-xs">{n.label}</span>
              </NavLink>
            ))}
          </nav>
          <ThemeToggle className="shrink-0" />
        </header>
        <main className="min-w-0 flex-1">
          <Outlet context={{ status, setStatus, onSetup }} />
        </main>
      </div>
      <CommandPalette />
    </div>
  )
}
