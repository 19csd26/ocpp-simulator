# 2. Sessions and cards

[Guide index](README.md) · previous: [Connect, versions and boot](01-connect-and-versions.md) · next: [Metering and V2G](03-metering-and-v2g.md)

A **session** (OCPP calls it a transaction) is one charge, from the moment a driver is authorized to the moment it ends. This page explains how a session starts
and ends in Volkai, on both OCPP versions, and the features around it: cards, remote start, auto charge, smart plug, reservations and the rest.

## 2.1 Vehicles and plugging in

**What it is.** A simulated electric vehicle with a battery, a start level, a target level and the power it can accept. You plug it into a connector like a driver would.

**How it works.** The vehicle models are listed in the [settings reference](reference/settings.md#vehicles): `city` (30 kWh), `mid` (60 kWh), `large` (100 kWh) and `scooter` (3 kWh).
The charging power is the smallest of: the charger's limit (including any charging profile from your CSMS), the vehicle's own maximum for AC or DC, and the EVSE rating. On DC the power **tapers** above 80 % battery.
Plugging in sets the connector status (`Occupied` on 2.0.1, `Preparing` on 1.6); nothing is charged until a card has been accepted.

**Try it.** On the Connectors tab pick a vehicle, set **Start SoC** to 20 % and **Charge to** 100 %, click **Plug in vehicle**. The connector turns `Occupied` (1.6: `Preparing`) and the live log shows the status.

```bash
curl -s -X POST http://localhost:8100/api/stations/$ID/evse/1/plug-in -H 'content-type: application/json' \
  -d '{"connector":1,"ev":"mid","soc":20,"target_soc":100}'
```

During a session the same card lets you: **Vehicle pauses** / **Resume** (the vehicle stops drawing power), **SoC +10**, **Make full** (the battery reaches its target at once), and **Unplug**.

## 2.2 Cards and authorization

**What it is.** A driver identifies with a card (an RFID id token). Volkai asks your CSMS to approve it with `Authorize`, and only an `Accepted` answer lets the session start.

**How it works.** The **Tap card** button (or `POST .../tap`) runs the following. The three offline fallbacks are controlled by the device model variables shown.

```mermaid
flowchart TD
    T["Card tapped"] --> R{"Reader failed?"}
    R -->|"yes"| R1["Nothing is read<br/>no Authorize is sent"]
    R -->|"no"| S{"Session already running?"}
    S -->|"same card"| STOP["Stop the session<br/>reason Local"]
    S -->|"other card"| OWN["Refused: not the session owner"]
    S -->|"no"| RES{"Connector reserved for another token?"}
    RES -->|"yes"| RES1["Refused: reserved for another"]
    RES -->|"no"| L{"Local list says Accepted<br/>and LocalPreAuthorize is on?"}
    L -->|"yes"| OK["Accepted from the local list"]
    L -->|"no"| ON{"Online?"}
    ON -->|"yes"| A["Send Authorize"]
    A --> AR{"Answer"}
    AR -->|"Accepted"| OK2["Accepted<br/>remember in the cache"]
    AR -->|"Invalid, Blocked, Expired"| NO["Refused: no session"]
    AR -->|"error or timeout"| OFF
    ON -->|"no"| OFF{"LocalAuthorizeOffline on?"}
    OFF -->|"yes"| LL{"In the local list<br/>or the valid cache?"}
    LL -->|"yes"| OK3["Use that status"]
    LL -->|"no"| UNK
    OFF -->|"no"| UNK{"OfflineTxForUnknownIdEnabled on?"}
    UNK -->|"yes"| OK4["Accepted offline"]
    UNK -->|"no"| NO
```

| Variable (2.0.1 device model; 1.6 key in brackets) | Default | Effect |
|---|---|---|
| `AuthCtrlr.LocalPreAuthorize` (`LocalPreAuthorize`) | off | Accept a card from the local list **without** asking the CSMS |
| `AuthCtrlr.LocalAuthorizeOffline` (`LocalAuthorizeOffline`) | on | While offline, trust the local list and the cache |
| `AuthCtrlr.OfflineTxForUnknownIdEnabled` (`AllowOfflineTxForUnknownId`) | off | While offline, accept any card |
| `AuthCacheCtrlr.Enabled` (`AuthorizationCacheEnabled`) / `LifeTime` | on / 86400 s | Remember cards the CSMS accepted; valid for the lifetime |

The tap result is one of `Accepted`, `Invalid` / `Blocked` / `Expired` (the CSMS refused), `Stopped` (the same card ended the session), `NotTheSessionOwner`,
`ReservedForOther`, `Unavailable` (the connector is faulted or inoperative) or `ReaderFailure`.

**Token types (2.0.1).** Next to the card box you can pick `ISO14443` (default), `ISO15693`, `eMAID`, `Central`, `Local` or `NoAuthorization`; it goes into the `idToken.type` field. OCPP 1.6 has no types, and cuts a card id to 20 characters (its `idTag` limit).

### Sequence (card accepted, OCPP 2.0.1)

```mermaid
sequenceDiagram
    participant CP as CP (Volkai)
    participant CSMS as CSMS
    CP->>CSMS: StatusNotification connector Occupied (vehicle plugged in)
    Note over CP: Tap card CARD1
    CP->>CSMS: Authorize idToken CARD1, type ISO14443
    CSMS-->>CP: idTokenInfo status Accepted
    Note over CP: Session can start
```

**Try it: a refused card.** Tap `UNKNOWN-0000` while connected to a CSMS that does not know it. The tap result is `Invalid`, the log shows `Authorize → Invalid`, and no session starts.
That is the expected behaviour, and is what the scenario `unknown_token` checks.

## 2.3 The cards used by tests

**What it is.** The card id that the **Tap card** box starts with and that **every built-in scenario** taps. Two are configured per charger: the **card** (default `CARD1`) and a **second card** (default `CARD2`, used by scenarios that run two sessions at once).

**Why it matters.** A real CSMS only accepts cards it knows. If your CSMS has never heard of `CARD1`, every scenario that charges will stop at "Charging starts", because the CSMS answers `Invalid`. Set the card to one your CSMS accepts: in **Add charger** or **Edit** (under the OCPP version), field **Card used by the Tap button and the tests**.

**How it works.** Scenario steps use the placeholders `$card` and `$card2`, resolved when the step runs. When a step times out because the CSMS refused the card, the failure message says so:

```
Charging starts: timed out after 20s waiting for expect:tx_state evse=1, state=Charging.
The CSMS refused the card 'CARD1' (Invalid): register it in your CSMS, or set the cards this charger uses (Edit charger, 'Cards used by tests').
```

**Try it.**

```bash
curl -s -X PATCH http://localhost:8100/api/stations/$ID -H 'content-type: application/json' -d '{"card_id":"MY-REAL-CARD","card_id_2":"MY-OTHER-CARD"}'
```

The card can be changed while the charger is connected. The `unknown_token` scenario always uses its own made-up card, because it needs one the CSMS does **not** know.

## 2.4 A session from start to end

**What it is.** The life of one charge. The **charging state** of the session moves between these states:

```mermaid
stateDiagram-v2
    [*] --> Idle
    Idle --> EVConnected: vehicle plugged in
    EVConnected --> Charging: card accepted and power allowed
    Charging --> SuspendedEV: vehicle pauses or battery full
    Charging --> SuspendedEVSE: limit is zero, fault, or inoperative
    SuspendedEV --> Charging: vehicle resumes
    SuspendedEVSE --> Charging: fault cleared or limit restored
    Charging --> EVConnected: session ends, cable still in
    Charging --> Idle: session ends and cable removed
    EVConnected --> Idle: cable removed
```

### OCPP 2.0.1: `TransactionEvent`

By default (`TxCtrlr.TxStartPoint = PowerPathClosed`) the session starts when a card has been accepted **and** power can flow. Other start points (`EVConnected`, `Authorized`) start it earlier.
`TxStopPoint` (default `EVConnected,Authorized`) decides what ends it: unplugging or a stop card.

```mermaid
sequenceDiagram
    participant CP as CP (Volkai)
    participant CSMS as CSMS
    CP->>CSMS: StatusNotification Occupied
    CP->>CSMS: Authorize CARD1
    CSMS-->>CP: Accepted
    CP->>CSMS: TransactionEvent Started seqNo 0, trigger ChargingStateChanged, chargingState Charging, meter 101000 Wh
    CSMS-->>CP: (empty result)
    loop every TxUpdatedInterval
        CP->>CSMS: TransactionEvent Updated, trigger MeterValuePeriodic, energy, power, current, voltage, SoC
    end
    Note over CP: Tap CARD1 again, or Stop session
    CP->>CSMS: TransactionEvent Ended, trigger StopAuthorized, stoppedReason Local
    CSMS-->>CP: (empty result)
    Note over CP: Unplug vehicle
    CP->>CSMS: StatusNotification Available
```

`seqNo` counts up from 0 within the transaction. The `Started` event carries the card (`idToken`) once, and the meter register at the start (`Transaction.Begin`).

### OCPP 1.6J: `StartTransaction`, `MeterValues`, `StopTransaction`

```mermaid
sequenceDiagram
    participant CP as CP (Volkai)
    participant CSMS as CSMS
    CP->>CSMS: StatusNotification connector 1 Preparing
    CP->>CSMS: Authorize idTag CARD1
    CSMS-->>CP: idTagInfo Accepted
    CP->>CSMS: StartTransaction connectorId 1, idTag CARD1, meterStart 101000
    CSMS-->>CP: transactionId 12, idTagInfo Accepted
    CP->>CSMS: StatusNotification connector 1 Charging
    loop every MeterValueSampleInterval
        CP->>CSMS: MeterValues transactionId 12, energy, power, current, voltage
    end
    Note over CP: Stop session
    CP->>CSMS: StopTransaction transactionId 12, meterStop 101250, reason Local
    CSMS-->>CP: idTagInfo Accepted
    CP->>CSMS: StatusNotification connector 1 Finishing
    Note over CP: Unplug vehicle
    CP->>CSMS: StatusNotification connector 1 Available
```

The transaction id is whatever **your CSMS** returns in the `StartTransaction` answer; Volkai remembers it and uses it in every later `MeterValues` and in `StopTransaction`.
If the CSMS answers the start with a status other than `Accepted` (for example `Invalid`), Volkai stops the session at once with reason `DeAuthorized`.

**Try it (either version).**

1. Plug in a vehicle, tap a card the CSMS accepts: the connector shows **Charging**, the meter box shows power in kW and the session energy in kWh.
2. Wait a few seconds: the session energy grows, and the log shows the periodic updates.
3. Click **Stop session** (or tap the same card): the log shows the end message, and the connector goes `Occupied` (1.6: `Finishing`).
4. **Unplug**: the connector goes `Available`.

```bash
curl -s -X POST http://localhost:8100/api/stations/$ID/evse/1/tap    -H 'content-type: application/json' -d '{"token":"CARD1"}'
curl -s -X POST http://localhost:8100/api/stations/$ID/evse/1/stop   -H 'content-type: application/json' -d '{"reason":"Local"}'
curl -s -X POST http://localhost:8100/api/stations/$ID/evse/1/unplug
```

## 2.5 How a session ends: stop reasons

The **Stop session** button has a reason menu; the other reasons happen by themselves.

| Reason | When it happens |
|---|---|
| `Local` | You tap the same card or click Stop session |
| `Remote` | Your CSMS sent `RequestStopTransaction` / `RemoteStopTransaction` |
| `EVDisconnected` | The vehicle is unplugged (or the `cable_unplugged` fault) |
| `StoppedByEV` | The battery reached its target (`end on full` is on) |
| `DeAuthorized` | 1.6: the CSMS refused the card in the `StartTransaction` answer |
| `EmergencyStop`, `GroundFault`, `OvercurrentFault`, `PowerQuality` | The matching [fault](04-faults-and-injection.md) |
| `PowerLoss` | After a power cut, when the charger comes back (see [power loss](04-faults-and-injection.md#42-power-loss)) |
| `ImmediateReset`, `Reboot` | Your CSMS reset the charger, or the watchdog rebooted it |
| `Timeout` | A card was accepted but no vehicle was plugged in within `TxCtrlr.EVConnectionTimeOut` (60 s). With the default start point no session exists yet, so the authorization simply expires (the log says *authorisation expired*); with start point `Authorized` the session already started and ends with this reason |
| `Other` | A fault that ends the session without a better name, for example the meter failure with *stop session* |

OCPP 1.6 has a shorter list: `ImmediateReset` becomes `HardReset`, and a reason 1.6 has no word for is sent as `Other`.

## 2.6 Remote start and remote stop (from your CSMS)

**What it is.** The CSMS starts or stops a session for a driver without a card, for example from a mobile app. Volkai answers like a real charger.

**How it works.** For `RequestStartTransaction` (2.0.1) or `RemoteStartTransaction` (1.6) the charger **answers `Accepted` first** and only then does the work, as the standard requires:

```mermaid
flowchart TD
    A["Remote start received"] --> B{"Connector free<br/>and operative?"}
    B -->|"no"| R["Answer Rejected"]
    B -->|"yes"| C["Answer Accepted"]
    C --> D{"AuthorizeRemoteStart on?"}
    D -->|"yes"| E["Send Authorize for the token"] --> F{"Accepted?"}
    F -->|"no"| X["Cancel: the session never starts"]
    F -->|"yes"| G
    D -->|"no"| G{"Vehicle plugged in?"}
    G -->|"yes"| H["Start the session"]
    G -->|"no"| W["Wait up to EVConnectionTimeOut for a vehicle"]
    W -->|"vehicle plugged in"| H
    W -->|"timeout"| X
```

```mermaid
sequenceDiagram
    participant CSMS as CSMS
    participant CP as CP (Volkai)
    CSMS->>CP: RequestStartTransaction idToken REMOTE1 type Central, evseId 1, remoteStartId 7
    CP-->>CSMS: status Accepted
    Note over CP: vehicle is plugged in
    CP->>CSMS: TransactionEvent Started, trigger RemoteStart, remoteStartId 7
    CSMS-->>CP: (empty result)
    CSMS->>CP: RequestStopTransaction transactionId
    CP-->>CSMS: status Accepted
    CP->>CSMS: TransactionEvent Ended, trigger RemoteStop, stoppedReason Remote
```

On **1.6** the messages are `RemoteStartTransaction` (with `idTag` and `connectorId`) answered `Accepted`, then `StartTransaction`; and `RemoteStopTransaction` with the integer `transactionId`.

**Try it.** No CSMS needed: use **Simulate a command** in the Messages tab (see [Tools](08-tools.md#82-simulate-a-csms-command)). Plug in a vehicle, choose `RequestStartTransaction` (1.6: `RemoteStartTransaction`), click Run: the session starts. Choose `RequestStopTransaction` with the shown transaction id and Run: it ends with reason `Remote`.

## 2.7 Auto charge

**What it is.** Plug-and-charge: the vehicle identifies **itself** as soon as it is plugged in, so no card is tapped. Real systems do this with a vehicle id (for example ISO 15118 plug and charge, or a vehicle id read from the car).

**How it works.** Half a second after the cable goes in, if no session is running and no card is waiting, Volkai taps a card as if the driver did, using the **vehicle id** you set (default `VID:FCA47A10DC70`, up to 20 characters) with token type `eMAID`. It then takes exactly the normal path: `Authorize`, and a session if accepted. If your CSMS refuses the vehicle id nothing starts, which is the useful thing to test.

```mermaid
flowchart TD
    P["Vehicle plugged in"] --> A{"Auto charge on<br/>and no smart plug?"}
    A -->|"no"| W["Wait for a card"]
    A -->|"yes"| D["Wait 0.5 s"]
    D --> C{"Still no session<br/>and no card?"}
    C -->|"no"| W
    C -->|"yes"| T["Tap the vehicle id as an eMAID token"]
    T --> Z["Authorize, then the session starts if accepted"]
```

```mermaid
sequenceDiagram
    participant CP as CP (Volkai)
    participant CSMS as CSMS
    Note over CP: Auto charge on, vehicle id VID:ABC123
    CP->>CSMS: StatusNotification Occupied (vehicle plugged in)
    Note over CP: 0.5 s later, no card tapped
    CP->>CSMS: Authorize idToken VID:ABC123, type eMAID
    CSMS-->>CP: Accepted
    CP->>CSMS: TransactionEvent Started
```

**Try it.** Tick **Auto charge** in Add / Edit, set the vehicle id, plug in a vehicle: the session starts by itself and the log shows `auto charge, the vehicle presents VID:...`. The scenario `auto_charge` does the same and checks it.

## 2.8 Smart plug

**What it is.** A socket with no cable detection: the connector never shows that something is plugged in. It stays `Available` until your CSMS sends a **remote start**, then goes straight to charging, and returns to `Available` when the session ends.

**How it works.** With **Smart plug** on (set before connecting), plugging in does nothing visible, and a card tap has nothing to start. A remote start itself "switches the socket on" with the connected load: it creates the vehicle, starts the session and reports `Occupied` (2.0.1) or `Charging` (1.6). When the session ends the socket switches off, so there is no `Preparing` or `Finishing` status at all.

```mermaid
sequenceDiagram
    participant CSMS as CSMS
    participant CP as CP (Volkai, smart plug)
    Note over CP: connector Available, no cable detection
    CSMS->>CP: RemoteStartTransaction idTag REMOTE1, connectorId 1
    CP-->>CSMS: status Accepted
    CP->>CSMS: StartTransaction connectorId 1
    CSMS-->>CP: transactionId 5
    CP->>CSMS: StatusNotification Charging
    CSMS->>CP: RemoteStopTransaction transactionId 5
    CP-->>CSMS: Accepted
    CP->>CSMS: StopTransaction reason Remote
    CP->>CSMS: StatusNotification Available
```

**Try it.** Tick **Smart plug** when adding the charger, connect, then run the scenario `smart_plug_remote_start` from the Test cases tab (it simulates the remote start for you), or use **Simulate a command** with a remote start.

## 2.9 Finishing mode (OCPP 1.6)

**What it is.** In OCPP 1.6, after a session ends while the cable is still plugged in, the connector reports `Finishing` until the cable is removed. Some chargers report `Preparing` instead; the switch lets you test both.

**How it works.** **Finishing mode** is on by default. Turn it off in Add / Edit: after a stop the connector reports `Preparing`, and after the unplug `Available`.

## 2.10 Reservations

**What it is.** The CSMS holds a connector for one driver until a time.

**How it works.** `ReserveNow` makes the connector `Reserved`. Only the reserved token can start a session there (any other card is refused with `ReservedForOther`), and the matching token **uses up** the reservation (the reservation id is carried in the `Started` / `StartTransaction`). A reservation that is not used expires: the connector returns to `Available` and a 2.0.1 charger sends `ReservationStatusUpdate` with `Expired`. `CancelReservation` removes it. The **State** tab lists the active reservations.

**Try it.** With **Simulate a command**, run `ReserveNow` (the sample has a valid body). The connector becomes `Reserved`; tap a different card and the tap result is `ReservedForOther`.

---
Previous: [Connect, versions and boot](01-connect-and-versions.md) · Next: [Metering and V2G](03-metering-and-v2g.md)
