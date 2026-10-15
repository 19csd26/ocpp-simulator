# Architecture

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
| `__main__.py` | `python -m ocpp_simulator --port N` |
| `config.py` | `StationConfig`, `EvseSpec`, `ConnectorSpec`, hardware `PRESETS`, `EV_MODELS`, connector types |
| `station.py` | `Station`: connection loop (backoff, `network_up`, wake event), boot, heartbeat loop (interval re-read every second), `request`/`send_raw`/`_call`, offline queue (`deliver_tx`, `flush_queue`), `snapshot()`. Contains `_Conn`, the socket adapter that logs frames and applies outgoing rules and latency/packet-loss faults. |
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

`backend/tests/`: `test_core`, `test_coverage` (all 64 messages), `test_commands`, `test_semantics`, `test_faults`,
`test_rules`, `test_scenarios` (30 fast + 8 `slow`), `test_api`. `conftest.py` provides a `csms` (mock CSMS on a free
port) and `make_station` fixture. Asyncio mode is `auto`.

## Limits and decisions

- In-memory only: restart loses stations. Deliberate (no database, per requirements).
- No authentication on the API; localhost only.
- Fault-event component names are best effort and unverified against OCTT.
- ISO 15118 messages are present and schema-valid but there is no real EV/PKI exchange.
