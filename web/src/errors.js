// Normalise anything thrown by `api.*` into the shape the ErrorDialog renders.
//
// The backend now always answers with `detail` as an object (see server/errors.py), so
// most of the work is already done server-side. Two cases still need handling here:
//
//  * failures that never reach the server at all — if the Python process isn't running,
//    `fetch` rejects with "Failed to fetch" and there is no response to read a detail
//    from. That used to surface as raw text with no hint about what to do;
//  * older/unexpected responses where `detail` is a plain string.

const GENERIC = {
  code: 'unknown',
  title: 'Something went wrong',
  what: '',
  fixes: [],
  action: null,
  retryable: true,
  detail: '',
  trace: '',
}

// A rejected fetch (as opposed to an HTTP error status) means the request never landed.
function isNetworkFailure(err) {
  if (!err) return false
  const msg = String(err.message || err)
  return (
    err.status == null &&
    (err instanceof TypeError ||
      /failed to fetch|networkerror|load failed|connection refused/i.test(msg))
  )
}

export function toExplained(err) {
  if (!err) return { ...GENERIC }

  // Already an Explained (e.g. straight off an SSE `explain` payload).
  if (typeof err === 'object' && err.code && err.title) return { ...GENERIC, ...err }

  const detail = err.detail
  if (detail && typeof detail === 'object' && detail.title) {
    return { ...GENERIC, ...detail }
  }

  if (isNetworkFailure(err)) {
    return {
      ...GENERIC,
      code: 'engine-down',
      title: "Night Reader's engine isn't responding",
      what:
        'The page loaded but it can’t reach the local engine that does the ' +
        'translating and file saving. That almost always means the Night Reader ' +
        'window that runs in the background was closed or crashed.',
      fixes: [
        'Check the black start.bat console window is still open. If it closed, run start.bat again.',
        'If it is open, look at it for an error message, then reload this page.',
        'Nothing is lost — every translated chapter is already saved to disk.',
      ],
      action: 'retry',
      detail: String(err.message || err),
    }
  }

  const text = typeof detail === 'string' && detail ? detail : String(err.message || err)
  return { ...GENERIC, title: text, detail: text, retryable: err.status >= 500 }
}

// One-line summary for the compact inline banners that already exist on most pages.
export function errorTitle(err) {
  return toExplained(err).title
}

// The block behind "Copy report" — everything a bug report needs, nothing more.
export function formatReport(e, context = '') {
  const lines = [
    `Night Reader error report`,
    `code:    ${e.code}`,
    `title:   ${e.title}`,
    context ? `context: ${context}` : null,
    `when:    ${new Date().toISOString()}`,
    `agent:   ${navigator.userAgent}`,
    '',
    e.detail ? `detail:\n${e.detail}` : null,
    e.trace ? `\ntraceback:\n${e.trace}` : null,
  ]
  return lines.filter((l) => l != null).join('\n')
}
