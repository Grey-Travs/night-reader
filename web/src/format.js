// Compact number formatting for the token/cost readouts.

export function fmtTokens(n) {
  const v = Number(n) || 0
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`
  if (v >= 1_000) return `${(v / 1_000).toFixed(1)}k`
  return String(v)
}

export function fmtCost(n) {
  const v = Number(n) || 0
  // Sub-cent totals read as "$0.00" otherwise, which looks like nothing was spent.
  if (v > 0 && v < 0.01) return '<$0.01'
  return `$${v.toFixed(2)}`
}

// Share of input that came from Claude's prompt cache rather than being re-sent. Worth
// surfacing here because the glossary is injected into every single call, so a high hit
// rate is the difference between that being nearly free and being the bulk of the spend.
export function cacheHitRate(tokens) {
  const t = tokens || {}
  const cached = Number(t.cache_read_input_tokens) || 0
  const fresh = (Number(t.input_tokens) || 0) + (Number(t.cache_creation_input_tokens) || 0)
  const total = cached + fresh
  if (!total) return null
  return cached / total
}

export function fmtPercent(x) {
  return x == null ? '—' : `${Math.round(x * 100)}%`
}

export function fmtDuration(seconds) {
  const s = Math.max(0, Math.floor(seconds))
  const m = Math.floor(s / 60)
  const h = Math.floor(m / 60)
  const pad = (n) => String(n).padStart(2, '0')
  return h > 0 ? `${h}:${pad(m % 60)}:${pad(s % 60)}` : `${m}:${pad(s % 60)}`
}
