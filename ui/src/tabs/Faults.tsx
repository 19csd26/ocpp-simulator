import { useState } from 'react'
import { api, type FaultSpec, type Meta, type Param, type Snapshot } from '../api'
import { useToast } from '../components/Toast'

function ParamField({ p, value, onChange }: { p: Param; value: unknown; onChange: (v: unknown) => void }) {
  if (p.type === 'boolean') return <label className="inline" style={{ alignSelf: 'end' }}><input type="checkbox" checked={Boolean(value)} onChange={(e) => onChange(e.target.checked)} />{p.label}</label>
  if (p.type === 'select') return <label>{p.label}<select value={String(value)} onChange={(e) => onChange(e.target.value)}>{p.options!.map((o) => <option key={o}>{o}</option>)}</select></label>
  return <label>{p.label}<input type="number" min={p.min} max={p.max} value={String(value)} onChange={(e) => onChange(e.target.value === '' ? '' : Number(e.target.value))} /></label>
}

function FaultCard({ kind, spec, st }: { kind: string; spec: FaultSpec; st: Snapshot }) {
  const toast = useToast()
  const [params, setParams] = useState<Record<string, unknown>>(Object.fromEntries(spec.params.map((p) => [p.key, p.default])))
  const [scope, setScope] = useState(spec.scope === 'both' ? 0 : 1)
  const active = st.faults.active.filter((f) => f.kind === kind)
  const inject = async () => {
    try {
      await api(`/api/stations/${st.id}/faults`, { method: 'POST', body: { kind, scope: spec.scope === 'station' || scope === 0 ? null : scope, params } })
      toast(`${spec.title} ${spec.oneshot ? 'triggered' : 'injected'}`)
    } catch (e) { toast((e as Error).message, 'error') }
  }
  return (
    <div className={`card fault-card ${active.length ? 'active' : ''}`}>
      <div className="row"><h3>{spec.title}</h3><span className="spacer" />{spec.oneshot && <span className="chip">one-shot</span>}{active.length > 0 && <span className="chip bad">ACTIVE</span>}</div>
      <p className="muted" style={{ fontSize: 13, lineHeight: 1.5 }}>{spec.description}</p>
      <details><summary>What the CSMS should do</summary><p>{spec.expect}</p></details>
      <div className="params">
        {spec.scope !== 'station' && <label>{spec.scope === 'both' ? 'EVSE (or whole station)' : 'EVSE'}
          <select value={scope} onChange={(e) => setScope(Number(e.target.value))}>{spec.scope === 'both' && <option value={0}>Whole station</option>}{st.evses.map((e) => <option key={e.id} value={e.id}>EVSE {e.id}</option>)}</select></label>}
        {spec.params.map((p) => <ParamField key={p.key} p={p} value={params[p.key]} onChange={(v) => setParams({ ...params, [p.key]: v })} />)}
      </div>
      <div className="row">
        <button className="danger" onClick={() => void inject()}>{spec.oneshot ? 'Trigger' : 'Inject fault'}</button>
        {active.map((f) => <button key={f.id} className="ghost small" onClick={() => void api(`/api/stations/${st.id}/faults/${f.id}`, { method: 'DELETE' }).catch((e) => toast(e.message, 'error'))}>Clear{f.scope ? ` (EVSE ${f.scope})` : ''}</button>)}
      </div>
    </div>
  )
}

export default function Faults({ st, meta }: { st: Snapshot; meta: Meta }) {
  const toast = useToast()
  const groups = [...new Set(Object.values(meta.faults).map((f) => f.group))]
  return (
    <div className="stack">
      <div className="card stack">
        <div className="row"><h3>Active faults</h3><span className="spacer" />{st.faults.active.length > 0 && <button className="ghost small" onClick={() => void api(`/api/stations/${st.id}/faults`, { method: 'DELETE' }).then(() => toast('All faults cleared')).catch((e) => toast(e.message, 'error'))}>Clear all</button>}</div>
        {st.faults.active.length === 0 ? <small className="muted">Nothing is broken right now.</small> : (
          <div className="quick">{st.faults.active.map((f) => (
            <span key={f.id} className="fault-pill">⚠ {f.title}{f.scope ? ` · EVSE ${f.scope}` : ''}{f.ends_at ? ` · ends ${new Date(f.ends_at * 1000).toLocaleTimeString()}` : ''}
              <button className="danger" onClick={() => void api(`/api/stations/${st.id}/faults/${f.id}`, { method: 'DELETE' }).catch((e) => toast(e.message, 'error'))}>Clear</button></span>
          ))}</div>
        )}
        {st.faults.history.length > 0 && <small className="muted">Recently ended: {st.faults.history.slice(-6).reverse().map((h) => h.title).join(' · ')}</small>}
      </div>
      {groups.map((g) => (
        <div key={g} className="stack">
          <div className="group-h">{g}</div>
          <div className="grid2">{Object.entries(meta.faults).filter(([, f]) => f.group === g).map(([k, f]) => <FaultCard key={k} kind={k} spec={f} st={st} />)}</div>
        </div>
      ))}
    </div>
  )
}
