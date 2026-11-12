import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'
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

// The missing-permission error is answered by the same sign-in, but asking for the WRITE
// permission. A plain reconnect would re-approve read-only access, which the saved token
// already has, so Google would never even show a consent screen and nothing would change.
const wantsWrite = (e) => e.code === 'google-scope'
const actionLabel = (e) =>
  e.action === 'reconnect-google' && wantsWrite(e) ? 'Allow writing to Docs' : ACTION_LABEL[e.action]

export function ErrorDialog({ e, context = '', onRetry, onAction, onClose }) {
  const [copied, setCopied] = useState(false)
  const [connecting, setConnecting] = useState(false)
  const [actionError, setActionError] = useState(null)
  const primaryRef = useRef(null)
  const open = useRef(true)
  const navigate = useNavigate()

  useEffect(() => {
    primaryRef.current?.focus()
    const onKey = (ev) => { if (ev.key === 'Escape') { ev.preventDefault(); onClose() } }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  useEffect(() => () => { open.current = false }, [])

  // Sign-in waits for the person to finish in Google's window, so the dialog stays open
  // and says so rather than vanishing as if it had worked. Once it has, the thing that
  // failed is tried again: that is what they were doing, and the reason it stopped is gone.
  async function reconnectGoogle() {
    setConnecting(true); setActionError(null)
    try {
      await api.googleLogin(wantsWrite(e))
      if (!open.current) return // closed while waiting: don't start anything behind their back
      onClose()
      onRetry?.()
    } catch (err) {
      if (!open.current) return
      setActionError(String(err?.message || err))
      setConnecting(false)
    }
  }

  async function copy() {
    const ok = await copyText(formatReport(e, context))
    setCopied(ok)
    setTimeout(() => setCopied(false), 1800)
  }

  // The action button only appears when something will actually happen. It used to show
  // for every known action and call `onAction`, which no caller ever passed, so "Reconnect
  // Google" closed the dialog and did nothing. Now the dialog handles the ones it can on
  // its own; a caller's `onAction` still wins, and `retry` still needs the caller's handler.
  const builtIn = {
    'reconnect-google': reconnectGoogle,
    settings: () => { onClose(); navigate('/settings') },
  }
  const label = actionLabel(e)
  const act = e.action === 'retry'
    ? (onRetry && (() => { onClose(); onRetry() }))
    : onAction ? () => { onClose(); onAction() } : builtIn[e.action]
  const showAction = !!label && !!act

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

        {connecting && (
          <p className="mt-4 text-sm" role="status">
            A Google window has opened on the computer running Night Reader. Approve it
            there{onRetry ? ', and this will carry on by itself' : ''}.
          </p>
        )}
        {actionError && <p className="mt-4 text-sm pill-review rounded-btn px-3 py-2">{actionError}</p>}

        <div className="mt-5 flex flex-wrap items-center gap-2">
          {showAction && (
            <button
              ref={primaryRef}
              onClick={act}
              disabled={connecting}
              className="btn btn-primary px-4 py-2 text-sm disabled:opacity-60"
            >
              {connecting ? 'Waiting for Google…' : label}
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
