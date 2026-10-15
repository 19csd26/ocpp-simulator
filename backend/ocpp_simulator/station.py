"""A simulated OCPP 2.0.1 charging station.

`Station` owns all state (connectors, transactions, device model, profiles, ...) and the
connection to the CSMS. Behaviour is split over mixins:
    ChargingMixin  - connectors, vehicles, authorisation, transactions, metering
    CommandsMixin  - the 40 CSMS -> station commands
    FaultsMixin    - fault injection catalogue
"""
import asyncio
import base64
import json
import logging
import random
import ssl
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ocpp.charge_point import camel_to_snake_case, remove_nones, serialize_as_dict, snake_to_camel_case
from ocpp.exceptions import OCPPError, ValidationError
from ocpp.v201 import call
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from ocpp_simulator.charging import ChargingMixin
from ocpp_simulator.commands import CommandsMixin
from ocpp_simulator.config import StationConfig
from ocpp_simulator.devicemodel import default_model
from ocpp_simulator.faults import FaultsMixin
from ocpp_simulator.hub import Hub
from ocpp_simulator.link import Link
from ocpp_simulator.rules import RuleSet
from ocpp_simulator.scenarios import Runner
from ocpp_simulator.summaries import summarize_call, summarize_result

log = logging.getLogger("station")
RESPONSE_TIMEOUT = 15


@dataclass
class Result:
    sent: bool
    ok: bool = False
    status: str | None = None
    response: dict | None = None
    error: str | None = None


