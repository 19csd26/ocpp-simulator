# Volkai: architecture

```
 Browser (React + Vite)                      Simulator backend (FastAPI, asyncio)                 CSMS under test
 ┌──────────────────┐   REST /api/*   ┌──────────────────────────────────────────────┐
 │ tabs, log panel  │ ───────────────▶│ main.py  routes                              │
 │ useSim hook      │ ◀─ WS /ws ──────│ hub.py   log rings + state fan-out           │
 └──────────────────┘  init/state/    │   │                                          │   WebSocket
                       log/scenario   │ station.py  Station ── _Conn ── Link ────────┼──▶ ws://…/<id>
                                      │   ├ charging.py  (mixin)  evses, tx, meter   │    ocpp2.0.1
                                      │   ├ commands.py  (mixin)  40 cmd_* handlers │
                                      │   ├ faults.py    (mixin)  24 fault kinds    │
                                      │   ├ devicemodel.py, smartcharging.py        │
                                      │   ├ rules.py     message rules              │
                                      │   └ scenarios.py Runner                     │
                                      └──────────────────────────────────────────────┘
```

Single process, single event loop, in-memory state. No database.

## Backend modules (`backend/ocpp_simulator/`)

| Module | Responsibility |
|---|---|
| `main.py` | `create_app()`: REST routes, `/ws`, serves `ui/dist`, localhost-only CORS. Deleting a station sets `removed`, calls `hub.forget`, publishes `removed`. |
| `__main__.py` | `python -m ocpp_simulator --port N`; refuses a non-local `--host` without `SIM_TOKEN` (unless `--insecure`) |
| `auth.py` | Optional shared-token auth (`SIM_TOKEN`): `POST /api/login` sets an HttpOnly, SameSite=Strict cookie holding a digest (never the token); `Authorization: Bearer` also works. All `/api/*` and `/ws` need it except `/api/auth`, `/api/login`, `/api/logout`. Login failures are limited to 10 per 5 minutes. |
| `config.py` | `StationConfig`, `EvseSpec`, `ConnectorSpec`, hardware `PRESETS`, `EV_MODELS`, connector types |
| `station.py` | `Station`: connection loop (backoff, `network_up`, wake event), boot, heartbeat loop (interval re-read every second), `request`/`send_raw`/`_call`, offline queue (`deliver_tx`, `flush_queue`), `snapshot()`. Contains `_Conn`, the socket adapter that logs frames and applies outgoing rules and latency/packet-loss faults. |
| `features.py` | Operator features outside the protocol: auto charge, smart plug, V2G control, meter register, local list editing, reboot reason, `simulate_command` (run a CSMS command locally) and `send_raw_frame`. |
| `about.py` | The owner's signature (served at `/api/about`, sent as `X-Created-By`). |
| `v16.py` | OCPP 1.6J. `V16Mixin` (part of `Station`): connector numbering, the 1.6 status model, translation of the internal events into 1.6 messages, configuration keys and the 19 CSMS commands (`cmd16_*`, adapters over the shared `cmd_*` logic). `Link16`: the 1.6 per-connection endpoint. |
| `link.py` | `Link(ChargePoint)`: one per connection. Builds a handler for each of the 40 CSMS commands (`COMMAND_NAMES`) that calls `Station.run_command`. Applies incoming rules in `route_message`. |
| `charging.py` | `ChargingMixin`: `Evse`/`Connector`/`Tx`/`EvState`, connector status computation, 1 Hz ticker (energy, SoC, periodic MeterValues), `_reconcile` (desired charging state, ChargingRateChanged, end on full), user actions (plug, tap, stop, authorize), transactions, power-loss resolution, `NotifyEvent`/`SecurityEventNotification`. |
| `commands.py` | `CommandsMixin`: `cmd_*` implementations: variables/reports, reset, availability, remote start/stop, local list, reservations, firmware/log, certificates (real CSRs via `cryptography`), monitoring, display messages, charging profiles. |
| `faults.py` | `CATALOG` (24 kinds with parameters and "expect" text) and `FaultsMixin` (`inject_fault`, `clear_fault`, `_start_*`/`_stop_*` per kind). |
| `devicemodel.py` | `DeviceModel`: 109 variables with types, limits, mutability; read/write/validate/report. |
| `smartcharging.py` | `validate_profile`, `limit_at` (effective limit across stack levels, Absolute/Relative/Recurring, A↔W), `composite_schedule`. |
| `rules.py` | `RuleSet`/`Rule` for outgoing and incoming message manipulation. |
| `scenarios.py` | `Runner` (step DSL: `ACTIONS`, `EXPECTS`), `build_catalog()` → 32 scenarios. |
| `templates.py` | Valid sample payloads for all 64 messages. |
| `summaries.py` | One-line human summaries of frames for the log. |
| `hub.py` | `Hub`: per-station log ring buffers, subscriber queues, throttled state snapshots, `forget()`. |
| `mock_csms.py` | Permissive CSMS accepting all 25 messages; can send commands; optional Basic auth and token list. Used by tests. |

