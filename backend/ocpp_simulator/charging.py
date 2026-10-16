"""Connectors, vehicles, authorisation, transactions and metering."""
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ocpp_simulator import smartcharging
from ocpp_simulator.config import EV_MODELS, EvseSpec

log = logging.getLogger("charging")

STOP_REASONS = {"DeAuthorized", "EmergencyStop", "EnergyLimitReached", "EVDisconnected", "GroundFault", "ImmediateReset", "Local",
                "LocalOutOfCredit", "MasterPass", "Other", "OvercurrentFault", "PowerLoss", "PowerQuality", "Reboot", "Remote",
                "SOCLimitReached", "StoppedByEV", "TimeLimitReached", "Timeout"}


@dataclass
class EvState:
    key: str
    label: str
    capacity_kwh: float
    max_ac_kw: float
    max_dc_kw: float
    soc: float = 20.0
    target_soc: float = 100.0
    ready: bool = True  # the vehicle accepts energy (False = paused / battery management stop)

    def public(self) -> dict:
        return {"key": self.key, "label": self.label, "capacity_kwh": self.capacity_kwh, "soc": round(self.soc, 1), "target_soc": self.target_soc, "ready": self.ready}


@dataclass
class Tx:
    id: str
    evse_id: int
    connector_id: int
    started_at: datetime
    id_token: dict | None = None
    seq_no: int = 0
    charging_state: str = "Idle"
    energy_wh: float = 0.0
    remote_start_id: int | None = None
    reservation_id: int | None = None
    id_token_sent: bool = False
    since_sample: float = 0.0
    time_charging_s: float = 0.0
    total_cost: float | None = None
    power_lost_at: datetime | None = None
    ended: bool = False


class Connector:
    def __init__(self, cid: int, ctype: str) -> None:
        self.id = cid
        self.type = ctype
        self.status: str | None = None  # last status reported to the CSMS
        self.operative = True


class Evse:
    def __init__(self, eid: int, spec: EvseSpec) -> None:
        self.id = eid
        self.spec = spec
        self.connectors = [Connector(i, c.type) for i, c in enumerate(spec.connectors, start=1)]
        self.operative = True
        self.register_wh = 100_000.0 + eid * 1_000  # lifetime energy meter
        self.plugged: Connector | None = None
        self.locked = False
        self.finishing = False  # OCPP 1.6: a session ended and the cable is still plugged in
        self.ev: EvState | None = None
        self.auth: dict | None = None  # authorised id token waiting for / running a session
        self.auth_at: float | None = None
        self.tx: Tx | None = None
        self.next_tx_id: str | None = None  # promised to the CSMS in a RequestStartTransaction response
        self.pending_remote: dict | None = None
        self.reported_limit_w: float | None = None  # the limit the CSMS last learned about
        self.power_w = 0.0
        self.limit_w = 0.0
        self.temperature = 30.0
        self.lock = asyncio.Lock()


