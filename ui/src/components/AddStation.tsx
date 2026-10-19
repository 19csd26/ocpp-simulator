import { useMemo, useState, type FormEvent } from 'react'
import { api, type Meta, type Snapshot } from '../api'
import { useToast } from './Toast'

interface Hw { current_type: 'AC' | 'DC'; max_power_kw: number; voltage: number; phases: number; connectors: { type: string }[] }
interface Saved { name: string; ocpp_id: string; csms_url: string; preset: string; security_profile: number; ocpp_version?: Version }
type Version = 'auto' | '2.0.1' | '1.6'
const PROTOCOL: Record<Version, string> = { auto: 'ocpp2.0.1 or ocpp1.6, whichever the CSMS accepts', '2.0.1': 'ocpp2.0.1', '1.6': 'ocpp1.6' }

interface Preset { name: string; url: string }
const PRESET_KEY = 'sim.csms-presets'
const loadPresets = (): Preset[] => { try { return JSON.parse(localStorage.getItem(PRESET_KEY) ?? '[]') } catch { return [] } }

const KEY = 'sim.saved'
const load = (): Saved[] => { try { return JSON.parse(localStorage.getItem(KEY) ?? '[]') } catch { return [] } }

/** The station is defined by exactly two things you type: the CSMS WebSocket URL and the charger id. */
export default function AddStation({ meta, onAdded, onCancel, edit }: { meta: Meta; onAdded: (s: Snapshot) => void; onCancel?: () => void; edit?: Snapshot }) {
  const toast = useToast()
  const saved = useMemo(() => (edit ? [] : load()), [edit])
  const last = saved[0]
  const cfg = edit?.config
  const [url, setUrl] = useState(edit?.csms_url ?? last?.csms_url ?? 'ws://localhost:8000/ocpp')
  const [chargerId, setChargerId] = useState(edit?.ocpp_id ?? last?.ocpp_id ?? 'CP001')
  const [name, setName] = useState(edit?.name ?? last?.name ?? '')
  const [password, setPassword] = useState('')
  const [dropPassword, setDropPassword] = useState(false)
  const [profile, setProfile] = useState(cfg?.security_profile ?? last?.security_profile ?? 1)
  const [version, setVersion] = useState<Version>(cfg?.ocpp_version ?? last?.ocpp_version ?? 'auto')
  const [verify, setVerify] = useState(cfg?.verify_tls ?? true)
  const [preset, setPreset] = useState(last?.preset ?? 'ac_22_dual')
  const [custom, setCustom] = useState<Hw[] | null>((cfg?.hardware as Hw[] | undefined) ?? null)
  const [hwTouched, setHwTouched] = useState(false)
  const [scale, setScale] = useState(edit?.time_scale ?? 1)
  const [bootOnReconnect, setBootOnReconnect] = useState(cfg?.boot_on_reconnect ?? true)
  const [autoCharge, setAutoCharge] = useState(cfg?.auto_charge ?? false)
  const [vehicleId, setVehicleId] = useState(cfg?.vehicle_id ?? 'VID:FCA47A10DC70')
  const [smartPlug, setSmartPlug] = useState(cfg?.smart_plug ?? false)
  const [finishing, setFinishing] = useState(cfg?.finishing_mode ?? true)
  const [vendor, setVendor] = useState(cfg?.vendor ?? '')
  const [modelName, setModelName] = useState(cfg?.model ?? '')
  const [serial, setSerial] = useState(cfg?.serial_number ?? '')
  const [firmware, setFirmware] = useState(cfg?.firmware_version ?? '')
  const [iccid, setIccid] = useState(cfg?.iccid ?? '')
  const [imsi, setImsi] = useState(cfg?.imsi ?? '')
  const [boxSerial, setBoxSerial] = useState(cfg?.charge_box_serial_number ?? '')
  const [meterType, setMeterType] = useState(cfg?.meter_type ?? '')
  const [meterSerial, setMeterSerial] = useState(cfg?.meter_serial_number ?? '')
  const [presets, setPresets] = useState<Preset[]>(loadPresets)
  const savePreset = () => {
    const name = window.prompt('Name for this CSMS address (for example "dev server"):', new URL(url.replace(/^ws/, 'http')).hostname)
    if (!name) return
    const next = [{ name, url: url.trim() }, ...presets.filter((p) => p.name !== name)].slice(0, 20)
    setPresets(next)
    localStorage.setItem(PRESET_KEY, JSON.stringify(next))
  }
  const dropPreset = (name: string) => { const next = presets.filter((p) => p.name !== name); setPresets(next); localStorage.setItem(PRESET_KEY, JSON.stringify(next)) }
  const [endOnFull, setEndOnFull] = useState(cfg?.end_on_full ?? true)
  const locked = !!edit?.want_connected  // identity, credentials and hardware can only change while disconnected
  const [connect, setConnect] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const hw: Hw[] = custom ?? (meta.presets[preset].evses as Hw[])
  const target = `${url.replace(/\/+$/, '')}/${chargerId || '<charger id>'}`
  const secure = url.startsWith('wss://')
  const setHw = (i: number, patch: Partial<Hw>) => { setHwTouched(true); setCustom(hw.map((e, j) => (i === j ? { ...e, ...patch } : e))) }

  /** Only the identity fields that were filled in are sent: an empty one keeps the default. */
  const identity = () => Object.fromEntries(Object.entries({ vendor, model: modelName, serial_number: serial, firmware_version: firmware, iccid, imsi, charge_box_serial_number: boxSerial, meter_type: meterType, meter_serial_number: meterSerial }).filter(([, v]) => v.trim() !== '').map(([k, v]) => [k, v.trim()]))

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      if (edit) {
        const body: Record<string, unknown> = { name: name.trim() || `Simulator ${chargerId}`, time_scale: scale, boot_on_reconnect: bootOnReconnect, end_on_full: endOnFull, auto_charge: autoCharge, vehicle_id: vehicleId, finishing_mode: finishing }
        if (!locked) {
          Object.assign(body, { ocpp_id: chargerId.trim(), csms_url: url.trim(), security_profile: profile, ocpp_version: version, verify_tls: verify, smart_plug: smartPlug, ...identity() })
          if (password || dropPassword) body.password = dropPassword ? '' : password
          if (hwTouched) Object.assign(body, custom ? { evses: custom } : { preset })
        }
        const snap = await api<Snapshot>(`/api/stations/${edit.id}`, { method: 'PATCH', body })
        toast(`Saved ${snap.ocpp_id}`)
        onAdded(snap)
        setBusy(false)
        return
      }
      const snap = await api<Snapshot>('/api/stations', {
        method: 'POST',
        body: { name: name.trim() || `Simulator ${chargerId}`, ocpp_id: chargerId.trim(), csms_url: url.trim(), password: password || null, security_profile: profile, ocpp_version: version, verify_tls: verify, auto_charge: autoCharge, vehicle_id: vehicleId, smart_plug: smartPlug, finishing_mode: finishing, ...identity(), preset, evses: custom, time_scale: scale, connect },
      })
      const entry: Saved = { name: name.trim(), ocpp_id: chargerId.trim(), csms_url: url.trim(), preset, security_profile: profile, ocpp_version: version }
      localStorage.setItem(KEY, JSON.stringify([entry, ...saved.filter((s) => !(s.ocpp_id === entry.ocpp_id && s.csms_url === entry.csms_url))].slice(0, 8)))
      toast(`Station ${snap.ocpp_id} added${connect ? ' – connecting…' : ''}`)
      onAdded(snap)
    } catch (err) {
      setError((err as Error).message)
    }
    setBusy(false)
  }

  return (
    <form className="card form" onSubmit={submit}>
      <div className="row"><h3>{edit ? `Edit ${edit.name}` : 'Add a simulated charger'}</h3><span className="spacer" />{onCancel && !edit && <button type="button" className="ghost small" onClick={onCancel}>Close</button>}</div>

      {locked && <p className="info">The charger is connected. Disconnect it to change the URL, charger ID, credentials or hardware; the name, speed-up and behaviour options can be saved now.</p>}
      <div className="cols">
        <label>WebSocket URL of the CSMS
          <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="ws://localhost:8000/ocpp" required spellCheck={false} disabled={locked} />
        </label>
        <label>Charger ID
          <input value={chargerId} disabled={locked} onChange={(e) => setChargerId(e.target.value)} placeholder="CP001" pattern="[A-Za-z0-9_.\-]{1,60}" required spellCheck={false} />
        </label>
      </div>
      <div className="row" style={{ gap: 6 }}>
        <small className="muted">Saved CSMS:</small>
        <select style={{ width: 220 }} value="" disabled={locked} onChange={(e) => { if (e.target.value) setUrl(e.target.value) }} aria-label="saved CSMS addresses">
          <option value="">{presets.length ? 'pick a saved address…' : 'none saved yet'}</option>
          {presets.map((p) => <option key={p.name} value={p.url}>{p.name} · {p.url.replace(/^wss?:\/\//, '')}</option>)}
        </select>
        <button type="button" className="ghost small" disabled={locked || !url.trim()} onClick={savePreset}>Save this address</button>
        {presets.some((p) => p.url === url.trim()) && <button type="button" className="ghost small" onClick={() => dropPreset(presets.find((p) => p.url === url.trim())!.name)}>Forget it</button>}
      </div>
      <div className="url-preview mono">Connects to: {target} · subprotocol {PROTOCOL[version]}</div>

      {saved.length > 0 && (
        <div className="saved">
          <small className="muted" style={{ alignSelf: 'center' }}>Recent:</small>
          {saved.map((s) => (
            <button type="button" key={s.csms_url + s.ocpp_id} className="ghost small" onClick={() => { setUrl(s.csms_url); setChargerId(s.ocpp_id); setName(s.name); setPreset(s.preset); setProfile(s.security_profile); setVersion(s.ocpp_version ?? 'auto') }}>{s.ocpp_id} · {s.csms_url.replace(/^wss?:\/\//, '')}</button>
          ))}
        </div>
      )}

      <div className="cols">
        <label>Password (only if the CSMS asks for HTTP Basic auth)
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder={edit ? (cfg?.has_password ? 'unchanged (a password is set)' : 'none set') : 'leave empty if not required'} autoComplete="off" disabled={locked || dropPassword} />
        </label>
        <label>Security profile
          <select value={profile} disabled={locked} onChange={(e) => setProfile(Number(e.target.value))}>
            <option value={0}>0 · none</option><option value={1}>1 · Basic auth (ws://)</option><option value={2}>2 · Basic auth + TLS (wss://)</option><option value={3}>3 · Mutual TLS</option>
          </select>
        </label>
        <label>OCPP version
          <select value={version} disabled={locked} onChange={(e) => setVersion(e.target.value as Version)}>
            <option value="auto">Auto · recognises 1.6 or 2.0.1</option><option value="2.0.1">OCPP 2.0.1</option><option value="1.6">OCPP 1.6J</option>
          </select>
        </label>
        <label>Display name (optional)<input value={name} onChange={(e) => setName(e.target.value)} placeholder={`Simulator ${chargerId}`} /></label>
      </div>
      {edit && cfg?.has_password && !locked && <label className="inline"><input type="checkbox" checked={dropPassword} onChange={(e) => setDropPassword(e.target.checked)} /> Remove the saved password</label>}
      {secure && <label className="inline"><input type="checkbox" disabled={locked} checked={verify} onChange={(e) => setVerify(e.target.checked)} /> Verify the CSMS TLS certificate (turn off for self-signed test servers)</label>}

      <div className="stack">
        <div className="cols">
          <label>Hardware
            <select value={custom ? 'custom' : preset} disabled={locked} onChange={(e) => { setHwTouched(true); if (e.target.value === 'custom') setCustom(hw); else { setCustom(null); setPreset(e.target.value) } }}>
              {Object.entries(meta.presets).map(([k, p]) => <option key={k} value={k}>{p.label}</option>)}
              <option value="custom">Custom…</option>
            </select>
          </label>
          <label>Charging speed-up: <b>{scale}×</b> <small>(energy and battery only; protocol timers stay real)</small>
            <input type="range" min={1} max={600} step={1} value={scale} onChange={(e) => setScale(Number(e.target.value))} />
          </label>
        </div>
        {custom ? (
          <fieldset disabled={locked} style={{ border: 0, padding: 0, margin: 0 }}>
            {custom.map((e, i) => (
              <div className="hw-row" key={i}>
                <b>EVSE {i + 1}</b>
                <label>Current<select value={e.current_type} onChange={(ev) => setHw(i, { current_type: ev.target.value as 'AC' | 'DC' })}><option>AC</option><option>DC</option></select></label>
                <label>Max kW<input type="number" min={1} value={e.max_power_kw} onChange={(ev) => setHw(i, { max_power_kw: Number(ev.target.value) })} /></label>
                <label>Phases<select value={e.phases} onChange={(ev) => setHw(i, { phases: Number(ev.target.value) })}><option value={1}>1</option><option value={3}>3</option></select></label>
                <label>Volts<input type="number" min={1} value={e.voltage} onChange={(ev) => setHw(i, { voltage: Number(ev.target.value) })} /></label>
                <label>Connectors (1–4, one used at a time)
                  <select value={e.connectors.map((c) => c.type).join(',')} onChange={(ev) => setHw(i, { connectors: ev.target.value.split(',').map((t) => ({ type: t })) })}>
                    {['cType2', 'cCCS2', 'cCCS2,cG105', 'cType1', 'cType2,cType2', 'cTesla'].map((v) => <option key={v}>{v}</option>)}
                  </select>
                </label>
                <button type="button" className="ghost small" disabled={custom.length < 2} onClick={() => { setHwTouched(true); setCustom(custom.filter((_, j) => j !== i)) }}>Remove</button>
              </div>
            ))}
            <div className="row" style={{ marginTop: 8 }}><button type="button" className="ghost small" disabled={custom.length >= 8} onClick={() => { setHwTouched(true); setCustom([...custom, { ...custom[custom.length - 1] }]) }}>+ Add EVSE</button></div>
          </fieldset>
        ) : (
          <small className="muted">{hw.length} EVSE{hw.length > 1 ? 's' : ''}, {hw.reduce((n, e) => n + e.connectors.length, 0)} connector(s): {hw.map((e, i) => `EVSE ${i + 1}: ${e.current_type} ${e.max_power_kw} kW (${e.connectors.map((c) => c.type).join(' / ')})`).join(' · ')}. Every connector reports Available as soon as the CSMS accepts the boot.</small>
        )}
      </div>

      <div className="stack" style={{ gap: 6 }}>
        <label className="inline"><input type="checkbox" checked={autoCharge} onChange={(e) => setAutoCharge(e.target.checked)} /> Auto charge: the vehicle identifies itself when plugged in, no card needed</label>
        {autoCharge && <label style={{ maxWidth: 360 }}>Vehicle id (idTag, max 20 characters)<input value={vehicleId} maxLength={20} onChange={(e) => setVehicleId(e.target.value)} /></label>}
        <label className="inline"><input type="checkbox" checked={smartPlug} disabled={locked} onChange={(e) => setSmartPlug(e.target.checked)} /> Smart plug: no cable detection, the connector stays Available until a remote start and then goes straight to Charging</label>
        <label className="inline"><input type="checkbox" checked={finishing} onChange={(e) => setFinishing(e.target.checked)} /> OCPP 1.6: show <b>Finishing</b> after a session until the cable is unplugged (off: <b>Preparing</b>)</label>
      </div>
      <details>
        <summary className="muted" style={{ cursor: 'pointer', fontSize: 12 }}>Charger identity sent in BootNotification (optional)</summary>
        <fieldset disabled={locked} style={{ border: 0, padding: 0, margin: '8px 0 0' }}>
          <div className="cols">
            <label>Vendor<input value={vendor} onChange={(e) => setVendor(e.target.value)} placeholder="SimCharge" /></label>
            <label>Model<input value={modelName} onChange={(e) => setModelName(e.target.value)} placeholder="from the hardware preset" /></label>
            <label>Firmware version<input value={firmware} onChange={(e) => setFirmware(e.target.value)} placeholder="1.0.0" /></label>
          </div>
          <div className="cols">
            <label>Charge point serial number<input value={serial} onChange={(e) => setSerial(e.target.value)} placeholder="generated" /></label>
            <label>Charge box serial number (1.6)<input value={boxSerial} onChange={(e) => setBoxSerial(e.target.value)} /></label>
            <label>ICCID (SIM card)<input value={iccid} onChange={(e) => setIccid(e.target.value)} placeholder="8991101200003204510" /></label>
          </div>
          <div className="cols">
            <label>IMSI<input value={imsi} onChange={(e) => setImsi(e.target.value)} placeholder="310150123456789" /></label>
            <label>Meter type (1.6)<input value={meterType} onChange={(e) => setMeterType(e.target.value)} placeholder="not sent unless filled in" /></label>
            <label>Meter serial number (1.6)<input value={meterSerial} onChange={(e) => setMeterSerial(e.target.value)} placeholder="generated" /></label>
          </div>
        </fieldset>
      </details>
      {edit ? (
        <>
          <label className="inline"><input type="checkbox" checked={bootOnReconnect} onChange={(e) => setBootOnReconnect(e.target.checked)} /> Send BootNotification again after every reconnect</label>
          <label className="inline"><input type="checkbox" checked={endOnFull} onChange={(e) => setEndOnFull(e.target.checked)} /> End the session (StoppedByEV) when the battery is full</label>
        </>
      ) : (
        <label className="inline"><input type="checkbox" checked={connect} onChange={(e) => setConnect(e.target.checked)} /> Connect immediately</label>
      )}
      {error && <p className="error">{error}</p>}
      <div className="row"><button type="submit" disabled={busy || !url || !chargerId}>{edit ? (busy ? 'Saving…' : 'Save changes') : busy ? 'Adding…' : 'Add charger'}</button>{edit && onCancel && <button type="button" className="ghost" onClick={onCancel}>Cancel</button>}</div>
    </form>
  )
}
