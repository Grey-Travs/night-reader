import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { copyText } from '../clipboard'
import { formatReport, toExplained } from '../errors'

// A modal rather than a toast, deliberately: these messages exist to be read and acted
// on, and a toast that vanishes in 2.6s can't carry three fix steps plus a traceback.
//
//   const showError = useError()
//   catch (e) { showError(e, { context: 'Detect pronouns', onRetry: run }) }

const ErrorCtx = createContext(() => {})
export const useError = () => useContext(ErrorCtx)

export function ErrorProvider({ children }) {
  const [current, setCurrent] = useState(null) // { e, context, onRetry, onAction }

  const show = useCallback((err, opts = {}) => {
    setCurrent({ e: toExplained(err), ...opts })
    return null
  }, [])

  return (
    <ErrorCtx.Provider value={show}>
      {children}
      {current && <ErrorDialog {...current} onClose={() => setCurrent(null)} />}
    </ErrorCtx.Provider>
  )
}

const ACTION_LABEL = {
  retry: 'Try again',
  'reconnect-google': 'Reconnect Google',
  settings: 'Open settings',
  setup: 'Run full setup',
}

export function ErrorDialog({ e, context = '', onRetry, onAction, onClose }) {
  const [copied, setCopied] = useState(false)
  const primaryRef = useRef(null)

  useEffect(() => {
    primaryRef.current?.focus()
    const onKey = (ev) => { if (ev.key === 'Escape') { ev.preventDefault(); onClose() } }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  async function copy() {
    const ok = await copyText(formatReport(e, context))
    setCopied(ok)
    setTimeout(() => setCopied(false), 1800)
  }

  // The action button only appears when we know what it should do: `retry` needs a
  // handler from the caller, the navigational ones can act on their own.
  const label = ACTION_LABEL[e.action]
  const act = e.action === 'retry' ? onRetry : onAction
  const showAction = !!label && (e.action !== 'retry' || !!onRetry)

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center p-4"
      style={{ background: 'color-mix(in oklab, var(--ink) 45%, transparent)' }}
      onClick={onClose}
      role="presentation"
    >
      <div
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="err-title"
        className="row-in max-h-[85vh] w-full max-w-lg overflow-y-auto rounded-card border p-5 shadow-lg"
        style={{ background: 'var(--elevated)', borderColor: 'var(--danger)' }}
        onClick={(ev) => ev.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-3">
          <h2 id="err-title" className="font-reading text-lg font-medium leading-snug">
            {e.title}
          </h2>
          <button onClick={onClose} className="tap -m-2 shrink-0 p-2 text-hint hover:text-ink" title="Close (Esc)">✕</button>
        </div>

        {context && <div className="mt-1 text-xs text-hint">while: {context}</div>}
        {e.what && <p className="mt-3 text-sm text-muted">{e.what}</p>}

        {e.fixes?.length > 0 && (
          <>
            <h3 className="mt-4 text-sm font-medium text-muted">Try this</h3>
            <ol className="mt-1.5 list-decimal space-y-1.5 pl-5 text-sm">
              {e.fixes.map((f, i) => <li key={i}>{f}</li>)}
            </ol>
          </>
        )}

        <div className="mt-5 flex flex-wrap items-center gap-2">
          {showAction && (
            <button
              ref={primaryRef}
              onClick={() => { onClose(); act?.() }}
              className="btn btn-primary px-4 py-2 text-sm"
            >
              {label}
            </button>
          )}
          <button
            ref={showAction ? undefined : primaryRef}
            onClick={onClose}
            className="btn btn-ghost px-4 py-2 text-sm"
          >
            Close
          </button>
          <button onClick={copy} className="btn btn-ghost ml-auto px-3 py-2 text-xs" title="Copy the full technical report">
            {copied ? 'Copied ✓' : 'Copy report'}
          </button>
        </div>

        {(e.detail || e.trace) && (
          <details className="mt-4">
            <summary className="cursor-pointer text-xs text-hint hover:text-accent-text">Technical details</summary>
            <pre className="sunken mt-2 max-h-52 overflow-auto p-3 font-mono text-[11px] leading-relaxed text-muted">
              {e.trace || e.detail}
            </pre>
          </details>
        )}
      </div>
    </div>
  )
}
