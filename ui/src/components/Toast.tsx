import { createContext, useCallback, useContext, useState, type ReactNode } from 'react'

type Kind = 'ok' | 'error'
const Ctx = createContext<(text: string, kind?: Kind) => void>(() => undefined)
export const useToast = () => useContext(Ctx)

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toast, setToast] = useState<{ text: string; kind: Kind; id: number } | null>(null)
  const show = useCallback((text: string, kind: Kind = 'ok') => {
    const id = Date.now()
    setToast({ text, kind, id })
    setTimeout(() => setToast((t) => (t && t.id === id ? null : t)), kind === 'error' ? 6000 : 3000)
  }, [])
  return (
    <Ctx.Provider value={show}>
      {children}
      {toast && <div className={`toast ${toast.kind}`} role="status">{toast.text}</div>}
    </Ctx.Provider>
  )
}
