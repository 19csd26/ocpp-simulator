import { useState } from 'react'
import { api, type LogEntry, type Meta, type Run, type Snapshot } from '../api'
import Connectors from '../tabs/Connectors'
import Device from '../tabs/Device'
import Faults from '../tabs/Faults'
import Messages from '../tabs/Messages'
import Scenarios from '../tabs/Scenarios'
import State from '../tabs/State'
import AddStation from './AddStation'
import Confirm from './Confirm'
import LogPanel from './LogPanel'
import { useToast } from './Toast'

export const STATE_TONE: Record<string, string> = { online: 'on', booting: 'warn', connecting: 'warn', offline: 'bad', rebooting: 'warn', unpowered: 'bad', stopped: '' }
const TABS = ['Connectors', 'Test cases', 'Faults', 'Messages', 'Device model', 'State'] as const
type Tab = (typeof TABS)[number]

export default function Console({ st, meta, logs, run, onClearLog, onDeleted }: { st: Snapshot; meta: Meta; logs: LogEntry[]; run: Run | null; onClearLog: () => void; onDeleted: () => void }) {
  const toast = useToast()
  const [tab, setTab] = useState<Tab>('Connectors')
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [editing, setEditing] = useState(false)
  const post = async (path: string, ok: string) => { try { await api(`/api/stations/${st.id}/${path}`, { method: 'POST' }); toast(ok) } catch (e) { toast((e as Error).message, 'error') } }
  const busy = st.want_connected
  const chargers = st.evses.reduce((n, e) => n + e.connectors.length, 0)

  if (editing) return <AddStation meta={meta} edit={st} onCancel={() => setEditing(false)} onAdded={() => setEditing(false)} />

  return (
    <>
      <div className="page-title">
        <div className="stack" style={{ gap: 4 }}>
          <div className="row"><h2>{st.name}</h2><span className={`chip ${STATE_TONE[st.state] ?? ''}`}>{st.state}{st.boot_status && st.state !== 'online' ? ` · boot ${st.boot_status}` : ''}</span></div>
          <small className="muted mono">{st.ocpp_id} → {st.csms_url.replace(/\/+$/, '')}/{st.ocpp_id} · {st.vendor} {st.model} · fw {st.firmware_version} · {st.evses.length} EVSE / {chargers} connector{chargers > 1 ? 's' : ''}</small>
          {st.last_error && st.state !== 'online' && <small className="error">⚠ {st.last_error}</small>}
        </div>
        <span className="spacer" />
        {busy ? <button className="ghost" onClick={() => void post('disconnect', 'Disconnected')}>Disconnect</button> : <button onClick={() => void post('connect', 'Connecting…')}>Connect</button>}
        <button className="ghost" onClick={() => setEditing(true)}>Edit</button>
        <button className="danger" onClick={() => setConfirmDelete(true)}>Remove</button>
      </div>
      <div className="stats">
        <span className="chip">Sent <b>{st.stats.sent}</b></span><span className="chip">Received <b>{st.stats.received}</b></span>
        <span className={`chip ${st.stats.errors ? 'bad' : ''}`}>Errors <b>{st.stats.errors}</b></span><span className={`chip ${st.stats.timeouts ? 'warn' : ''}`}>Timeouts <b>{st.stats.timeouts}</b></span>
        <span className="chip">Reconnects <b>{st.stats.reconnects}</b></span><span className="chip">Heartbeat <b>{st.heartbeat_interval}s</b></span>
        <span className={`chip ${st.queue ? 'warn' : ''}`}>Offline queue <b>{st.queue}</b></span>{st.time_scale > 1 && <span className="chip info">Charging ×{st.time_scale}</span>}
        {st.faults.active.length > 0 && <span className="chip bad">{st.faults.active.length} fault{st.faults.active.length > 1 ? 's' : ''} active</span>}
        {st.notes.length > 0 && <small className="muted" style={{ alignSelf: 'center' }}>{st.notes[st.notes.length - 1]}</small>}
      </div>
      <div className="tabs" role="tablist">{TABS.map((t) => <button key={t} role="tab" className={t === tab ? 'active' : ''} onClick={() => setTab(t)}>{t}{t === 'Faults' && st.faults.active.length > 0 ? ` (${st.faults.active.length})` : ''}{t === 'Test cases' && run?.status === 'running' ? ' ●' : ''}</button>)}</div>

      {tab === 'Connectors' && <Connectors st={st} meta={meta} goFaults={() => setTab('Faults')} />}
      {tab === 'Test cases' && <Scenarios st={st} meta={meta} run={run} />}
      {tab === 'Faults' && <Faults st={st} meta={meta} />}
      {tab === 'Messages' && <Messages st={st} meta={meta} />}
      {tab === 'Device model' && <Device st={st} />}
      {tab === 'State' && <State st={st} />}

      <LogPanel stationId={st.id} entries={logs} onClear={onClearLog} />
      {confirmDelete && (
        <Confirm title={`Remove ${st.ocpp_id}?`} confirm="Remove" onNo={() => setConfirmDelete(false)} onYes={async () => { setConfirmDelete(false); try { await api(`/api/stations/${st.id}`, { method: 'DELETE' }); onDeleted() } catch (e) { toast((e as Error).message, 'error') } }}>
          The simulated charger disconnects and its state and message log are discarded. Charging sessions in progress simply stop sending messages.
        </Confirm>
      )}
    </>
  )
}
