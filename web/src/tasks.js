// What the per-novel worker is doing, for the UI. Mirrors TASK_LABEL in server/app.py.
//
// ONE copy. There were four, each with its own comment saying it mirrored app.py, and
// three of them never learned about scanned-page work — so a running OCR job showed
// up in Activity, Project Activity and the Review inbox as "Translating chapter 7"
// where 7 was a PAGE number. tests/test_job_kinds.py fails if this drifts from the
// server's TASK_KINDS again.

// Verb forms that take a following noun: "Translating chapter 3", "Reading page 7".
export const TASK_LABEL = {
  translate: 'Translating',
  resolve: 'AI resolve on',
  pronouns: 'Fixing pronouns in',
  ocr: 'Reading',
  'ocr-verify': 'Double-checking',
}

// Standalone forms, for places that don't append a noun ("AI resolve now…").
export const TASK_LABEL_BARE = {
  translate: 'Translating',
  resolve: 'AI resolve',
  pronouns: 'Fixing pronouns',
  ocr: 'Reading a page',
  'ocr-verify': 'Double-checking a page',
}

// Kinds whose index is a page sequence number rather than a chapter index.
export const PAGE_TASK_KINDS = ['ocr', 'ocr-verify']

export const isPageTask = (kind) => PAGE_TASK_KINDS.includes(kind)

// Events for a page also carry page_id; kind alone is enough where they don't.
export const taskNoun = (kind) => (isPageTask(kind) ? 'page' : 'chapter')

/** "Reading page 7" / "Translating chapter 3" */
export function describeTask(kind, index) {
  return `${TASK_LABEL[kind] || TASK_LABEL.translate} ${taskNoun(kind)} ${index}`
}

/** The queue as a CHAPTER view: empty whenever its numbers aren't chapter indices.
 *
 * One worker queue carries both chapter work and scanned-page work, and `current` /
 * `pending` are page sequence numbers during an OCR run. Four separate places
 * compared them straight against a chapter index, so reading page 7 of a
 * photographed novel marked chapter 7 as busy — repair buttons disabled, paragraph
 * rewrites refused, a Review row locked, and a "Queued" badge on a chapter nothing
 * was going to touch. Anything asking "is this CHAPTER busy?" asks this instead.
 */
export function chapterScopedQueue(queue) {
  const q = queue || {}
  if (isPageTask(q.kind)) return { current: null, pending: [] }
  return { current: q.current ?? null, pending: q.pending || [] }
}
