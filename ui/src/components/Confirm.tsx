import type { ReactNode } from 'react'

export default function Confirm({ title, children, confirm, onYes, onNo }: { title: string; children: ReactNode; confirm: string; onYes: () => void; onNo: () => void }) {
  return (
    <div className="modal-bg" onMouseDown={(e) => e.target === e.currentTarget && onNo()}>
      <div className="modal" role="alertdialog">
        <h3>{title}</h3>
        <div className="muted" style={{ fontSize: 13, lineHeight: 1.5 }}>{children}</div>
        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <button className="ghost" autoFocus onClick={onNo}>Cancel</button>
          <button className="danger" onClick={onYes}>{confirm}</button>
        </div>
      </div>
    </div>
  )
}
