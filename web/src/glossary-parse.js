// Shared parsing for the glossary bulk-add box and the file Import button.
// No React imports on purpose: there is no JS test runner in this repo, so
// plain `node` must be able to exercise parseBulk directly.

export const MAX_ROWS = 2000     // mirrors server/bulk.py
export const MAX_UNTYPED = 200
const MAX_TERM_LEN = 100

// Mirror of _TYPE_SYNONYMS in server/bulk.py — update both together. Mappings
// follow the classifier's definitions: spells/curses are skills; effects, laws,
// items, and named groups are terms; codenames/aliases are names.
// "unknown"/"auto"/"" are deliberately absent: they mean "no type given",
// which must auto-classify rather than silently become "other".
const TYPE_SYNONYMS = {
  abilities: 'skill', ability: 'skill', alias: 'name', aliases: 'name',
  armor: 'term', art: 'skill', arts: 'skill',
  beast: 'term', beasts: 'term', buff: 'term', buffs: 'term',
  character: 'name', characters: 'name',
  cities: 'place', city: 'place',
  class: 'skill', classes: 'skill',
  codename: 'name', codenames: 'name',
  concept: 'term', concepts: 'term',
  countries: 'place', country: 'place',
  'cultural term': 'term', curse: 'skill', curses: 'skill',
  debuff: 'term', debuffs: 'term',
  dungeon: 'place', dungeons: 'place',
  faction: 'term', factions: 'term',
  group: 'term', groups: 'term',
  guild: 'term', guilds: 'term',
  herb: 'term', herbs: 'term',
  item: 'term', items: 'term',
  kingdom: 'place', kingdoms: 'place',
  law: 'term', laws: 'term',
  location: 'place', locations: 'place',
  magic: 'skill',
  misc: 'other', miscellaneous: 'other',
  monster: 'term', monsters: 'term',
  name: 'name', names: 'name',
  nickname: 'name', nicknames: 'name',
  npc: 'name', npcs: 'name',
  object: 'term', objects: 'term',
  organisation: 'term', organisations: 'term',
  organization: 'term', organizations: 'term',
  other: 'other', others: 'other',
  people: 'name', person: 'name',
  place: 'place', places: 'place',
  plan: 'term', plans: 'term', plant: 'term', plants: 'term',
  potion: 'term', potions: 'term',
  procedure: 'term', procedures: 'term',
  protagonist: 'name', protagonists: 'name',
  race: 'term', races: 'term',
  rank: 'term', ranks: 'term',
  realm: 'place', realms: 'place',
  region: 'place', regions: 'place',
  regulation: 'term', regulations: 'term',
  skill: 'skill', skills: 'skill', slang: 'term',
  spell: 'skill', spells: 'skill',
  status: 'term', statuses: 'term',
  system: 'term', systems: 'term',
  technique: 'skill', techniques: 'skill',
  term: 'term', terms: 'term',
  title: 'term', titles: 'term',
  town: 'place', towns: 'place',
  weapon: 'term', weapons: 'term',
  world: 'place', worlds: 'place',
}

const PRONOUN_SYNONYMS = { male: 'he', m: 'he', female: 'she', f: 'she' }

export const FORMAT_LABELS = {
  entries: 'Full entries (JSON)',
  grouped: 'Grouped JSON',
  list: 'JSON list',
  csv: 'CSV table',
  tsv: 'Spreadsheet paste',
  flat: 'Plain list',
}

export function normalizeType(raw) {
  return TYPE_SYNONYMS[String(raw || '').trim().toLowerCase()] || ''
}

// Minimal RFC-4180-ish CSV parser (handles quotes, delimiters and newlines in fields).
export function parseCsv(text, delim = ',') {
  const rows = []
  let row = [], cur = '', q = false
  for (let i = 0; i < text.length; i++) {
    const c = text[i]
    if (q) {
      if (c === '"') { if (text[i + 1] === '"') { cur += '"'; i++ } else q = false }
      else cur += c
    } else if (c === '"') q = true
    else if (c === delim) { row.push(cur); cur = '' }
    else if (c === '\n') { row.push(cur); rows.push(row); row = []; cur = '' }
    else if (c !== '\r') cur += c
  }
  if (cur !== '' || row.length) { row.push(cur); rows.push(row) }
  return rows.filter((r) => r.some((x) => x.trim() !== ''))
}

