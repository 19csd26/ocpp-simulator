# Volkai feature guide

A complete explanation of every feature of Volkai: what it is, how it works inside, a flow diagram, a sequence diagram, and a worked example you can
repeat, so you can understand a feature, test your CSMS with it, and inject the failure you want.

> Volkai: simulate any charger, break it on purpose. Designed and built by Raghav ([@19csd26](https://github.com/19csd26)).

## How to read a feature page

Every feature follows the same shape:

| Part | What you get |
|---|---|
| **What it is** | A plain definition |
| **How it works** | What the simulator does, and the settings that change it |
| **Flow** | A flow diagram of the decisions it makes |
| **Sequence** | The OCPP messages between the charger and your CSMS, in order |
| **Try it** | A full example: the clicks in the UI, the same thing through the API, and what you should see in the log and in your CSMS |

The diagrams use [Mermaid](https://mermaid.js.org) and render on GitHub. In the sequence diagrams **CP** is the simulated charge point (Volkai) and
**CSMS** is your central system.

## The pages

| # | Page | Features inside |
|---|---|---|
| 1 | [Connect, versions and boot](01-connect-and-versions.md) | Charger identity and URL, security, OCPP 1.6 / 2.0.1 and **Auto** detection, boot and connectors online, heartbeat, reconnect and back-off, time scale, hardware, editing a charger |
| 2 | [Sessions and cards](02-sessions-and-cards.md) | Vehicles, plug in, authorization (cards, cache, local list, offline rules), a session start to end for both versions, remote start / stop, stop reasons, **auto charge**, **smart plug**, Finishing mode, reservations, the cards used by tests |
| 3 | [Metering and V2G](03-metering-and-v2g.md) | The meter register, measurands, sampling, the meter register you can set, power limits, gun temperature, **bidirectional charging (V2G)** |
| 4 | [Faults and injection](04-faults-and-injection.md) | How to inject, **power meter failure** (5 modes), **power loss**, **network loss and the offline queue**, flapping, emergency stop, and the rest; how a fault looks on 1.6 and 2.0.1 |
| 5 | [Message rules](05-message-rules.md) | Delay, drop, duplicate or corrupt what the charger sends; time out, error, reject or garble what the CSMS asks |
| 6 | [Scenarios](06-scenarios.md) | Built-in test cases, the step language, writing your own, how a run is judged |
| 7 | [How the charger answers your CSMS](07-csms-commands.md) | Remote start / stop, reset, availability, trigger, firmware, logs, local list, reservations, charging profiles, configuration |
| 8 | [Tools](08-tools.md) | Send a message, **simulate a CSMS command**, **raw frame**, the live log, the station display, the Device model and State tabs, saved CSMS addresses |
| 9 | [API cheat sheet](09-api-cheatsheet.md) | Every endpoint with a `curl` example |
| 10 | [Recipes and troubleshooting](10-recipes.md) | End-to-end tests to run against your CSMS, and what to do when something does not work |

## Reference pages (generated from the code)

These always match the simulator because they are produced by `scripts/gen_guide.py` and checked by the test-suite.

| Page | Contents |
|---|---|
| [Faults](reference/faults.md) | All 24 faults: scope, what each does, what your CSMS should do, every parameter |
| [Scenarios](reference/scenarios.md) | All 35 scenarios, step by step |
| [Messages](reference/messages.md) | Every message and command for OCPP 2.0.1 and 1.6, and when it happens |
| [Settings](reference/settings.md) | The 2.0.1 device model, the 1.6 configuration keys, vehicles and hardware presets |

## Quick glossary

| Term | Meaning |
|---|---|
| **CSMS** | The central system your charger talks to (the server you are testing) |
| **CP** | Charge point, the charger. Here it is simulated |
| **EVSE / connector** | In 2.0.1 an EVSE is a charging point with one or more connectors. In 1.6 there are only connectors, numbered across the whole charger |
| **Session / transaction** | One charge, from authorization to the end |
| **Register** | The charger's lifetime energy meter. A session's energy is the register at the end minus the register at the start |
| **idTag / idToken** | The card or token that identifies the driver (1.6 calls it an idTag, 2.0.1 an idToken) |
| **Fault** | A failure Volkai injects on purpose |
| **Rule** | A change to how one message is sent or answered |
| **Scenario** | A scripted test case with checks that pass or fail |

---
Volkai · designed and built by Raghav ([@19csd26](https://github.com/19csd26)) · Copyright © 2026 Raghav (19csd26). All rights reserved.