`Station.apply_config` swaps in an edited configuration (hardware changes rebuild the device model and connectors).

`Station` composes `ChargingMixin`, `CommandsMixin`, `FaultsMixin`; it owns the device model, rule set, runner and offline queue.

## OCPP versions

The charger's physics (vehicles, power, energy, faults, rules, scenarios) is protocol independent. The version is negotiated per connection:
`StationConfig.ocpp_version` is `auto | 2.0.1 | 1.6`; the station offers the matching WebSocket subprotocol(s) (`auto` offers both) and sets
`Station.version` from the subprotocol the CSMS selects. A `Link` (2.0.1) or `Link16` (1.6) is then created for the connection.

Internally the station still builds events in one shape (the 2.0.1 `TransactionEvent` payload, snake_case fields). On a 1.6 connection:

| Internal event | 1.6 message |
|---|---|
| `request(...)` for BootNotification, Heartbeat, StatusNotification, MeterValues, Firmware / Log status, DataTransfer | translated by `V16Mixin.out16`; messages with no 1.6 equivalent (NotifyEvent, SecurityEventNotification, ...) are dropped quietly |
| `TransactionEvent` Started | `StartTransaction`; the integer `transactionId` from the CSMS is kept in `Station.tx16` (internal id to CSMS id), and survives reconnects |
| `TransactionEvent` Updated with meter values | `MeterValues` with `transactionId` (a pure state change is reported by `StatusNotification`) |
| `TransactionEvent` Ended | `StopTransaction` (reason mapped to the 1.6 enum, other values become `Other`) with `transactionData` |
| `NotifyEvent` (fault reported) | `StatusNotification` with an `errorCode` |
| connector status | `compute_status16` (Preparing, Charging, SuspendedEV, SuspendedEVSE, Finishing, ...), connector ids numbered across the station |

The offline queue holds the internal events, so the translation (and the transaction id lookup) happens when each event is finally sent, in order.
Scenario steps may carry `v16` (replacement fields for 1.6; `null` removes a field) and `only` (run on one version) so one catalogue serves both.

### OCPP versions

`StationConfig.ocpp_version` is `auto | 2.0.1 | 1.6`. `auto` offers `ocpp2.0.1, ocpp1.6` and the CSMS picks. `Station.version` is then `"2.0.1"` or `"1.6"`, and
`Station.request` / `send_raw` use the matching `call` module (`ocpp.v201.call` or `ocpp.v16.call`). `Station.cp_actions()` gives the messages a charger can send in the
protocol in use (25 or 10) and `command_names()` the commands it answers (40 or 19).

### Bidirectional charging

An EVSE has `v2x` (`charge` or `discharge`), an export register and an export power. In `_advance` a discharging vehicle moves energy from its battery to the export
register (`tx.export_wh`) instead of importing; the vehicle protects itself at 10 % (`DISCHARGE_FLOOR_SOC`). `Energy.Active.Export.Register`, `Power.Active.Export` and
`Current.Export` are ordinary measurands, so they follow the configured measurand lists.

## Key flows

**Connect and boot.** `Station.connect` sets `want_connected` and wakes the loop. The loop opens the socket
(subprotocol `ocpp2.0.1`, optional Basic auth/TLS), starts a `Link`, sends `BootNotification`. On `Accepted`,
`_after_boot` resolves interrupted transactions (power loss), sends `StatusNotification` for every connector,
and starts the heartbeat loop. On `Pending`/`Rejected` it retries after the CSMS interval.

**Outbound message.** `request()` → rules (delay/drop/duplicate/corrupt) → `_Conn` (latency, packet loss, logging) → socket. The `ocpp` library validates payloads against the official JSON schemas; `send_raw(validate=False)` bypasses this to send invalid payloads deliberately.

**Inbound command.** Socket → `_Conn` log → `Link.route_message` (incoming rules) → generated handler → `run_command` → `Station.cmd_*` → typed response. Handlers must never `await` their own `call()`, since the receive loop that reads the reply is running the handler; follow-up calls use `spawn()` (e.g. remote start answers `Accepted`, then authorizes).

**Transactions and offline.** `tx_event` builds `TransactionEvent` (Started/Updated/Ended, seqNo, trigger). If offline, events go to the queue with `offline=true` and their original timestamps and are flushed in order on reconnect. Power loss ends open transactions after the reboot with `stoppedReason=PowerLoss` at the cut time.

