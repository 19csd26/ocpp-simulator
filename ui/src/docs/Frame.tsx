import { useEffect, useState, type ReactNode } from 'react'

/** Card around a diagram: title, kind, a full screen toggle and the usage hint. */
export function Frame({ title, kind, children }: { title: string; kind: string; children: (fullscreen: boolean) => ReactNode }) {
  const [full, setFull] = useState(false)
  useEffect(() => {
    if (!full) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setFull(false) }
    window.addEventListener('keydown', onKey)
    document.body.style.overflow = 'hidden'
    return () => { window.removeEventListener('keydown', onKey); document.body.style.overflow = '' }
  }, [full])
  return (
    <figure className={`vk-diagram ${full ? 'full' : ''}`}>
      <div className="vk-head">
        <span className="chip">{kind}</span>
        <b>{title}</b>
        <span className="spacer" />
        <small className="muted vk-hint">Drag to pan · Ctrl/⌘ + scroll to zoom</small>
        <button className="small ghost" onClick={() => setFull(!full)}>{full ? '✕ Close' : '⛶ Full screen'}</button>
      </div>
      {children(full)}
    </figure>
  )
}
