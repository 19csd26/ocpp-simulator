"""Scripted test cases.

A scenario is a list of steps. Action steps drive the station ("plug_in", "tap", "inject", "send", ...); `expect` steps
check what the station did or what the CSMS answered, retrying until they pass or time out. A scenario passes when
every step passes. Custom scenarios can be submitted as JSON using the same step format.
"""
import asyncio
import time
import uuid
from dataclasses import dataclass, field

from ocpp_simulator.faults import CATALOG
from ocpp_simulator.v16 import CP_ACTIONS_16, STOP_ALIASES, STOP_REASONS_16
from ocpp_simulator.templates import message_template16, message_template

DEFAULT_TIMEOUT = 10.0


def dig(payload, path: str):
    cur = payload
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def matches(payload, where: dict) -> bool:
    """`where` maps dotted camelCase paths to expected values; a list of dicts is searched (e.g. eventData.0.cleared)."""
    return all(dig(payload, k) == v for k, v in (where or {}).items())


@dataclass
class StepResult:
    index: int
    label: str
    status: str = "pending"  # pending | running | passed | failed | skipped
    detail: str = ""
    seconds: float = 0.0

    def public(self) -> dict:
        return {"index": self.index, "label": self.label, "status": self.status, "detail": self.detail, "seconds": round(self.seconds, 1)}


@dataclass
class Run:
    id: str
    scenario_id: str
    title: str
    steps: list[StepResult]
    status: str = "running"  # running | passed | failed | stopped
    started: float = field(default_factory=time.time)
    finished: float | None = None

    def public(self) -> dict:
        return {"id": self.id, "scenario_id": self.scenario_id, "title": self.title, "status": self.status, "started": self.started, "finished": self.finished,
                "steps": [s.public() for s in self.steps]}


def describe(step: dict) -> str:
    if "label" in step:
        return step["label"]
    kind = step.get("do") or "expect:" + str(step.get("expect"))
    extras = {k: v for k, v in step.items() if k not in ("do", "expect", "label", "timeout")}
    return kind + (" " + ", ".join(f"{k}={v}" for k, v in extras.items()) if extras else "")


