import { useEffect, useState } from 'react'
import { api, type Meta, type Snapshot } from '../api'
import { useToast } from '../components/Toast'

function Send({ st, meta }: { st: Snapshot; meta: Meta }) {
  const toast = useToast()
  const [action, setAction] = useState('StatusNotification')
  const [body, setBody] = useState('{}')
  const [validate, setValidate] = useState(true)
  const [result, setResult] = useState<unknown>(null)
  const load = async (a: string) => {
    try { setBody(JSON.stringify(await api(`/api/stations/${st.id}/templates/${a}`), null, 2)) } catch (e) { toast((e as Error).message, 'error') }
  }
  useEffect(() => { void load(action) }, [action]) // eslint-disable-line react-hooks/exhaustive-deps
  const send = async () => {
    let payload: unknown
    try { payload = JSON.parse(body) } catch (e) { toast(`Invalid JSON: ${(e as Error).message}`, 'error'); return }
    try { setResult(await api(`/api/stations/${st.id}/send`, { method: 'POST', body: { action, payload, validate_schema: validate } })) } catch (e) { toast((e as Error).message, 'error') }
  }
  return (
    <div className="card stack">
      <h3>Send a message to the CSMS</h3>
      <small className="muted">All {(meta.versions?.[st.version]?.station_messages ?? meta.station_messages).length} messages a charging station can send in OCPP {st.version}. Edit the sample payload (spec camelCase) and send it; the CSMS's answer is shown below and in the live log. Turn validation off to send a deliberately broken message.</small>
      <div className="row">
        <label style={{ flex: 1, minWidth: 220 }}>Message<select value={action} onChange={(e) => setAction(e.target.value)}>{(meta.versions?.[st.version]?.station_messages ?? meta.station_messages).map((m) => <option key={m}>{m}</option>)}</select></label>
        <button className="ghost small" style={{ alignSelf: 'end' }} onClick={() => void load(action)}>Reset sample</button>
      </div>
      <textarea rows={12} value={body} onChange={(e) => setBody(e.target.value)} spellCheck={false} />
      <div className="row"><button onClick={() => void send()} disabled={!st.want_connected}>Send {action}</button><label className="inline"><input type="checkbox" checked={validate} onChange={(e) => setValidate(e.target.checked)} /> validate against the OCPP schema first</label></div>
      {result != null && <pre className="json">{JSON.stringify(result, null, 2)}</pre>}
    </div>
  )
}

function Rules({ st, meta }: { st: Snapshot; meta: Meta }) {
  const toast = useToast()
  const [direction, setDirection] = useState<'out' | 'in'>('out')
  const [action, setAction] = useState('*')
  const [effect, setEffect] = useState('delay')
  const [delay, setDelay] = useState(2000)
  const [code, setCode] = useState('InternalError')
  const [times, setTimes] = useState('')
  const [probability, setProbability] = useState(100)
  const effects = meta.rule_effects[direction]
  const ver = meta.versions?.[st.version]
  const actions = direction === 'out' ? (ver?.station_messages ?? meta.station_messages) : (ver?.commands ?? meta.commands)
  const add = async () => {
    const params: Record<string, unknown> = {}
    if (effect === 'delay') params.delay_ms = delay
    if (effect === 'error') params.code = code
    try {
      await api(`/api/stations/${st.id}/rules`, { method: 'POST', body: { direction, action, effect, params, times: times ? Number(times) : null, probability: probability / 100 } })
      toast('Rule added')
    } catch (e) { toast((e as Error).message, 'error') }
  }
  return (
    <div className="card stack">
      <h3>Message faults (rules)</h3>
      <small className="muted">Make individual messages misbehave. <b>Charger → CSMS</b>: delay, drop, duplicate or corrupt what the station sends. <b>CSMS → charger</b>: delay, ignore (timeout), refuse (Rejected), fail with a CALLERROR, or answer with an invalid response.</small>
      <div className="params">
        <label>Direction<select value={direction} onChange={(e) => { const d = e.target.value as 'out' | 'in'; setDirection(d); setEffect(meta.rule_effects[d][0]); setAction('*') }}><option value="out">Charger → CSMS</option><option value="in">CSMS → charger</option></select></label>
        <label>Message<select value={action} onChange={(e) => setAction(e.target.value)}><option value="*">Every message</option>{actions.map((a) => <option key={a}>{a}</option>)}</select></label>
        <label>Effect<select value={effect} onChange={(e) => setEffect(e.target.value)}>{effects.map((x) => <option key={x}>{x}</option>)}</select></label>
        {effect === 'delay' && <label>Delay (ms)<input type="number" min={1} value={delay} onChange={(e) => setDelay(Number(e.target.value))} /></label>}
        {effect === 'error' && <label>Error code<select value={code} onChange={(e) => setCode(e.target.value)}>{meta.error_codes.map((c) => <option key={c}>{c}</option>)}</select></label>}
        <label>Only the first … times<input type="number" min={1} placeholder="every time" value={times} onChange={(e) => setTimes(e.target.value)} /></label>
        <label>Probability: {probability}%<input type="range" min={1} max={100} value={probability} onChange={(e) => setProbability(Number(e.target.value))} /></label>
      </div>
      <div className="row"><button onClick={() => void add()}>Add rule</button>{st.rules.length > 0 && <button className="ghost small" onClick={() => void api(`/api/stations/${st.id}/rules`, { method: 'DELETE' })}>Remove all</button>}</div>
      {st.rules.length > 0 && (
        <table>
          <thead><tr><th>Direction</th><th>Message</th><th>Effect</th><th>Applies</th><th>Hits</th><th /></tr></thead>
          <tbody>{st.rules.map((r) => (
            <tr key={r.id} style={{ opacity: r.enabled ? 1 : 0.5 }}>
              <td>{r.direction === 'out' ? 'Charger → CSMS' : 'CSMS → charger'}</td><td className="mono">{r.action}</td>
              <td>{r.effect}{r.params.delay_ms ? ` ${r.params.delay_ms} ms` : ''}{r.params.code ? ` ${r.params.code}` : ''}</td>
              <td>{r.times ? `first ${r.times}×` : 'always'}{r.probability < 1 ? ` · ${Math.round(r.probability * 100)}%` : ''}{!r.enabled ? ' · used up' : ''}</td>
              <td>{r.hits}</td><td><button className="ghost small" onClick={() => void api(`/api/stations/${st.id}/rules/${r.id}`, { method: 'DELETE' })}>Remove</button></td>
            </tr>
          ))}</tbody>
        </table>
      )}
    </div>
  )
}

