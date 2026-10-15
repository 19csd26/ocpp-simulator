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

## Share it (tunnel) or run it in Docker

The API can be protected with a shared token. Set `SIM_TOKEN` and the UI shows a login screen first; scripts can send
`Authorization: Bearer <token>`. Without `SIM_TOKEN` the server only starts on localhost and refuses to listen on a network address.

**Free tunnel (recommended).** The simulator keeps running on your machine, so a CSMS on `localhost` stays reachable.

```bash
brew install cloudflared        # or use ngrok
scripts/share.sh                # prints a random access token and a public https URL
```
Send people the URL and the token. Stop with Ctrl+C. Use `SIM_TOKEN=... scripts/share.sh` to choose the token and
`TUNNEL=ngrok` to use ngrok.

**Docker.**
```bash
docker build -t ocpp-simulator .
docker run --rm -p 8100:8100 -e SIM_TOKEN="$(openssl rand -hex 16)" ocpp-simulator
```
The container listens on `0.0.0.0` and refuses to start without `SIM_TOKEN`. A hosted copy cannot reach a CSMS on your
laptop's `localhost`; give it a public `wss://` CSMS URL.

**Render (free web service).** `render.yaml` is a Blueprint that deploys the `Dockerfile` straight from this GitHub repo.

1. Render dashboard > **New > Blueprint**, connect GitHub and pick this repo.
2. Render asks for `SIM_TOKEN` (a secret, never stored in the repo): enter a long random value, e.g. `openssl rand -hex 16`.
3. Apply. When the build finishes, open `https://ocpp-simulator.onrender.com` (the exact URL is on the service page) and log in with the token.

Things to know about Render's free tier (check Render's current terms):
- Free web services sleep after about 15 minutes without traffic and take a while to wake. Stations are in memory, so a sleep or restart forgets them.
- Your CSMS must be reachable from the internet over `wss://`; a CSMS on your laptop's `localhost` is not. Expose it with a tunnel (`ngrok http 8000`) or deploy it.
- Pushing to `main` redeploys automatically (`autoDeploy`). To change the token later, edit `SIM_TOKEN` under Environment.

**Hugging Face Spaces (Docker).** Builds the `Dockerfile` for you, but Docker Spaces on the free CPU tier now require a Hugging Face **PRO** subscription (creating one on a free account fails with HTTP 402). Static Spaces are free but cannot run this backend. Use the tunnel above for free hosting.

1. Create a Hugging Face account and a token with **write** access (Settings > Access Tokens).
2. Publish: `HF_TOKEN=hf_xxx scripts/deploy_hf.sh <your-hf-username>/ocpp-simulator`
   (creates a **private** Space; add `VISIBILITY=public` for a public one). `DRY_RUN=1` builds the bundle without pushing.
3. In the Space: Settings > Variables and secrets > new **secret** `SIM_TOKEN` with a long random value
   (`openssl rand -hex 16`). The container will not start without it.
4. Open the **direct URL** `https://<username>-ocpp-simulator.hf.space`, not the huggingface.co page. The login cookie is
   `SameSite=Strict`, so it is not sent when the app is embedded in the huggingface.co frame.

Things to know about Spaces:
- **Your CSMS must be reachable from the internet** over `wss://`. A CSMS on your laptop's `localhost` is not. Expose it with a
  tunnel (`ngrok http 8000`) or deploy it. I believe Spaces only allows outbound connections on ports 80, 443 and 8080; check
  the current Spaces documentation, and use a standard `wss://host/ocpp` URL on 443.
- Free Spaces sleep when idle and restart on the next visit. Stations are held in memory, so a restart forgets them.
- A private Space adds a second lock (only you can open it); the token still protects a public one.

Treat the token like a password: anyone who has it can make the simulator connect to any URL and operate it.

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
- The API has **no authentication unless `SIM_TOKEN` is set**. Without it, keep it on localhost (CORS is limited to localhost). The token is one shared secret, not per-user accounts, and there is no HTTPS of its own: use a tunnel or reverse proxy for TLS.
- Stations live in memory; restarting the backend forgets them (the UI re-offers your last form values).