class Station(ChargingMixin, CommandsMixin, FaultsMixin):
    def __init__(self, cfg: StationConfig, hub: Hub, response_timeout: float = RESPONSE_TIMEOUT) -> None:
        self.cfg = cfg
        self.hub = hub
        self.response_timeout = response_timeout
        self.model = default_model(cfg)
        self.rules = RuleSet()
        self.runner = Runner(self)
        self.removed = False
        # connection
        self.state = "stopped"  # stopped | connecting | booting | online | offline | rebooting | unpowered
        self.boot_status: str | None = None
        self.want_connected = False
        self.powered = True
        self.ws = None
        self.link: Link | None = None
        self.conn_task: asyncio.Task | None = None
        self.ticker_task: asyncio.Task | None = None
        self.heartbeat_task: asyncio.Task | None = None
        self.next_boot_reason = "PowerUp"
        self.boot_required = True
        self.last_error: str | None = None
        self.connected_since: float | None = None
        self.reconnect_attempt = 0
        self.clock_offset = 0.0  # seconds, set by the clock_drift fault
        self.stats = {"sent": 0, "received": 0, "errors": 0, "timeouts": 0, "reconnects": 0}
        self.last_responses: dict[str, dict] = {}
        self.tx_queue: deque[dict] = deque()
        self.tx_lock = asyncio.Lock()
        self._bg: set[asyncio.Task] = set()
        self._pending: dict[tuple[str, str], str] = {}
        self.notes: deque[str] = deque(maxlen=50)
        self.init_charging()
        self.init_commands()
        self.init_faults()

    # ---- time ----------------------------------------------------------------------------------
    def now(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=self.clock_offset)

    def ts(self) -> str:
        return self.now().isoformat()

    # ---- task helpers ----------------------------------------------------------------------------
    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._bg.add(task)

        def done(t: asyncio.Task) -> None:
            self._bg.discard(t)
            if not t.cancelled() and t.exception():
                log.error("background task failed: %r", t.exception(), exc_info=t.exception())
                self.note(f"internal error: {t.exception()!r}")

        task.add_done_callback(done)
        return task

    def note(self, text: str) -> None:
        self.notes.append(text)
        self.hub.log(self.cfg.id, {"ts": time.time(), "dir": "sim", "kind": "note", "action": None, "msg_id": None, "summary": text, "payload": None, "note": None})

    def changed(self) -> None:
        self.hub.state_changed(self)

    def apply_config(self, cfg: StationConfig, hardware_changed: bool) -> None:
        """Swap in an edited configuration. New hardware starts from a fresh device model and idle connectors."""
        self.cfg = cfg
        if hardware_changed:
            self.model = default_model(cfg)
            self.init_charging()
        else:
            for component, name, value in (("SecurityCtrlr", "Identity", cfg.ocpp_id), ("SecurityCtrlr", "SecurityProfile", cfg.security_profile),
                                           ("ChargingStation", "Model", cfg.model)):
                var = self.model.find(component, name)
                if var is not None:
                    var.value = str(value)
        self.note("Charger settings edited" + (": hardware replaced, all connectors are idle" if hardware_changed else ""))
        self.changed()

    # ---- connection lifecycle ---------------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self.ws is not None and self.state in ("booting", "online") and self.powered

    @property
    def online(self) -> bool:
        return self.connected and self.boot_status == "Accepted"

    async def connect(self) -> None:
        self.want_connected = True
        if not self.powered:
            self.note("Cannot connect: the station has no power")
            return
        self.ensure_ticker()
        if self.conn_task is None or self.conn_task.done():
            self.conn_task = asyncio.get_running_loop().create_task(self._connection_loop())
        self.changed()

    async def disconnect(self) -> None:
        self.want_connected = False
        await self._drop_connection(graceful=True)
        if self.conn_task:
            self.conn_task.cancel()
            self.conn_task = None
        self.state = "stopped"
        self.boot_status = None
        self.changed()

    async def shutdown(self) -> None:
        await self.disconnect()
        for t in [self.ticker_task, self.heartbeat_task, *list(self._bg)]:
            if t:
                t.cancel()
        self.ticker_task = None

    async def _drop_connection(self, graceful: bool) -> None:
        ws, self.ws = self.ws, None
        self.link = None
        self.boot_status = None if not self.powered else self.boot_status
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
            self.heartbeat_task = None
        if ws is not None:
            try:
                if graceful:
                    await asyncio.wait_for(ws.close(), 3)
                else:
                    ws.transport.abort()  # power or network cut: no closing handshake
            except Exception:
                try:
                    await asyncio.wait_for(ws.close(), 1)
                except Exception:
                    pass

    def _ssl_context(self):
        if not self.cfg.csms_url.startswith("wss"):
            return None
        ctx = ssl.create_default_context(cafile=self.cfg.ca_file) if self.cfg.ca_file else ssl.create_default_context()
        if not self.cfg.verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        if self.cfg.client_cert and self.cfg.client_key:
            ctx.load_cert_chain(self.cfg.client_cert, self.cfg.client_key)
        return ctx

    def _headers(self) -> dict:
        if self.cfg.security_profile >= 1 and self.cfg.password:
            token = base64.b64encode(f"{self.cfg.ocpp_id}:{self.cfg.password}".encode()).decode()
            return {"Authorization": f"Basic {token}"}
        return {}

    def _backoff(self) -> float:
        m = self.model
        base = m.get_int("OCPPCommCtrlr", "RetryBackOffWaitMinimum", 2)
        repeat = m.get_int("OCPPCommCtrlr", "RetryBackOffRepeatTimes", 3)
        jitter = random.uniform(0, m.get_int("OCPPCommCtrlr", "RetryBackOffRandomRange", 2))
        return base * (2 ** min(self.reconnect_attempt, repeat)) + jitter

    async def _connection_loop(self) -> None:
        while self.want_connected and self.powered:
            if not self.network_up:
                self.state = "offline"
                self.changed()
                try:
                    await asyncio.wait_for(self._wake.wait(), 1)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
                continue
            self.state = "connecting"
            self.changed()
            url = f"{self.cfg.csms_url.rstrip('/')}/{self.cfg.ocpp_id}"
            try:
                ping = self.model.get_int("OCPPCommCtrlr", "WebSocketPingInterval", 30) or None
                async with connect(url, subprotocols=["ocpp2.0.1"], additional_headers=self._headers(), ssl=self._ssl_context(),
                                   open_timeout=10, ping_interval=ping, ping_timeout=ping, max_size=None) as ws:
                    self.ws = ws
                    self.last_error = None
                    self.connected_since = time.time()
                    self.link = Link(self.cfg.ocpp_id, _Conn(ws, self), response_timeout=self.response_timeout)
                    self.link.sim = self
                    self.state = "booting"
                    self.note(f"Connected to {url}")
                    self.changed()
                    boot = self.spawn(self._on_connected())
                    try:
                        await self.link.start()
                    finally:
                        boot.cancel()
            except ConnectionClosed as exc:
                self.last_error = f"connection closed ({exc.rcvd.code if exc.rcvd else 1006})"
            except InvalidHandshake as exc:
                self.last_error = f"CSMS refused the connection: {exc}"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
            self.ws = None
            self.link = None
            if self.heartbeat_task:
                self.heartbeat_task.cancel()
                self.heartbeat_task = None
            self.connected_since = None
            if not (self.want_connected and self.powered):
                break
            self.state = "offline"
            self.boot_status = self.boot_status if self.boot_status == "Accepted" else None
            self.stats["reconnects"] += 1
            wait = self._backoff()
            self.reconnect_attempt += 1
            self.note(f"Offline ({self.last_error}); reconnecting in {wait:.1f}s")
            self.changed()
            try:
                await asyncio.wait_for(self._wake.wait(), wait)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    # ---- boot / after-boot ---------------------------------------------------------------------------
    async def _on_connected(self) -> None:
        self.reconnect_attempt = 0
        if self.boot_required or self.cfg.boot_on_reconnect:
            if not await self._boot():
                return
        else:
            self.boot_status = "Accepted"
            self.state = "online"
        await self._after_boot()

    async def _boot(self) -> bool:
        reason = self.next_boot_reason
        while self.connected or self.state == "booting":
            res = await self.request(
                "BootNotification",
                reason=reason,
                charging_station={
                    "vendor_name": self.cfg.vendor, "model": self.cfg.model, "serial_number": self.cfg.serial_number,
                    "firmware_version": self.cfg.firmware_version,
                    "modem": {"iccid": self.cfg.iccid, "imsi": self.cfg.imsi},
                },
            )
            if not res.sent:
                return False
            if res.ok:
                self.boot_status = res.status
                interval = (res.response or {}).get("interval", 30)
                if res.status == "Accepted":
                    if interval:
                        self.model.write({"name": "OCPPCommCtrlr"}, {"name": "HeartbeatInterval"}, str(int(interval)))
                    self.state = "online"
                    self.next_boot_reason = "PowerUp"
                    self.boot_required = False
                    self.changed()
                    return True
                self.note(f"BootNotification {res.status}: retrying in {interval or 30}s")
                self.changed()
                reason = "PowerUp" if reason not in ("Triggered",) else reason
                await asyncio.sleep(max(1, interval or 30))
            else:
                self.note(f"BootNotification failed ({res.error}); retrying in 5s")
                await asyncio.sleep(5)
        return False

    async def _after_boot(self) -> None:
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
        self.heartbeat_task = asyncio.get_running_loop().create_task(self._heartbeat_loop())
        await self.resolve_power_loss_transactions()
        await self.send_all_statuses()
        await self.flush_queue()
        await self.after_boot_hook()
        self.changed()

    async def _heartbeat_loop(self) -> None:
        """Re-reads the interval every second, so a SetVariables change takes effect immediately."""
        waited = 0.0
        while True:
            await asyncio.sleep(1)
            waited += 1
            if waited >= max(1, self.model.get_int("OCPPCommCtrlr", "HeartbeatInterval", 30)):
                waited = 0.0
                if self.online:
                    await self.request("Heartbeat")

    # ---- sending ----------------------------------------------------------------------------------------
    async def request(self, action: str, *, skip_validation: bool = False, **fields) -> Result:
        """Send a station -> CSMS request and wait for the answer. `fields` are the snake_case dataclass fields."""
        link = self.link
        if link is None or self.ws is None:
            return Result(sent=False, error="offline")
        try:
            message = getattr(call, action)(**fields)
        except TypeError as exc:
            return Result(sent=False, error=f"bad payload: {exc}")
        return await self._call(link, message, action, skip_validation)

    async def _call(self, link: Link, message, action: str, skip_validation: bool = False) -> Result:
        try:
            response = await link.call(message, suppress=False, skip_schema_validation=skip_validation)
        except asyncio.TimeoutError:
            self.stats["timeouts"] += 1
            self.note(f"{action}: no response within {self.response_timeout}s")
            return Result(sent=True, error="timeout")
        except OCPPError as exc:
            self.stats["errors"] += 1
            return Result(sent=True, error=f"{exc.code}: {exc.description}")
        except ValidationError as exc:
            self.stats["errors"] += 1
            self.note(f"{action}: payload failed schema validation: {exc}")
            return Result(sent=False, error=f"schema: {exc}")
        except (ConnectionClosed, RuntimeError, OSError):
            return Result(sent=False, error="offline")
        body = remove_nones(snake_to_camel_case(serialize_as_dict(response))) if response is not None else {}
        status = body.get("status") or (body.get("idTokenInfo") or {}).get("status")
        self.last_responses[action] = {"status": status, "response": body, "at": time.time()}
        return Result(sent=True, ok=True, status=status, response=body)

    async def send_raw(self, action: str, payload: dict, *, validate: bool = True) -> Result:
        """Send any station -> CSMS message with a payload in the spec's camelCase (used by the UI and scenarios)."""
        if action not in self.CP_ACTIONS:
            return Result(sent=False, error=f"'{action}' is not a message a charging station can send")
        link = self.link
        if link is None:
            return Result(sent=False, error="offline")
        try:
            message = getattr(call, action)(**camel_to_snake_case(payload))
        except TypeError as exc:
            if validate:
                return Result(sent=False, error=f"bad payload: {exc}")
            raise
        return await self._call(link, message, action, skip_validation=not validate)

    CP_ACTIONS = {
        "Authorize", "BootNotification", "ClearedChargingLimit", "DataTransfer", "FirmwareStatusNotification", "Get15118EVCertificate",
        "GetCertificateStatus", "Heartbeat", "LogStatusNotification", "MeterValues", "NotifyChargingLimit", "NotifyCustomerInformation",
        "NotifyDisplayMessages", "NotifyEVChargingNeeds", "NotifyEVChargingSchedule", "NotifyEvent", "NotifyMonitoringReport", "NotifyReport",
        "PublishFirmwareStatusNotification", "ReportChargingProfiles", "ReservationStatusUpdate", "SecurityEventNotification", "SignCertificate",
        "StatusNotification", "TransactionEvent",
    }

    # ---- TransactionEvent with an offline queue (OCPP 2.0.1 requires queueing while offline) --------------
    async def deliver_tx(self, payload: dict) -> None:
        """Send a TransactionEvent, or queue it when offline. The caller holds `tx_lock` (this keeps seqNo order)."""
        if self.online and not self.tx_queue:
            res = await self.request("TransactionEvent", **payload)
            if res.sent and res.error != "timeout":
                return
        payload = {**payload, "offline": True}
        self.tx_queue.append(payload)
        self.note(f"TransactionEvent {payload['event_type']} queued (offline), {len(self.tx_queue)} waiting")
        self.changed()

    async def flush_queue(self) -> None:
        async with self.tx_lock:
            sent = 0
            while self.tx_queue and self.online:
                res = await self.request("TransactionEvent", **self.tx_queue[0])
                if not res.sent or res.error == "timeout":
                    break
                self.tx_queue.popleft()
                sent += 1
            if sent:
                self.note(f"Delivered {sent} queued TransactionEvent message(s); {len(self.tx_queue)} left")
                self.changed()

    # ---- frame logging (called by _Conn) -------------------------------------------------------------------
    def on_frame(self, direction: str, raw: str, note: str | None = None) -> None:
        try:
            frame = json.loads(raw)
            mtype, msg_id = frame[0], str(frame[1])
        except Exception:
            self.hub.log(self.cfg.id, {"ts": time.time(), "dir": direction, "kind": "raw", "action": None, "msg_id": None, "summary": str(raw)[:160], "payload": str(raw), "note": note})
            return
        if mtype == 2:
            action, payload = frame[2], frame[3]
            self._pending[(direction, msg_id)] = action
            kind, summary = "call", summarize_call(action, payload)
        else:
            call_dir = "out" if direction == "in" else "in"
            action = self._pending.pop((call_dir, msg_id), None)
            if mtype == 3:
                payload, kind, summary = frame[2], "result", summarize_result(action, frame[2])
            else:
                payload, kind = {"code": frame[2], "description": frame[3], "details": frame[4] if len(frame) > 4 else {}}, "error"
                summary = f"{action or 'request'} failed · {frame[2]}: {frame[3]}"
        if direction == "in":
            self.stats["received"] += 1
        else:
            self.stats["sent"] += 1
        self.hub.log(self.cfg.id, {"ts": time.time(), "dir": direction, "kind": kind, "action": action, "msg_id": msg_id, "summary": summary, "payload": payload, "note": note})
        self.changed()

    # ---- snapshot for the UI ----------------------------------------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "id": self.cfg.id, "name": self.cfg.name, "ocpp_id": self.cfg.ocpp_id, "csms_url": self.cfg.csms_url,
            "vendor": self.cfg.vendor, "model": self.cfg.model, "firmware_version": self.cfg.firmware_version,
            "state": self.state, "powered": self.powered, "boot_status": self.boot_status, "want_connected": self.want_connected,
            "last_error": self.last_error, "connected_since": self.connected_since, "reconnect_attempt": self.reconnect_attempt,
            "heartbeat_interval": self.model.get_int("OCPPCommCtrlr", "HeartbeatInterval", 30), "time_scale": self.cfg.time_scale,
            "config": {"security_profile": self.cfg.security_profile, "verify_tls": self.cfg.verify_tls, "has_password": bool(self.cfg.password),
                       "boot_on_reconnect": self.cfg.boot_on_reconnect, "end_on_full": self.cfg.end_on_full, "hardware": [e.model_dump() for e in self.cfg.evses]},
            "stats": dict(self.stats), "queue": len(self.tx_queue), "notes": list(self.notes)[-5:],
            "evses": self.evses_snapshot(), "faults": self.faults_snapshot(), "rules": self.rules.snapshot(),
            "last_responses": {k: {"status": v["status"]} for k, v in self.last_responses.items()},
            "scenario": self.runner.run.public() if self.runner.run else None,
            **self.commands_snapshot(),
        }


