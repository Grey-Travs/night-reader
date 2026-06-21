import { createContext, useCallback, useContext, useRef, useState } from 'react'

// Lightweight toast notifications. `const toast = useToast(); toast('Saved ✓')`.
const ToastCtx = createContext(() => {})
export const useToast = () => useContext(ToastCtx)

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([])
  const idRef = useRef(0)

  const push = useCallback((message, kind = 'info') => {
    const id = (idRef.current += 1)
    setToasts((t) => [...t, { id, message, kind }])
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 2600)
  }, [])

  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="fixed bottom-4 right-4 z-[90] flex flex-col items-end gap-2">
        {toasts.map((t) => (
          <div
            key={t.id}
            className="row-in max-w-xs rounded-card border px-4 py-2.5 text-sm shadow-lg"
            style={{ background: 'var(--elevated)', color: 'var(--ink)', borderColor: t.kind === 'error' ? 'var(--danger)' : 'var(--border)' }}
          >
            {t.message}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  )
}
