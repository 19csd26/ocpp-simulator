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

export default function Messages({ st, meta }: { st: Snapshot; meta: Meta }) {
  return <div className="grid2" style={{ alignItems: 'start' }}><Send st={st} meta={meta} /><Rules st={st} meta={meta} /></div>
}