class _Conn:
    """Connection adapter: frame logging plus outgoing message rules, latency and packet loss."""

    def __init__(self, ws, sim: Station) -> None:
        self.ws, self.sim = ws, sim

    async def recv(self) -> str:
        raw = await self.ws.recv()
        self.sim.on_frame("in", raw)
        return raw

    async def send(self, message: str) -> None:
        sim = self.sim
        note = None
        try:
            frame = json.loads(message)
        except ValueError:
            frame = None
        is_call = isinstance(frame, list) and frame and frame[0] == 2
        rule = sim.rules.match("out", frame[2]) if is_call else None
        if rule is not None:
            note = f"rule #{rule.id}: {rule.effect}"
            if rule.effect == "drop":
                sim.on_frame("out", message, note=note + " (not sent)")
                return
            if rule.effect == "delay":
                await asyncio.sleep(rule.params.get("delay_ms", 1000) / 1000)
            elif rule.effect == "corrupt":
                frame[3] = {**frame[3], "bogusField": True}  # additionalProperties are forbidden by the schema
                message = json.dumps(frame)
        latency = sim.extra_latency_s()
        if latency:
            await asyncio.sleep(latency)
        if sim.should_drop_packet():
            sim.on_frame("out", message, note="packet loss (not sent)")
            return
        sim.on_frame("out", message, note=note)
        await self.ws.send(message)
        if rule is not None and rule.effect == "duplicate":
            sim.on_frame("out", message, note=note + " (second copy)")
            await self.ws.send(message)
