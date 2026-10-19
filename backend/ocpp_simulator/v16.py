"""OCPP 1.6J support.

The station's physics (vehicles, power, energy, faults, scenarios, rules) is shared by both protocol versions. This module
adds what is specific to OCPP 1.6:

* `V16Mixin` (part of `Station`): connector numbering, the 1.6 connector status model, the translation of the station's
  internal events into 1.6 messages (StartTransaction / MeterValues / StopTransaction instead of TransactionEvent,
  StatusNotification with an errorCode instead of NotifyEvent, ...), the configuration keys and the CSMS commands.
* `Link16`: the per-connection endpoint that routes the CSMS commands of OCPP 1.6 to the station.

A connection speaks 1.6 when the WebSocket subprotocol negotiated with the CSMS is `ocpp1.6`.
"""
import asyncio
import random
import time

from ocpp.routing import on
from ocpp.v16 import ChargePoint as BaseChargePoint16
from ocpp.v16 import call_result as result16
from ocpp.v16.enums import Action as Action16

from ocpp_simulator import smartcharging
from ocpp_simulator.link import RuleRouting, snake

# ---- the 28 operations of OCPP 1.6 (Core, Firmware, Local list, Reservation, Smart charging, Remote trigger) -------------
CP_ACTIONS_16 = {
    "Authorize", "BootNotification", "DataTransfer", "DiagnosticsStatusNotification", "FirmwareStatusNotification", "Heartbeat",
    "MeterValues", "StartTransaction", "StatusNotification", "StopTransaction",
}
COMMAND_NAMES_16 = [
    "CancelReservation", "ChangeAvailability", "ChangeConfiguration", "ClearCache", "ClearChargingProfile", "DataTransfer",
    "GetCompositeSchedule", "GetConfiguration", "GetDiagnostics", "GetLocalListVersion", "RemoteStartTransaction",
    "RemoteStopTransaction", "ReserveNow", "Reset", "SendLocalList", "SetChargingProfile", "TriggerMessage", "UnlockConnector",
    "UpdateFirmware",
]
REJECT_PAYLOADS_16 = {
    "RemoteStartTransaction": {"status": "Rejected"}, "RemoteStopTransaction": {"status": "Rejected"}, "Reset": {"status": "Rejected"},
    "ChangeAvailability": {"status": "Rejected"}, "UnlockConnector": {"status": "UnlockFailed"}, "TriggerMessage": {"status": "Rejected"},
    "SetChargingProfile": {"status": "Rejected"}, "ClearChargingProfile": {"status": "Unknown"}, "GetCompositeSchedule": {"status": "Rejected"},
    "ReserveNow": {"status": "Rejected"}, "CancelReservation": {"status": "Rejected"}, "ClearCache": {"status": "Rejected"},
    "SendLocalList": {"status": "Failed"}, "ChangeConfiguration": {"status": "Rejected"}, "DataTransfer": {"status": "Rejected"},
}

# ---- value mappings between the station's internal (2.0.1 shaped) model and OCPP 1.6 -------------------------------------
ERROR_FOR_KIND = {
    "power_meter_failure": "PowerMeterFailure", "over_current": "OverCurrentFailure", "ground_fault": "GroundFailure",
    "over_voltage": "OverVoltage", "under_voltage": "UnderVoltage", "over_temperature": "HighTemperature",
    "emergency_stop": "OtherError", "tamper_detected": "OtherError", "contactor_failure": "PowerSwitchFailure",
    "connector_lock_failure": "ConnectorLockFailure", "rfid_reader_failure": "ReaderFailure", "ev_communication_loss": "EVCommunicationError",
}
ERROR_FOR_TECH = {
    "PowerMeterFailure": "PowerMeterFailure", "OverCurrent": "OverCurrentFailure", "GroundFault": "GroundFailure", "OverVoltage": "OverVoltage",
    "UnderVoltage": "UnderVoltage", "OverTemperature": "HighTemperature", "EmergencyStop": "OtherError", "TamperDetected": "OtherError",
    "ContactorFailure": "PowerSwitchFailure", "ConnectorLockFailure": "ConnectorLockFailure", "ReaderFailure": "ReaderFailure",
}
STOP_REASONS_16 = {"EmergencyStop", "EVDisconnected", "HardReset", "Local", "Other", "PowerLoss", "Reboot", "Remote", "SoftReset", "UnlockCommand", "DeAuthorized"}
STOP_ALIASES = {"ImmediateReset": "HardReset"}  # everything else that 1.6 has no word for becomes "Other"
FIRMWARE_STATUS_16 = {"DownloadScheduled": "Idle", "DownloadPaused": "Downloading", "InstallRebooting": "Installing", "InstallScheduled": "Downloaded",
                      "InstallVerificationFailed": "InstallationFailed", "InvalidSignature": "InstallationFailed", "SignatureVerified": "Downloaded"}
