import { useState } from 'react'

function current() {
  try { return localStorage.getItem('theme') === 'light' ? 'light' : 'dark' } catch { return 'dark' }
}

export default function ThemeToggle({ className = '' }) {
  const [theme, setTheme] = useState(current)

  function toggle() {
    const next = theme === 'dark' ? 'light' : 'dark'
    if (next === 'light') document.documentElement.setAttribute('data-theme', 'light')
    else document.documentElement.removeAttribute('data-theme')
    try { localStorage.setItem('theme', next) } catch { /* ignore */ }
    setTheme(next)
  }

  return (
    <button
      onClick={toggle}
      title={theme === 'dark' ? 'Switch to light' : 'Switch to dark'}
      aria-label="Toggle theme"
      className={`btn btn-ghost tap h-8 w-8 !p-0 ${className}`}
    >
      <span aria-hidden>{theme === 'dark' ? '☾' : '☀'}</span>
    </button>
  )
}
