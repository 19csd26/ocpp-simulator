"""Fault injection: realistic failure scenarios a CSMS has to cope with.

Every kind has a `_start_<kind>` / `_stop_<kind>` pair. A fault stays active until it is cleared (or its
`duration_s` runs out) and changes how the station behaves: what it reports, which messages it sends, and what it
refuses to do. One-shot kinds act once and leave nothing active.

NOTE on event naming: faults are reported with NotifyEvent. Component names follow the standardized component list of
OCPP 2.0.1 Appendix 3 as best understood (EVSE, Connector, LocalEnergyMeter, OverCurrentBreaker, PowerContactor,
GroundIsolationMonitoring, TokenReader, ElectricalFeeder, ChargingStation); the specific condition is carried in `techCode`.
Check these against your CSMS's expectations (or the OCTT) before relying on them for certification.
"""
import asyncio
import random
import time
from dataclasses import dataclass, field
from itertools import count

P = lambda key, label, typ="number", default=None, **kw: {"key": key, "label": label, "type": typ, "default": default, **kw}  # noqa: E731
DURATION = lambda default=0: P("duration_s", "Clear automatically after (seconds, 0 = until cleared)", "number", default, min=0)  # noqa: E731

CATALOG: dict[str, dict] = {
    "power_meter_failure": {
        "title": "Power meter failure", "scope": "evse", "group": "Metering",
        "description": "The energy meter of an EVSE misbehaves. Pick how: no readings, a frozen register, a register that goes backwards, a huge spike, or an invalid value.",
        "params": [P("mode", "Failure mode", "select", "stop_reporting", options=["stop_reporting", "frozen", "rollback", "spike", "invalid"]),
                   P("stop_session", "End the running session and mark the EVSE Faulted", "boolean", False), DURATION()],
        "expect": "CSMS should notice the NotifyEvent (LocalEnergyMeter Problem), tolerate missing/frozen/decreasing meter values without corrupting billing, and flag the session.",
    },
    "power_loss": {
        "title": "Power loss", "scope": "station", "group": "Power & network",
        "description": "The charger loses power: the connection drops without a closing handshake and nothing is sent. When power returns it reboots (BootNotification PowerUp) and closes open transactions with stoppedReason PowerLoss, timestamped at the moment of the cut.",
        "params": [DURATION(20)],
        "expect": "CSMS should mark the station offline, then accept Boot (PowerUp) and an Ended event (PowerLoss, offline=true) carrying the last known meter value.",
    },
    "network_loss": {
        "title": "Network loss (offline charging)", "scope": "station", "group": "Power & network",
        "description": "The WebSocket drops but the charger keeps power. Sessions continue offline; every TransactionEvent is queued with offline=true and delivered in order after reconnecting.",
        "params": [DURATION(20)],
        "expect": "CSMS should receive the queued TransactionEvents in seqNo order with offline=true and original timestamps, without duplicates.",
    },
    "network_flapping": {
        "title": "Unstable network (flapping)", "scope": "station", "group": "Power & network",
        "description": "The connection repeatedly drops and returns.",
        "params": [P("cycles", "Number of drops", "number", 3, min=1, max=20), P("down_s", "Seconds offline each time", "number", 4, min=1), P("up_s", "Seconds online between drops", "number", 6, min=1)],
        "expect": "CSMS should handle rapid reconnects (replacing the old socket) without losing or duplicating transaction messages.",
    },
    "high_latency": {
        "title": "High latency", "scope": "station", "group": "Power & network",
        "description": "Every message the station sends is delayed.",
        "params": [P("latency_ms", "Delay per message (ms)", "number", 2000, min=1, max=20000), DURATION(60)],
        "expect": "CSMS should keep working when answers come late and not mark the station dead.",
    },
    "packet_loss": {
        "title": "Packet loss", "scope": "station", "group": "Power & network",
        "description": "A share of the station's outgoing messages is silently dropped.",
        "params": [P("drop_percent", "Share dropped (%)", "number", 30, min=1, max=100), DURATION(60)],
        "expect": "CSMS must not assume every request arrives; the station times out and retries per its message-attempt settings.",
    },
    "over_voltage": {
        "title": "Over-voltage", "scope": "station", "group": "Electrical",
        "description": "The grid voltage rises above the allowed range. Running sessions stop with PowerQuality and every EVSE goes Faulted until cleared.",
        "params": [P("voltage", "Grid voltage (V)", "number", 280, min=1), DURATION(30)],
        "expect": "CSMS should see NotifyEvent (ElectricalFeeder), Ended with stoppedReason PowerQuality and Faulted connectors, then Available after the fault clears.",
    },
    "under_voltage": {
        "title": "Under-voltage", "scope": "station", "group": "Electrical",
        "description": "The grid voltage falls below the allowed range. Same effect as over-voltage.",
        "params": [P("voltage", "Grid voltage (V)", "number", 170, min=1), DURATION(30)],
        "expect": "As for over-voltage: PowerQuality stops and Faulted connectors.",
    },
    "over_current": {
        "title": "Over-current trip", "scope": "evse", "group": "Electrical",
        "description": "The breaker trips because of too high a current. The session ends with OvercurrentFault and the EVSE stays Faulted until cleared.",
        "params": [P("current_a", "Measured current (A)", "number", 80, min=1)],
        "expect": "CSMS should receive Ended (OvercurrentFault), NotifyEvent (OverCurrentBreaker) and a Faulted status, then Available after clearing.",
    },
    "ground_fault": {
        "title": "Ground / earth fault", "scope": "evse", "group": "Electrical",
        "description": "Leakage to earth is detected. The session ends with GroundFault and the EVSE stays Faulted until cleared.",
        "params": [],
        "expect": "CSMS should receive Ended (GroundFault), NotifyEvent (GroundIsolationMonitoring) and Faulted.",
    },
    "over_temperature": {
        "title": "Over-temperature", "scope": "evse", "group": "Electrical",
        "description": "The connector overheats. Either the power is derated, or the session is stopped and the EVSE faulted.",
        "params": [P("mode", "Reaction", "select", "derate", options=["derate", "stop"]), P("temperature_c", "Temperature (°C)", "number", 85), P("derate_percent", "Power left when derating (%)", "number", 50, min=0, max=100), DURATION(60)],
        "expect": "CSMS should see the temperature event, reduced power in MeterValues when derating, or an Ended + Faulted when stopping.",
    },
    "emergency_stop": {
        "title": "Emergency stop pressed", "scope": "both", "group": "Safety",
        "description": "The emergency button is pressed (on one EVSE or the whole station). Sessions end with EmergencyStop and connectors go Faulted until released.",
        "params": [],
        "expect": "CSMS should receive Ended (EmergencyStop), NotifyEvent and Faulted statuses.",
    },
    "tamper_detected": {
        "title": "Tamper / door opened", "scope": "station", "group": "Safety", "oneshot": True,
        "description": "The enclosure is opened: a SecurityEventNotification (TamperDetectionActivated) and a NotifyEvent are sent.",
        "params": [],
        "expect": "CSMS should log the security event and may alert an operator.",
    },
    "contactor_failure": {
        "title": "Contactor fails to close", "scope": "evse", "group": "Hardware",
        "description": "The power relay will not close: a vehicle can connect and be authorised but charging never starts (SuspendedEVSE).",
        "params": [DURATION()],
        "expect": "With the default TxStartPoint (PowerPathClosed) no transaction starts because power never flows; with TxStartPoint=Authorized or EVConnected a session starts and stays SuspendedEVSE with zero energy. Either way the NotifyEvent (PowerContactor) is sent.",
    },
    "connector_lock_failure": {
        "title": "Connector lock failure", "scope": "evse", "group": "Hardware",
        "description": "The cable lock jams: UnlockConnector is answered with UnlockFailed.",
        "params": [DURATION()],
        "expect": "CSMS should handle UnlockFailed and surface the connector problem.",
    },
    "rfid_reader_failure": {
        "title": "RFID reader failure", "scope": "station", "group": "Hardware",
        "description": "The card reader is dead: card taps are not read and no Authorize is sent.",
        "params": [DURATION()],
        "expect": "CSMS should see NotifyEvent (TokenReader Problem); remote start remains possible.",
    },
    "ev_communication_loss": {
        "title": "Vehicle communication loss", "scope": "evse", "group": "Vehicle",
        "description": "The charger loses its control signal to the vehicle: charging suspends (EVCommunicationLost). Optionally the session ends.",
        "params": [P("end_session", "End the session", "boolean", False), DURATION()],
        "expect": "CSMS should receive TransactionEvent Updated (EVCommunicationLost, SuspendedEV) and possibly Ended.",
    },
    "ev_suspends": {
        "title": "Vehicle pauses charging", "scope": "evse", "group": "Vehicle",
        "description": "The vehicle stops drawing power while still connected (SuspendedEV) until released.",
        "params": [DURATION()],
        "expect": "CSMS should see chargingState SuspendedEV and no energy accrual, then Charging again.",
    },
    "ev_battery_full": {
        "title": "Battery becomes full", "scope": "evse", "group": "Vehicle", "oneshot": True,
        "description": "The vehicle reaches 100 %: charging suspends and the session ends (StoppedByEV).",
        "params": [],
        "expect": "CSMS should receive SuspendedEV then Ended (StoppedByEV).",
    },
    "cable_unplugged": {
        "title": "Cable pulled during charging", "scope": "evse", "group": "Vehicle", "oneshot": True,
        "description": "The driver disconnects the cable mid-session: Ended with EVDisconnected.",
        "params": [],
        "expect": "CSMS should receive Ended (EVDisconnected, trigger EVDeparted) and an Available status.",
    },
    "grid_overload": {
        "title": "Grid overload (external limit)", "scope": "station", "group": "Smart charging",
        "description": "An energy-management system caps the whole site. The station announces it with NotifyChargingLimit, derates all EVSEs, and sends ClearedChargingLimit when it ends.",
        "params": [P("limit_kw", "Site limit (kW)", "number", 5, min=0), DURATION(60)],
        "expect": "CSMS should receive NotifyChargingLimit / ClearedChargingLimit and lower power in MeterValues.",
    },
    "clock_drift": {
        "title": "Clock drift", "scope": "station", "group": "Time",
        "description": "The station clock is wrong: every timestamp it sends is shifted.",
        "params": [P("offset_s", "Offset (seconds, negative = behind)", "number", 3600), DURATION()],
        "expect": "CSMS should notice timestamps far from its own time (compare with Heartbeat currentTime) and not reorder events incorrectly.",
    },
    "reboot_watchdog": {
        "title": "Watchdog reboot", "scope": "station", "group": "Power & network", "oneshot": True,
        "description": "The charger crashes and reboots itself: sessions end (Reboot) and it boots with reason Watchdog.",
        "params": [],
        "expect": "CSMS should accept BootNotification (Watchdog) and a Reboot-ended transaction.",
    },
    "firmware_update_failure": {
        "title": "Firmware / log transfer will fail", "scope": "station", "group": "Firmware",
        "description": "The next UpdateFirmware (or GetLog) fails at the chosen stage.",
        "params": [P("stage", "Fails at", "select", "download", options=["download", "signature", "install", "log"])],
        "expect": "CSMS should receive DownloadFailed / InvalidSignature / InstallationFailed (or UploadFailure for logs).",
    },
}