FIRMWARE_STATUSES = {"Downloaded", "DownloadFailed", "Downloading", "Idle", "InstallationFailed", "Installing", "Installed"}
DIAGNOSTICS_STATUS_16 = {"Uploaded": "Uploaded", "Uploading": "Uploading", "UploadFailed": "UploadFailed", "Idle": "Idle"}
UNITS_16 = {"Wh", "kWh", "varh", "kvarh", "W", "kW", "VA", "kVA", "var", "kvar", "A", "V", "K", "Celsius", "Fahrenheit", "Percent"}
PURPOSE_16 = {"ChargePointMaxProfile": "ChargingStationMaxProfile"}
PURPOSE_BACK = {v: k for k, v in PURPOSE_16.items()}
TX_STATE_STATUS = {"Charging": "Charging", "SuspendedEV": "SuspendedEV", "SuspendedEVSE": "SuspendedEVSE"}
TRIGGERS_16 = {"BootNotification", "DiagnosticsStatusNotification", "FirmwareStatusNotification", "Heartbeat", "MeterValues", "StatusNotification"}

# ---- configuration keys: (key, writable, kind, source). Sources: ("model", component, variable) | ("store", default) | ("static", value)
CONFIG_KEYS = [
    ("AllowOfflineTxForUnknownId", True, "bool", ("model", "AuthCtrlr", "OfflineTxForUnknownIdEnabled")),
    ("AuthorizationCacheEnabled", True, "bool", ("model", "AuthCacheCtrlr", "Enabled")),
    ("AuthorizeRemoteTxRequests", True, "bool", ("model", "AuthCtrlr", "AuthorizeRemoteStart")),
    ("ClockAlignedDataInterval", True, "int", ("model", "AlignedDataCtrlr", "Interval")),
    ("ConnectionTimeOut", True, "int", ("model", "TxCtrlr", "EVConnectionTimeOut")),
    ("ConnectorPhaseRotation", True, "csl", ("store", "NotApplicable")),
    ("GetConfigurationMaxKeys", False, "int", ("static", "99")),
    ("HeartbeatInterval", True, "int", ("model", "OCPPCommCtrlr", "HeartbeatInterval")),
    ("LocalAuthorizeOffline", True, "bool", ("model", "AuthCtrlr", "LocalAuthorizeOffline")),
    ("LocalPreAuthorize", True, "bool", ("model", "AuthCtrlr", "LocalPreAuthorize")),
    ("MeterValuesAlignedData", True, "csl", ("model", "AlignedDataCtrlr", "Measurands")),
    ("MeterValuesSampledData", True, "csl", ("model", "SampledDataCtrlr", "TxUpdatedMeasurands")),
    ("MeterValueSampleInterval", True, "int", ("model", "SampledDataCtrlr", "TxUpdatedInterval")),
    ("MinimumStatusDuration", True, "int", ("store", "0")),
    ("NumberOfConnectors", False, "int", ("dynamic", "connectors")),
    ("ResetRetries", True, "int", ("store", "1")),
    ("StopTransactionOnEVSideDisconnect", True, "bool", ("store", "true")),
    ("StopTransactionOnInvalidId", True, "bool", ("store", "true")),
    ("StopTxnAlignedData", True, "csl", ("model", "AlignedDataCtrlr", "TxEndedMeasurands")),
    ("StopTxnSampledData", True, "csl", ("model", "SampledDataCtrlr", "TxEndedMeasurands")),
    ("SupportedFeatureProfiles", False, "csl", ("static", "Core,FirmwareManagement,LocalAuthListManagement,Reservation,SmartCharging,RemoteTrigger")),
    ("TransactionMessageAttempts", True, "int", ("store", "3")),
    ("TransactionMessageRetryInterval", True, "int", ("store", "60")),
    ("UnlockConnectorOnEVSideDisconnect", True, "bool", ("store", "true")),
    ("WebSocketPingInterval", True, "int", ("model", "OCPPCommCtrlr", "WebSocketPingInterval")),
    ("LocalAuthListEnabled", True, "bool", ("model", "LocalAuthListCtrlr", "Enabled")),
    ("LocalAuthListMaxLength", False, "int", ("model", "LocalAuthListCtrlr", "Storage")),
    ("SendLocalListMaxLength", False, "int", ("static", "100")),
    ("ReserveConnectorZeroSupported", False, "bool", ("model", "ReservationCtrlr", "NonEvseSpecific")),
    ("ChargeProfileMaxStackLevel", False, "int", ("static", "99")),
    ("ChargingScheduleAllowedChargingRateUnit", False, "csl", ("static", "Current,Power")),
    ("ChargingScheduleMaxPeriods", False, "int", ("static", "24")),
    ("ConnectorSwitch3to1PhaseSupported", False, "bool", ("static", "false")),
    ("MaxChargingProfilesInstalled", False, "int", ("model", "SmartChargingCtrlr", "EntriesChargingProfiles")),
]
CONFIG_BY_KEY = {k[0]: k for k in CONFIG_KEYS}


