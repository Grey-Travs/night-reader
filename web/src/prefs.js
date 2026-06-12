// Client-only reading state, persisted in localStorage (never leaves the browser).
// Covers "continue where you left off" per novel and reader typography preferences.

function read(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback } catch { return fallback }
}
function write(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)) } catch { /* ignore */ }
}

// --- last read chapter, keyed by project id ---
export function getLastRead(pid) {
  const m = read('nr.lastRead', {})
  return (m && m[pid]) ?? null
}
export function setLastRead(pid, index) {
  const m = read('nr.lastRead', {})
  m[pid] = index
  write('nr.lastRead', m)
}

// --- reader typography ---
const DEFAULT_PREFS = { fontSize: 18, width: 68, sepia: false }
export function getReadingPrefs() {
  return { ...DEFAULT_PREFS, ...read('nr.reading', {}) }
}
export function setReadingPrefs(prefs) {
  write('nr.reading', { ...DEFAULT_PREFS, ...prefs })
}
