# User manual

## 1. Starting

Start the backend (see the README) and open http://127.0.0.1:8100. The sidebar shows whether the simulator
backend is connected, an **Add charger** button, and one entry per charger with its state and a summary
(for example "1 charging · 2 EVSE").

If the server was started with `SIM_TOKEN`, you first see an **Access token** screen; enter the token to unlock. The session lasts 7 days in this browser, and you are asked again if it expires.

## 2. Adding a charger

Click **Add charger**.

| Field | Meaning |
|---|---|
| WebSocket URL of the CSMS | Base URL, e.g. `ws://127.0.0.1:8000/ocpp` or `wss://csms.example.com/ocpp` |
| Charger ID | The station identity, e.g. `CP001`. Must already exist in your CSMS if it rejects unknown stations |
| Connects to | Read-only preview: `<URL>/<charger ID>` with subprotocol `ocpp2.0.1` |
| Password | Only if your CSMS requires HTTP Basic auth (username is the charger ID) |
| Security profile | 0 none, 1 Basic, 2 TLS + Basic, 3 mutual TLS |
| Display name | Optional label |
| Hardware | A preset or **Custom** (edit EVSEs and connectors: current type AC/DC, power, connector types) |
| Charging speed-up | Multiplies energy and battery progress only; protocol timers stay real |
| Connect immediately | Starts the connection straight away |

When the CSMS accepts `BootNotification`, every connector is reported `Available`. The header shows
**online** and counters (Sent, Received, Errors, Timeouts, Reconnects, Heartbeat interval, Offline queue).
Use **Disconnect** / **Connect** to control the link and **Remove** to delete the charger.

### Editing a charger

Click **Edit** in the charger header to change its settings in the same form used to add it.

- While the charger is **connected**, only the display name, charging speed-up and the two behaviour options
  (re-send BootNotification after reconnect; end the session when the battery is full) can be saved.
- After **Disconnect**, you can also change the URL, charger ID, password (leave blank to keep it, or tick
  *Remove the saved password*), security profile, TLS verification and hardware.
- Changing the hardware replaces the EVSEs and connectors and rebuilds the device model with its defaults.
  It is refused while a vehicle is plugged in, a fault is active or the offline queue is not empty.
- Reconnect (**Connect**) to apply a new URL, ID or credentials; the station then boots again as the new identity.

## 3. Connectors tab: running a session

Each EVSE card shows its connector status, power, limit, session energy and temperature.

**Whole-station buttons:** *Power loss*, *Network loss*, *Watchdog reboot*, *Emergency stop*.

**Per EVSE:**
1. Pick a **Vehicle** (city, mid-size, large, e-scooter), a **Start SoC** and **Charge to** target.
2. Click **Plug in vehicle** (connector becomes `Occupied`).
3. Enter a card ID and **Tap card**. The simulator sends `Authorize`; if accepted, a transaction starts and
   charging begins. You can also tap a card *before* plugging in.
4. While charging: **Stop session** or tap the same card again; **Vehicle pauses**, **SoC +10**, **Make full**; **Unplug**.

Which events start/stop a transaction depends on `TxCtrlr` start/stop points (Device model tab); the
default starts when the power path closes. The source of authorization can be Local, Cache or CSMS.

A session that is started remotely from your CSMS (`RequestStartTransaction`) is acknowledged first and
authorized afterwards, as the spec requires.

## 4. Test cases tab: scenarios

Choose a scenario and **Run**. Each step shows pending, running, passed, failed or skipped with a detail
message. A run always starts by clearing faults and unplugging vehicles. Only one scenario runs per charger; use **Stop** to cancel.

| Group | Scenarios |
|---|---|
| Basics | smoke_boot, rfid_session, unknown_token, authorize_then_plug, remote_start_from_csms, two_sessions, battery_full |
| Metering | meter_failure, meter_frozen, meter_rollback, meter_spike, meter_invalid, meter_failure_stops_session |
| Power & network | power_loss_session, power_loss_idle, network_loss_session, unstable_network, high_latency, watchdog_reboot, clock_drift |
| Safety & electrical | emergency_stop, over_current, ground_fault, over_voltage, over_temperature, tamper |
| Hardware / Vehicle | contactor_failure, reader_failure, ev_comm_loss, cable_pulled |
| Smart charging | grid_overload |
| Protocol | all_messages (sends every station message once) |