/** Run a CSMS command locally, as if your CSMS had sent it. Handy to try a charger with no CSMS at all. */
function Simulate({ st, meta }: { st: Snapshot; meta: Meta }) {
  const toast = useToast()
  const commands = meta.versions?.[st.version]?.commands ?? meta.commands
  const [action, setAction] = useState('RequestStartTransaction')
  const [body, setBody] = useState('{}')
  const [result, setResult] = useState<unknown>(null)
  const pick = commands.includes(action) ? action : commands[0]
  const load = async (a: string) => {
    try { setBody(JSON.stringify(await api(`/api/stations/${st.id}/templates/${a}?kind=command`), null, 2)) } catch (e) { toast((e as Error).message, 'error') }
  }
  useEffect(() => { void load(pick) }, [pick, st.version]) // eslint-disable-line react-hooks/exhaustive-deps
  const run = async () => {
    let payload: unknown
    try { payload = JSON.parse(body) } catch (e) { toast(`Invalid JSON: ${(e as Error).message}`, 'error'); return }
    try { setResult(await api(`/api/stations/${st.id}/inject`, { method: 'POST', body: { action: pick, payload } })) } catch (e) { toast((e as Error).message, 'error') }
  }
  return (
    <div className="card stack">
      <h3>Simulate a command from the CSMS</h3>
      <small className="muted">Runs the command on this charger exactly as if your CSMS had sent it, with no CSMS needed (it is marked “simulated” in the log). Start or stop a session remotely, change availability, reset, update firmware, set a charging profile, and so on.</small>
      <div className="row">
        <label style={{ flex: 1, minWidth: 220 }}>Command<select value={pick} onChange={(e) => setAction(e.target.value)}>{commands.map((c) => <option key={c}>{c}</option>)}</select></label>
        <button className="ghost small" style={{ alignSelf: 'end' }} onClick={() => void load(pick)}>Reset sample</button>
      </div>
      <textarea rows={9} value={body} onChange={(e) => setBody(e.target.value)} spellCheck={false} />
      <div className="row"><button onClick={() => void run()}>Run {pick}</button><small className="muted">e.g. UpdateFirmware runs the whole download, install and reboot flow</small></div>
      {result != null && <pre className="json">{JSON.stringify(result, null, 2)}</pre>}
    </div>
  )
}

