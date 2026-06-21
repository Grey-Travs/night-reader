import { Modal } from './ui'

// Keyboard cheatsheet for the reader (and the global palette). Opened with `?`.
const KEYS = [
  ['←  /  →', 'Previous / next chapter'],
  ['?', 'Show this shortcuts list'],
  ['Esc', 'Back out (popover → edit → close)'],
  ['Ctrl / ⌘ + K', 'Command palette — jump anywhere'],
  ['Aa', 'Reading options (size, width, theme, font)'],
]

export default function ShortcutsHelp({ onClose }) {
  return (
    <Modal onClose={onClose}>
      <div className="flex items-center justify-between border-b border-line px-6 py-4">
        <h3 className="font-medium">Keyboard shortcuts</h3>
        <button onClick={onClose} className="btn btn-quiet text-lg leading-none">✕</button>
      </div>
      <dl className="space-y-2 px-6 py-5 text-sm">
        {KEYS.map(([k, v]) => (
          <div key={k} className="flex items-center justify-between gap-4">
            <dt className="text-muted">{v}</dt>
            <dd><kbd className="rounded-btn border border-line px-2 py-1 font-mono text-xs" style={{ background: 'var(--surface)' }}>{k}</kbd></dd>
          </div>
        ))}
      </dl>
    </Modal>
  )
}