`remote_start_from_csms` is interactive: it waits (up to 180 s) for you to send the command from your CSMS.

### Custom scenarios

Paste JSON: a list of steps, each with `do` or `expect`, optional `timeout` (seconds), `label`, and
`continue_on_fail`.

```json
[
  {"do": "plug_in", "ev": "large", "soc": 20},
  {"do": "tap", "token": "CARD1"},
  {"expect": "tx_state", "state": "Charging", "timeout": 20},
  {"do": "inject", "fault": "power_meter_failure", "params": {"mode": "frozen"}},
  {"expect": "message_sent", "action": "NotifyEvent", "timeout": 30},
  {"do": "clear"},
  {"do": "stop"}, {"do": "unplug"}
]
```

**Actions (`do`):** connect, disconnect, plug_in, unplug, tap, stop, inject, clear, reset, send, set_variable, set_ev, wait, wait_message.
**Expectations (`expect`):** online, connector_status, tx_state, tap_status, energy_gt, faulted, queue_gte, queue_empty, send_ok, response, message_sent, message_received.
Expectations poll until true or their timeout (default 10 s) expires, then fail.

## 5. Faults tab

One card per fault kind (24). Choose the scope (station or an EVSE), set parameters, click **Inject**. Active
faults are listed and can be cleared individually or all at once. Many have *Clear automatically after N seconds*.

| Fault | What it does |
|---|---|
| power_meter_failure | Modes: stop_reporting, frozen, rollback, spike, invalid; optionally ends the session and faults the EVSE |
| power_loss | Station goes dark; on return it reboots (Boot with `PowerUp`) and closes open transactions with `PowerLoss` timestamped at the cut |
| network_loss | Drops the WebSocket; events queue offline and flush after reconnect |
| network_flapping | Repeated drop/return cycles |
| high_latency, packet_loss | Delays or loses messages |
| over_voltage, under_voltage, over_current, ground_fault, over_temperature | Electrical/thermal faults with trips or derating |
| emergency_stop, tamper_detected | Safety events |
| contactor_failure, connector_lock_failure, rfid_reader_failure | Hardware failures |
| ev_communication_loss, ev_suspends, ev_battery_full, cable_unplugged | Vehicle-side events |
| grid_overload | External limit; reported via `ChargingRateChanged` |
| clock_drift | Station clock offset |
| reboot_watchdog | Reboots the station |
| firmware_update_failure | The next firmware / log transfer fails |

Each card states what a correct CSMS should do in response ("expect" text).

## 6. Messages tab

**Send a message.** Pick any of the 25 station-to-CSMS actions. A valid sample payload is loaded; edit it and
**Send**. Tick the option to skip schema validation to send deliberately invalid payloads.

**Rules.** Add rules that match a message and change its handling:

- Outgoing: delay, drop, duplicate, corrupt.
- Incoming commands: delay, timeout (no reply), error (CALLERROR), reject, invalid response.

The table shows hit counts; remove a rule or clear all. Rules apply live.

## 7. Device model tab

Browse and edit the 109 variables by component. Changes apply immediately (for example
`OCPPCommCtrlr.HeartbeatInterval`, `SampledDataCtrlr.TxUpdatedInterval`, `TxCtrlr` start/stop points,
`AuthCtrlr` settings). Your CSMS can also read and change them with `GetVariables` / `SetVariables`.

## 8. State tab

Live view of charging profiles, reservations, the offline queue, the local authorization list, the authorization
cache, certificates, variable monitors, display messages, firmware and log transfers, and network profiles.

## 9. Live OCPP log

The panel at the bottom streams every frame in both directions with a one-line summary; click a row to see
the raw JSON. Search, filter by direction or message type, hide heartbeats, pause, auto-scroll, resize and
**Export** to a file. Simulator notes (yellow) explain what the station did and why.

## 10. Troubleshooting

| Symptom | Likely cause |
|---|---|
| Stays offline, Reconnects rising | Wrong URL/ID, CSMS rejects the station, or password required |
| Boot accepted but `Pending`/`Rejected` | CSMS policy; the station retries after the interval it returns |
| Tap card says Invalid | Token not registered/accepted in your CSMS |
| Scenario step "timed out" | Expected behaviour did not occur; read the failed step's detail and the log |
| "A scenario is already running" | Click **Stop** first |
| UI shows backend disconnected | The simulator backend is not running on the expected port |