const RAW_TEMPLATES: Record<string, Record<string, unknown>> = {
  '1.6': {
    BootNotification: [2, 'raw-1', 'BootNotification', { chargePointVendor: 'SimCharge', chargePointModel: 'SIM-AC', firmwareVersion: '1.0.0' }],
    Heartbeat: [2, 'raw-2', 'Heartbeat', {}],
    Authorize: [2, 'raw-3', 'Authorize', { idTag: 'CARD1' }],
    StartTransaction: [2, 'raw-4', 'StartTransaction', { connectorId: 1, idTag: 'CARD1', meterStart: 0, timestamp: '' }],
    StopTransaction: [2, 'raw-5', 'StopTransaction', { transactionId: 1, meterStop: 1000, timestamp: '' }],
    MeterValues: [2, 'raw-6', 'MeterValues', { connectorId: 1, meterValue: [{ timestamp: '', sampledValue: [{ value: '1000', measurand: 'Energy.Active.Import.Register', unit: 'Wh' }] }] }],
    StatusNotification: [2, 'raw-7', 'StatusNotification', { connectorId: 1, errorCode: 'NoError', status: 'Available', timestamp: '' }],
    CallError: [4, 'raw-8', 'FormationViolation', 'Malformed JSON payload', {}],
  },
  '2.x': {
    BootNotification: [2, 'raw-1', 'BootNotification', { reason: 'PowerUp', chargingStation: { model: 'SIM', vendorName: 'SimCharge' } }],
    Heartbeat: [2, 'raw-2', 'Heartbeat', {}],
    Authorize: [2, 'raw-3', 'Authorize', { idToken: { idToken: 'CARD1', type: 'ISO14443' } }],
    'TransactionEvent (Started)': [2, 'raw-4', 'TransactionEvent', { eventType: 'Started', timestamp: '', triggerReason: 'Authorized', seqNo: 0, transactionInfo: { transactionId: 'tx-raw-1' }, evse: { id: 1, connectorId: 1 } }],
    'TransactionEvent (Ended)': [2, 'raw-5', 'TransactionEvent', { eventType: 'Ended', timestamp: '', triggerReason: 'StopAuthorized', seqNo: 1, transactionInfo: { transactionId: 'tx-raw-1', stoppedReason: 'Local' }, evse: { id: 1, connectorId: 1 } }],
    MeterValues: [2, 'raw-6', 'MeterValues', { evseId: 1, meterValue: [{ timestamp: '', sampledValue: [{ value: 1000, measurand: 'Energy.Active.Import.Register', unitOfMeasure: { unit: 'Wh' } }] }] }],
    StatusNotification: [2, 'raw-7', 'StatusNotification', { timestamp: '', connectorStatus: 'Available', evseId: 1, connectorId: 1 }],
    CallError: [4, 'raw-8', 'FormationViolation', 'Malformed JSON payload', {}],
  },
}

/** Put any text on the WebSocket, valid or not. */
function Raw({ st }: { st: Snapshot }) {
  const toast = useToast()
  const templates = RAW_TEMPLATES[st.version === '1.6' ? '1.6' : '2.x']
  const stamp = (v: unknown): unknown => JSON.parse(JSON.stringify(v).replace(/"timestamp":""/g, `"timestamp":"${new Date().toISOString()}"`))
  const [text, setText] = useState(() => JSON.stringify(stamp(templates.Heartbeat)))
  const send = async () => {
    try { await api(`/api/stations/${st.id}/raw`, { method: 'POST', body: { frame: text } }); toast('Frame sent') } catch (e) { toast((e as Error).message, 'error') }
  }
  const format = () => { try { setText(JSON.stringify(JSON.parse(text), null, 2)) } catch (e) { toast(`Not valid JSON: ${(e as Error).message}`, 'error') } }
  return (
    <div className="card stack">
      <h3>Raw frame</h3>
      <small className="muted">Sends exactly this text on the WebSocket: no validation, no waiting for an answer (the reply appears in the log). Use it for edge cases such as malformed JSON, a wrong message type or a made-up CALLERROR.</small>
      <div className="quick">{Object.entries(templates).map(([name, frame]) => <button key={name} className="ghost small" onClick={() => setText(JSON.stringify(stamp(frame)))}>{name}</button>)}</div>
      <textarea rows={7} value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} style={{ fontFamily: 'var(--mono, monospace)' }} />
      <div className="row"><button onClick={() => void send()} disabled={!st.want_connected}>Send raw</button><button className="ghost small" onClick={format}>Format JSON</button></div>
    </div>
  )
}

export default function Messages({ st, meta }: { st: Snapshot; meta: Meta }) {
  return (
    <div className="stack">
      <div className="grid2" style={{ alignItems: 'start' }}><Send st={st} meta={meta} /><Rules st={st} meta={meta} /></div>
      <div className="grid2" style={{ alignItems: 'start' }}><Simulate st={st} meta={meta} /><Raw st={st} /></div>
    </div>
  )
}
