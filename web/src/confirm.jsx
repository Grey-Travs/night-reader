import { createContext, useCallback, useContext, useRef, useState } from 'react'
import { Modal } from './components/ui'

// Promise-based styled confirm, replacing window.confirm.
//   const confirm = useConfirm()
//   if (await confirm({ title, body, confirmLabel, danger })) { … }
const ConfirmCtx = createContext(async () => false)
export const useConfirm = () => useContext(ConfirmCtx)

export function ConfirmProvider({ children }) {
  const [state, setState] = useState(null)
  const resolver = useRef(null)

  const confirm = useCallback((opts) => new Promise((resolve) => {
    resolver.current = resolve
    setState(typeof opts === 'string' ? { body: opts } : (opts || {}))
  }), [])

  const close = (result) => {
    setState(null)
    const r = resolver.current
    resolver.current = null
    r?.(result)
  }

  return (
    <ConfirmCtx.Provider value={confirm}>
      {children}
      {state && (
        <Modal onClose={() => close(false)}>
          <div className="px-6 py-5">
            {state.title && <h3 className="mb-2 font-medium">{state.title}</h3>}
            <p className="whitespace-pre-line text-sm text-muted">{state.body}</p>
            <div className="mt-5 flex justify-end gap-2">
              <button onClick={() => close(false)} className="btn btn-ghost px-4 py-2 text-sm">{state.cancelLabel || 'Cancel'}</button>
              <button onClick={() => close(true)} className="btn btn-primary px-4 py-2 text-sm" style={state.danger ? { background: 'var(--danger)', color: '#fff' } : undefined}>{state.confirmLabel || 'Confirm'}</button>
            </div>
          </div>
        </Modal>
      )}
    </ConfirmCtx.Provider>
  )
}
