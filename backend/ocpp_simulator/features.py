"""Operator features of the simulator that are not part of the OCPP protocol itself.

Auto charge (the vehicle presents its own id), smart plug (no cable detection), bidirectional charging (V2G), setting the meter
register by hand, editing the local authorization list, rebooting with a chosen boot reason, and simulating a command as if
the CSMS had sent it.
"""
import asyncio
import json
import uuid

from ocpp.charge_point import camel_to_snake_case, remove_nones, snake_to_camel_case

from ocpp_simulator.charging import EvState
from ocpp_simulator.config import EV_MODELS

BOOT_REASONS = ["ApplicationReset", "FirmwareUpdate", "LocalReset", "PowerUp", "RemoteReset", "ScheduledReset", "Triggered", "Unknown", "Watchdog"]
SMART_PLUG_VEHICLE = "mid"  # a smart plug has no cable detection, so the "vehicle" it charges is whatever is behind the socket


class FeaturesMixin:
    def command_names(self) -> list[str]:
        """The commands a CSMS can send to this charger in the protocol in use."""
        from ocpp_simulator.link import COMMAND_NAMES
        from ocpp_simulator.v16 import COMMAND_NAMES_16

        return list(COMMAND_NAMES_16) if self.v16 else list(COMMAND_NAMES)

    # ------------------------------------------------------------------------------------------ auto charge
    async def _auto_charge(self, evse_id: int) -> None:
        """Plug-and-charge: right after the cable goes in, the vehicle presents its own id and the session starts without a card."""
        await asyncio.sleep(0.5)
        evse = self.evse(evse_id)
        if evse.plugged is None or evse.tx is not None or evse.auth is not None:
            return
        self.note(f"EVSE {evse_id}: auto charge, the vehicle presents {self.cfg.vehicle_id}")
        await self.tap_card(evse_id, self.cfg.vehicle_id, "eMAID")

    # ------------------------------------------------------------------------------------------- smart plug
    async def smart_plug_on(self, evse) -> None:
        """A smart plug starts only from a remote start: switch the socket on with whatever is connected behind it."""
        if evse.plugged is not None:
            return
        evse.plugged = evse.connectors[0]
        evse.finishing = False
        evse.ev = EvState(key=SMART_PLUG_VEHICLE, soc=20.0, target_soc=100.0, **EV_MODELS[SMART_PLUG_VEHICLE])
        self.note(f"EVSE {evse.id}: smart plug switched on by the remote start")

    def smart_plug_off(self, evse) -> None:
        evse.plugged = None
        evse.ev = None
        evse.locked = False
        evse.finishing = False

    # ------------------------------------------------------------------------------- bidirectional charging
    async def set_v2x(self, evse_id: int, mode: str, power_kw: float | None = None) -> dict:
        """mode: "charge" (grid to vehicle) or "discharge" (vehicle to grid / home)."""
        if mode not in ("charge", "discharge"):
            raise ValueError("mode must be 'charge' or 'discharge'")
        evse = self.evse(evse_id)
        async with evse.lock:
            if mode == "discharge":
                if evse.ev is None or evse.tx is None:
                    raise ValueError(f"EVSE {evse_id} needs a vehicle and a running session before it can discharge")
                if evse.ev.max_discharge_kw <= 0:
                    raise ValueError(f"{evse.ev.label} cannot feed energy back (it is not a bidirectional vehicle)")
                if evse.ev.soc <= 10:
                    raise ValueError("The battery is at 10 % or less: it will not discharge")
                evse.discharge_w = max(0.0, float(power_kw or 0) * 1000)
            evse.v2x = mode
            if mode == "charge":
                evse.discharge_w = 0.0
            self.note(f"EVSE {evse_id}: {'vehicle-to-grid discharge' if mode == 'discharge' else 'grid-to-vehicle charging'}"
                      + (f" at {power_kw} kW" if mode == "discharge" and power_kw else ""))
            await self._reconcile(evse)
        self.changed()
        return {"ok": True, "mode": mode}

    # --------------------------------------------------------------------------------------- meter register
    async def set_meter_register(self, evse_id: int, import_kwh: float | None = None, export_kwh: float | None = None) -> dict:
        """Overwrite the lifetime energy register (to see how the CSMS reacts to a jump, a rollback or a reset)."""
        evse = self.evse(evse_id)
        if import_kwh is not None:
            if import_kwh < 0:
                raise ValueError("The register cannot be negative")
            evse.register_wh = import_kwh * 1000
        if export_kwh is not None:
            if export_kwh < 0:
                raise ValueError("The register cannot be negative")
            evse.export_register_wh = export_kwh * 1000
        self.note(f"EVSE {evse_id}: meter register set to {evse.register_wh / 1000:.3f} kWh")
        self.changed()
        return {"ok": True}

    # ------------------------------------------------------------------------------- local authorization list
    def local_list_add(self, token: str, status: str = "Accepted") -> dict:
        token = token.strip()
        if not token or len(token) > 36:
            raise ValueError("A token must be 1 to 36 characters")
        if status not in ("Accepted", "Blocked", "Expired", "Invalid"):
            raise ValueError("Unknown status")
        self.local_list["entries"][token] = {"status": status, "type": "ISO14443"}
        self.local_list["version"] += 1
        self._sync_list_count()
        self.changed()
        return {"ok": True, "version": self.local_list["version"]}

    def local_list_remove(self, token: str) -> dict:
        if self.local_list["entries"].pop(token, None) is None:
            raise ValueError(f"Token {token} is not in the local list")
        self.local_list["version"] += 1
        self._sync_list_count()
        self.changed()
        return {"ok": True, "version": self.local_list["version"]}

    def _sync_list_count(self) -> None:
        var = self.model.find("LocalAuthListCtrlr", "Entries")
        if var is not None:
            var.value = str(len(self.local_list["entries"]))

    # ----------------------------------------------------------------------------------- reboot with a reason
    async def reboot_with(self, reason: str) -> dict:
        if reason not in BOOT_REASONS:
            raise ValueError(f"Unknown boot reason '{reason}'. Use one of: {', '.join(BOOT_REASONS)}")
        self.spawn(self.reboot(reason, "ImmediateReset"))
        return {"ok": True, "reason": reason}

    # ------------------------------------------------------------------------ simulate an incoming CSMS command
    async def simulate_command(self, action: str, payload: dict) -> dict:
        """Run a CSMS command as if it had arrived over the wire (no CSMS needed). It appears in the log, marked as simulated."""
        if action not in self.command_names():
            raise ValueError(f"'{action}' is not a command a CSMS can send in OCPP {self.version}")
        message_id = f"sim-{uuid.uuid4().hex[:8]}"
        self.on_frame("in", json.dumps([2, message_id, action, payload]), note="simulated here: the CSMS did not send this")
        kwargs = camel_to_snake_case(payload or {})
        try:
            result = await (self.run_command16 if self.v16 else self.run_command)(action, kwargs)
        except TypeError as exc:
            raise ValueError(f"Invalid payload for {action}: {exc}")
        body = remove_nones(snake_to_camel_case(result or {}))
        self.on_frame("out", json.dumps([3, message_id, body]), note="simulated here")
        return body

    # ----------------------------------------------------------------------------------------- raw frames
    async def send_raw_frame(self, text: str) -> dict:
        """Put any JSON text on the WebSocket exactly as given (a CALL, a CALLRESULT, a CALLERROR, or something malformed).

        Nothing is validated and no answer is waited for: the CSMS's reply shows up in the log.
        """
        if self.link is None or self.ws is None or not self.connected:
            raise ValueError("The charger is not connected")
        text = text.strip()
        if not text:
            raise ValueError("Nothing to send")
        try:
            json.loads(text)
        except ValueError:
            pass  # deliberately allowed: sending malformed JSON is a legitimate test of the CSMS
        await self.link._connection.send(text)
        return {"ok": True, "bytes": len(text)}
