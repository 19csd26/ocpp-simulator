import { useState } from 'react'
import { api, type EvseState, type Meta, type Snapshot } from '../api'
import { useToast } from '../components/Toast'

const STATE_CHIP: Record<string, string> = { Charging: 'on', EVConnected: 'info', SuspendedEV: 'warn', SuspendedEVSE: 'warn', Idle: '' }
const fmt = (s: number) => `${Math.floor(s / 3600) ? Math.floor(s / 3600) + ':' : ''}${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`

function EvseCard({ id, e, meta, faults, goFaults, version }: { id: string; e: EvseState; meta: Meta; faults: Snapshot['faults']['active']; goFaults: () => void; version: string }) {
  const toast = useToast()
  const [ev, setEv] = useState('mid')
  const [soc, setSoc] = useState(20)
  const [target, setTarget] = useState(100)
  const [connector, setConnector] = useState(e.connectors[0].id)
  const [token, setToken] = useState('CARD1')
  const [reason, setReason] = useState('Local')
  const [tokenType, setTokenType] = useState('ISO14443')
  const [v2gKw, setV2gKw] = useState(5)
  const [meterKwh, setMeterKwh] = useState('')
  const base = `/api/stations/${id}/evse/${e.id}`
  const act = async (path: string, body?: unknown, ok?: string) => {
    try {
      const r = await api<{ status?: string; ok?: boolean }>(`${base}/${path}`, { method: 'POST', body: body ?? {} })
      const bad = r && r.ok === false
      toast(bad ? `${path}: ${r.status}` : ok ?? `${path}: ${r.status ?? 'done'}`, bad ? 'error' : 'ok')
    } catch (err) { toast((err as Error).message, 'error') }
  }
  const quick = async (kind: string, params: Record<string, unknown> = {}) => {
    try { await api(`/api/stations/${id}/faults`, { method: 'POST', body: { kind, scope: e.id, params } }); toast(`${meta.faults[kind].title} injected on EVSE ${e.id}`) } catch (err) { toast((err as Error).message, 'error') }
  }
  const mine = faults.filter((f) => f.scope === e.id || f.scope === null)
  const tx = e.tx
  const charging = tx?.charging_state === 'Charging'

  return (
    <div className={`card evse ${charging ? 'charging' : ''} ${e.faulted ? 'faulted' : ''}`}>
      <div className="evse-head">
        <h3>EVSE {e.id}</h3>
        <span className="chip">{e.current_type} · {e.max_power_kw} kW{e.current_type === 'AC' ? ` · ${e.phases}φ` : ''}</span>
        {!e.operative && <span className="chip bad">Inoperative</span>}
        <span className="spacer" />
        <small className="muted mono" title="Lifetime energy register">{e.register_kwh.toFixed(1)} kWh · {e.temperature.toFixed(0)}°C</small>
      </div>
      <div className="row">
        {e.connectors.map((c) => (
          <span key={c.id} className={`conn s-${c.status}`} title={`Connector ${c.id}`}><i /><span style={{ color: 'var(--text)' }}>{c.id} · {c.type}</span><b>{c.status}</b>{c.plugged && ' 🔌'}{version === '1.6' && <small className="muted" title="OCPP 1.6 numbers the connectors of the whole charge point"> #{c.ocpp_id}</small>}</span>
        ))}
      </div>
      {mine.length > 0 && <div className="quick">{mine.map((f) => <span key={f.id} className="fault-pill">⚠ {f.title}<button className="danger" onClick={() => void api(`/api/stations/${id}/faults/${f.id}`, { method: 'DELETE' }).catch((x) => toast(x.message, 'error'))}>Clear</button></span>)}</div>}
      {e.reservation && <span className="chip" style={{ color: 'var(--violet)' }}>Reserved for {e.reservation.token} until {new Date(e.reservation.expiry).toLocaleTimeString()}</span>}
      {e.remote_pending && <span className="chip warn">Remote start accepted – waiting for a vehicle</span>}

      {e.plugged_connector === null ? (
        <div className="stack">
          <div className="row">
            <label style={{ flex: 2, minWidth: 190 }}>Vehicle
              <select value={ev} onChange={(x) => setEv(x.target.value)}>{Object.entries(meta.ev_models).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}</select>
            </label>
            {e.connectors.length > 1 && <label style={{ width: 120 }}>Connector<select value={connector} onChange={(x) => setConnector(Number(x.target.value))}>{e.connectors.map((c) => <option key={c.id} value={c.id}>{c.id} · {c.type}</option>)}</select></label>}
          </div>
          <div className="row">
            <label style={{ flex: 1, minWidth: 140 }}>Start SoC: <b>{soc}%</b><input type="range" min={0} max={99} value={soc} onChange={(x) => setSoc(Number(x.target.value))} /></label>
            <label style={{ flex: 1, minWidth: 140 }}>Charge to: <b>{target}%</b><input type="range" min={5} max={100} value={target} onChange={(x) => setTarget(Number(x.target.value))} /></label>
          </div>
          <div className="row">
            <button onClick={() => void act('plug-in', { connector, ev, soc, target_soc: target }, `Vehicle plugged into EVSE ${e.id}`)}>🔌 Plug in vehicle</button>
            <span className="muted" style={{ fontSize: 12 }}>or tap a card first:</span>
            <input style={{ width: 110 }} value={token} onChange={(x) => setToken(x.target.value)} />
            <select style={{ width: 120 }} value={tokenType} onChange={(x) => setTokenType(x.target.value)} title="Id token type">{meta.token_types.map((t) => <option key={t}>{t}</option>)}</select>
            <button className="ghost" onClick={() => void act('tap', { token, type: tokenType })}>Tap card</button>
          </div>
          {e.authorized && <span className="chip info">Authorised: {e.authorized} – start charging by plugging in</span>}
        </div>
      ) : (
        <div className="stack">
          {e.ev && (
            <div className="stack" style={{ gap: 6 }}>
              <div className="row"><b style={{ fontSize: 13 }}>{e.ev.label}</b><span className="spacer" /><span className="mono">{e.ev.soc.toFixed(1)}% → {e.ev.target_soc}%</span></div>
              <div className="soc"><i style={{ width: `${e.ev.soc}%` }} /><u style={{ left: `${e.ev.target_soc}%` }} /></div>
            </div>
          )}
          <div className="meter">
            <div><span>Power</span><b>{e.power_kw.toFixed(1)}</b> <small>kW</small></div>
            <div><span>Limit</span><b>{e.limit_kw.toFixed(1)}</b> <small>kW</small></div>
            <div><span>Session</span><b>{tx ? tx.energy_kwh.toFixed(3) : '0.000'}</b> <small>kWh</small></div>
          </div>
          <div className="meter">
            <div><span>Voltage</span><b>{e.voltage.toFixed(0)}</b> <small>V</small></div>
            <div><span>Current</span><b>{e.current_a.toFixed(1)}</b> <small>A</small></div>
            <div><span>Gun temp</span><b>{e.temperature.toFixed(1)}</b> <small>°C</small></div>
          </div>
          {e.v2x === 'discharge' && (
            <div className="meter v2g-live">
              <div><span>Feeding back</span><b>{e.export_power_kw.toFixed(1)}</b> <small>kW</small></div>
              <div><span>Session export</span><b>{tx ? tx.export_kwh.toFixed(3) : '0.000'}</b> <small>kWh</small></div>
              <div><span>Export register</span><b>{e.export_register_kwh.toFixed(1)}</b> <small>kWh</small></div>
            </div>
          )}
          {tx ? (
            <div className="row">
              <span className={`chip ${STATE_CHIP[tx.charging_state] ?? ''}`}>{tx.charging_state}</span>
              <small className="muted mono">tx {tx.id.slice(0, 8)} · seq {tx.seq_no} · {fmt(tx.duration_s)}{tx.id_token ? ` · ${tx.id_token}` : ''}{tx.total_cost != null ? ` · cost ${tx.total_cost}` : ''}</small>
            </div>
          ) : (
            <div className="row"><span className="chip info">{e.authorized ? `Authorised (${e.authorized})` : 'Vehicle connected – waiting for authorisation'}</span></div>
          )}
          <div className="row">
            <input style={{ width: 110 }} value={token} onChange={(x) => setToken(x.target.value)} aria-label="card id" />
            <select style={{ width: 120 }} value={tokenType} onChange={(x) => setTokenType(x.target.value)} title="Id token type">{meta.token_types.map((t) => <option key={t}>{t}</option>)}</select>
            <button className="ghost" onClick={() => void act('tap', { token, type: tokenType })}>{tx ? 'Tap (stops if same card)' : 'Tap card'}</button>
            {tx && <>
              <select style={{ width: 150 }} value={reason} onChange={(x) => setReason(x.target.value)}>{meta.stop_reasons.map((r) => <option key={r}>{r}</option>)}</select>
              <button className="warn small" onClick={() => void act('stop', { reason }, 'Session stopped')}>Stop session</button>
            </>}
          </div>
          {tx && e.ev?.bidirectional && (
            <div className="row v2g">
              <b style={{ fontSize: 12 }}>V2G</b>
              <label className="inline" style={{ gap: 4 }}>feed back <input type="number" min={1} max={e.ev.max_discharge_kw} step={1} style={{ width: 70 }} value={v2gKw} onChange={(x) => setV2gKw(Number(x.target.value))} /> kW</label>
              {e.v2x === 'discharge'
                ? <button className="small" onClick={() => void act('v2x', { mode: 'charge' }, 'Back to charging (G2V)')}>⇩ Charge (G2V)</button>
                : <button className="small" onClick={() => void act('v2x', { mode: 'discharge', power_kw: v2gKw }, `Feeding ${v2gKw} kW back (V2G)`)}>⇧ Discharge (V2G)</button>}
              <small className="muted">this vehicle can give up to {e.ev.max_discharge_kw} kW</small>
            </div>
          )}
          <div className="row">
            <button className="ghost small" onClick={() => void act('ev', { ready: !(e.ev?.ready ?? true) })}>{e.ev?.ready === false ? 'Resume vehicle' : 'Vehicle pauses'}</button>
            <button className="ghost small" onClick={() => void act('ev', { soc: Math.min(100, (e.ev?.soc ?? 0) + 10) })}>SoC +10</button>
            <button className="ghost small" onClick={() => void act('ev', { target_soc: e.ev?.soc ?? 100 })} title="Make the battery full">Make full</button>
            <span className="spacer" />
            <button className="danger small" onClick={() => void act('unplug', {}, 'Vehicle disconnected')}>Unplug</button>
          </div>
        </div>
      )}
      <div className="divider" />
      <div className="row">
        <small className="muted">Meter register</small>
        <input style={{ width: 110 }} type="number" min={0} step="0.1" placeholder={`${e.register_kwh.toFixed(1)} kWh`} value={meterKwh} onChange={(x) => setMeterKwh(x.target.value)} aria-label="meter register in kWh" />
        <button className="ghost small" disabled={meterKwh === ''} onClick={() => void act('meter', { import_kwh: Number(meterKwh) }, `Meter register set to ${meterKwh} kWh`).then(() => setMeterKwh(''))}>Set</button>
        <small className="muted">to see how the CSMS handles a jump or a rollback</small>
      </div>
      <div className="quick">
        <small className="muted" style={{ alignSelf: 'center' }}>Inject:</small>
        <button className="danger small" onClick={() => void quick('power_meter_failure', { mode: 'stop_reporting' })}>Meter failure</button>
        <button className="danger small" onClick={() => void quick('over_current')}>Over-current</button>
        <button className="danger small" onClick={() => void quick('ground_fault')}>Ground fault</button>
        <button className="danger small" onClick={() => void quick('emergency_stop')}>Emergency stop</button>
        <button className="ghost small" onClick={goFaults}>More…</button>
      </div>
    </div>
  )
}

export default function Connectors({ st, meta, goFaults }: { st: Snapshot; meta: Meta; goFaults: () => void }) {
  const toast = useToast()
  const [bootReason, setBootReason] = useState('PowerUp')
  const reboot = async () => {
    try { await api(`/api/stations/${st.id}/reboot`, { method: 'POST', body: { reason: bootReason } }); toast(`Rebooting (${bootReason})`) } catch (err) { toast((err as Error).message, 'error') }
  }
  const station = async (kind: string, params: Record<string, unknown>, label: string) => {
    try { await api(`/api/stations/${st.id}/faults`, { method: 'POST', body: { kind, params } }); toast(label) } catch (err) { toast((err as Error).message, 'error') }
  }
  return (
    <div className="stack">
      <div className="card row">
        <b>Whole station:</b>
        <button className="danger small" onClick={() => void station('power_loss', { duration_s: 15 }, 'Power cut for 15 s')}>⚡ Power loss (15 s)</button>
        <button className="danger small" onClick={() => void station('network_loss', { duration_s: 20 }, 'Network cut for 20 s')}>📡 Network loss (20 s)</button>
        <button className="danger small" onClick={() => void station('reboot_watchdog', {}, 'Watchdog reboot')}>↻ Watchdog reboot</button>
        <button className="danger small" onClick={() => void station('emergency_stop', {}, 'Emergency stop pressed')}>⛔ Emergency stop</button>
        <span className="spacer" />
        {st.version !== '1.6' && <select style={{ width: 150 }} value={bootReason} onChange={(x) => setBootReason(x.target.value)} title="The reason sent in the next BootNotification">{meta.boot_reasons.map((r) => <option key={r}>{r}</option>)}</select>}
        <button className="ghost small" onClick={() => void reboot()} disabled={!st.want_connected}>↻ Reboot{st.version === '1.6' ? '' : ' with this reason'}</button>
        <small className="muted">Every connector is reported to the CSMS as soon as the boot is accepted.</small>
      </div>
      <div className="evse-grid">{st.evses.map((e) => <EvseCard key={e.id} id={st.id} e={e} meta={meta} faults={st.faults.active} goFaults={goFaults} version={st.version} />)}</div>
    </div>
  )
}
