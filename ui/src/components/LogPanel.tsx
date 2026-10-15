import { useEffect, useMemo, useRef, useState } from 'react'
import type { LogEntry } from '../api'

const time = (ts: number) => {
  const d = new Date(ts * 1000)
  return `${d.toLocaleTimeString([], { hour12: false })}.${String(d.getMilliseconds()).padStart(3, '0')}`
}
const arrow = (d: string) => (d === 'in' ? 'CSMS → CP' : d === 'out' ? 'CP → CSMS' : 'simulator')

/** Live OCPP message log: both directions, readable summaries, raw JSON on click. Always visible under the console. */
export default function LogPanel({ stationId, entries, onClear }: { stationId: string; entries: LogEntry[]; onClear: () => void }) {
  const [open, setOpen] = useState(true)
  const [big, setBig] = useState(false)
  const [paused, setPaused] = useState(false)
  const [follow, setFollow] = useState(true)
  const [text, setText] = useState('')
  const [dir, setDir] = useState('')
  const [action, setAction] = useState('')
  const [quiet, setQuiet] = useState(true)
  const [openRows, setOpenRows] = useState<Set<number>>(new Set())
  const [frozen, setFrozen] = useState<LogEntry[] | null>(null)
  const box = useRef<HTMLDivElement>(null)

  const source = paused ? (frozen ?? entries) : entries
  const actions = useMemo(() => [...new Set(entries.map((e) => e.action).filter(Boolean) as string[])].sort(), [entries])
  const shown = useMemo(() => {
    const q = text.trim().toLowerCase()
    return source.filter((e) => (!dir || e.dir === dir) && (!action || e.action === action) && !(quiet && e.action === 'Heartbeat') &&
      (!q || e.summary.toLowerCase().includes(q) || (e.action ?? '').toLowerCase().includes(q) || JSON.stringify(e.payload ?? '').toLowerCase().includes(q))).slice(-1200)
  }, [source, text, dir, action, quiet])

  useEffect(() => { if (follow && !paused && box.current) box.current.scrollTop = box.current.scrollHeight }, [shown, follow, paused, open, big])
  const toggle = (id: number) => setOpenRows((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n })

  return (
    <div className={`logpanel ${big ? 'big' : ''}`}>
      <div className="log-head">
        <b>Live OCPP log</b>
        <span className={`chip ${paused ? 'warn' : 'on'}`}>{paused ? 'Paused' : 'Streaming'}</span>
        <small className="muted">{shown.length} of {entries.length}</small>
        <span className="spacer" />
        {open && <>
          <input placeholder="Search messages…" value={text} onChange={(e) => setText(e.target.value)} style={{ width: 170 }} />
          <select value={dir} onChange={(e) => setDir(e.target.value)}><option value="">Both directions</option><option value="out">Sent by the charger</option><option value="in">Received from CSMS</option></select>
          <select value={action} onChange={(e) => setAction(e.target.value)}><option value="">All messages</option>{actions.map((a) => <option key={a}>{a}</option>)}</select>
          <label className="inline"><input type="checkbox" checked={quiet} onChange={(e) => setQuiet(e.target.checked)} /> hide heartbeats</label>
          <label className="inline"><input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} /> auto-scroll</label>
          <button className="ghost small" onClick={() => { setFrozen(paused ? null : entries); setPaused(!paused) }}>{paused ? 'Resume' : 'Pause'}</button>
          <button className="ghost small" onClick={onClear}>Clear</button>
          <a href={`/api/stations/${stationId}/log/export`}><button className="ghost small">Export</button></a>
          <button className="ghost small" onClick={() => setBig(!big)}>{big ? 'Smaller' : 'Larger'}</button>
        </>}
        <button className="ghost small" onClick={() => setOpen(!open)}>{open ? '▾ Hide' : '▴ Show'}</button>
      </div>
      {open && (
        <div className="terminal" ref={box}>
          {shown.length === 0 && <p className="muted" style={{ padding: 10 }}>No messages yet. Connect the station and they appear here the moment they are sent or received.</p>}
          {shown.map((e) => (
            <div key={e.id} className={`lr ${e.kind}`}>
              <button className="ll" onClick={() => toggle(e.id)}>
                <span className="lt">{time(e.ts)}</span>
                <span className={`ld ${e.dir}`}>{arrow(e.dir)}</span>
                <span className="la">{e.action ?? (e.kind === 'note' ? 'note' : '—')}</span>
                <span className="ls">{e.summary}{e.note && <span className="lnote"> · {e.note}</span>}</span>
                <span className="muted">{e.payload != null ? (openRows.has(e.id) ? '▾' : '▸') : ''}</span>
              </button>
              {openRows.has(e.id) && e.payload != null && <pre className="json lj">{JSON.stringify(e.payload, null, 2)}</pre>}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
