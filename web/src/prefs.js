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
// theme: default | paper | sepia | oled   ·   font: serif | sans
const DEFAULT_PREFS = { fontSize: 18, width: 68, theme: 'default', font: 'serif', autoAdvance: false, glossaryTips: true }
export function getReadingPrefs() {
  const raw = read('nr.reading', {}) || {}
  const merged = { ...DEFAULT_PREFS, ...raw }
  // Migrate the legacy `sepia` boolean to the new theme model.
  if (raw.sepia && (!raw.theme || raw.theme === 'default')) merged.theme = 'sepia'
  delete merged.sepia
  return merged
}
export function setReadingPrefs(prefs) {
  const { sepia, ...rest } = prefs || {}
  write('nr.reading', { ...DEFAULT_PREFS, ...rest })
}

// --- per-chapter scroll position (resume mid-chapter), keyed pid:index ---
export function getScrollPos(pid, index) {
  const m = read('nr.scroll', {})
  return (m && m[`${pid}:${index}`]) ?? 0
}
export function setScrollPos(pid, index, ratio) {
  const m = read('nr.scroll', {}) || {}
  const key = `${pid}:${index}`
  // Drop the very top and the very bottom: starting a barely-read chapter at 0 is
  // natural, and not restoring a fully-read chapter to its end avoids an auto-advance
  // loop the next time it's opened.
  if (!ratio || ratio < 0.02 || ratio > 0.98) delete m[key]
  else m[key] = Math.min(1, ratio)
  // Cap the map so it can't grow without bound.
  const keys = Object.keys(m)
  if (keys.length > 300) for (const k of keys.slice(0, keys.length - 300)) delete m[k]
  write('nr.scroll', m)
}

// --- guide / hints ---
export function getHintsOn() { return read('nr.hints', true) !== false }
export function setHintsOn(on) { write('nr.hints', !!on) }
export function getGuideSeen() { return read('nr.guideSeen', false) === true }
export function setGuideSeen() { write('nr.guideSeen', true) }

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
