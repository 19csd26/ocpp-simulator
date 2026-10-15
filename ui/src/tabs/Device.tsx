import { useEffect, useMemo, useState } from 'react'
import { api, type Snapshot } from '../api'
import { useToast } from '../components/Toast'

interface Variable { component: string; evse_id: number | null; connector_id: number | null; variable: string; value: string | null; data_type: string; mutability: string; unit: string | null; values_list: string | null }

export default function Device({ st }: { st: Snapshot }) {
  const toast = useToast()
  const [vars, setVars] = useState<Variable[]>([])
  const [q, setQ] = useState('')
  const [edits, setEdits] = useState<Record<string, string>>({})
  const load = () => api<Variable[]>(`/api/stations/${st.id}/variables`).then(setVars).catch(() => undefined)
  useEffect(() => { void load(); const t = setInterval(load, 5000); return () => clearInterval(t) }, [st.id]) // eslint-disable-line react-hooks/exhaustive-deps
  const key = (v: Variable) => `${v.component}|${v.evse_id ?? ''}|${v.connector_id ?? ''}|${v.variable}`
  const shown = useMemo(() => vars.filter((v) => !q || `${v.component} ${v.variable} ${v.value}`.toLowerCase().includes(q.toLowerCase())), [vars, q])
  const save = async (v: Variable) => {
    const value = edits[key(v)]
    if (value === undefined || value === v.value) return
    try { await api(`/api/stations/${st.id}/variables`, { method: 'PUT', body: { component: v.component, variable: v.variable, value, evse_id: v.evse_id } }); toast(`${v.component}.${v.variable} = ${value}`); setEdits(({ [key(v)]: _, ...rest }) => rest); void load() } catch (e) { toast((e as Error).message, 'error') }
  }
  return (
    <div className="card stack">
      <div className="row"><h3>Device model</h3><small className="muted">{vars.length} variables. The CSMS reads and changes these with GetVariables / SetVariables / GetBaseReport; you can change the read-write ones here too. Changes take effect immediately (heartbeat interval, meter sampling, start/stop points, …).</small><span className="spacer" /><input style={{ width: 220 }} placeholder="Filter…" value={q} onChange={(e) => setQ(e.target.value)} /></div>
      <div style={{ overflow: 'auto', maxHeight: '60vh' }}>
        <table>
          <thead><tr><th>Component</th><th>Variable</th><th>Value</th><th>Type</th><th>Access</th></tr></thead>
          <tbody>{shown.map((v) => (
            <tr key={key(v)}>
              <td className="mono">{v.component}{v.evse_id != null ? ` [${v.evse_id}${v.connector_id != null ? '/' + v.connector_id : ''}]` : ''}</td>
              <td className="mono">{v.variable}</td>
              <td style={{ minWidth: 220 }}>{v.mutability === 'ReadOnly' ? <span className="mono muted">{v.value ?? '—'}{v.unit ? ` ${v.unit}` : ''}</span> : (
                <input value={edits[key(v)] ?? v.value ?? ''} onChange={(e) => setEdits({ ...edits, [key(v)]: e.target.value })} onBlur={() => void save(v)} onKeyDown={(e) => e.key === 'Enter' && void save(v)} />)}</td>
              <td><small className="muted">{v.data_type}{v.values_list ? ` (${v.values_list.slice(0, 40)}${v.values_list.length > 40 ? '…' : ''})` : ''}</small></td>
              <td><small className="muted">{v.mutability}</small></td>
            </tr>
          ))}</tbody>
        </table>
      </div>
    </div>
  )
}
