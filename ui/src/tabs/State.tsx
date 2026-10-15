import type { Snapshot } from '../api'

const Section = ({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) => (
  <div className="card stack"><div className="row"><h3>{title}</h3>{hint && <small className="muted">{hint}</small>}</div>{children}</div>
)
const Empty = ({ text }: { text: string }) => <small className="muted">{text}</small>

/** What the CSMS has configured on the simulated charger through its commands. */
export default function State({ st }: { st: Snapshot }) {
  return (
    <div className="grid2" style={{ alignItems: 'start' }}>
      <Section title="Charging profiles" hint="SetChargingProfile">
        {st.profiles.length === 0 ? <Empty text="None installed." /> : (
          <table><thead><tr><th>ID</th><th>EVSE</th><th>Purpose</th><th>Stack</th><th>Limit</th></tr></thead>
            <tbody>{st.profiles.map((p) => <tr key={p.id}><td>{p.id}</td><td>{p.evse_id || 'station'}</td><td>{p.purpose}</td><td>{p.stack_level}</td><td className="mono">{p.periods.map((x) => `${x.limit} ${p.unit}@${x.start_period}s`).join(', ')}</td></tr>)}</tbody></table>
        )}
      </Section>
      <Section title="Reservations" hint="ReserveNow">
        {st.reservations.length === 0 ? <Empty text="None." /> : <table><thead><tr><th>ID</th><th>EVSE</th><th>Token</th><th>Expires</th></tr></thead><tbody>{st.reservations.map((r) => <tr key={r.id}><td>{r.id}</td><td>{r.evse_id ?? 'any'}</td><td>{r.token}</td><td>{new Date(r.expiry).toLocaleString()}</td></tr>)}</tbody></table>}
      </Section>
      <Section title="Offline queue" hint="TransactionEvents waiting for the CSMS">
        {st.tx_queue.length === 0 ? <Empty text="Empty – everything has been delivered." /> : <table><thead><tr><th>Event</th><th>Seq</th><th>Transaction</th><th>Time</th></tr></thead><tbody>{st.tx_queue.map((q, i) => <tr key={i}><td>{q.event_type}</td><td>{q.seq_no}</td><td className="mono">{q.transaction_id}</td><td>{new Date(q.timestamp).toLocaleTimeString()}</td></tr>)}</tbody></table>}
      </Section>
      <Section title="Local authorization list" hint={`version ${st.local_list.version} · ${st.local_list.count} entries`}>
        {st.local_list.entries.length === 0 ? <Empty text="Empty." /> : <table><thead><tr><th>Token</th><th>Status</th></tr></thead><tbody>{st.local_list.entries.map((e) => <tr key={e.token}><td className="mono">{e.token}</td><td>{e.status}</td></tr>)}</tbody></table>}
      </Section>
      <Section title="Authorization cache" hint="tokens the CSMS accepted">
        {st.cache.length === 0 ? <Empty text="Empty." /> : <div className="quick">{st.cache.map((c) => <span key={c.token} className="chip">{c.token}</span>)}</div>}
      </Section>
      <Section title="Certificates" hint="InstallCertificate / CertificateSigned">
        {st.certificates.length === 0 ? <Empty text="None installed." /> : <table><thead><tr><th>Type</th><th>Serial</th></tr></thead><tbody>{st.certificates.map((c, i) => <tr key={i}><td>{c.type}</td><td className="mono">{c.serial.slice(0, 24)}</td></tr>)}</tbody></table>}
      </Section>
      <Section title="Variable monitors" hint="SetVariableMonitoring">
        {st.monitors.length === 0 ? <Empty text="None." /> : <table><thead><tr><th>ID</th><th>Variable</th><th>Type</th><th>Threshold</th><th>State</th></tr></thead><tbody>{st.monitors.map((m) => <tr key={m.id}><td>{m.id}</td><td className="mono">{m.component.name}.{m.variable.name}</td><td>{m.type}</td><td>{m.value}</td><td>{m.breached ? <span className="chip bad">BREACHED</span> : 'ok'}</td></tr>)}</tbody></table>}
      </Section>
      <Section title="Display messages" hint="SetDisplayMessage">
        {st.display_messages.length === 0 ? <Empty text="None." /> : st.display_messages.map((m) => <div key={m.id}><span className="chip">{m.priority}</span> {m.message.content}</div>)}
      </Section>
      <Section title="Firmware and logs">
        <div className="row"><span className="chip">Version {st.firmware.version}</span><span className="chip info">Firmware: {st.firmware.status}</span><span className="chip info">Log upload: {st.log_upload.status}</span>{st.firmware.published > 0 && <span className="chip">{st.firmware.published} published</span>}</div>
        <small className="muted">Updates run the full status flow (Downloading → Downloaded → Installing → reboot → Installed). Arm “Firmware / log transfer will fail” under Faults to test failures.</small>
      </Section>
      <Section title="Network profiles" hint="SetNetworkProfile">
        {Object.keys(st.network_profiles).length === 0 ? <Empty text="None." /> : <pre className="json">{JSON.stringify(st.network_profiles, null, 2)}</pre>}
      </Section>
    </div>
  )
}
