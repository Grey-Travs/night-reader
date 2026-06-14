// Thin client for the FastAPI backend. Paths are proxied to :8000 by Vite in dev,
// and same-origin when the built frontend is served by the backend in production.

async function req(path, opts) {
  const r = await fetch(path, opts)
  if (!r.ok) {
    let detail
    try { detail = (await r.json()).detail } catch { detail = r.statusText }
    const err = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
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
  getProject: (pid) => get(`/api/projects/${pid}`),
  updateProject: (pid, body) => post(`/api/projects/${pid}`, body),
  deleteProject: (pid) => del(`/api/projects/${pid}`),

  chapters: (pid, refresh = false) =>
    get(`/api/projects/${pid}/chapters` + (refresh ? '?refresh=true' : '')),
  chapter: (pid, i) => get(`/api/projects/${pid}/chapters/${i}`),
  saveChapter: (pid, i, translation) => put(`/api/projects/${pid}/chapters/${i}`, { translation }),
  searchChapters: (pid, q) => get(`/api/projects/${pid}/search?q=${encodeURIComponent(q)}`),
  exportUrl: (pid, format) => `/api/projects/${pid}/export?format=${format}`,

  glossary: (pid) => get(`/api/projects/${pid}/glossary`),
  reviewGlossary: (pid, body) => post(`/api/projects/${pid}/glossary/review`, body),
  saveGlossaryTerm: (pid, body) => post(`/api/projects/${pid}/glossary/term`, body),
  deleteGlossaryTerm: (pid, body) => post(`/api/projects/${pid}/glossary/term/delete`, body),
  learnGlossary: (pid) => post(`/api/projects/${pid}/glossary/learn`),
  importGlossary: (pid, body) => post(`/api/projects/${pid}/glossary/import`, body),
  glossaryExportUrl: (pid, format) => `/api/projects/${pid}/glossary/export?format=${format}`,

  translate: (pid, body) => post(`/api/projects/${pid}/translate`, body),
  cancelQueue: (pid) => post(`/api/projects/${pid}/translate/cancel`),
  activeJob: (pid) => get(`/api/projects/${pid}/active-job`),
  streamUrl: (pid, jobId) => `/api/projects/${pid}/translate/${jobId}/stream`,
}