class ChargingMixin:
    # ---------------------------------------------------------------------------------------- setup
    def init_charging(self) -> None:
        self.evses = [Evse(i, spec) for i, spec in enumerate(self.cfg.evses, start=1)]
        self.operative = True
        self.reservations: dict[int, dict] = {}
        self.profiles: list[dict] = []
        self.local_list: dict = {"version": 0, "entries": {}}
        self.auth_cache: dict[str, dict] = {}
        self._last_tick = time.monotonic()
        self._aligned_bucket: int | None = None
        self.event_seq = 0
        self.event_ids = 0
        self.last_alive = self.now()

    def evse(self, evse_id: int) -> Evse:
        if not 1 <= evse_id <= len(self.evses):
            raise ValueError(f"EVSE {evse_id} does not exist")
        return self.evses[evse_id - 1]

    def connector(self, evse_id: int, connector_id: int = 1) -> Connector:
        for c in self.evse(evse_id).connectors:
            if c.id == connector_id:
                return c
        raise ValueError(f"Connector {connector_id} does not exist on EVSE {evse_id}")

    def ensure_ticker(self) -> None:
        if self.ticker_task is None or self.ticker_task.done():
            self._last_tick = time.monotonic()
            self.ticker_task = asyncio.get_running_loop().create_task(self._ticker())

    # ------------------------------------------------------------------------------------- status
    def compute_status(self, evse: Evse, conn: Connector) -> str:
        if self.v16:
            return self.compute_status16(evse, conn)
        if self.evse_faulted(evse):
            return "Faulted"
        if not (self.operative and evse.operative and conn.operative):
            return "Unavailable"
        if evse.tx is not None or evse.plugged is conn:
            return "Occupied"
        if evse.plugged is not None:
            return "Unavailable"  # another connector of the same EVSE is in use
        if any(r["evse_id"] in (evse.id, None) and r["expiry"] > self.now() for r in self.reservations.values()):
            return "Reserved"
        return "Available"

    async def send_status(self, evse: Evse, conn: Connector) -> None:
        res = await self.request("StatusNotification", timestamp=self.ts(), connector_status=conn.status, evse_id=evse.id, connector_id=conn.id)
        if not res.sent:
            conn.status = None  # not delivered: resend after the next boot / reconnect

    async def sync_status(self) -> None:
        """Report every connector whose status changed since it was last reported."""
        for evse in self.evses:
            for conn in evse.connectors:
                new = self.compute_status(evse, conn)
                if new != conn.status:
                    conn.status = new
                    if self.online:
                        await self.send_status(evse, conn)
        self.changed()

    async def send_all_statuses(self) -> None:
        for evse in self.evses:
            for conn in evse.connectors:
                conn.status = self.compute_status(evse, conn)
                await self.send_status(evse, conn)
        if self.v16:
            await self.send_status0_16()

    # ------------------------------------------------------------------------------- tx settings
    def start_points(self) -> set[str]:
        if self.v16:
            return {"PowerPathClosed"}  # OCPP 1.6: StartTransaction once the vehicle is connected and the user authorised
        return set(self.model.get_list("TxCtrlr", "TxStartPoint"))

    def stop_points(self) -> set[str]:
        if self.v16:
            return {"EVConnected", "Authorized"}
        return set(self.model.get_list("TxCtrlr", "TxStopPoint"))

    # ---------------------------------------------------------------------------------- metering
    def meter_fault(self, evse: Evse):
        return self.find_fault("power_meter_failure", evse.id)

    def reported_energy_wh(self, evse: Evse) -> float | None:
        """The energy register value the station reports (None = the meter delivers no reading)."""
        fault = self.meter_fault(evse)
        if fault is None:
            return evse.register_wh
        mode = fault.params.get("mode", "stop_reporting")
        if mode == "stop_reporting":
            return None
        if mode == "frozen":
            return fault.state.setdefault("frozen", evse.register_wh)
        if mode == "rollback":
            return evse.register_wh - float(fault.params.get("rollback_wh", 50_000))
        if mode == "spike":
            return evse.register_wh + float(fault.params.get("spike_wh", 10_000_000))
        if mode == "invalid":
            return -1.0
        return evse.register_wh

    def voltage_now(self, evse: Evse) -> float:
        override = self.voltage_override()
        return override if override is not None else evse.spec.voltage

    def _sample(self, evse: Evse, tx: Tx | None, measurand: str, context: str) -> dict | None:
        spec = evse.spec
        phases = spec.phases if spec.current_type == "AC" else 1
        v = self.voltage_now(evse)
        unit, value, location = None, None, "Outlet"
        if measurand == "Energy.Active.Import.Register":
            value, unit = self.reported_energy_wh(evse), "Wh"
        elif measurand == "Power.Active.Import":
            value, unit = evse.power_w, "W"
        elif measurand == "Power.Offered":
            value, unit = evse.limit_w, "W"
        elif measurand == "Current.Import":
            value, unit = (evse.power_w / (v * phases)) if v else 0, "A"
        elif measurand == "Current.Offered":
            value, unit = (evse.limit_w / (v * phases)) if v else 0, "A"
        elif measurand == "Voltage":
            value, unit = v, "V"
        elif measurand == "Frequency":
            value, unit, location = 50.0, "Hz", "Outlet"
        elif measurand == "SoC":
            if evse.ev is None:
                return None
            value, unit, location = evse.ev.soc, "Percent", "EV"
        else:
            return None
        if value is None:
            return None
        return {"value": round(float(value), 3), "context": context, "measurand": measurand, "location": location, "unit_of_measure": {"unit": unit}}

    def meter_values(self, evse: Evse, tx: Tx | None, controller: str, variable: str, context: str) -> list[dict]:
        sampled = [s for m in self.model.get_list(controller, variable) if (s := self._sample(evse, tx, m, context))]
        return [{"timestamp": self.ts(), "sampled_value": sampled}] if sampled else []

    # --------------------------------------------------------------------------- power / charging
    def power_limit_w(self, evse: Evse, tx: Tx | None) -> float:
        spec = evse.spec
        phases = spec.phases if spec.current_type == "AC" else 1
        limits = [spec.max_power_kw * 1000 * self.derate_factor(evse)]
        cap = self.station_cap_w()
        if cap is not None:
            limits.append(cap / max(1, len(self.evses)))
        profile = smartcharging.limit_at(self.profiles, evse.id, self.now(), voltage=spec.voltage, phases=phases,
                                         tx_id=tx.id if tx else None, tx_start=tx.started_at if tx else None)
        if profile is not None:
            limits.append(profile)
        return max(0.0, min(limits))

    @staticmethod
    def ev_power_w(ev: EvState, spec: EvseSpec) -> float:
        base = ev.max_dc_kw if spec.current_type == "DC" else ev.max_ac_kw
        if spec.current_type == "DC" and ev.soc > 80:
            base *= max(0.15, (100 - ev.soc) / 20)  # DC charging tapers off near full
        return base * 1000

    def desired_state(self, evse: Evse) -> str:
        if evse.plugged is None:
            return "Idle"
        if evse.auth is None and (evse.tx is None or evse.tx.id_token is None):
            return "EVConnected"
        if evse.ev is not None and (not evse.ev.ready or evse.ev.soc >= evse.ev.target_soc or not self.ev_comm_ok(evse)):
            return "SuspendedEV"
        if self.evse_faulted(evse) or not (self.operative and evse.operative) or self.contactor_failed(evse) or evse.limit_w <= 0:
            return "SuspendedEVSE"
        return "Charging"

    # ------------------------------------------------------------------------ transaction events
    def _tx_payload(self, tx: Tx, event_type: str, trigger: str, *, meter_value=None, charging_state=None, stopped_reason=None,
                    timestamp: str | None = None, offline: bool = False) -> dict:
        seq, tx.seq_no = tx.seq_no, tx.seq_no + 1
        info: dict = {"transaction_id": tx.id}
        if charging_state:
            info["charging_state"] = charging_state
        if event_type == "Started" and tx.remote_start_id is not None:
            info["remote_start_id"] = tx.remote_start_id
        if stopped_reason:
            info["stopped_reason"] = stopped_reason
        if event_type == "Ended":
            info["time_spent_charging"] = int(tx.time_charging_s)
        payload: dict = {"event_type": event_type, "timestamp": timestamp or self.ts(), "trigger_reason": trigger, "seq_no": seq,
                         "transaction_info": info, "evse": {"id": tx.evse_id, "connector_id": tx.connector_id}}
        if meter_value:
            payload["meter_value"] = meter_value
        if tx.id_token and not tx.id_token_sent:
            payload["id_token"] = tx.id_token
            tx.id_token_sent = True
        if tx.reservation_id is not None and event_type == "Started":
            payload["reservation_id"] = tx.reservation_id
        if offline or not self.online:
            payload["offline"] = True
        return payload

    async def tx_event(self, tx: Tx, event_type: str, trigger: str, **kw) -> None:
        async with self.tx_lock:
            await self.deliver_tx(self._tx_payload(tx, event_type, trigger, **kw))

    # ------------------------------------------------------------------------- authorisation
    async def authorize(self, token: str, ttype: str = "ISO14443") -> tuple[str, str]:
        """Returns (status, source). Online: the CSMS decides; offline: local list, then cache, then the offline policy."""
        m = self.model
        entry = self.local_list["entries"].get(token)
        if entry and m.get_bool("LocalAuthListCtrlr", "Enabled", True) and m.get_bool("AuthCtrlr", "LocalPreAuthorize", False) and entry.get("status") == "Accepted":
            return "Accepted", "local list"
        if self.online:
            res = await self.request("Authorize", id_token={"id_token": token, "type": ttype})
            if res.ok:
                status = ((res.response or {}).get("idTokenInfo") or (res.response or {}).get("idTagInfo") or {}).get("status", "Invalid")
                if status == "Accepted" and m.get_bool("AuthCacheCtrlr", "Enabled", True):
                    self.auth_cache[token] = {"status": "Accepted", "expires": time.time() + m.get_int("AuthCacheCtrlr", "LifeTime", 86400)}
                return status, "CSMS"
            self.note(f"Authorize failed ({res.error}); using offline rules")
        # offline behaviour (OCPP 2.0.1 C03/C14)
        if m.get_bool("AuthCtrlr", "LocalAuthorizeOffline", True):
            if entry and m.get_bool("LocalAuthListCtrlr", "Enabled", True):
                return entry.get("status", "Invalid"), "local list (offline)"
            cached = self.auth_cache.get(token)
            if cached and cached["expires"] > time.time() and m.get_bool("AuthCacheCtrlr", "Enabled", True):
                return cached["status"], "cache (offline)"
        if m.get_bool("AuthCtrlr", "OfflineTxForUnknownIdEnabled", False):
            return "Accepted", "unknown id allowed offline"
        return "Invalid", "offline, unknown token"

    # ---------------------------------------------------------------------------- user actions
    async def plug_in(self, evse_id: int, connector_id: int = 1, ev: str = "mid", soc: float = 20.0, target_soc: float = 100.0) -> dict:
        evse, conn = self.evse(evse_id), self.connector(evse_id, connector_id)
        if ev not in EV_MODELS:
            raise ValueError(f"Unknown vehicle model '{ev}'")
        async with evse.lock:
            if evse.plugged is not None:
                raise ValueError(f"EVSE {evse_id} already has a vehicle connected")
            if self.evse_faulted(evse):
                raise ValueError(f"EVSE {evse_id} is faulted")
            evse.plugged = conn
            evse.finishing = False
            evse.locked = True
            evse.ev = EvState(key=ev, soc=max(0.0, min(100.0, soc)), target_soc=max(1.0, min(100.0, target_soc)), **{k: v for k, v in EV_MODELS[ev].items()})
            self.note(f"EVSE {evse_id}: vehicle connected ({EV_MODELS[ev]['label']}, SoC {evse.ev.soc:.0f}%)")
            await self.sync_status()
            await self._on_plugged(evse)
        return {"ok": True}

    async def _on_plugged(self, evse: Evse) -> None:
        conn = evse.plugged
        if evse.pending_remote and evse.pending_remote.get("authorized", True):
            evse.auth = evse.pending_remote["id_token"]
            evse.auth_at = time.time()
        if evse.tx is None:
            if "EVConnected" in self.start_points():
                await self._begin_tx(evse, "CablePluggedIn")
            else:
                await self._reconcile(evse)
        else:
            await self.tx_event(evse.tx, "Updated", "CablePluggedIn", charging_state=self.desired_state(evse))
            evse.tx.charging_state = self.desired_state(evse)
            await self._reconcile(evse)

    async def unplug(self, evse_id: int) -> dict:
        evse = self.evse(evse_id)
        async with evse.lock:
            if evse.plugged is None:
                raise ValueError(f"EVSE {evse_id} has no vehicle connected")
            if evse.tx is not None:
                await self._end_tx(evse, "EVDisconnected", "EVDeparted")
            evse.plugged = None
            evse.ev = None
            evse.locked = False
            evse.auth = None
            evse.power_w = 0.0
            self.note(f"EVSE {evse_id}: vehicle disconnected")
            await self.sync_status()
        return {"ok": True}

    async def tap_card(self, evse_id: int, token: str, ttype: str = "ISO14443") -> dict:
        evse = self.evse(evse_id)
        if self.reader_failed():
            self.note("RFID reader failure: the card was not read")
            return {"ok": False, "status": "ReaderFailure"}
        async with evse.lock:
            if evse.tx is not None and evse.tx.id_token and evse.tx.id_token.get("id_token") == token:
                await self._end_tx(evse, "Local", "StopAuthorized")
                await self.sync_status()
                return {"ok": True, "status": "Stopped"}
            if evse.tx is not None:
                self.note(f"EVSE {evse_id}: card {token} does not match the running session")
                return {"ok": False, "status": "NotTheSessionOwner"}
            res = self.reservation_for(evse)
            if res is not None and res["id_token"]["id_token"] != token:
                self.note(f"EVSE {evse_id}: reserved for another token")
                return {"ok": False, "status": "ReservedForOther"}
            if not (self.operative and evse.operative) or self.evse_faulted(evse):
                return {"ok": False, "status": "Unavailable"}
            status, source = await self.authorize(token, ttype)
            self.note(f"EVSE {evse_id}: card {token} → {status} ({source})")
            if status != "Accepted":
                return {"ok": False, "status": status}
            evse.auth = {"id_token": token, "type": ttype}
            evse.auth_at = time.time()
            if "Authorized" in self.start_points() and evse.tx is None:
                await self._begin_tx(evse, "Authorized")
            else:
                await self._reconcile(evse)
            await self.sync_status()
        return {"ok": True, "status": "Accepted"}

    async def stop_session(self, evse_id: int, reason: str = "Local", trigger: str = "StopAuthorized") -> dict:
        evse = self.evse(evse_id)
        if reason not in STOP_REASONS:
            raise ValueError(f"Unknown stop reason '{reason}'")
        async with evse.lock:
            if evse.tx is None:
                raise ValueError(f"EVSE {evse_id} has no running session")
            await self._end_tx(evse, reason, trigger)
            await self.sync_status()
        return {"ok": True}

    async def set_ev(self, evse_id: int, *, ready: bool | None = None, soc: float | None = None, target_soc: float | None = None) -> dict:
        evse = self.evse(evse_id)
        if evse.ev is None:
            raise ValueError(f"EVSE {evse_id} has no vehicle connected")
        async with evse.lock:
            if ready is not None:
                evse.ev.ready = ready
            if soc is not None:
                evse.ev.soc = max(0.0, min(100.0, soc))
            if target_soc is not None:
                evse.ev.target_soc = max(1.0, min(100.0, target_soc))
            await self._reconcile(evse)
        return {"ok": True}

    # ------------------------------------------------------------------------- transaction core
    def reservation_for(self, evse: Evse) -> dict | None:
        now = self.now()
        for r in self.reservations.values():
            if r["evse_id"] in (evse.id, None) and r["expiry"] > now:
                return r
        return None

    async def _begin_tx(self, evse: Evse, trigger: str) -> None:
        conn = evse.plugged or evse.connectors[0]
        tx = Tx(id=evse.next_tx_id or str(uuid.uuid4()), evse_id=evse.id, connector_id=conn.id, started_at=self.now(), id_token=evse.auth)
        evse.next_tx_id = None
        if evse.pending_remote:
            tx.remote_start_id = evse.pending_remote.get("remote_start_id")
            for p in evse.pending_remote.get("profiles", []):
                p["transaction_id"] = tx.id
                self.profiles.append(p)
            evse.pending_remote = None
        res = self.reservation_for(evse)
        if res is not None and evse.auth and res["id_token"]["id_token"] == evse.auth["id_token"]:
            tx.reservation_id = res["id"]
            self.reservations.pop(res["id"], None)
        evse.tx = tx
        evse.finishing = False
        evse.limit_w = self.power_limit_w(evse, tx)
        evse.reported_limit_w = evse.limit_w
        tx.charging_state = self.desired_state(evse)
        meter = self.meter_values(evse, tx, "SampledDataCtrlr", "TxStartedMeasurands", "Transaction.Begin")
        await self.tx_event(tx, "Started", trigger, meter_value=meter, charging_state=tx.charging_state)
        self.note(f"EVSE {evse.id}: transaction {tx.id[:8]} started ({trigger})")
        await self.sync_status()

    async def _reconcile(self, evse: Evse, hint: str | None = None) -> None:
        """Bring the charging state in line with plug / authorisation / limits, starting a transaction when the start point is reached."""
        evse.limit_w = self.power_limit_w(evse, evse.tx)
        new = self.desired_state(evse)
        if evse.tx is None:
            if new == "Charging" and "PowerPathClosed" in self.start_points() and evse.auth is not None:
                await self._begin_tx(evse, "RemoteStart" if (evse.auth or {}).get("type") == "Central" else "ChargingStateChanged")
            return
        if new != evse.tx.charging_state:
            evse.tx.charging_state = new
            await self.tx_event(evse.tx, "Updated", hint or "ChargingStateChanged", charging_state=new)
            evse.reported_limit_w = evse.limit_w
        elif hint == "ChargingRateChanged" and evse.reported_limit_w is not None and abs(evse.limit_w - evse.reported_limit_w) > 1:
            evse.reported_limit_w = evse.limit_w
            await self.tx_event(evse.tx, "Updated", "ChargingRateChanged", charging_state=new)
        if evse.tx is not None and evse.ev is not None and evse.ev.soc >= evse.ev.target_soc and self.cfg.end_on_full:
            # the vehicle is full: it ends the session itself (however it got full)
            await self._end_tx(evse, "StoppedByEV", "ChargingStateChanged")
            await self.sync_status()
        self.changed()

    async def reconcile(self, evse: Evse, hint: str | None = None) -> None:
        async with evse.lock:
            await self._reconcile(evse, hint)

    async def _end_tx(self, evse: Evse, reason: str, trigger: str, *, timestamp: str | None = None, offline: bool = False, energy_wh: float | None = None) -> None:
        tx = evse.tx
        if tx is None:
            return
        meter = self.meter_values(evse, tx, "SampledDataCtrlr", "TxEndedMeasurands", "Transaction.End")
        if energy_wh is not None and meter:  # power loss: the last known register value
            for sv in meter[0]["sampled_value"]:
                if sv["measurand"] == "Energy.Active.Import.Register":
                    sv["value"] = round(energy_wh, 3)
        tx.ended = True
        evse.tx = None
        evse.finishing = evse.plugged is not None  # OCPP 1.6: the connector is "Finishing" until the cable is unplugged
        evse.auth = None
        evse.auth_at = None
        evse.power_w = 0.0
        await self.tx_event(tx, "Ended", trigger, meter_value=meter, stopped_reason=reason, timestamp=timestamp, offline=offline,
                            charging_state="Idle" if evse.plugged is None else "EVConnected")
        self.note(f"EVSE {evse.id}: transaction {tx.id[:8]} ended ({reason}), {tx.energy_wh / 1000:.3f} kWh")

    async def end_all(self, reason: str, trigger: str = "AbnormalCondition", only: int | None = None) -> None:
        for evse in self.evses:
            if only is not None and evse.id != only:
                continue
            async with evse.lock:
                if evse.tx is not None:
                    await self._end_tx(evse, reason, trigger)
        await self.sync_status()

    # --------------------------------------------------------------------------- power loss glue
    async def resolve_power_loss_transactions(self) -> None:
        """After a power cut the station remembers open transactions and closes them with reason PowerLoss once it is back."""
        ended = False
        for evse in self.evses:
            tx = evse.tx
            if tx is not None and tx.power_lost_at is not None:
                async with evse.lock:
                    await self._end_tx(evse, "PowerLoss", "AbnormalCondition", timestamp=tx.power_lost_at.isoformat(), offline=True,
                                       energy_wh=evse.register_wh)
                ended = True
        if ended:
            await self.sync_status()

    # ------------------------------------------------------------------------------------ events
    async def notify_event(self, component: str, variable: str, value, *, trigger: str = "Alerting", evse_id: int | None = None, connector_id: int | None = None,
                           cleared: bool = False, tech_code: str | None = None, tech_info: str | None = None,
                           notification: str = "HardWiredNotification") -> None:
        if self.v16:
            await self.notify_event16(tech_code, cleared, evse_id, connector_id, tech_info)
            return
        self.event_ids += 1
        comp: dict = {"name": component}
        if evse_id is not None:
            comp["evse"] = {"id": evse_id, **({"connector_id": connector_id} if connector_id else {})}
        ev = {"event_id": self.event_ids, "timestamp": self.ts(), "trigger": trigger, "actual_value": str(value), "event_notification_type": notification,
              "component": comp, "variable": {"name": variable}, "cleared": cleared}
        if tech_code:
            ev["tech_code"] = tech_code
        if tech_info:
            ev["tech_info"] = tech_info
        seq, self.event_seq = self.event_seq, self.event_seq + 1
        await self.request("NotifyEvent", generated_at=self.ts(), seq_no=seq, event_data=[ev])

    async def security_event(self, kind: str, info: str | None = None) -> None:
        await self.request("SecurityEventNotification", type=kind, timestamp=self.ts(), **({"tech_info": info} if info else {}))

    # ------------------------------------------------------------------------------------ ticker
    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(1)
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("tick failed")

    async def _tick(self) -> None:
        mono = time.monotonic()
        dt, self._last_tick = mono - self._last_tick, mono
        self.last_alive = self.now()
        scaled = dt * self.cfg.time_scale
        for evse in self.evses:
            self._advance(evse, dt, scaled)
        await self._periodic(dt)
        self.tick_faults()
        self.changed()

    def _advance(self, evse: Evse, dt: float, scaled: float) -> None:
        tx = evse.tx
        evse.limit_w = self.power_limit_w(evse, tx)
        evse.temperature = self.temperature_of(evse)
        if tx is not None and tx.charging_state == "Charging" and evse.ev is not None:
            watts = min(evse.limit_w, self.ev_power_w(evse.ev, evse.spec))
            evse.power_w = watts
            wh = watts * scaled / 3600
            evse.register_wh += wh
            tx.energy_wh += wh
            tx.time_charging_s += dt
            evse.ev.soc = min(100.0, evse.ev.soc + wh / (evse.ev.capacity_kwh * 1000) * 100)
        else:
            evse.power_w = 0.0
        if tx is not None:
            tx.since_sample += dt
            want = self.state_flip_needed(evse)
            if want:
                self.spawn(self._state_flip(evse))

    def state_flip_needed(self, evse: Evse) -> bool:
        tx = evse.tx
        return tx is not None and not getattr(evse, "_flipping", False) and self.desired_state(evse) != tx.charging_state

    async def _state_flip(self, evse: Evse) -> None:
        evse._flipping = True
        try:
            async with evse.lock:
                if evse.tx is not None:
                    await self._reconcile(evse)
        finally:
            evse._flipping = False

    async def _periodic(self, dt: float) -> None:
        m = self.model
        interval = m.get_int("SampledDataCtrlr", "TxUpdatedInterval", 0)
        for evse in self.evses:
            tx = evse.tx
            if tx is not None and interval > 0 and tx.since_sample >= interval and m.get_bool("SampledDataCtrlr", "Enabled", True):
                tx.since_sample = 0
                self.spawn(self._sample_event(evse, tx))
        # clock aligned data
        aligned = m.get_int("AlignedDataCtrlr", "Interval", 0)
        if aligned > 0 and m.get_bool("AlignedDataCtrlr", "Enabled", True):
            bucket = int(self.now().timestamp() // aligned)
            if self._aligned_bucket is None:
                self._aligned_bucket = bucket
            elif bucket != self._aligned_bucket:
                self._aligned_bucket = bucket
                self.spawn(self._aligned_meter_values())
        now_t, now = time.time(), self.now()
        timeout = m.get_int("TxCtrlr", "EVConnectionTimeOut", 60)
        for evse in self.evses:
            if evse.plugged is None and evse.auth_at and now_t - evse.auth_at > timeout:
                self.spawn(self._connect_timeout(evse))
            if evse.pending_remote and now_t > evse.pending_remote["expires"]:
                evse.pending_remote = None
                evse.next_tx_id = None
                self.note(f"EVSE {evse.id}: remote start expired (no vehicle connected in {timeout}s)")
        for rid, res in list(self.reservations.items()):
            if res["expiry"] <= now:
                self.reservations.pop(rid, None)
                self.spawn(self._reservation_expired(rid))
        self.evaluate_monitors()

    async def _sample_event(self, evse: Evse, tx: Tx) -> None:
        if evse.tx is not tx or tx.ended:
            return
        meter = self.meter_values(evse, tx, "SampledDataCtrlr", "TxUpdatedMeasurands", "Sample.Periodic")
        async with self.tx_lock:
            await self.deliver_tx(self._tx_payload(tx, "Updated", "MeterValuePeriodic", meter_value=meter, charging_state=tx.charging_state))

    async def _aligned_meter_values(self) -> None:
        for evse in self.evses:
            meter = self.meter_values(evse, evse.tx, "AlignedDataCtrlr", "Measurands", "Sample.Clock")
            if meter and self.online:
                await self.request("MeterValues", evse_id=evse.id, meter_value=meter)

    async def _connect_timeout(self, evse: Evse) -> None:
        async with evse.lock:
            if evse.plugged is None and evse.auth is not None:
                evse.auth = None
                evse.auth_at = None
                self.note(f"EVSE {evse.id}: authorisation expired (no vehicle connected)")
                if evse.tx is not None:
                    await self._end_tx(evse, "Timeout", "EVConnectTimeout")
                await self.sync_status()

    async def _reservation_expired(self, rid: int) -> None:
        self.note(f"Reservation {rid} expired")
        if self.online:
            await self.request("ReservationStatusUpdate", reservation_id=rid, reservation_update_status="Expired")
        await self.sync_status()

    # ---------------------------------------------------------------------------------- snapshot
    def evses_snapshot(self) -> list[dict]:
        out = []
        for evse in self.evses:
            tx = evse.tx
            out.append({
                "id": evse.id, "current_type": evse.spec.current_type, "max_power_kw": evse.spec.max_power_kw, "voltage": evse.spec.voltage,
                "phases": evse.spec.phases, "operative": evse.operative, "locked": evse.locked, "power_kw": round(evse.power_w / 1000, 2),
                "limit_kw": round(evse.limit_w / 1000, 2), "register_kwh": round(evse.register_wh / 1000, 3), "temperature": round(evse.temperature, 1),
                "plugged_connector": evse.plugged.id if evse.plugged else None, "authorized": evse.auth["id_token"] if evse.auth else None,
                "faulted": self.evse_faulted(evse), "remote_pending": evse.pending_remote is not None,
                "ev": evse.ev.public() if evse.ev else None,
                "connectors": [{"id": c.id, "type": c.type, "status": self.compute_status(evse, c), "plugged": evse.plugged is c, "ocpp_id": self.cid16(evse, c)} for c in evse.connectors],
                "reservation": self._res_public(self.reservation_for(evse)),
                "tx": None if tx is None else {
                    "id": tx.id, "connector_id": tx.connector_id, "started_at": tx.started_at.isoformat(), "charging_state": tx.charging_state,
                    "energy_kwh": round(tx.energy_wh / 1000, 3), "id_token": tx.id_token["id_token"] if tx.id_token else None, "seq_no": tx.seq_no,
                    "duration_s": int((self.now() - tx.started_at).total_seconds()), "total_cost": tx.total_cost, "remote_start_id": tx.remote_start_id,
                    "ocpp_id": self.tx16.get(tx.id),
                },
            })
        return out

    @staticmethod
    def _res_public(r: dict | None) -> dict | None:
        return None if r is None else {"id": r["id"], "token": r["id_token"]["id_token"], "expiry": r["expiry"].isoformat()}