class Runner:
    def __init__(self, station) -> None:
        self.st = station
        self.run: Run | None = None
        self.task: asyncio.Task | None = None
        self.history: list[Run] = []
        self.ctx: dict = {}
        self.restore: dict = {}  # settings a scenario changed temporarily, put back when it ends
        self.start_log_id = 0

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self, scenario_id: str, title: str, steps: list[dict]) -> Run:
        if self.running:
            raise ValueError("A scenario is already running on this station; stop it first")
        for i, step in enumerate(steps):
            if "do" not in step and "expect" not in step:
                raise ValueError(f"Step {i + 1} needs either 'do' or 'expect'")
            if "do" in step and step["do"] not in self.ACTIONS:
                raise ValueError(f"Step {i + 1}: unknown action '{step['do']}'. Known: {sorted(self.ACTIONS)}")
            if "expect" in step and step["expect"] not in self.EXPECTS:
                raise ValueError(f"Step {i + 1}: unknown expectation '{step['expect']}'. Known: {sorted(self.EXPECTS)}")
        self.run = Run(uuid.uuid4().hex[:8], scenario_id, title, [StepResult(i, describe(s)) for i, s in enumerate(steps)])
        self.ctx = {}
        logs = self.st.hub.logs[self.st.cfg.id]
        self.start_log_id = logs[-1]["id"] if logs else 0
        self.task = asyncio.get_running_loop().create_task(self._execute(self.run, steps))
        return self.run

    def stop(self) -> None:
        if self.running:
            self.task.cancel()

    def _publish(self) -> None:
        self.st.hub.publish({"type": "scenario", "station_id": self.st.cfg.id, "run": self.run.public()})

    def for_version(self, step: dict) -> dict | None:
        """A step may carry `only` (run on one OCPP version) and `v16` (fields replaced when the station speaks 1.6; None removes a field)."""
        only = step.get("only")
        if only and only != self.st.version:
            return None
        over = step.get("v16")
        step = {k: v for k, v in step.items() if k not in ("v16", "only")}
        if over and self.st.v16:
            step = {**step, **over}
            step = {k: v for k, v in step.items() if v is not None}
        return step

    async def _execute(self, run: Run, steps: list[dict]) -> None:
        try:
            for step, result in zip(steps, run.steps):
                resolved = self.for_version(step)
                if resolved is None:
                    result.status, result.detail = "skipped", f"not applicable to OCPP {self.st.version}"
                    self._publish()
                    continue
                step = resolved
                result.status = "running"
                self._publish()
                t0 = time.monotonic()
                try:
                    if "do" in step:
                        result.detail = await self.ACTIONS[step["do"]](self, step) or ""
                    else:
                        result.detail = await self._expect(step)
                    result.status = "passed"
                except AssertionError as exc:
                    result.status, result.detail = "failed", str(exc)
                except (ValueError, TypeError, KeyError) as exc:
                    result.status, result.detail = "failed", f"{type(exc).__name__}: {exc}"
                result.seconds = time.monotonic() - t0
                self._publish()
                if result.status == "failed" and not step.get("continue_on_fail"):
                    for rest in run.steps[result.index + 1:]:
                        rest.status = "skipped"
                    break
            run.status = "failed" if any(s.status == "failed" for s in run.steps) else "passed"
        except asyncio.CancelledError:
            run.status = "stopped"
            for s in run.steps:
                if s.status in ("running", "pending"):
                    s.status = "skipped"
            raise
        finally:
            for key, value in self.restore.items():
                setattr(self.st.cfg, key, value)
            self.restore = {}
            run.finished = time.time()
            self.history.append(run)
            self.history = self.history[-20:]
            self._publish()

    # ------------------------------------------------------------------------------------ actions
    async def a_connect(self, s):
        await self.st.connect()

    async def a_disconnect(self, s):
        await self.st.disconnect()

    async def a_plug_in(self, s):
        await self.st.plug_in(s.get("evse", 1), s.get("connector", 1), s.get("ev", "mid"), s.get("soc", 20), s.get("target_soc", 100))

    async def a_unplug(self, s):
        await self.st.unplug(s.get("evse", 1))

    def card(self, token: str | None) -> str:
        """`$card` / `$card2` (or nothing) mean the cards set on the charger, so a scenario works with whatever your CSMS accepts."""
        return {None: self.st.cfg.card_id, "$card": self.st.cfg.card_id, "$card2": self.st.cfg.card_id_2}.get(token, token)

    async def a_tap(self, s):
        token = self.card(s.get("token"))
        self.ctx["tap_card"] = token
        self.ctx["tap"] = await self.st.tap_card(s.get("evse", 1), token, s.get("type", "ISO14443"))
        return f"{token} → {self.ctx['tap'].get('status')}"

    async def a_stop(self, s):
        await self.st.stop_session(s.get("evse", 1), s.get("reason", "Local"), s.get("trigger", "StopAuthorized"))

    async def a_inject(self, s):
        fault = await self.st.inject_fault(s["fault"], s.get("scope"), s.get("params"))
        return f"fault #{fault['id']}"

    async def a_clear(self, s):
        target = s.get("fault", "all")
        if target == "all":
            return f"cleared {await self.st.clear_all_faults()} fault(s)"
        cleared = 0
        for f in [f for f in self.st.faults.values() if f.kind == target and f.active]:
            await self.st.clear_fault(f.id)
            cleared += 1
        if not cleared:
            raise AssertionError(f"no active '{target}' fault to clear")
        return f"cleared {cleared}"

    CONFIG_KEYS = {"auto_charge": bool, "smart_plug": bool, "finishing_mode": bool}

    async def a_config(self, s):
        """Change a station setting for the length of this scenario (auto_charge, smart_plug, finishing_mode)."""
        key = s["key"]
        if key not in self.CONFIG_KEYS:
            raise ValueError(f"Cannot change '{key}' from a scenario. Known: {sorted(self.CONFIG_KEYS)}")
        self.restore.setdefault(key, getattr(self.st.cfg, key))
        setattr(self.st.cfg, key, bool(s.get("value", True)))
        return f"{key} = {bool(s.get('value', True))} until the scenario ends"

    async def a_v2x(self, s):
        await self.st.set_v2x(s.get("evse", 1), s.get("mode", "discharge"), s.get("power_kw"))
        return f"{s.get('mode', 'discharge')}" + (f" at {s['power_kw']} kW" if s.get("power_kw") else "")

    async def a_reboot(self, s):
        await self.st.reboot_with(s.get("reason", "PowerUp"))

    async def a_simulate(self, s):
        """Run a CSMS command locally, as if the CSMS had sent it."""
        body = await self.st.simulate_command(s["action"], s.get("payload", {}))
        return f"{s['action']} → {body.get('status', 'ok')}"

    async def a_reset(self, s):
        """Leave nothing behind from an earlier run: clear every fault and unplug every vehicle."""
        faults = await self.st.clear_all_faults()
        unplugged = 0
        for evse in self.st.evses:
            if evse.plugged is not None:
                await self.st.unplug(evse.id)
                unplugged += 1
        return f"cleared {faults} fault(s), unplugged {unplugged} vehicle(s)"

    async def a_send(self, s):
        payload = s.get("payload") or (message_template16 if self.st.v16 else message_template)(s["action"], self.st)
        res = await self.st.send_raw(s["action"], payload, validate=s.get("validate", True))
        self.ctx["send"] = res
        return f"{s['action']} → {res.status or res.error or 'ok'}"

    async def a_set_variable(self, s):
        status, _ = self.st.model.write({"name": s["component"]}, {"name": s["variable"]}, str(s["value"]))
        assert status in ("Accepted", "RebootRequired"), f"{s['component']}.{s['variable']} was {status}"

    async def a_set_ev(self, s):
        await self.st.set_ev(s.get("evse", 1), ready=s.get("ready"), soc=s.get("soc"), target_soc=s.get("target_soc"))

    async def a_wait(self, s):
        await asyncio.sleep(float(s.get("seconds", 1)))

    async def a_wait_message(self, s):
        """Wait for a message (e.g. a command you send from your CSMS dashboard) to appear in the log."""
        return await self._poll(lambda: self._find_message(s), s.get("timeout", 60), f"message {s.get('action')} ({s.get('dir', 'in')})")

    ACTIONS = {"connect": a_connect, "disconnect": a_disconnect, "plug_in": a_plug_in, "unplug": a_unplug, "tap": a_tap, "stop": a_stop, "inject": a_inject,
               "clear": a_clear, "reset": a_reset, "config": a_config, "v2x": a_v2x, "reboot": a_reboot, "simulate": a_simulate, "send": a_send, "set_variable": a_set_variable, "set_ev": a_set_ev, "wait": a_wait, "wait_message": a_wait_message}

    # -------------------------------------------------------------------------------- expectations
    def _find_message(self, s: dict) -> str | None:
        want_dir = s.get("dir", "out")
        count = 0
        for e in self.st.hub.logs[self.st.cfg.id]:
            if e["id"] <= self.start_log_id or e["dir"] != want_dir or e["kind"] != s.get("kind", "call"):
                continue
            if s.get("action") and e["action"] != s["action"]:
                continue
            if matches(e["payload"], s.get("where", {})):
                count += 1
        need = s.get("count_gte", 1)
        return f"{count} matching message(s)" if count >= need else None

    async def _poll(self, check, timeout: float, what: str) -> str:
        deadline = time.monotonic() + timeout
        while True:
            result = check()
            if result:
                return result if isinstance(result, str) else "ok"
            if time.monotonic() > deadline:
                raise AssertionError(f"timed out after {timeout:g}s waiting for {what}")
            await asyncio.sleep(0.25)

    async def _expect(self, s: dict) -> str:
        try:
            return await self._poll(lambda: self.EXPECTS[s["expect"]](self, s), s.get("timeout", DEFAULT_TIMEOUT), self._what(s))
        except AssertionError as exc:
            tap = self.ctx.get("tap") or {}
            if tap.get("status") in ("Invalid", "Blocked", "Expired") and s.get("expect") not in ("tap_status",):
                raise AssertionError(f"{exc}. The CSMS refused the card {self.ctx.get('tap_card')!r} ({tap['status']}): register it in your CSMS, "
                                     "or set the cards this charger uses (Edit charger, 'Cards used by tests').") from None
            raise

    @staticmethod
    def _what(s: dict) -> str:
        return describe({k: v for k, v in s.items() if k != "label"})

    def e_online(self, s):
        return self.st.online if s.get("value", True) else not self.st.online

    def e_connector_status(self, s):
        c = self.st.connector(s.get("evse", 1), s.get("connector", 1))
        return self.st.compute_status(self.st.evse(s.get("evse", 1)), c) == s["status"] and f"status is {s['status']}"

    def e_tx_state(self, s):
        tx = self.st.evse(s.get("evse", 1)).tx
        want = s["state"]
        return (tx is None) if want == "none" else (tx is not None and tx.charging_state == want)

    def e_export_gt(self, s):
        tx = self.st.evse(s.get("evse", 1)).tx
        return tx is not None and tx.export_wh > s.get("wh", 0)

    def e_energy_gt(self, s):
        tx = self.st.evse(s.get("evse", 1)).tx
        return tx is not None and tx.energy_wh > s.get("wh", 0)

    def e_message_sent(self, s):
        return self._find_message({**s, "dir": "out", "kind": "call"})

    def e_message_received(self, s):
        return self._find_message({**s, "dir": "in", "kind": "call"})

    def e_response(self, s):
        got = self.st.last_responses.get(s["action"])
        if got is None:
            return False
        if "status" in s and got["status"] != s["status"]:
            return False
        return got["at"] >= self.run.started and f"{s['action']} → {got['status']}"

    def e_send_ok(self, s):
        res = self.ctx.get("send")
        return res is not None and res.sent and res.ok

    def e_tap_status(self, s):
        got = self.ctx.get("tap")
        return got is not None and got.get("status") == s["status"]

    def e_faulted(self, s):
        return self.st.evse_faulted(self.st.evse(s.get("evse", 1))) == s.get("value", True)

    def e_queue_gte(self, s):
        return len(self.st.tx_queue) >= s["n"] and f"{len(self.st.tx_queue)} queued"

    def e_queue_empty(self, s):
        return not self.st.tx_queue

    EXPECTS = {"online": e_online, "connector_status": e_connector_status, "tx_state": e_tx_state, "energy_gt": e_energy_gt, "export_gt": e_export_gt, "message_sent": e_message_sent,
               "message_received": e_message_received, "response": e_response, "send_ok": e_send_ok, "tap_status": e_tap_status, "faulted": e_faulted,
               "queue_gte": e_queue_gte, "queue_empty": e_queue_empty}


