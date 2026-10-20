# Volkai: user manual

> Looking for the full explanation of a feature, with flow and sequence diagrams and worked examples? See the **[feature guide](guide/README.md)**.

## 1. Starting

Start the backend (see the README) and open http://127.0.0.1:8100. In Docker: `docker compose up --build` and open http://localhost:8100; to reach a CSMS on your computer use `host.docker.internal` instead of `localhost` in its URL. The sidebar shows whether the simulator
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
| OCPP version | **Auto** recognises 1.6 or 2.0.1: it offers `ocpp2.0.1` and `ocpp1.6` and uses whichever your CSMS accepts (the header shows which). **2.0.1** or **1.6** offers only that one. |
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

## 2b. OCPP 1.6 and 2.0.1

A charger speaks one version per connection, shown as a chip next to its name (`OCPP 1.6` or `OCPP 2.0.1`) and in the sidebar.

- **Auto** (the default in the form) is the most flexible choice: the charger offers both protocols during the WebSocket handshake and
  your CSMS picks. Use **2.0.1** or **1.6** to force one, for example when a CSMS must be tested on a specific version.
- Changing the version needs the charger to be disconnected (**Edit**).
- If you see *"CSMS refused the connection: unsupported subprotocol: ocpp1.6"*, your CSMS answered with a protocol this charger did not offer.
  Set the OCPP version to **Auto** or **1.6**.

What changes on a 1.6 charger (everything else, including vehicles, faults, rules and scenarios, works the same):

| | OCPP 2.0.1 | OCPP 1.6J |
|---|---|---|
| Connectors | EVSE 1 / connector 1 | Numbered `#1..#N` across the whole charge point (shown next to each connector); `#0` is the station |
| Connector status | Available, Occupied, Reserved, Unavailable, Faulted | Available, **Preparing**, **Charging**, **SuspendedEV**, **SuspendedEVSE**, **Finishing**, Reserved, Unavailable, Faulted |
| Session | `TransactionEvent` Started / Updated / Ended | `StartTransaction` (CSMS returns an integer id), `MeterValues`, `StopTransaction` |
| Faults | `NotifyEvent` | `StatusNotification` with an `errorCode` (for example `GroundFailure`, `PowerMeterFailure`, `HighTemperature`) |
| Settings | Device model (GetVariables / SetVariables) | Configuration keys (GetConfiguration / ChangeConfiguration); the Device tab becomes **Configuration keys** |
| Remote control | RequestStart/StopTransaction | RemoteStart/StopTransaction |
| After a stop with the cable plugged in | Occupied | **Finishing** until you unplug |

Messages that exist only in 2.0.1 (device model reports, display messages, monitoring, certificates, ISO 15118, `NotifyEvent`,
`SecurityEventNotification`, ...) are quietly not sent on a 1.6 charger. The **Messages** tab lists the 10 messages a 1.6 charger can send.
Offline behaviour is the same: events queue and are delivered in order after reconnecting, and a session that started offline gets its transaction id then.

## 2c. Features for testing a CSMS

