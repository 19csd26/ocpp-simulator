# OCPP 2.0.1 Charge Point Simulator

A charge-point simulator with a web UI, for testing a CSMS (central system) without hardware.
Type the CSMS WebSocket URL and a charger ID; the station connects, boots, and reports every connector
`Available`. Then plug in vehicles, tap cards, inject faults (power meter failure, power loss, network
loss, ...) and run scripted test scenarios while watching the live OCPP log.

There is no database and no CSMS integration. The UI remembers your last entries in browser localStorage only.

| Document | Read it for |
|---|---|
| [docs/USER_MANUAL.md](docs/USER_MANUAL.md) | Using the UI: adding a charger, sessions, faults, scenarios, message rules |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it is built: modules, data flow, extension points, REST/WebSocket API |

## Quick start

Developed and tested with Python 3.14 and Node 26; recent versions of both should work.

```bash
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cd ../ui && npm install && npm run build          # the backend serves ui/dist
cd ../backend && .venv/bin/python -m ocpp_simulator --port 8100
```

Open http://127.0.0.1:8100, click **Add charger**, enter the CSMS URL (e.g. `ws://127.0.0.1:8000/ocpp`) and a
charger ID (e.g. `CP001`). The station connects to `<url>/<charger id>` with subprotocol `ocpp2.0.1`.

Development with live reload: run the backend on 8100, then `npm run dev` in `ui/`
(it proxies `/api` and `/ws`; set `SIM_BACKEND` to point elsewhere).

No CSMS handy? Run the bundled permissive one: `.venv/bin/python -m ocpp_simulator.mock_csms --help`.

## Features

- **All 64 OCPP 2.0.1 messages**: 25 charge-point-to-CSMS and 40 CSMS-to-charge-point (DataTransfer both ways). A test fails if one is missing.
- **Station definition**: presets (AC 7 kW, AC 22 kW dual, DC 60 kW dual, DC 150 kW, mixed hub) or a custom EVSE/connector editor; Basic-auth password and security profile; time-scale speed-up.
- **Sessions**: plug in, tap card, remote start/stop, stop, unplug; vehicle models with state of charge and target; full battery ends the session (`StoppedByEV`).
- **Device model**: 109 variables, editable live (heartbeat, sampled data, start/stop points, authorization).
- **Offline behaviour**: `TransactionEvent`s queue with `offline=true` and original timestamps, then flush in order on reconnect.
- **Smart charging**: profile validation, stack levels, absolute/relative/recurring profiles, composite schedules, `ChargingRateChanged`.
- **Fault injection (24 kinds)**: `power_meter_failure` (stop reporting, frozen, rollback, spike, invalid), `power_loss`, network loss / flapping / latency / packet loss, over/under-voltage, over-current, ground fault, over-temperature, emergency stop, tamper, contactor and lock failures, reader failure, vehicle comm loss, clock drift, watchdog reboot, failing firmware/log transfers, and more.
- **Message rules**: delay, drop, duplicate or corrupt outgoing messages; delay, time out, error, reject or invalidate replies to incoming commands.
- **Scenarios**: 32 built-in, plus custom JSON scenarios (`do` / `expect` steps).
- **Live log** with search, direction/message filters and JSON export.

## Tests

```bash
cd backend
.venv/bin/python -m pytest                                # fast suite (~277 tests, several minutes)
.venv/bin/python -m pytest -m slow tests/test_scenarios.py  # slow scenarios
```

Tests start the mock CSMS in-process on free ports; nothing external is needed.

## Caveats

- Component names in fault-generated `NotifyEvent`s are best effort against the standardized component list and are **not verified against OCTT**.
- Scenarios that start sessions use cards `CARD1` and `CARD2`; register them as accepted tokens in your CSMS. `unknown_token` expects the CSMS to refuse its token.
- The simulator API has **no authentication**. Keep it on localhost (CORS is limited to localhost).
- Stations live in memory; restarting the backend forgets them (the UI re-offers your last form values).