# kinds that make an EVSE Faulted while active
FAULTED = {"over_voltage", "under_voltage", "over_current", "ground_fault", "emergency_stop"}


@dataclass
class Fault:
    id: int
    kind: str
    scope: int | None
    params: dict
    started_at: float
    ends_at: float | None = None
    state: dict = field(default_factory=dict)
    active: bool = True

    def public(self) -> dict:
        return {"id": self.id, "kind": self.kind, "title": CATALOG[self.kind]["title"], "scope": self.scope, "params": self.params, "started_at": self.started_at,
                "ends_at": self.ends_at, "active": self.active}


class FaultsMixin:
    def init_faults(self) -> None:
        self.faults: dict[int, Fault] = {}
        self.fault_history: list[dict] = []
        self._fault_ids = count(1)
        self.network_up = True
        self._wake = asyncio.Event()
        self._flap_task: asyncio.Task | None = None

    # ------------------------------------------------------------------------------------ queries
    def find_fault(self, kind: str, evse_id: int | None = None) -> Fault | None:
        for f in self.faults.values():
            if f.active and f.kind == kind and (f.scope is None or evse_id is None or f.scope == evse_id):
                return f
        return None

    def evse_faulted(self, evse) -> bool:
        return any(f.active and f.state.get("faulted") and (f.scope is None or f.scope == evse.id) for f in self.faults.values())

    def voltage_override(self) -> float | None:
        for kind in ("over_voltage", "under_voltage"):
            f = self.find_fault(kind)
            if f:
                return float(f.params.get("voltage"))
        return None

    def derate_factor(self, evse) -> float:
        f = self.find_fault("over_temperature", evse.id)
        if f and f.params.get("mode") == "derate":
            return float(f.params.get("derate_percent", 50)) / 100
        return 1.0

    def station_cap_w(self) -> float | None:
        f = self.find_fault("grid_overload")
        return float(f.params["limit_kw"]) * 1000 if f else None

    def contactor_failed(self, evse) -> bool:
        return self.find_fault("contactor_failure", evse.id) is not None

    def ev_comm_ok(self, evse) -> bool:
        return self.find_fault("ev_communication_loss", evse.id) is None

    def reader_failed(self) -> bool:
        return self.find_fault("rfid_reader_failure") is not None

    def lock_failed(self, evse) -> bool:
        return self.find_fault("connector_lock_failure", evse.id) is not None

    def temperature_of(self, evse) -> float:
        f = self.find_fault("over_temperature", evse.id)
        target = float(f.params["temperature_c"]) if f else 28 + (evse.power_w + evse.export_power_w) / 1000 * 0.35
        return evse.temperature + (target - evse.temperature) * 0.3

    def extra_latency_s(self) -> float:
        return sum(float(f.params.get("latency_ms", 0)) for f in self.faults.values() if f.active and f.kind == "high_latency") / 1000

    def should_drop_packet(self) -> bool:
        return any(f.active and f.kind == "packet_loss" and random.random() * 100 < float(f.params.get("drop_percent", 0)) for f in self.faults.values())

    def tick_faults(self) -> None:
        pass  # expiry is handled by per-fault timers (they keep running while the station has no power)

    def faults_snapshot(self) -> dict:
        return {"active": [f.public() for f in self.faults.values() if f.active], "history": self.fault_history[-20:]}

    # ------------------------------------------------------------------------------- injecting
    @staticmethod
    def _coerce(spec: dict, params: dict) -> dict:
        out = {}
        for p in spec["params"]:
            value = params.get(p["key"], p["default"])
            if p["type"] == "number":
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    raise ValueError(f"{p['label']} must be a number")
                if p.get("min") is not None and value < p["min"]:
                    raise ValueError(f"{p['label']} must be at least {p['min']}")
                if p.get("max") is not None and value > p["max"]:
                    raise ValueError(f"{p['label']} must be at most {p['max']}")
            elif p["type"] == "boolean":
                value = value in (True, "true", "True", 1, "1")
            elif p["type"] == "select" and value not in p["options"]:
                raise ValueError(f"{p['label']} must be one of {p['options']}")
            out[p["key"]] = value
        return out

    async def inject_fault(self, kind: str, scope: int | None = None, params: dict | None = None) -> dict:
        spec = CATALOG.get(kind)
        if spec is None:
            raise ValueError(f"Unknown fault '{kind}'")
        if spec["scope"] == "evse":
            if scope is None:
                raise ValueError(f"'{spec['title']}' needs an EVSE")
            self.evse(scope)
        elif spec["scope"] == "station":
            scope = None
        elif scope is not None:
            self.evse(scope)
        params = self._coerce(spec, params or {})
        if not spec.get("oneshot") and (existing := self.find_fault(kind, scope)) and existing.scope == scope:
            raise ValueError(f"'{spec['title']}' is already active here (id {existing.id}); clear it first")
        fault = Fault(next(self._fault_ids), kind, scope, params, time.time())
        duration = params.get("duration_s") or 0
        if duration:
            fault.ends_at = fault.started_at + duration
        self.note(f"FAULT injected: {spec['title']}" + (f" on EVSE {scope}" if scope else ""))
        if spec.get("oneshot"):
            await getattr(self, f"_start_{kind}")(fault)
            fault.active = False
            self.fault_history.append(fault.public())
            self.changed()
            return fault.public()
        self.faults[fault.id] = fault
        await getattr(self, f"_start_{kind}")(fault)
        if duration:
            self.spawn(self._expire(fault.id, duration))
        self.changed()
        return fault.public()

    async def _expire(self, fault_id: int, duration: float) -> None:
        await asyncio.sleep(duration)
        if fault_id in self.faults and self.faults[fault_id].active:
            await self.clear_fault(fault_id)

    async def clear_fault(self, fault_id: int) -> dict:
        fault = self.faults.get(fault_id)
        if fault is None or not fault.active:
            raise ValueError(f"No active fault with id {fault_id}")
        fault.active = False
        self.faults.pop(fault_id, None)
        await getattr(self, f"_stop_{fault.kind}")(fault)
        self.fault_history.append(fault.public())
        self.note(f"FAULT cleared: {CATALOG[fault.kind]['title']}")
        await self.sync_status()
        for evse in self.evses:
            await self.reconcile(evse)
        self.changed()
        return fault.public()

    async def clear_all_faults(self) -> int:
        ids = list(self.faults)
        for i in ids:
            try:
                await self.clear_fault(i)
            except ValueError:
                pass
        return len(ids)

    # ---------------------------------------------------------------------- network / power helpers
    async def net_down(self) -> None:
        self.network_up = False
        await self._drop_connection(graceful=False)
        self.state = "offline"
        self.changed()

    def net_up(self) -> None:
        self.network_up = True
        self.reconnect_attempt = 0
        self._wake.set()

    # ----------------------------------------------------------------------------------- the faults
    async def _start_power_meter_failure(self, f: Fault) -> None:
        evse = self.evse(f.scope)
        mode = f.params["mode"]
        await self.notify_event("LocalEnergyMeter", "Problem", "true", evse_id=f.scope, tech_code="PowerMeterFailure", tech_info=f"mode={mode}")
        if f.params["stop_session"]:
            f.state["faulted"] = True
            if evse.tx is not None:
                async with evse.lock:
                    await self._end_tx(evse, "Other", "AbnormalCondition")
            await self.sync_status()
        elif evse.tx is not None:
            meter = self.meter_values(evse, evse.tx, "SampledDataCtrlr", "TxUpdatedMeasurands", "Sample.Periodic")
            async with self.tx_lock:
                await self.deliver_tx(self._tx_payload(evse.tx, "Updated", "AbnormalCondition", meter_value=meter, charging_state=evse.tx.charging_state))

    async def _stop_power_meter_failure(self, f: Fault) -> None:
        await self.notify_event("LocalEnergyMeter", "Problem", "false", evse_id=f.scope, cleared=True, tech_code="PowerMeterFailure")

    async def _start_power_loss(self, f: Fault) -> None:
        self.powered = False
        for evse in self.evses:
            evse.power_w = 0.0
            if evse.tx is not None:
                evse.tx.power_lost_at = self.now()
        for task in (self.ticker_task, self.heartbeat_task, self.conn_task):
            if task:
                task.cancel()
        self.ticker_task = self.heartbeat_task = self.conn_task = None
        await self._drop_connection(graceful=False)
        self.state = "unpowered"
        self.boot_status = None
        for evse in self.evses:
            for conn in evse.connectors:
                conn.status = None
        self.note("POWER LOST: the station is dead (no messages, no meter)")

    async def _stop_power_loss(self, f: Fault) -> None:
        self.powered = True
        self.boot_required = True
        self.next_boot_reason = "PowerUp"
        self.note("Power restored: booting")
        self.state = "stopped"
        if self.want_connected:
            await self.connect()
        else:
            self.ensure_ticker()

    async def _start_network_loss(self, f: Fault) -> None:
        await self.net_down()
        self.note("NETWORK LOST: charging continues offline, events are queued")

    async def _stop_network_loss(self, f: Fault) -> None:
        self.note("Network restored")
        self.net_up()

    async def _start_network_flapping(self, f: Fault) -> None:
        async def run() -> None:
            for _ in range(int(f.params["cycles"])):
                await self.net_down()
                await asyncio.sleep(f.params["down_s"])
                self.net_up()
                await asyncio.sleep(f.params["up_s"])
            if f.active:
                await self.clear_fault(f.id)

        self._flap_task = self.spawn(run())

    async def _stop_network_flapping(self, f: Fault) -> None:
        if self._flap_task:
            self._flap_task.cancel()
        self.net_up()

    async def _start_high_latency(self, f: Fault) -> None:
        pass

    async def _stop_high_latency(self, f: Fault) -> None:
        pass

    _start_packet_loss, _stop_packet_loss = _start_high_latency, _stop_high_latency

    async def _grid_fault(self, f: Fault, label: str) -> None:
        f.state["faulted"] = True
        v = f.params["voltage"]
        await self.notify_event("ElectricalFeeder", "Problem", "true", tech_code=label, tech_info=f"{v} V")
        await self.end_all("PowerQuality")

    async def _start_over_voltage(self, f: Fault) -> None:
        await self._grid_fault(f, "OverVoltage")

    async def _start_under_voltage(self, f: Fault) -> None:
        await self._grid_fault(f, "UnderVoltage")

    async def _stop_over_voltage(self, f: Fault) -> None:
        await self.notify_event("ElectricalFeeder", "Problem", "false", cleared=True, tech_code="OverVoltage")

    async def _stop_under_voltage(self, f: Fault) -> None:
        await self.notify_event("ElectricalFeeder", "Problem", "false", cleared=True, tech_code="UnderVoltage")

    async def _evse_trip(self, f: Fault, component: str, code: str, reason: str, info: str | None = None) -> None:
        f.state["faulted"] = True
        await self.notify_event(component, "Problem", "true", evse_id=f.scope, tech_code=code, tech_info=info)
        await self.end_all(reason, only=f.scope)

    async def _start_over_current(self, f: Fault) -> None:
        await self._evse_trip(f, "OverCurrentBreaker", "OverCurrent", "OvercurrentFault", f"{f.params['current_a']} A")

    async def _start_ground_fault(self, f: Fault) -> None:
        await self._evse_trip(f, "GroundIsolationMonitoring", "GroundFault", "GroundFault")

    async def _stop_over_current(self, f: Fault) -> None:
        await self.notify_event("OverCurrentBreaker", "Problem", "false", evse_id=f.scope, cleared=True, tech_code="OverCurrent")

    async def _stop_ground_fault(self, f: Fault) -> None:
        await self.notify_event("GroundIsolationMonitoring", "Problem", "false", evse_id=f.scope, cleared=True, tech_code="GroundFault")

    async def _start_over_temperature(self, f: Fault) -> None:
        evse = self.evse(f.scope)
        await self.notify_event("EVSE", "Temperature", f.params["temperature_c"], evse_id=f.scope, tech_code="OverTemperature")
        if f.params["mode"] == "stop":
            f.state["faulted"] = True
            await self.end_all("Other", only=f.scope)
        else:
            evse.limit_w = self.power_limit_w(evse, evse.tx)

    async def _stop_over_temperature(self, f: Fault) -> None:
        await self.notify_event("EVSE", "Temperature", 30, evse_id=f.scope, cleared=True, tech_code="OverTemperature")

    async def _start_emergency_stop(self, f: Fault) -> None:
        f.state["faulted"] = True
        await self.notify_event("EVSE" if f.scope else "ChargingStation", "Problem", "true", evse_id=f.scope, tech_code="EmergencyStop")
        await self.end_all("EmergencyStop", only=f.scope)

    async def _stop_emergency_stop(self, f: Fault) -> None:
        await self.notify_event("EVSE" if f.scope else "ChargingStation", "Problem", "false", evse_id=f.scope, cleared=True, tech_code="EmergencyStop")

    async def _start_tamper_detected(self, f: Fault) -> None:
        await self.security_event("TamperDetectionActivated", "Enclosure opened")
        await self.notify_event("ChargingStation", "Problem", "true", tech_code="TamperDetected")

    async def _start_contactor_failure(self, f: Fault) -> None:
        await self.notify_event("PowerContactor", "Problem", "true", evse_id=f.scope, tech_code="ContactorFailure")
        await self.reconcile(self.evse(f.scope))

    async def _stop_contactor_failure(self, f: Fault) -> None:
        await self.notify_event("PowerContactor", "Problem", "false", evse_id=f.scope, cleared=True, tech_code="ContactorFailure")

    async def _start_connector_lock_failure(self, f: Fault) -> None:
        await self.notify_event("Connector", "Problem", "true", evse_id=f.scope, connector_id=1, tech_code="ConnectorLockFailure")

    async def _stop_connector_lock_failure(self, f: Fault) -> None:
        await self.notify_event("Connector", "Problem", "false", evse_id=f.scope, connector_id=1, cleared=True, tech_code="ConnectorLockFailure")

    async def _start_rfid_reader_failure(self, f: Fault) -> None:
        await self.notify_event("TokenReader", "Problem", "true", tech_code="ReaderFailure")

    async def _stop_rfid_reader_failure(self, f: Fault) -> None:
        await self.notify_event("TokenReader", "Problem", "false", cleared=True, tech_code="ReaderFailure")

    async def _start_ev_communication_loss(self, f: Fault) -> None:
        evse = self.evse(f.scope)
        if evse.tx is not None:
            async with evse.lock:
                await self._reconcile(evse, "EVCommunicationLost")
                if f.params["end_session"] and evse.tx is not None:
                    await self._end_tx(evse, "Other", "EVCommunicationLost")
                    await self.sync_status()

    async def _stop_ev_communication_loss(self, f: Fault) -> None:
        pass

    async def _start_ev_suspends(self, f: Fault) -> None:
        evse = self.evse(f.scope)
        if evse.ev is None:
            raise ValueError("No vehicle is connected to that EVSE")
        evse.ev.ready = False
        await self.reconcile(evse)

    async def _stop_ev_suspends(self, f: Fault) -> None:
        evse = self.evse(f.scope)
        if evse.ev is not None:
            evse.ev.ready = True

    async def _start_ev_battery_full(self, f: Fault) -> None:
        evse = self.evse(f.scope)
        if evse.ev is None:
            raise ValueError("No vehicle is connected to that EVSE")
        await self.set_ev(f.scope, soc=100.0)

    async def _start_cable_unplugged(self, f: Fault) -> None:
        await self.unplug(f.scope)

    async def _start_grid_overload(self, f: Fault) -> None:
        await self.request("NotifyChargingLimit", charging_limit={"charging_limit_source": "EMS", "is_grid_critical": True})
        for evse in self.evses:
            await self.reconcile(evse, "ChargingRateChanged")

    async def _stop_grid_overload(self, f: Fault) -> None:
        await self.request("ClearedChargingLimit", charging_limit_source="EMS")

    async def _start_clock_drift(self, f: Fault) -> None:
        self.clock_offset = float(f.params["offset_s"])

    async def _stop_clock_drift(self, f: Fault) -> None:
        self.clock_offset = 0.0

    async def _start_reboot_watchdog(self, f: Fault) -> None:
        self.spawn(self.reboot("Watchdog", "Reboot", pause=1.0))

    async def _start_firmware_update_failure(self, f: Fault) -> None:
        self.fw_failure = f.params["stage"]

    async def _stop_firmware_update_failure(self, f: Fault) -> None:
        self.fw_failure = None
