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

// --- per-chapter read/unread, keyed by project id (so the list shows what's new) ---
export function getReadChapters(pid) {
  const m = read('nr.read', {})
  return new Set((m && m[pid]) || [])
}
export function markChapterRead(pid, index) {
  const m = read('nr.read', {})
  const arr = new Set((m && m[pid]) || [])
  if (arr.has(index)) return
  arr.add(index)
  m[pid] = [...arr]
  write('nr.read', m)
}

// --- reader typography ---
const DEFAULT_PREFS = { fontSize: 18, width: 68, sepia: false }
export function getReadingPrefs() {
  return { ...DEFAULT_PREFS, ...read('nr.reading', {}) }
}
export function setReadingPrefs(prefs) {
  write('nr.reading', { ...DEFAULT_PREFS, ...prefs })
}

// --- a rate-limit-paused job, so the resume banner + auto-resume survive a reload ---
export function getPausedJob(pid) {
  const m = read('nr.paused', {})
  return (m && m[pid]) ?? null
}
export function setPausedJob(pid, info) {
  const m = read('nr.paused', {})
  m[pid] = info
  write('nr.paused', m)
}
export function clearPausedJob(pid) {
  const m = read('nr.paused', {})
  if (m && pid in m) { delete m[pid]; write('nr.paused', m) }
}