// File Import path (unchanged behavior): full entries with type defaulting to
// "other" — an imported file is authoritative, so nothing gets auto-classified.
export function parseGlossaryFile(name, text) {
  if (name.toLowerCase().endsWith('.json') || text.trim().startsWith('[')) {
    const arr = JSON.parse(text)
    return (Array.isArray(arr) ? arr : []).map((e) => ({
      korean: e.korean || '', english: e.english || '', type: e.type || 'other',
      note: e.note || '', pronoun: e.pronoun || '', register: e.register || '',
    }))
  }
  const rows = parseCsv(text)
  if (!rows.length) return []
  const header = rows[0].map((h) => h.trim().toLowerCase())
  const hasHeader = header.includes('korean') && header.includes('english')
  const at = (n) => header.indexOf(n)
  const col = hasHeader
    ? { korean: at('korean'), english: at('english'), type: at('type'), note: at('note'), pronoun: at('pronoun'), register: at('register') }
    : { korean: 0, english: 1, type: 2, note: 5, pronoun: 3, register: 4 }
  const body = hasHeader ? rows.slice(1) : rows
  const cell = (r, i) => (i >= 0 && i < r.length ? (r[i] || '').trim() : '')
  return body.map((r) => ({
    korean: cell(r, col.korean), english: cell(r, col.english),
    type: cell(r, col.type) || 'other', note: cell(r, col.note),
    pronoun: cell(r, col.pronoun), register: cell(r, col.register),
  }))
}

// ---- smart bulk-add parsing -------------------------------------------------

const HANGUL = /[ㄱ-힝]/
// "Kael (name)" — the suffix only counts as a type when it normalizes to one,
// so "Kael (the Red)" stays a whole term.
const ANNOTATION = /^(.+?)\s*\(([A-Za-z ]{2,24})\)$/
// Export column order (app.py export_glossary); headerless tables are read this way.
const POSITIONAL = { korean: 0, english: 1, type: 2, pronoun: 3, register: 4, note: 5 }
const ENGLISH_FIRST = { korean: -1, english: 0, type: 1, note: 2, pronoun: -1, register: -1 }

function makeRow({ korean = '', english = '', type = '', note = '', pronoun = '', register = '' }) {
  korean = String(korean || '').trim()
  english = String(english || '').trim()
  const p = String(pronoun || '').trim()
  const row = {
    korean, english,
    type: normalizeType(type),
    note: String(note || '').trim(),
    pronoun: PRONOUN_SYNONYMS[p.toLowerCase()] || p,
    register: String(register || '').trim(),
  }
  if (!english && !korean) return null
  if (!english) row.invalid = 'needs an English spelling'
  else if (english.length > MAX_TERM_LEN) row.invalid = 'too long — looks like prose, not a term'
  return row
}

function objRow(e) {
  return makeRow({
    korean: e.korean, english: e.english, type: e.type, note: e.note,
    pronoun: e.pronoun, register: e.register ?? e.speech_register,
  })
}

function flatItem(s) {
  const t = String(s || '').trim()
  if (!t) return null
  const m = ANNOTATION.exec(t)
  if (m && normalizeType(m[2])) return makeRow({ english: m[1], type: m[2] })
  return makeRow({ english: t })
}

function result(format, rows, notices = [], error = null) {
  return { format, rows, notices, error }
}

function parseJsonBulk(text) {
  let data
  try {
    data = JSON.parse(text)
  } catch (err) {
    try {
      // One lenient retry: LLM/chat output often carries trailing commas or
      // curly quotes. Only already-invalid JSON reaches this, so the quote
      // mapping can't corrupt a valid note.
      data = JSON.parse(text.replace(/[“”„]/g, '"').replace(/,\s*([\]}])/g, '$1'))
    } catch {
      return result('entries', [], [],
        `This looks like JSON but couldn’t be read (${err.message}). Fix it, or paste a plain comma-separated list.`)
    }
  }

  if (Array.isArray(data)) {
    const rows = [], notices = []
    let objects = 0, dropped = 0
    for (const item of data) {
      if (typeof item === 'string') { const r = flatItem(item); if (r) rows.push(r) }
      else if (item && typeof item === 'object' && !Array.isArray(item)) {
        objects++
        const r = objRow(item)
        if (r) rows.push(r)
      } else dropped++
    }
    if (dropped) notices.push(`${dropped} item${dropped === 1 ? ' was' : 's were'} neither text nor an object and got ignored`)
    return result(objects ? 'entries' : 'list', rows, notices)
  }

  if (data && typeof data === 'object') {
    // A single pasted entry, not a grouped object.
    if (typeof data.english === 'string' || typeof data.korean === 'string') {
      const r = objRow(data)
      return result('entries', r ? [r] : [])
    }
    // Grouped by type: {"names": [...], "places": [...]} — keys are type labels.
    const rows = [], notices = []
    for (const [key, val] of Object.entries(data)) {
      const groupType = normalizeType(key)
      const items = Array.isArray(val) ? val : [val]
      if (!groupType) notices.push(`“${key}” isn’t a known type — its ${items.length} item${items.length === 1 ? '' : 's'} will have types auto-detected`)
      for (const item of items) {
        let r = null
        if (typeof item === 'string') r = flatItem(item)
        else if (item && typeof item === 'object' && !Array.isArray(item)) r = objRow(item)
        if (!r) continue
        if (!r.type && groupType) r.type = groupType // an item's own explicit type wins
        rows.push(r)
      }
    }
    return result('grouped', rows, notices)
  }

  return result('flat', [], [], 'That JSON isn’t a list, entries, or groups — paste an array or a {"names": […]} object.')
}

