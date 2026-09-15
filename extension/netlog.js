// Records the requests the page makes, so a failed submit stops being a mystery.
//
// This runs in the PAGE's own JavaScript context (manifest `world: "MAIN"`), which is the
// only place `window.fetch` and `XMLHttpRequest` can be wrapped — a normal content script
// lives in an isolated world and sees its own copies, not the page's.
//
// It exists because rounds of DOM-level debugging could not distinguish "the click never
// reached the handler" from "the handler ran and the server refused". The answer is in the
// network, not the DOM. It earned its keep immediately: the create request carried the
// right chapter name and came back 400, and it was firing twice.
//
// Read-only. It forwards a summary and never alters a request or a response.

(() => {
  const MAX_BODY = 600

  // LIVE CREDENTIALS pass through here, so redaction is the whole point of this section.
  //
  // Two lessons, both learned by leaking something. First, this site puts a Firebase JWT in
  // the query string, and that JWT carries the account's email and user id. Second — and
  // worse — the auth endpoints exchange a `refresh_token`, which is long-lived and is NOT
  // JWT-shaped, so a redactor that only knew about JWTs printed it in full.
  //
  // The real fix is the origin filter below: the credentials all live in third-party auth
  // and analytics traffic, which is also noise. Dropping anything not addressed to this
  // site removes the entire class of leak. Key-based redaction stays as a second layer,
  // because one narrow regex has already proved insufficient once.
  const SECRET_KEY = /(^|_|\b)(token|jwt|auth|authorization|key|secret|password|credential|session|cookie)s?($|_|\b)/i

  // Hosts whose traffic is only ever credentials or telemetry. Everything else is kept.
  //
  // This started out as a same-origin filter, which was wrong and hid the answer: this
  // site's own API is NOT served from the page's host — the account token's audience is a
  // different project entirely — so filtering to same-origin silently discarded the exact
  // request being investigated. Excluding known auth and analytics hosts keeps the
  // credentials out without deciding, in advance, where the API must live.
  const BORING_HOST = /(^|\.)(googleapis\.com|google\.com|gstatic\.com|firebaseio\.com|firebaseapp\.com|cloudflareinsights\.com|sentry\.io|doubleclick\.net)$/i

  function interesting(raw) {
    try {
      const u = new URL(raw, location.origin)
      if (BORING_HOST.test(u.hostname)) return false
      // Cloudflare's RUM beacon is same-origin but is pure telemetry.
      return !u.pathname.startsWith('/cdn-cgi/')
    } catch {
      return false
    }
  }

  function redactUrl(raw) {
    try {
      const u = new URL(raw, location.origin)
      for (const k of [...u.searchParams.keys()]) {
        if (SECRET_KEY.test(k)) u.searchParams.set(k, '<redacted>')
      }
      // Keep the HOST. It is not a secret, and hiding it is what stopped an API on another
      // domain from being recognised as the site's own.
      const sameHost = u.origin === location.origin
      return (sameHost ? '' : u.origin) + u.pathname + (u.search || '')
    } catch {
      return String(raw).replace(/([?&][^=&]*(?:token|auth|key|secret)[^=&]*=)[^&]+/gi,
                                 '$1<redacted>')
    }
  }

  function redactBody(text) {
    return String(text || '')
      // JSON keys whose NAME looks secret, whatever the value's shape.
      .replace(/("[^"]*(?:token|jwt|auth|key|secret|password|credential)[^"]*"\s*:\s*)"[^"]*"/gi,
               '$1"<redacted>"')
      // The same in a urlencoded body.
      .replace(/([^=&?\s]*(?:token|jwt|auth|key|secret|password)[^=&?\s]*=)[^&\s]+/gi,
               '$1<redacted>')
      // Bare JWTs, wherever they turn up.
      .replace(/eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/g, '<redacted-jwt>')
      // Email addresses: not a credential, but not ours to print either.
      .replace(/[\w.+-]+@[\w-]+\.[\w.]+/g, '<redacted-email>')
  }

  // Response bodies: every write, plus GETs against the site's own /app/ API.
  //
  // GET bodies were dropped wholesale once, because the icon endpoints return pages of JSON
  // that buried the request that mattered. But the /app/ GETs are how the page learns its
  // chapter list — ids included — and that list is the only way to address a chapter that
  // was created in an earlier session. Narrowing by path keeps the signal without the noise.
  const keepBody = (method, url) => {
    if (method !== 'GET') return true
    try {
      return new URL(url, location.origin).pathname.startsWith('/app/')
    } catch {
      return false
    }
  }

  const clip = (s) => {
    const text = redactBody(typeof s === 'string' ? s : '')
    return text.length > MAX_BODY ? `${text.slice(0, MAX_BODY)}…` : text
  }

  // The request body matters as much as the response: if the site sends the operation in
  // the body and ours goes out empty, that is the difference between a 200 and a 400.
  function describeSent(body) {
    if (body == null) return ''
    try {
      if (typeof body === 'string') return clip(body)
      if (body instanceof URLSearchParams) return clip(body.toString())
      if (body instanceof FormData) {
        return clip([...body.entries()]
          .map(([k, v]) => `${k}=${typeof v === 'string' ? v : '<file>'}`).join('&'))
      }
      if (body instanceof Blob) return `<blob ${body.size} bytes>`
      return clip(JSON.stringify(body))
    } catch {
      return '<unreadable body>'
    }
  }

  function report(entry) {
    try {
      window.postMessage({ source: 'nr-net', entry }, '*')
    } catch { /* a body that will not serialise is not worth failing over */ }
  }

  /**
   * Forward the chapter objects out of an API response, if that is what it is.
   *
   * A chapter can only be updated by PUTting its whole record back, so changing one created
   * in an earlier session means knowing its `id`, `uid` and the rest — and the only place
   * those exist is the list the page fetches for itself. The summary log clips bodies to a
   * few hundred characters, which would shred that list, so the objects travel on their own
   * channel, unclipped and uncollapsed.
   *
   * Matched by SHAPE rather than by path: anything carrying `id` and `displayName` is a
   * chapter, whichever endpoint served it.
   */
  function reportChapters(text) {
    try {
      const parsed = JSON.parse(text)
      const rows = Array.isArray(parsed) ? parsed
        : Array.isArray(parsed?.data) ? parsed.data
        : parsed?.data && typeof parsed.data === 'object' ? [parsed.data]
        : []
      const chapters = rows.filter((r) => r && r.id && typeof r.displayName === 'string')
      if (chapters.length) {
        window.postMessage({ source: 'nr-chapters', chapters }, '*')
      }
    } catch { /* not JSON, or not chapters — nothing to forward */ }
  }

  // --- calling the site's API for real -------------------------------------
  //
  // The site's chapter API takes everything in the query string, so a request is a template:
  // POST /app/chapter to create, PUT /app/chapter to set the fields and the content. Driving
  // that directly is far more reliable than clicking — no Vue state, no disabled buttons, no
  // dialog focus trap, no timing — and it is two requests instead of eight steps.
  //
  // The auth token is learned by WATCHING the page's own requests and is kept here, in the
  // page's context, where it already lives. It is never logged, never included in a report,
  // and never sent anywhere except back to the site it came from. The caller asks for an
  // operation; this function supplies the credential.
  let apiOrigin = ''
  let apiToken = ''

  function learn(rawUrl) {
    try {
      const u = new URL(rawUrl, location.origin)
      const token = u.searchParams.get('token')
      if (token && token.length > 40) {
        apiToken = token
        apiOrigin = u.origin
      }
    } catch { /* not a URL we can learn from */ }
  }

  window.addEventListener('message', async (e) => {
    if (e.source !== window || e.data?.source !== 'nr-api-call') return
    const { id, method, path, params, body } = e.data
    const reply = (out) => window.postMessage({ source: 'nr-api-reply', id, ...out }, '*')
    if (!apiToken || !apiOrigin) {
      reply({ ok: false, error: 'no API token seen yet — open a chapter page once so the '
        + 'page makes a request of its own, then try again' })
      return
    }
    try {
      const u = new URL(path, apiOrigin)
      for (const [k, v] of Object.entries(params || {})) {
        u.searchParams.set(k, String(v))
      }
      u.searchParams.set('token', apiToken)
      const res = await realFetch(u.toString(), {
        method,
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      })
      const text = await res.text()
      let parsed
      try {
        parsed = JSON.parse(text)
      } catch {
        parsed = { raw: text.slice(0, 400) }
      }
      reply({ ok: res.ok && parsed?.success !== false, status: res.status, data: parsed })
    } catch (err) {
      reply({ ok: false, error: String(err?.message || err) })
    }
  })

  // --- fetch ---------------------------------------------------------------
  const realFetch = window.fetch
  if (typeof realFetch === 'function') {
    window.fetch = function (...args) {
      const [input, init = {}] = args
      const url = typeof input === 'string' ? input : input?.url || ''
      // Third-party traffic is where the credentials are, and it tells us nothing about
      // this site's own API. Drop it before it is even summarised.
      learn(url)
      if (!interesting(url)) return realFetch.apply(this, args)
      const method = (init.method || input?.method || 'GET').toUpperCase()
      const sent = describeSent(init.body)
      const started = Date.now()
      return realFetch.apply(this, args).then(
        (res) => {
          // Clone so the page still gets to read its own body.
          const copy = res.clone()
          const base = { via: 'fetch', method, url: redactUrl(url), status: res.status,
                         ok: res.ok, ms: Date.now() - started, sent }
          if (!keepBody(method, url)) {
            report({ ...base, body: '' })
          } else {
            copy.text().then(
              (body) => {
                report({ ...base, body: clip(body) })
                reportChapters(body)
              },
              () => report({ ...base, body: '' }),
            )
          }
          return res
        },
        (err) => {
          report({ via: 'fetch', method, url: redactUrl(url), status: 0, ok: false,
                   ms: Date.now() - started, sent,
                   body: `network error: ${err?.message || err}` })
          throw err
        },
      )
    }
  }

  // --- XMLHttpRequest ------------------------------------------------------
  const open = XMLHttpRequest.prototype.open
  const send = XMLHttpRequest.prototype.send
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.__nr = { method: String(method || 'GET').toUpperCase(), url: String(url || '') }
    return open.call(this, method, url, ...rest)
  }
  XMLHttpRequest.prototype.send = function (...args) {
    const meta = this.__nr || { method: '?', url: '?' }
    learn(meta.url)
    if (!interesting(meta.url)) return send.apply(this, args)
    const sent = describeSent(args[0])
    const started = Date.now()
    this.addEventListener('loadend', () => {
      if (typeof this.responseText === 'string') reportChapters(this.responseText)
      report({
        via: 'xhr', method: meta.method, url: redactUrl(meta.url), status: this.status,
        ok: this.status >= 200 && this.status < 300, ms: Date.now() - started, sent,
        body: keepBody(meta.method, meta.url) && typeof this.responseText === 'string'
          ? clip(this.responseText) : '',
      })
    })
    return send.apply(this, args)
  }
})()