| Feature | Where | What it does |
|---|---|---|
| **Cards used by tests** | Add / Edit charger | `card_id` (default `CARD1`) and `card_id_2` (default `CARD2`, used by EVSE 2). The scenarios, the sample messages and the Connectors tab tap these cards, so set them to cards your CSMS accepts. See [the guide](guide/02-sessions-and-cards.md#23-the-cards-used-by-tests). |
| **Auto charge** | Add / Edit charger | The vehicle presents its own id (default `VID:FCA47A10DC70`, max 20 characters) half a second after it is plugged in, so no card is tapped. The CSMS must accept that id. |
| **Smart plug** | Add / Edit charger (disconnected) | No cable detection: the connector stays Available until a remote start, goes straight to Charging, and is Available again after the stop. Needs a remote start (use *Simulate a command* below if you have no CSMS). |
| **Finishing mode** | Add / Edit charger | OCPP 1.6: after a session the connector shows Finishing until the cable is pulled (off: Preparing). |
| **V2G (bidirectional)** | Connectors tab, during a session | For the mid-size and large EV: *Discharge (V2G)* feeds a chosen kW back, the battery goes down (stops at 10 %), the export register grows and `Energy.Active.Export.Register` / `Power.Active.Export` can be reported. *Charge (G2V)* returns to normal. |
| **Meter register** | Connectors tab, each EVSE | Type a kWh value and *Set*: the next readings continue from it. Use it to see how the CSMS handles a jump, a rollback or a reset. |
| **Token type** | Connectors tab, next to the card id | ISO14443, ISO15693, eMAID, Central, Local or NoAuthorization (2.x). |
| **Reboot with a reason** | Connectors tab, top row | Reboots and sends the chosen boot reason (PowerUp, Watchdog, RemoteReset, ScheduledReset, FirmwareUpdate, ...). |
| **Local authorization list** | State tab | Add and remove tokens by hand; the list version goes up with every change. |
| **Station display** | State tab | Shows the message your CSMS set with SetDisplayMessage (highest priority for the current state) or a default, plus the running session's energy, power and cost. |
| **Simulate a command from the CSMS** | Messages tab | Runs any CSMS command on the charger exactly as if your CSMS had sent it, with no CSMS needed. It is marked "simulated" in the log. |
| **Raw frame** | Messages tab | Sends any text on the WebSocket as is (a CALL, a CALLRESULT, a CALLERROR, malformed JSON) to test the CSMS's error handling. |
| **Saved CSMS addresses** | Add charger | *Save this address* names a URL (for example "dev server"); pick it later from the list. They are kept in this browser. |
| **Charger identity** | Add / Edit charger, *Charger identity* | Vendor, model, firmware, serial numbers, ICCID, IMSI (and, for 1.6, charge box serial, meter type and serial) sent in BootNotification. |

Measurands you can switch on (Device model: `SampledDataCtrlr.TxUpdatedMeasurands`, or the 1.6 key `MeterValuesSampledData`) now also include `Temperature` (OCPP 1.6 only: in 2.0.1 it is not a measurand),
`Power.Factor`, `Energy.Active.Export.Register`, `Power.Active.Export` and `Current.Export`.

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

Choose a scenario and **Run**. Every built-in scenario runs on both OCPP versions: version-specific checks are swapped automatically (for example a `TransactionEvent` Ended check becomes a `StopTransaction` check on 1.6) and steps that only make sense on the other version show as *skipped*. Each step shows pending, running, passed, failed or skipped with a detail
message. A run always starts by clearing faults and unplugging vehicles. Only one scenario runs per charger; use **Stop** to cancel.

| Group | Scenarios |
|---|---|
| Basics | smoke_boot, rfid_session, unknown_token, authorize_then_plug, remote_start_from_csms, two_sessions, battery_full |
| Metering | meter_failure, meter_frozen, meter_rollback, meter_spike, meter_invalid, meter_failure_stops_session |
| Power & network | power_loss_session, power_loss_idle, network_loss_session, unstable_network, high_latency, watchdog_reboot, clock_drift |
| Safety & electrical | emergency_stop, over_current, ground_fault, over_voltage, over_temperature, tamper |
| Hardware / Vehicle | contactor_failure, reader_failure, ev_comm_loss, cable_pulled |
| Smart charging | grid_overload |
| Authorization | auto_charge, smart_plug_remote_start |
| Bidirectional charging | v2g_discharge |
| Protocol | all_messages (sends every station message once) |

`remote_start_from_csms` is interactive: it waits (up to 180 s) for you to send the command from your CSMS.

### Custom scenarios

Paste JSON: a list of steps, each with `do` or `expect`, optional `timeout` (seconds), `label`, and
`continue_on_fail`.

```json
[
  {"do": "plug_in", "ev": "large", "soc": 20},
  {"do": "tap", "token": "$card"},
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
| Tap card says Invalid | Token not registered/accepted in your CSMS; for scenarios set the charger's *Cards used by tests* |
| Scenario step "timed out" | Expected behaviour did not occur; read the failed step's detail and the log |
| "A scenario is already running" | Click **Stop** first |
| UI shows backend disconnected | The simulator backend is not running on the expected port |

---
Volkai · designed and built by Raghav ([@19csd26](https://github.com/19csd26)) · Copyright © 2026 Raghav (19csd26). All rights reserved.
