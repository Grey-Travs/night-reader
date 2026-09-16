// Thin client for the FastAPI backend. Paths are proxied to :8000 by Vite in dev,
// and same-origin when the built frontend is served by the backend in production.

async function req(path, opts) {
  const r = await fetch(path, opts)
  if (!r.ok) {
    let detail
    try { detail = (await r.json()).detail } catch { detail = r.statusText }
    // The backend answers with `detail` as a structured object (see server/errors.py).
    // `message` must stay a short human sentence, because plenty of call sites render
    // `e.message` straight into a banner — stringifying the whole object there would
    // dump raw JSON at the user. The full payload stays on `err.detail` for ErrorDialog.
    const message =
      typeof detail === 'string' ? detail
      : detail && typeof detail === 'object' && detail.title ? detail.title
      : r.statusText || `Request failed (${r.status})`
    const err = new Error(message)
    err.status = r.status
    err.detail = detail
    throw err
  }
  return r.status === 204 ? null : r.json()
}
const get = (p) => req(p)
const post = (p, body) =>
  req(p, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) })
const put = (p, body) =>
  req(p, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) })
const del = (p) => req(p, { method: 'DELETE' })

export const api = {
  status: () => get('/api/status'),
  initConfig: () => post('/api/init'),
  googleLogin: () => post('/api/google/login'),
  settings: () => get('/api/settings'),
  updateSettings: (body) => post('/api/settings', body),

  listProjects: () => get('/api/projects'),
  createProject: (url, name) => post('/api/projects', { url, name }),
  createTextProject: (body) => post('/api/projects/text', body),
  createImagesProject: (name) => post('/api/projects/images', { name }),
  getProject: (pid) => get(`/api/projects/${pid}`),
  updateProject: (pid, body) => post(`/api/projects/${pid}`, body),
  deleteProject: (pid) => del(`/api/projects/${pid}`),

  // Scanned pages (photographed / screenshotted novels).
  // One image per request as a raw body, like importBundle — it keeps the backend
  // free of a multipart dependency and gives per-file progress and per-file failure.
  // Thread the `batch` returned by the first upload through the rest of one drop:
  // a batch is how the reader says "these photos are one chapter".
  scans: () => get('/api/scans'),
  pages: (pid) => get(`/api/projects/${pid}/pages`),
  page: (pid, pageId) => get(`/api/projects/${pid}/pages/${pageId}`),
  pageImageUrl: (pid, pageId) => `/api/projects/${pid}/pages/${pageId}/image`,
  uploadPage: (pid, file, { batch = '', label = '', name = '' } = {}) => {
    const q = new URLSearchParams({ batch, label, name: name || file.name || '' })
    return req(`/api/projects/${pid}/pages?${q}`, {
      method: 'POST',
      headers: { 'Content-Type': file.type || 'application/octet-stream' },
      body: file,
    })
  },
  savePage: (pid, pageId, body) => post(`/api/projects/${pid}/pages/${pageId}`, body),
  reorderPages: (pid, ids) => post(`/api/projects/${pid}/pages/reorder`, { ids }),
  deletePages: (pid, ids) => post(`/api/projects/${pid}/pages/delete`, { ids }),
  // readPages / verifyPages QUEUE work on the novel's worker (like resolveChapter),
  // so they stream to the live console and honour Stop and the rate-limit resume.
  readPages: (pid, ids) => post(`/api/projects/${pid}/pages/ocr`, { ids: ids || [] }),
  verifyPages: (pid, ids) => post(`/api/projects/${pid}/pages/verify`, { ids: ids || [] }),
  stitchPages: (pid, body) => post(`/api/projects/${pid}/pages/stitch`, body || {}),
  buildChapters: (pid, body) => post(`/api/projects/${pid}/pages/build`, body || {}),

  // Move / back up novels between devices (portable .zip bundles).
  // `images` also packs a scanned novel's original photos — off by default, because
  // a photographed library runs to gigabytes and the text alone keeps it usable.
  bundleUrl: (pid, images = false) =>
    `/api/projects/${pid}/bundle${images ? '?images=true' : ''}`,
  backupAllUrl: (images = false) => `/api/backup${images ? '?images=true' : ''}`,
  importBundle: (file) =>
    req('/api/import', { method: 'POST', headers: { 'Content-Type': 'application/zip' }, body: file }),
  searchAll: (q) => get(`/api/search?q=${encodeURIComponent(q)}`),

  reviewInbox: () => get('/api/review'),

  // Upkeep: glossary approvals and consistency across every novel at once.
  allPendingTerms: () => get('/api/glossary/pending'),
  consistencySummary: (refresh = false) =>
    get('/api/consistency/summary' + (refresh ? '?refresh=true' : '')),
  reviewGlossaryBulk: (byProject) => post('/api/glossary/review-bulk', { by_project: byProject }),
  unifyBulk: (pids) => post('/api/consistency/unify-bulk', { pids: pids || [] }),

  chapters: (pid, refresh = false) =>
    get(`/api/projects/${pid}/chapters` + (refresh ? '?refresh=true' : '')),
  chapter: (pid, i) => get(`/api/projects/${pid}/chapters/${i}`),
  previousChapter: (pid, i) => get(`/api/projects/${pid}/chapters/${i}/previous`),
  saveChapter: (pid, i, translation) => put(`/api/projects/${pid}/chapters/${i}`, { translation }),
  scanChapter: (pid, i) => get(`/api/projects/${pid}/chapters/${i}/scan`),
  deepScanChapter: (pid, i) => post(`/api/projects/${pid}/chapters/${i}/scan/deep`),
  fixChapter: (pid, i, body) => post(`/api/projects/${pid}/chapters/${i}/fix`, body || {}),
  // resolve / fixPronouns QUEUE work on the novel's worker and return a job, rather
  // than blocking until it's done — that's what makes them visible in Activity.
  resolveChapter: (pid, i) => post(`/api/projects/${pid}/chapters/${i}/resolve`),
  fixPronouns: (pid, i) => post(`/api/projects/${pid}/chapters/${i}/fix-pronouns`),
  fixPronounsFlagged: (pid) => post(`/api/projects/${pid}/pronouns/fix-flagged`),
  acceptChapter: (pid, i) => post(`/api/projects/${pid}/chapters/${i}/accept`),
  searchChapters: (pid, q) => get(`/api/projects/${pid}/search?q=${encodeURIComponent(q)}`),
  exportUrl: (pid, format) => `/api/projects/${pid}/export?format=${format}`,

  // Per-paragraph rewrites. Generating writes only to the variant history — nothing
  // reaches the chapter until applyParagraph — so these run inline rather than
  // queueing behind a translation.
  chapterVariants: (pid, i) => get(`/api/projects/${pid}/chapters/${i}/variants`),
  paragraphSource: (pid, i, body) => post(`/api/projects/${pid}/chapters/${i}/paragraph/source`, body),
  retranslateParagraph: (pid, i, body) => post(`/api/projects/${pid}/chapters/${i}/paragraph/retranslate`, body),
  rephraseParagraph: (pid, i, body) => post(`/api/projects/${pid}/chapters/${i}/paragraph/rephrase`, body),
  applyParagraph: (pid, i, body) => post(`/api/projects/${pid}/chapters/${i}/paragraph/apply`, body),
  discardParagraph: (pid, i, body) => post(`/api/projects/${pid}/chapters/${i}/paragraph/discard`, body),

  consistencyScan: (pid) => get(`/api/projects/${pid}/consistency`),
  consistencyReplace: (pid, body) => post(`/api/projects/${pid}/consistency/replace`, body),

  glossary: (pid) => get(`/api/projects/${pid}/glossary`),
  reviewGlossary: (pid, body) => post(`/api/projects/${pid}/glossary/review`, body),
  saveGlossaryTerm: (pid, body) => post(`/api/projects/${pid}/glossary/term`, body),
  deleteGlossaryTerm: (pid, body) => post(`/api/projects/${pid}/glossary/term/delete`, body),
  deleteGlossaryTerms: (pid, body) => post(`/api/projects/${pid}/glossary/term/delete-bulk`, body),
  learnGlossary: (pid) => post(`/api/projects/${pid}/glossary/learn`),
  detectPronouns: (pid) => post(`/api/projects/${pid}/glossary/detect-pronouns`),
  bulkAddGlossary: (pid, body) => post(`/api/projects/${pid}/glossary/bulk-add`, body),
  importGlossary: (pid, body) => post(`/api/projects/${pid}/glossary/import`, body),
  copyGlossary: (pid, body) => post(`/api/projects/${pid}/glossary/copy`, body),
  glossaryExportUrl: (pid, format) => `/api/projects/${pid}/glossary/export?format=${format}`,

  translate: (pid, body) => post(`/api/projects/${pid}/translate`, body),
  // stopCurrent also aborts the chapter in flight; without it only the backlog is
  // dropped and the running chapter finishes (the original behaviour).
  cancelQueue: (pid, stopCurrent = false) =>
    post(`/api/projects/${pid}/translate/cancel`, { stop_current: stopCurrent }),
  resumeNow: (pid) => post(`/api/projects/${pid}/translate/resume`),
  queueOverview: () => get('/api/queue'),
  activeJob: (pid) => get(`/api/projects/${pid}/active-job`),
  streamUrl: (pid, jobId) => `/api/projects/${pid}/translate/${jobId}/stream`,

  // Series: several Google Docs that are really one novel. A doc caps out around 100
  // tabs, so a long novel arrives split, and the library would otherwise show the parts
  // as unrelated novels with separate glossaries.
  listSeries: () => get('/api/series'),
  suggestSeries: () => get('/api/series/suggest'),
  createSeries: (name, projectIds) => post('/api/series', { name, project_ids: projectIds }),
  series: (sid) => get(`/api/series/${sid}`),
  updateSeries: (sid, body) => post(`/api/series/${sid}`, body),
  // Unlinks the series only — the member novels are never deleted.
  deleteSeries: (sid) => del(`/api/series/${sid}`),
  // The novels in no series, which is what can be added to one.
  unlinkedNovels: () => get('/api/series/unlinked'),
  // How a novel carries on once its document is full — and the only way an imported
  // novel, which has no document at all, ever gets a next chapter. The numbering has to
  // be re-resolved afterwards: the stored mapping predates the document being added.
  addSeriesMember: (sid, projectId) =>
    post(`/api/series/${sid}/members`, { project_id: projectId }),
  // refresh=true re-reads the documents; without it the stored numbering is served as-is,
  // because a resolved chapter number must never be silently recomputed.
  seriesMapping: (sid, refresh = false) =>
    get(`/api/series/${sid}/mapping${refresh ? '?refresh=true' : ''}`),
  confirmSeriesMapping: (sid, overrides) => put(`/api/series/${sid}/mapping`, { overrides }),
  // dry_run reports every disagreement with its evidence and writes nothing. This is the
  // one step that changes what FUTURE translations read, so it gets a look first.
  mergeSeriesGlossary: (sid, picks = {}, dryRun = true) =>
    post(`/api/series/${sid}/glossary/merge`, { picks, dry_run: dryRun }),

  // Set every series' publishing link from the site's own series export. Matching
  // reports only; applying writes just the URL and leaves pricing alone.
  matchPublishLinks: (csv) => post('/api/posting/targets/match', { csv }),
  applyPublishLinks: (assignments) => post('/api/posting/targets/apply', { assignments }),

  // What a posting run would do. Writes nothing and opens no browser — this is the gate
  // in front of the free/paid cutoff, which is painful to change once readers have been
  // through a chapter.
  postingPlan: (sid, { targetId, start, end } = {}) => {
    const q = new URLSearchParams({ sid })
    if (targetId) q.set('target_id', targetId)
    if (start) q.set('start', String(start))
    if (end) q.set('end', String(end))
    return get(`/api/posting/plan?${q}`)
  },
  postingLedger: (sid, targetId = 'default') =>
    get(`/api/posting/ledger?${new URLSearchParams({ sid, target_id: targetId })}`),

  // Starting a run. The app cannot post — only the extension, inside the logged-in
  // browser, can — so pressing Start leaves a request in a file that an open meiko tab
  // picks up on its next poll. That indirection is also what makes a phone trigger work:
  // the run waits on disk rather than in the tab that asked for it.
  postingRun: (sid, targetId = 'default') =>
    get(`/api/posting/run?${new URLSearchParams({ sid, target_id: targetId })}`),
  startPostingRun: (sid, targetId, options) =>
    post('/api/posting/run', { sid, target_id: targetId, options }),
  cancelPostingRun: (sid, targetId = 'default') =>
    del(`/api/posting/run?${new URLSearchParams({ sid, target_id: targetId })}`),
  resumePostingRun: (sid, targetId = 'default') =>
    post('/api/posting/run/resume', { sid, target_id: targetId }),

  // Novels already published but not in the library. The extension does the reading, so
  // all this needs is the candidate list it pushed.
  importCandidates: () => get('/api/import/candidates'),

  // Reaching this app from a phone, so a run can be started from anywhere. The access key
  // rides in a cookie once the phone has opened the link, so nothing here has to carry it.
  remote: () => get('/api/remote'),
  updateRemote: (body) => post('/api/remote', body),
  testRemoteNotification: () => post('/api/remote/test-notification'),
}
