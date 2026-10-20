import { lazy, Suspense, useEffect, useState } from 'react'
import AddStation from './components/AddStation'
import Console, { STATE_TONE } from './components/Console'
import Login from './components/Login'
import Signature from './components/Signature'
import { ToastProvider } from './components/Toast'
import { api } from './api'
import { useSim } from './useSim'
import { docsHash, parseDocsHash, type DocsRoute } from './docs/route'

const Docs = lazy(() => import('./docs/Docs'))

function Shell() {
  const sim = useSim()
  const [selected, setSelected] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const [docs, setDocs] = useState<DocsRoute | null>(() => parseDocsHash(window.location.hash))
  useEffect(() => {
    const sync = () => setDocs(parseDocsHash(window.location.hash))
    window.addEventListener('hashchange', sync)
    return () => window.removeEventListener('hashchange', sync)
  }, [])
  const openDocs = (page = 'README', anchor?: string) => { window.location.hash = docsHash(page, anchor) }
  const leaveDocs = () => { if (docs) history.pushState(null, '', window.location.pathname + window.location.search); setDocs(null) }
  const first = sim.order[0] ?? null
  const current = selected && sim.stations[selected] ? selected : first
  const st = current ? sim.stations[current] : null

  useEffect(() => { if (selected && !sim.stations[selected]) setSelected(null) }, [sim.stations, selected])

  return (
    <div className="app">
      <aside className="side">
        <div className="brand"><img className="brand-logo" src="/logo.svg" alt="Volkai logo" width={44} height={44} /><div><b>VOLKAI</b><small>OCPP 1.6 · 2.0.1</small></div></div>
        <div className="sim-link"><span className={`dot ${sim.connected ? 'on' : 'bad'}`} />{sim.connected ? 'Simulator backend connected' : 'Backend unreachable – retrying…'}</div>
        <button onClick={() => { setAdding(true); setSelected(null); leaveDocs() }}>＋ Add charger</button>
        <button className={`st-item ${docs ? 'active' : ''}`} onClick={() => openDocs()}><span className="t">📖 Docs &amp; guide</span><small>Features, diagrams, examples</small></button>
        {sim.order.map((id) => {
          const s = sim.stations[id]
          return (
            <button key={id} className={`st-item ${current === id && !adding && !docs ? 'active' : ''}`} onClick={() => { setSelected(id); setAdding(false); leaveDocs() }}>
              <span className="t"><span className={`dot ${s.state === 'online' ? 'on' : s.state === 'stopped' ? '' : STATE_TONE[s.state] === 'bad' ? 'bad' : 'warn'}`} />{s.name}</span>
              <small className="mono">{s.ocpp_id} · {s.state} · v{s.negotiated ?? (s.config.ocpp_version === 'auto' ? 'auto' : s.version)}</small>
              <small>{s.evses.reduce((n, e) => n + (e.tx ? 1 : 0), 0)} charging · {s.evses.length} EVSE{s.faults.active.length ? ` · ⚠ ${s.faults.active.length}` : ''}</small>
            </button>
          )
        })}
        <div className="spacer" />
        <small className="muted" style={{ padding: '0 6px', lineHeight: 1.5 }}>No database: chargers live in memory and your last entries are remembered in this browser. The API has no login, so keep it on localhost.</small>
        <Signature />
      </aside>
      <main className="main">
        {docs ? <Suspense fallback={<p className="muted">Loading the guide…</p>}><Docs route={docs} go={openDocs} /></Suspense> : !sim.meta ? <p className="muted">Loading…</p> : adding || !st ? (
          <>
            {!st && !adding && (
              <div className="welcome card">
                <h2>Simulate an OCPP 1.6 or 2.0.1 charge point</h2>
                <p className="muted" style={{ maxWidth: 560, lineHeight: 1.6 }}>Type the WebSocket URL of your CSMS and a charger ID, pick the hardware, and the simulator boots, brings every connector online and lets you run sessions, inject faults (power meter failure, power loss, network loss, …) and run test cases.</p>
                <button onClick={() => setAdding(true)}>＋ Add your first charger</button>
              </div>
            )}
            {(adding || !st) && <AddStation meta={sim.meta} onCancel={st ? () => setAdding(false) : undefined} onAdded={(s) => { setAdding(false); setSelected(s.id); void sim.refresh() }} />}
          </>
        ) : (
          <Console key={st.id} st={st} meta={sim.meta} logs={sim.logs[st.id] ?? []} run={sim.runs[st.id] ?? null} onClearLog={() => sim.clearLog(st.id)} onDeleted={() => { setSelected(null); void sim.refresh() }} />
        )}
        <Signature compact />
      </main>
    </div>
  )
}

/** Shows the login only when the server was started with SIM_TOKEN; otherwise straight to the simulator. */
function Gate() {
  const [state, setState] = useState<'checking' | 'login' | 'ok'>('checking')
  const check = () => api<{ required: boolean; authenticated: boolean }>('/api/auth').then((a) => setState(!a.required || a.authenticated ? 'ok' : 'login')).catch(() => setState('ok'))
  useEffect(() => {
    void check()
    const expired = () => setState('login')
    window.addEventListener('sim-unauthorized', expired)
    return () => window.removeEventListener('sim-unauthorized', expired)
  }, [])
  if (state === 'checking') return null
  return state === 'login' ? <Login onLoggedIn={() => setState('ok')} /> : <Shell />
}

export default function App() {
  return <ToastProvider><Gate /></ToastProvider>
}
