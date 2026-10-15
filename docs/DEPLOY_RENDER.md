# Deploying on Render and connecting a charger

This guide deploys the simulator on Render's free tier from this GitHub repo, then connects a simulated charger to your
CSMS. These steps follow Render's Blueprint flow; button names may differ slightly from what you see in the dashboard.

## What you need

- A GitHub account with this repo (`render.yaml` is at its root) and a Render account (sign in with GitHub is simplest).
- A long random access token for the simulator. Generate one and keep it safe; it is your login password:
  ```bash
  openssl rand -hex 16
  ```
- A CSMS that is reachable **from the internet over `wss://`**. Render cannot reach `localhost` on your laptop (see part 2).

## Part 1: Deploy the simulator

1. Sign in at https://dashboard.render.com.
2. Click **New**, then **Blueprint**.
3. Connect your GitHub account if asked and give Render access to the repo. Because the repo is public you can also paste
   its URL.
4. Select the repo and the `main` branch. Render reads `render.yaml` and lists one web service, `ocpp-simulator`, on the
   free plan.
5. Render asks for a value for **SIM_TOKEN**. Paste the token you generated. It is stored as a secret on Render and is not
   written to the repo.
6. Click **Apply** (or **Deploy Blueprint**).
7. Open the service and watch the **Logs** tab. The first build takes a few minutes: it builds the UI, installs the Python
   packages, then starts the server. When the service shows **Live**, it is ready.
8. Open the service URL shown at the top of the service page, for example `https://ocpp-simulator.onrender.com` (the exact
   name can differ if it was taken), and enter your token at the **Access token** screen.

Every push to `main` redeploys automatically. To change the token later, edit `SIM_TOKEN` under the service's
**Environment** tab and redeploy.

### If the deploy fails

| Symptom | What to check |
|---|---|
| Build fails | Open **Logs** and read the first error. |
| Service restarts in a loop | `SIM_TOKEN` is missing under **Environment**. The server refuses to start without it. |
| Login says "Wrong token" | The token differs from the `SIM_TOKEN` value. After editing it, redeploy. |
| "Too many wrong tokens" | Ten wrong tries lock login for about five minutes. Wait and retry. |
| First load is very slow | The free tier sleeps after about 15 minutes idle and takes a while to wake. |

## Part 2: Make your CSMS reachable

The simulator on Render connects out to your CSMS, so the CSMS needs a public address.

1. **Deployed CSMS:** use its public `wss://` address.
2. **CSMS on your laptop:** expose it with a tunnel while you test, for example
   ```bash
   ngrok http 8000
   ```
   ngrok prints an address like `https://abc123.ngrok-free.app`. The WebSocket form of it is `wss://abc123.ngrok-free.app`.
   The address changes every time the tunnel restarts on a free ngrok plan.

The CSMS WebSocket endpoint is `<base>/ocpp/<charger id>`, so the **base URL you give the simulator** is
`wss://abc123.ngrok-free.app/ocpp`. The simulator appends the charger ID itself.

## Part 3: Prepare the CSMS side

Do this once per charger in your CSMS (the charging management system in `/Users/raghav/csms`):

1. **Create the station** under **Stations > Add station** with the **OCPP ID** you will use, for example `CP001`.
   The CSMS rejects unknown station IDs, so this must exist before the simulator connects.
2. **Password (optional):** if you set a charger password on the station, the CSMS requires HTTP Basic auth. Keep it for
   step 4 below. Leave it empty if the CSMS does not ask for one.
3. **Cards:** to start sessions by tapping a card, register the tokens the simulator uses by default, `CARD1` and `CARD2`,
   as accepted ID tokens (in the CSMS or through its API). An unknown token is refused, which is the correct behaviour.
4. **Tariff (optional):** assign a tariff to the site or connector if you want session costs to appear.

## Part 4: Connect the charger

In the simulator UI:

1. Click **Add charger**.
2. **WebSocket URL of the CSMS:** the base URL from part 2, for example `wss://abc123.ngrok-free.app/ocpp`.
3. **Charger ID:** the OCPP ID from part 3, for example `CP001`.
4. Check the **Connects to:** preview. It must read `<base URL>/<charger ID>` with subprotocol `ocpp2.0.1`.
5. **Password:** enter it only if the station has one in the CSMS. Set **Security profile** to **1 (Basic auth)** when
   using a password and a `ws://` or `wss://` URL, or to **2** with a `wss://` URL. Use **0** if there is no password.
6. Pick the **Hardware** (a preset or **Custom**), keep **Connect immediately** ticked, and click **Add charger**.

### What you should see

1. The charger header shows **connecting**, then **booting**, then **online**.
2. The simulator sends `BootNotification`; the CSMS answers `Accepted`.
3. Every connector then reports `StatusNotification` **Available**. The Connectors tab shows them as Available, and the CSMS
   Stations page shows the station online with its connectors.
4. The live log at the bottom lists each message in both directions.

### Run a first session

1. On the Connectors tab, choose a vehicle and click **Plug in vehicle**. The connector becomes **Occupied**.
2. Enter `CARD1` and click **Tap card**. The simulator sends `Authorize`; if the CSMS accepts it, a transaction starts and the
   connector shows **Charging**.
3. In the CSMS, the session appears under Sessions and its energy and cost grow.
4. Click **Stop session**, then **Unplug**. The CSMS marks the session completed.

### Try the test cases

- **Test cases** tab: run `smoke_boot`, `rfid_session`, then `power_loss_session` or `meter_failure`.
- **Faults** tab: inject `power_meter_failure` or `power_loss` while a session runs and watch what your CSMS does.

## If the charger will not connect

| What you see | Likely cause |
|---|---|
| Stays **connecting** or **offline**, Reconnects keeps rising | The URL is wrong or unreachable from the internet. Open the Render **Logs** and the simulator's log panel for the error. A `localhost` or private-network URL cannot work from Render. |
| Error mentions HTTP 401 or 403 | The station has a password in the CSMS. Enter it and use security profile 1 or 2. |
| Error mentions HTTP 404 or the station is refused | The OCPP ID does not exist in the CSMS. Create it in part 3. |
| Error mentions subprotocol | The CSMS endpoint is not speaking `ocpp2.0.1`. Check the URL path ends in `/ocpp`. |
| Boot is `Pending` or `Rejected` | A CSMS policy. The charger retries after the interval the CSMS returns. |
| Tap card says Invalid | The card is not registered as an accepted token in the CSMS. |
| It worked, then everything vanished | The Render free tier slept or restarted and the simulator keeps stations in memory. Open the page and add the charger again. |
| The ngrok address stopped working | A free ngrok address changes on restart. Use the new address and edit the charger (**Edit**, while disconnected) or add it again. |

## Notes

- The simulator forgets all chargers when Render sleeps or redeploys. Your last form values are remembered in the browser,
  so re-adding a charger takes a few clicks.
- Treat `SIM_TOKEN` like a password: anyone who has it can make your simulator connect to any URL and operate it.
- To run it on your own machine instead, see the tunnel option in the README (`scripts/share.sh`).
