export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

function readable(detail: unknown, fallback: string): string {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return detail.map((d: { loc?: unknown[]; msg?: string }) => `${(d.loc ?? []).slice(1).join('.')}: ${(d.msg ?? '').replace(/^Value error, /, '')}`).join('; ')
  return fallback
}

export async function api<T = unknown>(path: string, opts: { method?: string; body?: unknown } = {}): Promise<T> {
  const res = await fetch(path, {
    method: opts.method ?? (opts.body === undefined ? 'GET' : 'POST'),
    headers: { 'Content-Type': 'application/json' },
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
  })
  if (res.status === 401 && !path.startsWith('/api/login')) window.dispatchEvent(new Event('sim-unauthorized'))  // session expired: show the login again
  if (res.status === 204) return undefined as T
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new ApiError(res.status, readable((data as { detail?: unknown }).detail, `Request failed (${res.status})`))
  return data as T
}

// ---- types mirrored from the backend snapshot -------------------------------------------------------------------
export interface ConnectorState { id: number; type: string; status: string; plugged: boolean }
export interface TxState {
  id: string; connector_id: number; started_at: string; charging_state: string; energy_kwh: number; id_token: string | null
  seq_no: number; duration_s: number; total_cost: number | null; remote_start_id: number | null
}
export interface EvState { key: string; label: string; capacity_kwh: number; soc: number; target_soc: number; ready: boolean }
export interface EvseState {
  id: number; current_type: 'AC' | 'DC'; max_power_kw: number; voltage: number; phases: number; operative: boolean; locked: boolean
  power_kw: number; limit_kw: number; register_kwh: number; temperature: number; plugged_connector: number | null; authorized: string | null
  faulted: boolean; remote_pending: boolean; ev: EvState | null; connectors: ConnectorState[]
  reservation: { id: number; token: string; expiry: string } | null; tx: TxState | null
}
export interface FaultRecord { id: number; kind: string; title: string; scope: number | null; params: Record<string, unknown>; started_at: number; ends_at: number | null; active: boolean }
export interface Rule { id: number; direction: 'out' | 'in'; action: string; effect: string; params: Record<string, unknown>; times: number | null; probability: number; enabled: boolean; hits: number }
export interface StepState { index: number; label: string; status: 'pending' | 'running' | 'passed' | 'failed' | 'skipped'; detail: string; seconds: number }
export interface Run { id: string; scenario_id: string; title: string; status: 'running' | 'passed' | 'failed' | 'stopped'; started: number; finished: number | null; steps: StepState[] }
export interface Snapshot {
  id: string; name: string; ocpp_id: string; csms_url: string; vendor: string; model: string; firmware_version: string
  state: string; powered: boolean; boot_status: string | null; want_connected: boolean; last_error: string | null
  connected_since: number | null; reconnect_attempt: number; heartbeat_interval: number; time_scale: number
  config: { security_profile: number; verify_tls: boolean; has_password: boolean; boot_on_reconnect: boolean; end_on_full: boolean; hardware: { current_type: 'AC' | 'DC'; max_power_kw: number; voltage: number; phases: number; connectors: { type: string }[] }[] }
  stats: { sent: number; received: number; errors: number; timeouts: number; reconnects: number }; queue: number; notes: string[]
  evses: EvseState[]; faults: { active: FaultRecord[]; history: FaultRecord[] }; rules: Rule[]
  last_responses: Record<string, { status: string | null }>; scenario: Run | null
  profiles: { id: number; evse_id: number; purpose: string; stack_level: number; kind: string; unit: string; periods: { start_period: number; limit: number }[] }[]
  reservations: { id: number; evse_id: number | null; token: string; expiry: string }[]
  local_list: { version: number; count: number; entries: { token: string; status: string }[] }
  cache: { token: string; status: string }[]
  certificates: { type: string; serial: string }[]
  display_messages: { id: number; priority: string; message: { content: string } }[]
  monitors: { id: number; component: { name: string }; variable: { name: string }; type: string; value: number; severity: number; breached: boolean }[]
  firmware: { status: string; version: string; published: number }
  log_upload: { status: string; request_id: number | null }
  network_profiles: Record<string, unknown>
  tx_queue: { event_type: string; seq_no: number; transaction_id: string; timestamp: string }[]
}
export interface LogEntry { id: number; ts: number; dir: 'in' | 'out' | 'sim'; kind: 'call' | 'result' | 'error' | 'note' | 'raw'; action: string | null; msg_id: string | null; summary: string; payload: unknown; note: string | null }
export interface Param { key: string; label: string; type: 'number' | 'boolean' | 'select'; default: number | boolean | string; options?: string[]; min?: number; max?: number }
export interface FaultSpec { title: string; scope: 'station' | 'evse' | 'both'; group: string; description: string; params: Param[]; expect: string; oneshot?: boolean }
export interface Meta {
  presets: Record<string, { label: string; model: string; evses: { current_type: string; max_power_kw: number; voltage: number; phases: number; connectors: { type: string }[] }[] }>
  ev_models: Record<string, { label: string; capacity_kwh: number; max_ac_kw: number; max_dc_kw: number }>
  connector_types: string[]; faults: Record<string, FaultSpec>
  scenarios: { id: string; title: string; group: string; description: string; steps: number }[]
  station_messages: string[]; commands: string[]; rule_effects: { out: string[]; in: string[] }; error_codes: string[]; stop_reasons: string[]
  scenario_steps: { do: string[]; expect: string[] }
}