# ======================================================================================= catalogue
def _session_start(evse=1, token="$card", ev="large", soc=20):
    return [{"do": "plug_in", "evse": evse, "ev": ev, "soc": soc}, {"do": "tap", "evse": evse, "token": token},
            {"expect": "tx_state", "evse": evse, "state": "Charging", "timeout": 20, "label": "Charging starts"},
            {"expect": "energy_gt", "evse": evse, "wh": 5, "timeout": 20, "label": "Energy is flowing"}]


def _cleanup(evse=1):
    return [{"do": "clear", "fault": "all", "continue_on_fail": True},
            {"expect": "online", "timeout": 60, "label": "Station is back online"},
            {"do": "unplug", "evse": evse, "continue_on_fail": True}]


def _ended(reason, trigger=None, **extra):
    where = {"eventType": "Ended", "transactionInfo.stoppedReason": reason, **extra}
    if trigger:
        where["triggerReason"] = trigger
    reason16 = STOP_ALIASES.get(reason, reason)
    reason16 = reason16 if reason16 in STOP_REASONS_16 else "Other"
    return {"expect": "message_sent", "action": "TransactionEvent", "where": where, "timeout": 30, "label": f"CSMS received Ended ({reason})",
            "v16": {"action": "StopTransaction", "where": {"reason": reason16}, "label": f"CSMS received StopTransaction ({reason16})"}}


