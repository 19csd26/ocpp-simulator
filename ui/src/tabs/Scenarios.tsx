import { useState } from 'react'
import { api, type Meta, type Run, type Snapshot } from '../api'
import { useToast } from '../components/Toast'

const ICON: Record<string, string> = { passed: '✓', failed: '✕', running: '', pending: '·', skipped: '–' }
const EXAMPLE = JSON.stringify([
  { do: 'plug_in', evse: 1, ev: 'mid', soc: 30 },
  { do: 'tap', evse: 1, token: 'CARD1' },
  { expect: 'tx_state', evse: 1, state: 'Charging', timeout: 20, label: 'Charging starts' },
  { do: 'inject', fault: 'power_meter_failure', scope: 1, params: { mode: 'frozen' } },
  { do: 'wait', seconds: 6 },
  { expect: 'message_sent', action: 'NotifyEvent', where: { 'eventData.0.component.name': 'LocalEnergyMeter' } },
  { do: 'clear', fault: 'all' },
  { do: 'tap', evse: 1 },
  { do: 'unplug', evse: 1 },
], null, 2)

function RunView({ run }: { run: Run }) {
  const done = run.steps.filter((s) => s.status === 'passed').length
  return (
    <div className="card stack">
      <div className="row"><h3>{run.title}</h3><span className={`chip ${run.status === 'passed' ? 'on' : run.status === 'failed' ? 'bad' : run.status === 'running' ? 'info' : 'warn'}`}>{run.status.toUpperCase()}</span><span className="spacer" /><small className="muted">{done}/{run.steps.length} steps passed</small></div>
      <div>{run.steps.map((s) => (
        <div className="run-step" key={s.index}>
          <span className={`ic ${s.status}`}>{ICON[s.status]}</span>
          <div>{s.label}{s.detail && <small className={s.status === 'failed' ? 'error' : 'muted'}>{s.detail}</small>}</div>
          <small className="muted mono">{s.seconds ? `${s.seconds}s` : ''}</small>
        </div>
      ))}</div>
    </div>
  )
}

export default function Scenarios({ st, meta, run }: { st: Snapshot; meta: Meta; run: Run | null }) {
  const toast = useToast()
  const [custom, setCustom] = useState(EXAMPLE)
  const [title, setTitle] = useState('My scenario')
  const current = run ?? st.scenario
  const running = current?.status === 'running'
  const groups = [...new Set(meta.scenarios.map((s) => s.group))]
  const start = async (path: string, body?: unknown) => {
    try { await api(path, { method: 'POST', body: body ?? {} }) } catch (e) { toast((e as Error).message, 'error') }
  }
  let parsed: unknown[] | null = null
  let parseError = ''
  try { parsed = JSON.parse(custom); if (!Array.isArray(parsed)) throw new Error('A scenario is a JSON array of steps') } catch (e) { parseError = (e as Error).message }

  return (
    <div className="stack">
      {current && (
        <div className="stack">
          <div className="row">{running && <button className="danger" onClick={() => void start(`/api/stations/${st.id}/scenarios/stop`)}>Stop scenario</button>}<small className="muted">{running ? 'Running – watch the live log below.' : 'Last run'}</small></div>
          <RunView run={current} />
        </div>
      )}
      <div className="card stack">
        <h3>Test cases</h3>
        <small className="muted">Each scenario drives the simulated charger and checks what it sent and how the CSMS answered. It passes only if every step passes. Scenarios that wait for you (remote start) tell you when.</small>
      </div>
      {groups.map((g) => (
        <div className="stack" key={g}>
          <div className="group-h">{g}</div>
          <div className="grid2">{meta.scenarios.filter((s) => s.group === g).map((s) => (
            <div className="card stack" key={s.id}>
              <div className="row"><h3>{s.title}</h3><span className="spacer" /><small className="muted">{s.steps} steps</small></div>
              <p className="muted" style={{ fontSize: 13, lineHeight: 1.5 }}>{s.description}</p>
              <div className="row"><button disabled={running || !st.want_connected && s.id !== 'smoke_boot'} title={!st.want_connected ? 'Connect the station first' : ''} onClick={() => void start(`/api/stations/${st.id}/scenarios/${s.id}/run`)}>▶ Run</button></div>
            </div>
          ))}</div>
        </div>
      ))}
      <div className="group-h">Your own</div>
      <div className="card stack">
        <h3>Custom scenario (JSON)</h3>
        <small className="muted">Actions: <span className="mono">{meta.scenario_steps.do.join(', ')}</span><br />Checks: <span className="mono">{meta.scenario_steps.expect.join(', ')}</span>. Add <span className="mono">"timeout"</span> to a check and <span className="mono">"continue_on_fail": true</span> to keep going after a failure.</small>
        <label>Title<input value={title} onChange={(e) => setTitle(e.target.value)} /></label>
        <textarea rows={14} value={custom} onChange={(e) => setCustom(e.target.value)} spellCheck={false} />
        {parseError && <small className="error">{parseError}</small>}
        <div className="row"><button disabled={running || !parsed} onClick={() => void start(`/api/stations/${st.id}/scenarios/custom`, { title, steps: parsed })}>▶ Run custom scenario</button></div>
      </div>
    </div>
  )
}