def _bool_text(value) -> str:
    return "true" if str(value).lower() in ("true", "1") else "false"


class V16Mixin:
    # ------------------------------------------------------------------------------------------ identity
    def init_v16(self) -> None:
        self._negotiated: str | None = None  # "2.0.1" or "1.6", set from the WebSocket subprotocol
        self.tx16: dict[str, int] = {}  # internal transaction id -> the integer transactionId the CSMS assigned
        self.config_store: dict[str, str] = {k[0]: k[3][1] for k in CONFIG_KEYS if k[3][0] == "store"}

    @property
    def version(self) -> str:
        """The protocol in use: what the CSMS selected on the connection, else what this charger is configured for (auto: 2.0.1)."""
        if self._negotiated:
            return self._negotiated
        return "1.6" if self.cfg.ocpp_version == "1.6" else "2.0.1"

    @property
    def v16(self) -> bool:
        return self.version == "1.6"

    SUBPROTOCOLS = {"2.0.1": "ocpp2.0.1", "1.6": "ocpp1.6"}

    def offered_subprotocols(self) -> list[str]:
        if self.cfg.ocpp_version == "auto":
            return [self.SUBPROTOCOLS[v] for v in ("2.0.1", "1.6")]  # Auto recognises 1.6 and 2.0.1: the CSMS picks
        return [self.SUBPROTOCOLS[self.cfg.ocpp_version]]

    # ---------------------------------------------------------------------------------- connector numbering
    def connectors16(self) -> list[tuple[int, object, object]]:
        """OCPP 1.6 numbers every connector of the charge point 1..N (there are no EVSEs); 0 is the whole station."""
        out, n = [], 0
        for evse in self.evses:
            for conn in evse.connectors:
                n += 1
                out.append((n, evse, conn))
        return out

    def cid16(self, evse, conn) -> int:
        return next(n for n, e, c in self.connectors16() if e is evse and c is conn)

    def resolve16(self, connector_id: int):
        return next(((e, c) for n, e, c in self.connectors16() if n == connector_id), None)

    def active_connector(self, evse):
        return evse.plugged or evse.connectors[0]

    # -------------------------------------------------------------------------------------------- status
    def compute_status16(self, evse, conn) -> str:
        if self.evse_faulted(evse):
            return "Faulted"
        if not (self.operative and evse.operative and conn.operative):
            return "Unavailable"
        if evse.plugged is not None and evse.plugged is not conn:
            return "Unavailable"  # another connector of the same EVSE is in use
        if evse.tx is not None:
            return TX_STATE_STATUS.get(evse.tx.charging_state, "SuspendedEVSE")
        if self.cfg.smart_plug:
            return "Reserved" if self._reserved(evse) else "Available"  # no cable detection: nothing happens until a remote start
        if evse.plugged is conn:
            return "Finishing" if getattr(evse, "finishing", False) and self.cfg.finishing_mode else "Preparing"
        if evse.auth is not None:
            return "Preparing"  # authorised, waiting for the vehicle
        if self._reserved(evse):
            return "Reserved"
        return "Available"

    def _reserved(self, evse) -> bool:
        return any(r["evse_id"] in (evse.id, None) and r["expiry"] > self.now() for r in self.reservations.values())

    def status0_16(self) -> str:
        if any(f.active and f.scope is None and f.state.get("faulted") for f in self.faults.values()):
            return "Faulted"
        return "Available" if self.operative else "Unavailable"

    def error_code16(self, evse=None) -> str:
        for f in self.faults.values():
            if f.active and f.kind in ERROR_FOR_KIND and (f.scope is None or evse is None or f.scope == evse.id):
                return ERROR_FOR_KIND[f.kind]
        return "NoError"

    async def send_status0_16(self) -> None:
        await self.request_native("StatusNotification", connector_id=0, error_code=self.error_code16(), status=self.status0_16(), timestamp=self.ts())

    async def notify_event16(self, tech_code: str | None, cleared: bool, evse_id: int | None, connector_id: int | None, tech_info: str | None) -> None:
        """OCPP 1.6 has no NotifyEvent: a problem is reported as the errorCode of a StatusNotification."""
        code = "NoError" if cleared else ERROR_FOR_TECH.get(tech_code or "", "OtherError")
        fields = {"error_code": code, "timestamp": self.ts()}
        if tech_code and not cleared:
            fields["vendor_error_code"] = tech_code[:50]
        if tech_info and not cleared:
            fields["info"] = tech_info[:50]
        if evse_id is None:
            await self.request_native("StatusNotification", connector_id=0, status=self.status0_16(), **fields)
            return
        evse = self.evse(evse_id)
        conn = self.connector(evse_id, connector_id) if connector_id else self.active_connector(evse)
        await self.request_native("StatusNotification", connector_id=self.cid16(evse, conn), status=self.compute_status16(evse, conn), **fields)

    # ----------------------------------------------------------------- translating internal events to 1.6
    def mv16(self, meter_value: list | None) -> list[dict]:
        out = []
        for mv in meter_value or []:
            sampled = []
            for s in mv.get("sampled_value", []):
                unit = (s.get("unit_of_measure") or {}).get("unit")
                item = {"value": str(s["value"]), "context": s.get("context", "Sample.Periodic"), "format": "Raw", "measurand": s["measurand"],
                        "location": s.get("location", "Outlet")}
                if unit in UNITS_16:
                    item["unit"] = unit
                if s.get("phase"):
                    item["phase"] = s["phase"]
                sampled.append(item)
            out.append({"timestamp": mv["timestamp"], "sampled_value": sampled})
        return out

    @staticmethod
    def register_wh(meter_value: list | None) -> int | None:
        for mv in meter_value or []:
            for s in mv.get("sampled_value", []):
                if s.get("measurand", "Energy.Active.Import.Register") == "Energy.Active.Import.Register":
                    try:
                        return int(round(float(s["value"])))
                    except (TypeError, ValueError):
                        return None
        return None

    def out16(self, action: str, f: dict):
        """Translate a message the station builds in its internal form into (1.6 action, 1.6 fields), or None when 1.6 has no such message."""
        if action == "Heartbeat":
            return "Heartbeat", {}
        if action == "DataTransfer":
            return "DataTransfer", {k: f[k] for k in ("vendor_id", "message_id", "data") if k in f}
        if action == "BootNotification":
            cs = f.get("charging_station", {})
            modem = cs.get("modem") or {}
            out = {"charge_point_vendor": cs.get("vendor_name"), "charge_point_model": cs.get("model"), "firmware_version": cs.get("firmware_version")}
            for src, dst in (("serial_number", "charge_point_serial_number"),):
                if cs.get(src):
                    out[dst] = cs[src]
            for key in ("iccid", "imsi"):
                if modem.get(key):
                    out[key] = modem[key]
            for value, dst in ((self.cfg.charge_box_serial_number, "charge_box_serial_number"), (self.cfg.meter_type, "meter_type"),
                               (self.cfg.meter_serial_number, "meter_serial_number")):
                if value:
                    out[dst] = value[:25]
            return "BootNotification", out
        if action == "Authorize":
            return "Authorize", {"id_tag": f["id_token"]["id_token"][:20]}
        if action == "StatusNotification":
            evse, conn = self.evse(f["evse_id"]), self.connector(f["evse_id"], f["connector_id"])
            status = f["connector_status"]
            return "StatusNotification", {"connector_id": self.cid16(evse, conn), "error_code": self.error_code16(evse) if status == "Faulted" else "NoError",
                                          "status": status, "timestamp": f["timestamp"]}
        if action == "MeterValues":
            evse = self.evse(f["evse_id"])
            out = {"connector_id": self.cid16(evse, self.active_connector(evse)), "meter_value": self.mv16(f.get("meter_value"))}
            if evse.tx is not None and evse.tx.id in self.tx16:
                out["transaction_id"] = self.tx16[evse.tx.id]
            return "MeterValues", out
        if action == "FirmwareStatusNotification":
            status = FIRMWARE_STATUS_16.get(f["status"], f["status"])
            return "FirmwareStatusNotification", {"status": status if status in FIRMWARE_STATUSES else "Idle"}
        if action == "LogStatusNotification":
            return "DiagnosticsStatusNotification", {"status": DIAGNOSTICS_STATUS_16.get(f["status"], "Idle")}
        return None

    async def send_tx16(self, payload: dict):
        """Deliver one internal transaction event as the matching OCPP 1.6 message(s)."""
        from ocpp_simulator.station import Result

        event, info = payload["event_type"], payload["transaction_info"]
        tid = info["transaction_id"]
        meter = payload.get("meter_value")
        evse_id, conn_id = payload["evse"]["id"], payload["evse"]["connector_id"]
        cid = self.cid16(self.evse(evse_id), self.connector(evse_id, conn_id))
        if event == "Started":
            token = (payload.get("id_token") or {}).get("id_token") or self.evse(evse_id).auth and self.evse(evse_id).auth["id_token"] or self.cfg.ocpp_id
            start = self.register_wh(meter)
            fields = {"connector_id": cid, "id_tag": token[:20], "meter_start": start if start is not None else int(self.evse(evse_id).register_wh),
                      "timestamp": payload["timestamp"]}
            if payload.get("reservation_id") is not None:
                fields["reservation_id"] = payload["reservation_id"]
            res = await self.request_native("StartTransaction", **fields)
            if res.ok and (res.response or {}).get("transactionId") is not None:
                self.tx16[tid] = int(res.response["transactionId"])
                status = ((res.response or {}).get("idTagInfo") or {}).get("status", "Accepted")
                if status != "Accepted":
                    self.note(f"CSMS answered StartTransaction with {status}: stopping the session")
                    self.spawn(self._deauthorize16(tid))
            return res
        if tid not in self.tx16:
            self.note("Transaction event dropped: the CSMS never gave this transaction an id")
            return Result(sent=True, ok=True)
        if event == "Updated":
            if not meter:
                return Result(sent=True, ok=True)  # a pure charging-state change is reported through StatusNotification
            return await self.request_native("MeterValues", connector_id=cid, transaction_id=self.tx16[tid], meter_value=self.mv16(meter))
        reason = info.get("stopped_reason") or "Other"
        reason = STOP_ALIASES.get(reason, reason)
        fields = {"transaction_id": self.tx16[tid], "meter_stop": self.register_wh(meter) if self.register_wh(meter) is not None else int(self.evse(evse_id).register_wh),
                  "timestamp": payload["timestamp"], "reason": reason if reason in STOP_REASONS_16 else "Other"}
        if (payload.get("id_token") or {}).get("id_token"):
            fields["id_tag"] = payload["id_token"]["id_token"][:20]
        if meter:
            fields["transaction_data"] = self.mv16(meter)
        res = await self.request_native("StopTransaction", **fields)
        if res.sent and res.error != "timeout":
            self.tx16.pop(tid, None)
        return res

    async def _deauthorize16(self, tid: str) -> None:
        for evse in self.evses:
            if evse.tx is not None and evse.tx.id == tid:
                await self.stop_session(evse.id, "DeAuthorized", "Deauthorized")

    # -------------------------------------------------------------------------------------- configuration
    def config_items(self) -> list[dict]:
        items = []
        for key, writable, kind, source in CONFIG_KEYS:
            items.append({"key": key, "readonly": not writable, "value": self.config_value(key), "kind": kind})
        return items

    def config_value(self, key: str) -> str | None:
        _, _, kind, source = CONFIG_BY_KEY[key]
        if source[0] == "model":
            var = self.model.find(source[1], source[2])
            if var is None or var.value is None:
                return "false" if kind == "bool" else "0"
            return _bool_text(var.value) if kind == "bool" else str(var.value)
        if source[0] == "dynamic":
            return str(len(self.connectors16()))
        if source[0] == "store":
            return self.config_store.get(key, source[1])
        return source[1]

    def change_configuration(self, key: str, value: str) -> str:
        if key not in CONFIG_BY_KEY:
            return "NotSupported"
        _, writable, kind, source = CONFIG_BY_KEY[key]
        if not writable:
            return "Rejected"
        if kind == "int":
            try:
                int(value)
            except (TypeError, ValueError):
                return "Rejected"
        elif kind == "bool":
            if str(value).lower() not in ("true", "false"):
                return "Rejected"
            value = value.lower()
        if source[0] == "model":
            status, _ = self.model.write({"name": source[1]}, {"name": source[2]}, str(value))
            if status not in ("Accepted", "RebootRequired"):
                return "Rejected"
        else:
            self.config_store[key] = str(value)
        self.note(f"Configuration {key} = {value}")
        self.changed()
        return "Accepted"

    # ------------------------------------------------------------------------------- charging profiles
    def profile_from16(self, p: dict) -> dict:
        sched = p["charging_schedule"]
        out = {"id": p["charging_profile_id"], "stack_level": p["stack_level"],
               "charging_profile_purpose": PURPOSE_16.get(p["charging_profile_purpose"], p["charging_profile_purpose"]),
               "charging_profile_kind": p["charging_profile_kind"],
               "charging_schedule": [{"id": 1, **{k: sched[k] for k in ("start_schedule", "duration", "charging_rate_unit", "charging_schedule_period", "min_charging_rate") if k in sched}}]}
        for key in ("recurrency_kind", "valid_from", "valid_to"):
            if p.get(key) is not None:
                out[key] = p[key]
        if p.get("transaction_id") is not None:
            out["transaction_id"] = next((uuid_ for uuid_, n in self.tx16.items() if n == p["transaction_id"]), str(p["transaction_id"]))
        return out

    # ----------------------------------------------------------------------------------------- commands
    async def run_command16(self, name: str, kwargs: dict) -> dict:
        return await getattr(self, f"cmd16_{snake(name)}")(**kwargs)

    async def cmd16_reset(self, type: str) -> dict:
        if type not in ("Hard", "Soft"):
            return {"status": "Rejected"}
        self.spawn(self.reboot("RemoteReset", "HardReset" if type == "Hard" else "SoftReset"))
        return {"status": "Accepted"}

    async def cmd16_remote_start_transaction(self, id_tag: str, connector_id: int | None = None, charging_profile: dict | None = None) -> dict:
        evse_id = None
        if connector_id:
            target = self.resolve16(connector_id)
            if target is None:
                return {"status": "Rejected"}
            evse_id = target[0].id
        profile = None
        if charging_profile:
            profile = {**self.profile_from16(charging_profile), "charging_profile_purpose": "TxProfile"}
            profile.pop("transaction_id", None)
        res = await self.cmd_request_start_transaction({"id_token": id_tag, "type": "Central"}, random.randint(1, 2**31 - 1), evse_id=evse_id, charging_profile=profile)
        return {"status": "Accepted" if res["status"] == "Accepted" else "Rejected"}

    async def cmd16_remote_stop_transaction(self, transaction_id: int) -> dict:
        for evse in self.evses:
            if evse.tx is not None and self.tx16.get(evse.tx.id) == transaction_id:
                self.spawn(self.stop_session(evse.id, "Remote", "RemoteStop"))
                return {"status": "Accepted"}
        return {"status": "Rejected"}

    async def cmd16_unlock_connector(self, connector_id: int) -> dict:
        target = self.resolve16(connector_id)
        if target is None:
            return {"status": "NotSupported"}
        res = await self.cmd_unlock_connector(target[0].id, target[1].id)
        return {"status": "Unlocked" if res["status"] == "Unlocked" else "UnlockFailed"}

    async def cmd16_change_availability(self, connector_id: int, type: str) -> dict:
        evse = None
        if connector_id:
            target = self.resolve16(connector_id)
            if target is None:
                return {"status": "Rejected"}
            evse = {"id": target[0].id, "connector_id": target[1].id}
        res = await self.cmd_change_availability(type, evse)
        return {"status": res["status"] if res["status"] in ("Accepted", "Scheduled") else "Rejected"}

    async def cmd16_change_configuration(self, key: str, value: str) -> dict:
        return {"status": self.change_configuration(key, value)}

    async def cmd16_get_configuration(self, key: list | None = None) -> dict:
        wanted = key or [k for k, *_ in CONFIG_KEYS]
        known = [i for i in self.config_items() if i["key"] in wanted]
        return {"configuration_key": [{"key": i["key"], "readonly": i["readonly"], "value": i["value"]} for i in known],
                "unknown_key": [k for k in wanted if k not in CONFIG_BY_KEY]}

    async def cmd16_clear_cache(self) -> dict:
        res = await self.cmd_clear_cache()
        return {"status": "Accepted" if res.get("status") == "Accepted" else "Rejected"}

    async def cmd16_get_local_list_version(self) -> dict:
        if not self.model.get_bool("LocalAuthListCtrlr", "Enabled", True):
            return {"list_version": -1}
        return {"list_version": self.local_list["version"]}

    async def cmd16_send_local_list(self, list_version: int, update_type: str, local_authorization_list: list | None = None) -> dict:
        if not self.model.get_bool("LocalAuthListCtrlr", "Enabled", True):
            return {"status": "NotSupported"}
        if update_type == "Differential" and list_version <= self.local_list["version"]:
            return {"status": "VersionMismatch"}
        entries = {} if update_type == "Full" else self.local_list["entries"]
        for item in local_authorization_list or []:
            if item.get("id_tag_info"):
                entries[item["id_tag"]] = {**item["id_tag_info"]}
            else:
                entries.pop(item["id_tag"], None)
        if len(entries) > self.model.get_int("LocalAuthListCtrlr", "Storage", 1000):
            return {"status": "Failed"}
        self.local_list = {"version": list_version, "entries": entries}
        var = self.model.find("LocalAuthListCtrlr", "Entries")
        if var is not None:
            var.value = str(len(entries))
        return {"status": "Accepted"}

    async def cmd16_reserve_now(self, connector_id: int, expiry_date: str, id_tag: str, reservation_id: int, parent_id_tag: str | None = None) -> dict:
        evse_id = None
        if connector_id:
            target = self.resolve16(connector_id)
            if target is None:
                return {"status": "Rejected"}
            evse_id = target[0].id
        res = await self.cmd_reserve_now(reservation_id, expiry_date, {"id_token": id_tag, "type": "Central"}, evse_id=evse_id)
        return {"status": res["status"] if res["status"] in ("Accepted", "Faulted", "Occupied", "Rejected", "Unavailable") else "Rejected"}

    async def cmd16_cancel_reservation(self, reservation_id: int) -> dict:
        return {"status": (await self.cmd_cancel_reservation(reservation_id))["status"]}

    async def cmd16_set_charging_profile(self, connector_id: int, cs_charging_profiles: dict) -> dict:
        if connector_id and self.resolve16(connector_id) is None:
            return {"status": "Rejected"}
        evse_id = self.resolve16(connector_id)[0].id if connector_id else 0
        res = await self.cmd_set_charging_profile(evse_id, self.profile_from16(cs_charging_profiles))
        return {"status": "Accepted" if res["status"] == "Accepted" else "Rejected"}

    async def cmd16_clear_charging_profile(self, id: int | None = None, connector_id: int | None = None, charging_profile_purpose: str | None = None,
                                           stack_level: int | None = None) -> dict:
        criteria = {}
        if connector_id is not None:
            target = self.resolve16(connector_id) if connector_id else None
            criteria["evse_id"] = target[0].id if target else 0
        if charging_profile_purpose:
            criteria["charging_profile_purpose"] = PURPOSE_16.get(charging_profile_purpose, charging_profile_purpose)
        if stack_level is not None:
            criteria["stack_level"] = stack_level
        res = await self.cmd_clear_charging_profile(id, criteria)
        return {"status": "Accepted" if res["status"] == "Accepted" else "Unknown"}

    async def cmd16_get_composite_schedule(self, connector_id: int, duration: int, charging_rate_unit: str | None = None) -> dict:
        evse_id = 0
        if connector_id:
            target = self.resolve16(connector_id)
            if target is None:
                return {"status": "Rejected"}
            evse_id = target[0].id
        res = await self.cmd_get_composite_schedule(duration, evse_id, charging_rate_unit)
        if res["status"] != "Accepted":
            return {"status": "Rejected"}
        s = res["schedule"]
        return {"status": "Accepted", "connector_id": connector_id, "schedule_start": s["schedule_start"],
                "charging_schedule": {"duration": s["duration"], "start_schedule": s["schedule_start"], "charging_rate_unit": s["charging_rate_unit"],
                                      "charging_schedule_period": s["charging_schedule_period"]}}

    async def cmd16_trigger_message(self, requested_message: str, connector_id: int | None = None) -> dict:
        if requested_message not in TRIGGERS_16:
            return {"status": "NotImplemented"}
        if connector_id and self.resolve16(connector_id) is None:
            return {"status": "Rejected"}
        self.spawn(self._send_triggered16(requested_message, connector_id))
        return {"status": "Accepted"}

    async def _send_triggered16(self, message: str, connector_id: int | None) -> None:
        targets = [t for t in self.connectors16() if connector_id in (None, 0) or t[0] == connector_id]
        if message == "BootNotification":
            self.boot_required = True
            await self._boot()
        elif message == "Heartbeat":
            await self.request("Heartbeat")
        elif message == "StatusNotification":
            if connector_id in (None, 0):
                await self.send_status0_16()
            for _, evse, conn in targets:
                conn.status = self.compute_status(evse, conn)
                await self.send_status(evse, conn)
        elif message == "MeterValues":
            for _, evse, conn in targets:
                if evse.plugged is not None and evse.plugged is not conn:
                    continue
                meter = self.meter_values(evse, evse.tx, "SampledDataCtrlr", "TxUpdatedMeasurands", "Trigger")
                if meter:
                    await self.request("MeterValues", evse_id=evse.id, meter_value=meter)
        elif message == "FirmwareStatusNotification":
            await self.request("FirmwareStatusNotification", status=self.firmware["status"] if self.firmware["status"] != "Installed" else "Idle")
        elif message == "DiagnosticsStatusNotification":
            await self.request("LogStatusNotification", status=self.log_upload["status"] if self.log_upload["status"] != "Uploaded" else "Idle")

    async def cmd16_get_diagnostics(self, location: str, retries: int | None = None, retry_interval: int | None = None,
                                    start_time: str | None = None, stop_time: str | None = None) -> dict:
        self.spawn(self._diagnostics_flow16())
        return {"file_name": f"{self.cfg.ocpp_id}-diagnostics-{time.strftime('%Y%m%d%H%M%S')}.zip"}

    async def _diagnostics_flow16(self) -> None:
        self.log_upload["status"] = "Uploading"
        await self.request("LogStatusNotification", status="Uploading")
        await asyncio.sleep(2)
        failed = self.fw_failure == "log"
        self.log_upload["status"] = "UploadFailed" if failed else "Uploaded"
        await self.request("LogStatusNotification", status=self.log_upload["status"])

    async def cmd16_update_firmware(self, location: str, retrieve_date: str, retries: int | None = None, retry_interval: int | None = None) -> dict:
        if self.fw_task is not None and not self.fw_task.done():
            self.fw_task.cancel()
        self.fw_task = self.spawn(self._firmware_flow16(retrieve_date))
        return {}

    async def _fw16(self, status: str) -> None:
        self.firmware["status"] = status
        await self.request("FirmwareStatusNotification", status=status)

    async def _firmware_flow16(self, retrieve_date: str) -> None:
        wait = (smartcharging.parse_dt(retrieve_date) - self.now()).total_seconds()
        if wait > 0:
            await asyncio.sleep(min(wait, 300))
        await self._fw16("Downloading")
        await asyncio.sleep(2)
        if self.fw_failure in ("download", "signature"):
            await self._fw16("DownloadFailed")
            return
        await self._fw16("Downloaded")
        await asyncio.sleep(1)
        await self._fw16("Installing")
        await asyncio.sleep(2)
        if self.fw_failure == "install":
            await self._fw16("InstallationFailed")
            return
        await self._fw16("Installed")

    async def cmd16_data_transfer(self, vendor_id: str, message_id: str | None = None, data: str | None = None) -> dict:
        res = await self.cmd_data_transfer(vendor_id, message_id, data)
        return {"status": res["status"] if res["status"] in ("Accepted", "Rejected", "UnknownMessageId", "UnknownVendorId") else "Rejected",
                **({"data": str(res["data"])} if res.get("data") is not None else {})}


class Link16(RuleRouting, BaseChargePoint16):
    """OCPP 1.6 endpoint."""

    reject_payloads = REJECT_PAYLOADS_16


def _make_handler16(name: str):
    async def handler(self, **kwargs):
        result = await self.sim.run_command16(name, kwargs)
        return getattr(result16, name)(**result)

    handler.__name__ = f"on_{snake(name)}"
    return on(Action16(name))(handler)


for _name in COMMAND_NAMES_16:
    setattr(Link16, f"on_{snake(_name)}", _make_handler16(_name))

__all__ = ["V16Mixin", "Link16", "CP_ACTIONS_16", "COMMAND_NAMES_16"]