COMPONENT_ERROR_16 = {"LocalEnergyMeter": "PowerMeterFailure", "OverCurrentBreaker": "OverCurrentFailure", "GroundIsolationMonitoring": "GroundFailure",
                      "EVSE": "HighTemperature", "PowerContactor": "PowerSwitchFailure", "TokenReader": "ReaderFailure", "ElectricalFeeder": "OverVoltage"}


def _event(component, **where):
    code = "NoError" if where.get("eventData.0.cleared") else COMPONENT_ERROR_16.get(component, "OtherError")
    return {"expect": "message_sent", "action": "NotifyEvent", "where": {"eventData.0.component.name": component, **where}, "timeout": 20, "label": f"NotifyEvent for {component}",
            "v16": {"action": "StatusNotification", "where": {"errorCode": code}, "label": f"StatusNotification with errorCode {code}"}}


def _occupied():
    """The connector is in use but no transaction runs: Occupied in OCPP 2.0.1, Finishing in 1.6 (the session ended with the cable still plugged in)."""
    return {"expect": "connector_status", "status": "Occupied", "timeout": 15, "v16": {"status": "Finishing"}}


def build_catalog() -> dict[str, dict]:
    S: dict[str, dict] = {}

    def add(sid, title, group, description, steps):
        S[sid] = {"id": sid, "title": title, "group": group, "description": description, "steps": steps}

    add("smoke_boot", "Boot and connectors online", "Basics", "The station connects, is accepted by the CSMS and reports every connector Available.",
        [{"do": "disconnect"}, {"do": "connect"}, {"expect": "online", "timeout": 30}, {"expect": "response", "action": "BootNotification", "status": "Accepted", "timeout": 20},
         {"expect": "connector_status", "evse": 1, "status": "Available"}])
    add("rfid_session", "RFID charging session", "Basics", "Plug in, tap a card, charge for a few seconds, tap again to stop, unplug.",
        [*_session_start(), {"do": "wait", "seconds": 4}, {"do": "tap"}, {"expect": "tx_state", "state": "none", "label": "Session ended"},
         _ended("Local", "StopAuthorized"), {"do": "unplug"}, {"expect": "connector_status", "status": "Available", "timeout": 15}])
    add("unknown_token", "Unknown RFID card is refused", "Basics", "A card the CSMS does not know must not start a session. Fails if the CSMS accepts it.",
        [{"do": "plug_in"}, {"do": "tap", "token": "UNKNOWN-TOKEN-0000"}, {"expect": "tap_status", "status": "Invalid", "label": "CSMS answers Invalid / Blocked"},
         {"expect": "tx_state", "state": "none"}, {"do": "unplug"}])
    add("authorize_then_plug", "Authorize first, plug in later", "Basics", "The driver taps a card before connecting the cable; the session starts when the vehicle is connected.",
        [{"do": "tap"}, {"do": "plug_in", "ev": "large"}, {"expect": "tx_state", "state": "Charging", "timeout": 20}, {"do": "stop"}, {"do": "unplug"}])
    add("remote_start_from_csms", "Remote start / stop from your CSMS", "Basics",
        "Interactive: plug a vehicle in, then send RequestStartTransaction (and later RequestStopTransaction) from your CSMS dashboard or app.",
        [{"do": "plug_in", "ev": "large"}, {"do": "wait_message", "dir": "in", "action": "RequestStartTransaction", "timeout": 180, "label": "WAIT: start the session from your CSMS now", "v16": {"action": "RemoteStartTransaction"}},
         {"expect": "tx_state", "state": "Charging", "timeout": 30}, {"do": "wait_message", "dir": "in", "action": "RequestStopTransaction", "timeout": 180, "label": "WAIT: stop it from your CSMS", "v16": {"action": "RemoteStopTransaction"}},
         {"expect": "tx_state", "state": "none", "timeout": 30}, {"do": "unplug"}])
    add("two_sessions", "Two connectors charging at once", "Basics", "Two EVSEs run independent sessions at the same time (needs a station with at least 2 EVSEs).",
        [*_session_start(1, "$card"), *_session_start(2, "$card2"), {"do": "stop", "evse": 1}, {"expect": "tx_state", "evse": 2, "state": "Charging"},
         {"do": "stop", "evse": 2}, {"do": "unplug", "evse": 1}, {"do": "unplug", "evse": 2}])
    add("battery_full", "Vehicle becomes full", "Basics", "A nearly full small battery finishes: SuspendedEV then Ended (StoppedByEV).",
        [{"do": "plug_in", "ev": "scooter", "soc": 99}, {"do": "tap"}, _ended("StoppedByEV"), {"do": "unplug"}])

    add("meter_failure", "Power meter failure during a session", "Metering",
        "The meter stops reporting, then returns. The CSMS must see the NotifyEvent and keep the session consistent.",
        [*_session_start(), {"do": "inject", "fault": "power_meter_failure", "scope": 1, "params": {"mode": "stop_reporting"}}, _event("LocalEnergyMeter", **{"eventData.0.actualValue": "true"}),
         {"do": "wait", "seconds": 8}, {"do": "clear", "fault": "power_meter_failure"}, _event("LocalEnergyMeter", **{"eventData.0.cleared": True}),
         {"do": "wait", "seconds": 3}, {"do": "tap"}, {"expect": "tx_state", "state": "none"}, {"do": "unplug"}])
    for mode, text in (("frozen", "register stops changing"), ("rollback", "register goes backwards"), ("spike", "register jumps up by 10,000 kWh"), ("invalid", "register reports an invalid value")):
        add(f"meter_{mode}", f"Meter {mode}: {text}", "Metering", f"A running session reports a meter whose {text}. Check how the CSMS treats the energy and billing.",
            [*_session_start(), {"do": "inject", "fault": "power_meter_failure", "scope": 1, "params": {"mode": mode}}, {"do": "wait", "seconds": 8},
             {"do": "clear", "fault": "power_meter_failure"}, {"do": "wait", "seconds": 3}, {"do": "tap"}, {"do": "unplug"}])
    add("meter_failure_stops_session", "Meter failure ends the session", "Metering", "The meter dies and the station ends the session and goes Faulted.",
        [*_session_start(), {"do": "inject", "fault": "power_meter_failure", "scope": 1, "params": {"mode": "stop_reporting", "stop_session": True}},
         _ended("Other", "AbnormalCondition"), {"expect": "connector_status", "status": "Faulted", "timeout": 15}, {"do": "clear", "fault": "power_meter_failure"},
         _occupied(), {"do": "unplug"}])

    add("power_loss_session", "Power loss during a session", "Power & network",
        "Power fails for 10 s while charging. On return the station boots (PowerUp) and closes the transaction with PowerLoss.",
        [*_session_start(), {"do": "inject", "fault": "power_loss", "params": {"duration_s": 10}}, {"expect": "online", "value": False, "timeout": 10, "label": "Station goes dark"},
         {"expect": "online", "timeout": 40, "label": "Power returns and the station boots"}, {"expect": "message_sent", "action": "BootNotification", "where": {"reason": "PowerUp"}, "label": "CSMS received Boot (PowerUp) after the outage", "v16": {"where": None, "label": "CSMS received a Boot after the outage"}},
         _ended("PowerLoss", "AbnormalCondition", offline=True), {"do": "unplug"}])
    add("power_loss_idle", "Power loss with no session", "Power & network", "The idle station loses power for 8 s and comes back.",
        [{"do": "inject", "fault": "power_loss", "params": {"duration_s": 8}}, {"expect": "online", "value": False, "timeout": 10}, {"expect": "online", "timeout": 40},
         {"expect": "connector_status", "status": "Available", "timeout": 15}])
    add("network_loss_session", "Network loss while charging (offline queue)", "Power & network",
        "The WebSocket drops for 12 s while a session runs. Events are queued with offline=true and delivered in order afterwards.",
        [{"do": "set_variable", "component": "SampledDataCtrlr", "variable": "TxUpdatedInterval", "value": 3, "label": "Report every 3 s so the outage queues events"}, *_session_start(), {"do": "inject", "fault": "network_loss", "params": {"duration_s": 12}}, {"expect": "queue_gte", "n": 2, "timeout": 20, "label": "Events pile up offline"},
         {"expect": "online", "timeout": 60, "label": "Reconnected"}, {"expect": "queue_empty", "timeout": 30, "label": "Queue delivered"},
         {"expect": "message_sent", "action": "TransactionEvent", "where": {"offline": True}, "count_gte": 2, "label": "CSMS received offline events",
          "v16": {"action": "MeterValues", "where": None, "label": "CSMS received the queued MeterValues"}}, {"do": "tap"}, {"do": "unplug"}])
    add("unstable_network", "Unstable network (flapping)", "Power & network", "The connection drops and returns three times.",
        [{"do": "inject", "fault": "network_flapping", "params": {"cycles": 3, "down_s": 3, "up_s": 5}}, {"expect": "message_sent", "action": "BootNotification", "count_gte": 3, "timeout": 60},
         {"expect": "online", "timeout": 40}])
    add("high_latency", "Slow network (high latency)", "Power & network", "Every message is delayed by 2 s for 20 s while a session runs.",
        [*_session_start(), {"do": "inject", "fault": "high_latency", "params": {"latency_ms": 2000, "duration_s": 20}}, {"do": "wait", "seconds": 22}, {"do": "tap"}, {"do": "unplug"}])
    add("watchdog_reboot", "Watchdog reboot during a session", "Power & network", "The charger crashes and reboots: Ended (Reboot) then Boot (Watchdog).",
        [*_session_start(), {"do": "inject", "fault": "reboot_watchdog"}, _ended("Reboot"), {"expect": "message_sent", "action": "BootNotification", "where": {"reason": "Watchdog"}, "timeout": 30, "v16": {"where": None}},
         {"expect": "online", "timeout": 30}, {"do": "unplug"}])
    add("clock_drift", "Station clock is wrong", "Power & network", "Timestamps are one hour ahead for 15 s while charging.",
        [*_session_start(), {"do": "inject", "fault": "clock_drift", "params": {"offset_s": 3600, "duration_s": 15}}, {"do": "wait", "seconds": 16}, {"do": "tap"}, {"do": "unplug"}])

    add("emergency_stop", "Emergency stop during a session", "Safety & electrical", "The emergency button stops the session (EmergencyStop) and the EVSE goes Faulted until released.",
        [*_session_start(), {"do": "inject", "fault": "emergency_stop", "scope": 1}, _ended("EmergencyStop"), {"expect": "connector_status", "status": "Faulted", "timeout": 15},
         {"do": "clear", "fault": "emergency_stop"}, _occupied(), {"do": "unplug"}])
    add("over_current", "Over-current trip", "Safety & electrical", "The breaker trips: Ended (OvercurrentFault), Faulted, then recovery.",
        [*_session_start(), {"do": "inject", "fault": "over_current", "scope": 1}, _ended("OvercurrentFault"), _event("OverCurrentBreaker"), {"do": "clear", "fault": "over_current"}, {"do": "unplug"}])
    add("ground_fault", "Ground / earth fault", "Safety & electrical", "Leakage current: Ended (GroundFault), Faulted, then recovery.",
        [*_session_start(), {"do": "inject", "fault": "ground_fault", "scope": 1}, _ended("GroundFault"), _event("GroundIsolationMonitoring"), {"do": "clear", "fault": "ground_fault"}, {"do": "unplug"}])
    add("over_voltage", "Grid over-voltage", "Safety & electrical", "Voltage rises to 280 V for 15 s: Ended (PowerQuality) and Faulted connectors, then recovery.",
        [*_session_start(), {"do": "inject", "fault": "over_voltage", "params": {"voltage": 280, "duration_s": 15}}, _ended("PowerQuality"),
         {"expect": "connector_status", "status": "Faulted", "timeout": 15}, _occupied(), {"do": "unplug"}])
    add("over_temperature", "Over-temperature derating", "Safety & electrical", "The connector overheats: power is halved for 20 s.",
        [*_session_start(), {"do": "inject", "fault": "over_temperature", "scope": 1, "params": {"mode": "derate", "derate_percent": 40, "duration_s": 20}},
         _event("EVSE", **{"eventData.0.variable.name": "Temperature"}), {"do": "wait", "seconds": 22}, {"do": "tap"}, {"do": "unplug"}])
    add("tamper", "Tamper / door opened", "Safety & electrical", "The enclosure is opened: SecurityEventNotification and NotifyEvent.",
        [{"do": "inject", "fault": "tamper_detected"}, {"expect": "message_sent", "action": "SecurityEventNotification", "where": {"type": "TamperDetectionActivated"}, "timeout": 15,
                                                                                    "v16": {"action": "StatusNotification", "where": {"errorCode": "OtherError"}}}])

    add("contactor_failure", "Contactor fails to close", "Hardware", "A vehicle connects and is authorised, but power never flows.",
        [{"do": "inject", "fault": "contactor_failure", "scope": 1}, {"do": "plug_in"}, {"do": "tap"}, {"do": "wait", "seconds": 5}, {"expect": "tx_state", "state": "none"},
         _event("PowerContactor"), {"do": "clear", "fault": "contactor_failure"}, {"expect": "tx_state", "state": "Charging", "timeout": 20}, {"do": "stop"}, {"do": "unplug"}])
    add("reader_failure", "RFID reader failure", "Hardware", "The card reader is down: a tap is not read and no Authorize is sent.",
        [{"do": "inject", "fault": "rfid_reader_failure"}, {"do": "plug_in"}, {"do": "tap"}, {"expect": "tap_status", "status": "ReaderFailure"}, _event("TokenReader"),
         {"do": "clear", "fault": "rfid_reader_failure"}, {"do": "unplug"}])
    add("ev_comm_loss", "Vehicle communication loss", "Vehicle", "The control signal to the vehicle is lost, then restored.",
        [*_session_start(), {"do": "inject", "fault": "ev_communication_loss", "scope": 1}, {"expect": "tx_state", "state": "SuspendedEV", "timeout": 10},
         {"expect": "message_sent", "action": "TransactionEvent", "where": {"triggerReason": "EVCommunicationLost"}, "only": "2.0.1"}, {"do": "clear", "fault": "ev_communication_loss"},
         {"expect": "tx_state", "state": "Charging", "timeout": 10}, {"do": "tap"}, {"do": "unplug"}])
    add("cable_pulled", "Cable pulled during charging", "Vehicle", "The driver unplugs mid-session: Ended (EVDisconnected).",
        [*_session_start(), {"do": "inject", "fault": "cable_unplugged", "scope": 1}, _ended("EVDisconnected", "EVDeparted"), {"expect": "connector_status", "status": "Available", "timeout": 15}])
    add("grid_overload", "Grid overload (energy management limit)", "Smart charging", "An external limit caps the site at 4 kW for 20 s: NotifyChargingLimit then ClearedChargingLimit.",
        [*_session_start(), {"do": "inject", "fault": "grid_overload", "params": {"limit_kw": 4, "duration_s": 20}},
         {"expect": "message_sent", "action": "NotifyChargingLimit", "timeout": 15, "only": "2.0.1"}, {"expect": "message_sent", "action": "ClearedChargingLimit", "timeout": 40, "only": "2.0.1"}, {"do": "tap"}, {"do": "unplug"}])

    add("v2g_discharge", "Vehicle-to-grid discharge", "Bidirectional charging",
        "A bidirectional vehicle charges, then feeds 5 kW back to the grid: the export register and the session's export energy grow, "
        "and the vehicle's battery goes down. (Bidirectional vehicles: the mid-size and large EV.)",
        [{"do": "set_variable", "component": "SampledDataCtrlr", "variable": "TxUpdatedMeasurands",
          "value": "Energy.Active.Import.Register,Energy.Active.Export.Register,Power.Active.Import,Power.Active.Export,SoC", "label": "Report the export register too"},
         *_session_start(ev="large", soc=80), {"do": "v2x", "mode": "discharge", "power_kw": 5, "label": "Start feeding 5 kW back"},
         {"expect": "export_gt", "wh": 5, "timeout": 20, "label": "Export energy is flowing"}, {"do": "wait", "seconds": 3},
         {"do": "v2x", "mode": "charge", "label": "Back to charging"}, {"expect": "tx_state", "state": "Charging", "timeout": 10},
         {"do": "stop"}, {"do": "unplug"}])
    add("auto_charge", "Auto charge (the vehicle identifies itself)", "Authorization",
        "No card is tapped: the vehicle presents its own id (VID) as soon as it is plugged in and the session starts. Fails if the CSMS refuses that id.",
        [{"do": "config", "key": "auto_charge", "value": True}, {"do": "plug_in", "ev": "large"},
         {"expect": "tx_state", "state": "Charging", "timeout": 20, "label": "Charging starts without a card"}, {"do": "stop"}, {"do": "unplug"}])
    add("smart_plug_remote_start", "Smart plug: remote start without a cable", "Authorization",
        "A smart plug has no cable detection: the connector stays Available until a remote start, goes straight to Charging, and is Available again after the stop. "
        "The remote start is simulated here, as if the CSMS had sent it.",
        [{"do": "config", "key": "smart_plug", "value": True}, {"expect": "connector_status", "status": "Available", "timeout": 10, "label": "Connector is Available"},
         {"do": "simulate", "action": "RequestStartTransaction", "payload": {"idToken": {"idToken": "REMOTE1", "type": "Central"}, "remoteStartId": 1, "evseId": 1},
          "v16": {"action": "RemoteStartTransaction", "payload": {"idTag": "REMOTE1", "connectorId": 1}}},
         {"expect": "tx_state", "state": "Charging", "timeout": 20, "label": "Charging starts from the remote start alone"},
         {"do": "stop"}, {"expect": "connector_status", "status": "Available", "timeout": 15, "label": "Back to Available"}])

    messages = ["Authorize", "BootNotification", "ClearedChargingLimit", "DataTransfer", "FirmwareStatusNotification", "Get15118EVCertificate", "GetCertificateStatus",
                "Heartbeat", "LogStatusNotification", "MeterValues", "NotifyChargingLimit", "NotifyCustomerInformation", "NotifyDisplayMessages", "NotifyEVChargingNeeds",
                "NotifyEVChargingSchedule", "NotifyEvent", "NotifyMonitoringReport", "NotifyReport", "PublishFirmwareStatusNotification", "ReportChargingProfiles",
                "ReservationStatusUpdate", "SecurityEventNotification", "SignCertificate", "StatusNotification", "TransactionEvent"]
    steps = []
    for m in messages:
        steps += [{"do": "send", "action": m, "label": f"Send {m}", "only": "2.0.1"}, {"expect": "send_ok", "label": f"CSMS answers {m} without an error", "only": "2.0.1"}]
    for m in ("BootNotification", "StatusNotification", "Authorize", "StartTransaction", "MeterValues", "StopTransaction", "Heartbeat", "DataTransfer",
              "DiagnosticsStatusNotification", "FirmwareStatusNotification"):
        steps += [{"do": "send", "action": m, "label": f"Send {m}", "only": "1.6"}, {"expect": "send_ok", "label": f"CSMS answers {m} without an error", "only": "1.6"}]
    add("all_messages", "Send every station message once", "Protocol",
        "Sends every message a charging station can send (25 in OCPP 2.0.1, 10 in 1.6) and checks the CSMS answers each without a CALLERROR or timeout.", steps)
    return S


CATALOG_SCENARIOS = build_catalog()
Station_Runner = Runner


def validate_catalog() -> None:
    """Every referenced fault kind and action must exist (called from the tests)."""
    for sid, sc in CATALOG_SCENARIOS.items():
        for step in sc["steps"]:
            if step.get("do") == "inject":
                assert step["fault"] in CATALOG, (sid, step)
            if step.get("do") in ("send",):
                assert step["action"], (sid, step)
