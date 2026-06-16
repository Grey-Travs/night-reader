import { useState } from 'react'
import { useHints } from '../hints'

// A small "?" bubble with a plain-language tip. Hidden when hints are toggled off.
// IMPORTANT: hints are UI chrome only — never place them inside the chapter prose,
// so the reader's "Copy text" (which copies the translation string, not the page)
// never picks them up.
export default function Hint({ text, className = '' }) {
  const { on } = useHints()
  const [open, setOpen] = useState(false)
  if (!on) return null
  return (
    <span className={`relative inline-flex align-middle ${className}`}>
      <button
        type="button"
        onClick={(e) => { e.stopPropagation(); e.preventDefault(); setOpen((v) => !v) }}
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        aria-label="Hint"
        className="inline-flex h-4 w-4 items-center justify-center rounded-full text-[10px] font-bold leading-none"
        style={{ background: 'var(--b-muted-bg)', color: 'var(--muted)' }}
      >?</button>
      {open && (
        <span
          role="tooltip"
          className="absolute left-1/2 top-full z-50 mt-1 w-56 -translate-x-1/2 rounded-card border border-line p-2 text-xs font-normal normal-case leading-snug shadow-lg"
          style={{ background: 'var(--elevated)', color: 'var(--ink)' }}
        >{text}</span>
      )}
    </span>
  )
}