**Metering.** A 1 Hz ticker advances energy and SoC (scaled by `time_scale`). `reported_energy_wh` applies the meter-fault mode (stop reporting, frozen, rollback, spike, invalid). Periodic `MeterValues`/`TransactionEvent` follow `SampledDataCtrlr` settings.

**Scenarios.** `Runner.start` validates steps, prepends the clean-station `reset` step (from the API), and runs them in a task. `do` steps call station actions; `expect` steps poll until true or timeout. Log-based expectations only count messages after the run started. Progress is published as `scenario` events.

**UI updates.** State changes call `Hub.state_changed`, which throttles and pushes snapshots; logs are pushed per frame. The UI (`useSim.ts`) keeps a `removedIds` set so a queued snapshot cannot resurrect a deleted station.

## Public API

REST (all under `/api`):

| Method & path | Purpose |
|---|---|
| `GET /meta` | Presets, EV models, fault catalogue, scenarios, message actions |
| `GET /scenarios/{id}` | Scenario definition |
| `GET/POST /stations`, `GET/DELETE /stations/{id}` | List, create (URL + ID + options), inspect, remove |
| `PATCH /stations/{id}` | Edit name, speed-up, behaviour flags; while disconnected also URL, ID, password (`""` clears), security profile, TLS, hardware (`preset` or `evses`). 409 if locked or busy |
| `POST /stations/{id}/connect`, `/disconnect` | Control the link |
| `POST /stations/{id}/evse/{n}/plug-in`, `/unplug`, `/tap`, `/stop`, `/ev` | Operate a session |
| `POST /stations/{id}/faults`, `DELETE …/faults[/{fid}]` | Inject / clear faults |
| `POST /stations/{id}/send`, `GET …/templates/{action}` | Send a station message; sample payload |
| `GET/POST/DELETE /stations/{id}/rules[/{rid}]` | Message rules |
| `POST /stations/{id}/scenarios/{sid}/run`, `/custom`, `/stop`; `GET …/runs` | Scenarios |
| `GET/DELETE /stations/{id}/log`, `GET …/log/export` | Log |
| `GET/PUT /stations/{id}/variables` | Device model |

WebSocket `/ws` events: `init` (all stations + history), `state` (snapshot), `log` (one frame), `scenario` (run progress), `removed`.

## Frontend (`ui/src/`)

`api.ts` (types, fetch helper) · `useSim.ts` (WebSocket hook) · `App.tsx` (sidebar, station header, tabs) ·
`components/` (`AddStation`, `Console`, `LogPanel`, `Toast`, `Confirm`) · `tabs/` (`Connectors`, `Faults`, `Scenarios`, `Messages`, `Device`, `State`).

## Extending

- **New fault**: add an entry to `faults.CATALOG` and `_start_<kind>`/`_stop_<kind>` in `FaultsMixin`; the UI card is generated from the catalogue.
- **New scenario**: add to `scenarios.build_catalog()` (or paste custom JSON in the UI).
- **New scenario step**: add an `a_*` method to `Runner.ACTIONS` or an expectation to `EXPECTS`.
- **New device variable**: `devicemodel.default_model`.
- **New hardware preset or vehicle**: `config.PRESETS` / `config.EV_MODELS`.
- **OCPP message coverage** is guarded by tests in `tests/test_coverage.py`; adding or removing a message must keep it green.

## Testing

`backend/tests/`: `test_features` (auto charge, smart plug, V2G, identity, meter register, simulate command, raw frames, scenarios on every version), `test_core`, `test_coverage` (all 64 messages), `test_commands`, `test_semantics`, `test_faults`,
`test_rules`, `test_scenarios` (30 fast + 8 `slow`), `test_api`. `conftest.py` provides a `csms` (mock CSMS on a free
port) and `make_station` fixture. Asyncio mode is `auto`.

## Limits and decisions

- In-memory only: restart loses stations. Deliberate (no database, per requirements).
- Authentication is one optional shared token (`SIM_TOKEN`), not accounts. Without it the API is open and the server only binds to localhost. TLS is left to the tunnel or proxy in front.
- Fault-event component names are best effort and unverified against OCTT.
- ISO 15118 messages are present and schema-valid but there is no real EV/PKI exchange.
- OCPP 1.6 covers the 28 operations of the 1.6 specification. The 1.6 security whitepaper extensions (signed firmware, certificates, `SecurityEventNotification`, `ExtendedTriggerMessage`, `GetLog`) are not implemented.
- A 1.6 `StopTransaction` does not carry the optional `idTag`.

---
Volkai · designed and built by Raghav ([@19csd26](https://github.com/19csd26)) · Copyright © 2026 Raghav (19csd26). All rights reserved.