function headerCols(header) {
  const at = (n) => header.indexOf(n)
  return { korean: at('korean'), english: at('english'), type: at('type'),
           pronoun: at('pronoun'), register: at('register'), note: at('note') }
}

function mapTable(body, col, format, notices = []) {
  const cell = (r, i) => (i >= 0 && i < r.length ? String(r[i] || '').trim() : '')
  const rows = []
  for (const r of body) {
    const row = makeRow({
      korean: cell(r, col.korean), english: cell(r, col.english), type: cell(r, col.type),
      pronoun: cell(r, col.pronoun), register: cell(r, col.register), note: cell(r, col.note),
    })
    if (row) rows.push(row)
  }
  return result(format, rows, notices)
}

// Positional (headerless) tables are only meaningful when column 0 really is
// the Korean column — that's what makes "Kael, Sera" a flat list while
// "카엘, Kael, name" is a table row. Empty first cells also count: our own CSV
// export writes canonical names as ",Kael,name,…".
function koreanFirstCol(rows) {
  const first = rows.map((r) => String(r[0] || '').trim())
  return first.filter((c) => !c || HANGUL.test(c)).length / first.length >= 0.8
}

// "Kael, name" / "fireball, skill" lines: column 1 is a type label on nearly
// every row, which no real flat list looks like.
function typeSecondCol(rows) {
  const second = rows.map((r) => String(r[1] || '').trim())
  return second.filter((c) => normalizeType(c)).length / second.length >= 0.8
}

function parseTsvBulk(text) {
  const rows = parseCsv(text, '\t')
  if (!rows.length) return result('tsv', [])
  const header = rows[0].map((h) => h.trim().toLowerCase())
  if (header.includes('english')) return mapTable(rows.slice(1), headerCols(header), 'tsv')
  if (koreanFirstCol(rows)) return mapTable(rows, POSITIONAL, 'tsv')
  if (rows.every((r) => r.length >= 2) && typeSecondCol(rows)) return mapTable(rows, ENGLISH_FIRST, 'tsv')
  return result('tsv', [], [],
    'Couldn’t tell which spreadsheet column is which — add a header row (korean, english, type, pronoun, register, note).')
}

function detectCommaCsv(text) {
  const rows = parseCsv(text)
  if (!rows.length) return null
  const header = rows[0].map((h) => h.trim().toLowerCase())
  if (header.includes('english')) return mapTable(rows.slice(1), headerCols(header), 'csv')
  const uniform = rows.length >= 2 && rows.every((r) => r.length === rows[0].length && r.length >= 2)
  if (uniform && koreanFirstCol(rows)) return mapTable(rows, POSITIONAL, 'csv')
  if (uniform && typeSecondCol(rows)) return mapTable(rows, ENGLISH_FIRST, 'csv')
  if (rows.length === 1 && rows[0].length >= 2 && HANGUL.test(String(rows[0][0] || ''))) {
    return mapTable(rows, POSITIONAL, 'csv') // one pasted table row: 카엘, Kael, name
  }
  return null // it's a flat list
}

// The bulk-add box: figure out what the paste is and reduce it to rows of
// {korean, english, type, note, pronoun, register} (+ .invalid on bad rows).
// type === '' means "auto-classify on the server" — the one path that uses
// the user's Claude plan.
export function parseBulk(input) {
  const text = String(input || '').replace(/^\uFEFF/, '').trim()
  if (!text) return result('flat', [])
  if (text[0] === '[' || text[0] === '{') return parseJsonBulk(text)
  if (text.includes('\t')) return parseTsvBulk(text)
  const csv = detectCommaCsv(text)
  if (csv) return csv
  return result('flat', text.split(/[,\n;]/).map(flatItem).filter(Boolean))
}
