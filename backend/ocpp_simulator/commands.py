"""CSMS -> station commands: all 40 of OCPP 2.0.1 (DataTransfer is bidirectional).

Each `cmd_<snake_case>` receives the payload as snake_case keyword arguments and returns the
snake_case fields of the result. Anything that must follow the response (reports, status flows,
reboots) is started with `self.spawn(...)`: a handler may never await its own request, because the
same loop reads the CSMS's answer.
"""
import asyncio
import hashlib
import re
import time
from datetime import timedelta

from ocpp_simulator import smartcharging
from ocpp_simulator.link import snake

SUPPORTED_TRIGGERS = {"BootNotification", "LogStatusNotification", "FirmwareStatusNotification", "Heartbeat", "MeterValues",
                      "SignChargingStationCertificate", "StatusNotification", "TransactionEvent", "PublishFirmwareStatusNotification"}
MAX_CERTS = 10


class CommandsMixin:
    def init_commands(self) -> None:
        self.display_messages: dict[int, dict] = {}
        self.monitors: dict[int, dict] = {}
        self._monitor_ids = 0
        self.monitor_last: dict[int, float] = {}
        self.certs: list[dict] = []
        self.network_profiles: dict[int, dict] = {}
        self.firmware = {"status": "Idle", "request_id": None, "version_pending": None, "published": {}}
        self.log_upload = {"status": "Idle", "request_id": None}
        self.pending_csr_key = None
        self.fw_task = None
        self.log_task = None
        self.fw_failure: str | None = None

    # ------------------------------------------------------------------------------------ dispatch
    async def run_command(self, name: str, kwargs: dict) -> dict:
        fn = getattr(self, f"cmd_{snake(name)}")
        self.stats["received"] += 0  # counted at frame level
        return await fn(**kwargs)

    async def after_boot_hook(self) -> None:
        """Called once the station is accepted by the CSMS: finish what a reboot interrupted."""
        if self.firmware.get("version_pending_done"):
            self.firmware["version_pending_done"] = False
            self.firmware["status"] = "Installed"
            await self.request("FirmwareStatusNotification", status="Installed", request_id=self.firmware["request_id"] or 0)

    # ---------------------------------------------------------------------------------- reset / power
    async def reboot(self, boot_reason: str, tx_reason: str = "ImmediateReset", pause: float = 2.0) -> None:
        self.note(f"Rebooting ({boot_reason})")
        self.state = "rebooting"
        self.changed()
        await self.end_all(tx_reason, "ResetCommand")
        await self.request_flush_pause()
        self.boot_required = True
        self.next_boot_reason = boot_reason
        for evse in self.evses:
            for conn in evse.connectors:
                conn.status = None
        await self._drop_connection(graceful=True)
        await asyncio.sleep(pause)

    async def request_flush_pause(self) -> None:
        await asyncio.sleep(0.2)

    async def cmd_reset(self, type: str, evse_id: int | None = None) -> dict:
        if evse_id is not None:
            if not 1 <= evse_id <= len(self.evses):
                return {"status": "Rejected", "status_info": {"reason_code": "UnknownEvse"}}
            evse = self.evse(evse_id)
            if type == "OnIdle" and evse.tx is not None:
                self.spawn(self._evse_reset_when_idle(evse))
                return {"status": "Scheduled"}
            self.spawn(self._evse_reset(evse))
            return {"status": "Accepted"}
        if type == "OnIdle" and any(e.tx for e in self.evses):
            self.spawn(self._reset_when_idle())
            return {"status": "Scheduled"}
        self.spawn(self.reboot("RemoteReset", "ImmediateReset"))
        return {"status": "Accepted"}

    async def _reset_when_idle(self) -> None:
        while any(e.tx for e in self.evses):
            await asyncio.sleep(1)
        await self.reboot("RemoteReset", "ImmediateReset")

    async def _evse_reset_when_idle(self, evse) -> None:
        while evse.tx:
            await asyncio.sleep(1)
        await self._evse_reset(evse)

    async def _evse_reset(self, evse) -> None:
        async with evse.lock:
            if evse.tx is not None:
                await self._end_tx(evse, "ImmediateReset", "ResetCommand")
            evse.operative = False
            await self.sync_status()
        await asyncio.sleep(2)
        evse.operative = True
        await self.sync_status()

    # ------------------------------------------------------------------------------------ availability
    async def cmd_change_availability(self, operational_status: str, evse: dict | None = None) -> dict:
        operative = operational_status == "Operative"
        targets = self.evses
        conn_id = None
        if evse:
            if not 1 <= evse["id"] <= len(self.evses):
                return {"status": "Rejected", "status_info": {"reason_code": "UnknownEvse"}}
            targets = [self.evse(evse["id"])]
            conn_id = evse.get("connector_id")
            if conn_id is not None:
                try:
                    self.connector(evse["id"], conn_id)
                except ValueError:
                    return {"status": "Rejected", "status_info": {"reason_code": "UnknownConnector"}}
        busy = [e for e in targets if e.tx is not None] if not operative else []
        if busy:
            self.spawn(self._apply_availability(evse["id"] if evse else None, conn_id, operative, wait=True))
            return {"status": "Scheduled"}
        await self._apply_availability(evse["id"] if evse else None, conn_id, operative, wait=False)
        return {"status": "Accepted"}

    async def _apply_availability(self, evse_id, conn_id, operative: bool, wait: bool) -> None:
        targets = [self.evse(evse_id)] if evse_id else self.evses
        while wait and any(e.tx for e in targets):
            await asyncio.sleep(1)
        if evse_id is None:
            self.operative = operative
        elif conn_id is None:
            targets[0].operative = operative
        else:
            self.connector(evse_id, conn_id).operative = operative
        self.note(f"Availability set to {'Operative' if operative else 'Inoperative'} (EVSE {evse_id or 'all'})")
        await self.sync_status()

    # ----------------------------------------------------------------------------------- transactions
    async def cmd_request_start_transaction(self, id_token: dict, remote_start_id: int, evse_id: int | None = None,
                                            group_id_token: dict | None = None, charging_profile: dict | None = None) -> dict:
        if evse_id is None:
            free = [e for e in self.evses if e.tx is None and self.evse_ready(e)]
            plugged = [e for e in free if e.plugged is not None]
            chosen = (plugged or free or [None])[0]
            if chosen is None:
                return {"status": "Rejected", "status_info": {"reason_code": "NoEvseAvailable"}}
            evse_id = chosen.id
        if not 1 <= evse_id <= len(self.evses):
            return {"status": "Rejected", "status_info": {"reason_code": "UnknownEvse"}}
        evse = self.evse(evse_id)
        if evse.tx is not None:
            return {"status": "Rejected", "transaction_id": evse.tx.id, "status_info": {"reason_code": "TxInProgress"}}
        if not self.evse_ready(evse):
            return {"status": "Rejected", "status_info": {"reason_code": "Unavailable"}}
        res = self.reservation_for(evse)
        if res is not None and res["id_token"]["id_token"] != id_token["id_token"]:
            return {"status": "Rejected", "status_info": {"reason_code": "Reserved"}}
        profiles = []
        if charging_profile:
            if charging_profile.get("charging_profile_purpose") != "TxProfile":
                return {"status": "Rejected", "status_info": {"reason_code": "InvalidProfile", "additional_info": "Only a TxProfile may accompany a remote start"}}
            ok, code, info = smartcharging.validate_profile({**charging_profile, "transaction_id": "pending"}, evse_id, len(self.evses), {evse_id: "pending"})
            if not ok:
                return {"status": "Rejected", "status_info": {"reason_code": code, "additional_info": info}}
            charging_profile = {k: v for k, v in charging_profile.items() if k != "transaction_id"}
            profiles = [{**charging_profile, "_evse_id": evse_id}]
        # OCPP 2.0.1 F01: answer first; with AuthorizeRemoteStart the token is checked afterwards and a refusal simply
        # means the transaction never starts. (A handler must never await its own request: the loop that would read the
        # CSMS's answer is the one running this handler.)
        authorize_first = self.model.get_bool("AuthCtrlr", "AuthorizeRemoteStart", False)
        evse.next_tx_id = evse.next_tx_id or str(__import__("uuid").uuid4())
        timeout = self.model.get_int("TxCtrlr", "EVConnectionTimeOut", 60)
        evse.pending_remote = {"id_token": id_token, "remote_start_id": remote_start_id, "profiles": profiles, "expires": time.time() + timeout,
                               "authorized": not authorize_first}
        if authorize_first:
            self.spawn(self._authorize_remote(evse, id_token))
        else:
            evse.auth = id_token
            evse.auth_at = time.time()
            if evse.plugged is not None:
                self.spawn(self._remote_start_now(evse))
            else:
                self.note(f"EVSE {evse_id}: remote start accepted, waiting up to {timeout}s for a vehicle")
        return {"status": "Accepted", "transaction_id": evse.next_tx_id}

    async def _authorize_remote(self, evse, id_token: dict) -> None:
        status, source = await self.authorize(id_token["id_token"], id_token.get("type", "Central"))
        pending = evse.pending_remote
        if pending is None or pending["id_token"] is not id_token:
            return
        if status != "Accepted":
            self.note(f"EVSE {evse.id}: remote start refused, token {id_token['id_token']} → {status} ({source})")
            evse.pending_remote = None
            evse.next_tx_id = None
            return
        pending["authorized"] = True
        evse.auth = id_token
        evse.auth_at = time.time()
        if evse.plugged is not None:
            await self._remote_start_now(evse)

    def evse_ready(self, evse) -> bool:
        return self.operative and evse.operative and not self.evse_faulted(evse)

    async def _remote_start_now(self, evse) -> None:
        async with evse.lock:
            if evse.tx is not None or evse.pending_remote is None:
                return
            if "Authorized" in self.start_points() or "EVConnected" in self.start_points():
                await self._begin_tx(evse, "RemoteStart")
            else:
                await self._reconcile(evse)
            await self.sync_status()

    async def cmd_request_stop_transaction(self, transaction_id: str) -> dict:
        for evse in self.evses:
            if evse.tx is not None and evse.tx.id == transaction_id:
                self.spawn(self.stop_session(evse.id, "Remote", "RemoteStop"))
                return {"status": "Accepted"}
        return {"status": "Rejected", "status_info": {"reason_code": "TxNotFound"}}

    async def cmd_get_transaction_status(self, transaction_id: str | None = None) -> dict:
        if transaction_id is None:
            return {"messages_in_queue": bool(self.tx_queue)}
        ongoing = any(e.tx is not None and e.tx.id == transaction_id for e in self.evses)
        queued = any(q["transaction_info"]["transaction_id"] == transaction_id for q in self.tx_queue)
        return {"messages_in_queue": queued, "ongoing_indicator": ongoing}

    async def cmd_cost_updated(self, total_cost: float, transaction_id: str) -> dict:
        for evse in self.evses:
            if evse.tx is not None and evse.tx.id == transaction_id:
                evse.tx.total_cost = total_cost
                self.note(f"Running cost updated: {total_cost}")
        return {}

    async def cmd_unlock_connector(self, evse_id: int, connector_id: int) -> dict:
        try:
            self.connector(evse_id, connector_id)
        except ValueError:
            return {"status": "UnknownConnector"}
        evse = self.evse(evse_id)
        if evse.tx is not None:
            return {"status": "OngoingAuthorizedTransaction"}
        if self.lock_failed(evse):
            return {"status": "UnlockFailed", "status_info": {"reason_code": "ConnectorLockFailure"}}
        evse.locked = False
        return {"status": "Unlocked"}

    # ----------------------------------------------------------------------------------- reservations
    async def cmd_reserve_now(self, id: int, expiry_date_time: str, id_token: dict, evse_id: int | None = None,
                              connector_type: str | None = None, group_id_token: dict | None = None) -> dict:
        if not self.model.get_bool("ReservationCtrlr", "Enabled", True):
            return {"status": "Rejected"}
        expiry = smartcharging.parse_dt(expiry_date_time)
        if expiry <= self.now():
            return {"status": "Rejected", "status_info": {"reason_code": "ExpiryInPast"}}
        if evse_id is None:
            if not self.model.get_bool("ReservationCtrlr", "NonEvseSpecific", False):
                return {"status": "Rejected", "status_info": {"reason_code": "NonEvseSpecificNotSupported"}}
        else:
            if not 1 <= evse_id <= len(self.evses):
                return {"status": "Rejected", "status_info": {"reason_code": "UnknownEvse"}}
            evse = self.evse(evse_id)
            if self.evse_faulted(evse):
                return {"status": "Faulted"}
            if not (self.operative and evse.operative):
                return {"status": "Unavailable"}
            other = self.reservation_for(evse)
            if evse.tx is not None or evse.plugged is not None or (other is not None and other["id"] != id):
                return {"status": "Occupied"}
        self.reservations[id] = {"id": id, "evse_id": evse_id, "id_token": id_token, "expiry": expiry, "connector_type": connector_type}
        self.spawn(self.sync_status())
        return {"status": "Accepted"}

    async def cmd_cancel_reservation(self, reservation_id: int) -> dict:
        if self.reservations.pop(reservation_id, None) is None:
            return {"status": "Rejected"}
        self.spawn(self.sync_status())
        return {"status": "Accepted"}

    # ------------------------------------------------------------------------------- smart charging
    def _tx_ids(self) -> dict[int, str]:
        return {e.id: e.tx.id for e in self.evses if e.tx is not None}

    async def cmd_set_charging_profile(self, evse_id: int, charging_profile: dict) -> dict:
        if not self.model.get_bool("SmartChargingCtrlr", "Enabled", True):
            return {"status": "Rejected", "status_info": {"reason_code": "NotSupported"}}
        ok, code, info = smartcharging.validate_profile(charging_profile, evse_id, len(self.evses), self._tx_ids())
        if not ok:
            return {"status": "Rejected", "status_info": {"reason_code": code, "additional_info": info}}
        stored = {**charging_profile, "_evse_id": evse_id}
        # same id replaces; same purpose + stack level on the same EVSE is replaced as well
        self.profiles = [p for p in self.profiles if p["id"] != stored["id"] and not (
            p["_evse_id"] == evse_id and p["charging_profile_purpose"] == stored["charging_profile_purpose"] and p["stack_level"] == stored["stack_level"])]
        if len(self.profiles) >= self.model.get_int("SmartChargingCtrlr", "EntriesChargingProfiles", 100):
            return {"status": "Rejected", "status_info": {"reason_code": "TooManyProfiles"}}
        self.profiles.append(stored)
        self.note(f"Charging profile {stored['id']} installed on EVSE {evse_id} ({stored['charging_profile_purpose']})")
        self.spawn(self._reconcile_all())
        return {"status": "Accepted"}

    async def _reconcile_all(self) -> None:
        for evse in self.evses:
            await self.reconcile(evse, "ChargingRateChanged")

    async def cmd_clear_charging_profile(self, charging_profile_id: int | None = None, charging_profile_criteria: dict | None = None) -> dict:
        crit = charging_profile_criteria or {}

        def hit(p: dict) -> bool:
            if charging_profile_id is not None and p["id"] != charging_profile_id:
                return False
            if crit.get("evse_id") is not None and p["_evse_id"] != crit["evse_id"]:
                return False
            if crit.get("charging_profile_purpose") and p["charging_profile_purpose"] != crit["charging_profile_purpose"]:
                return False
            return crit.get("stack_level") is None or p["stack_level"] == crit["stack_level"]

        removed = [p for p in self.profiles if hit(p)]
        if not removed:
            return {"status": "Unknown"}
        self.profiles = [p for p in self.profiles if p not in removed]
        self.spawn(self._reconcile_all())
        return {"status": "Accepted"}

    async def cmd_get_charging_profiles(self, request_id: int, charging_profile: dict, evse_id: int | None = None) -> dict:
        crit = charging_profile or {}
        found = [p for p in self.profiles
                 if (evse_id is None or p["_evse_id"] == evse_id)
                 and (not crit.get("charging_profile_purpose") or p["charging_profile_purpose"] == crit["charging_profile_purpose"])
                 and (crit.get("stack_level") is None or p["stack_level"] == crit["stack_level"])
                 and (not crit.get("charging_profile_id") or p["id"] in crit["charging_profile_id"])]
        if not found:
            return {"status": "NoProfiles"}
        self.spawn(self._report_profiles(request_id, found, crit.get("charging_limit_source")))
        return {"status": "Accepted"}

    async def _report_profiles(self, request_id: int, found: list[dict], source: str | None) -> None:
        by_evse: dict[int, list[dict]] = {}
        for p in found:
            by_evse.setdefault(p["_evse_id"], []).append({k: v for k, v in p.items() if not k.startswith("_")})
        for eid, profiles in by_evse.items():
            await self.request("ReportChargingProfiles", request_id=request_id, charging_limit_source=source or "CSO", evse_id=eid, charging_profile=profiles)

    async def cmd_get_composite_schedule(self, duration: int, evse_id: int, charging_rate_unit: str | None = None) -> dict:
        if not 0 <= evse_id <= len(self.evses):
            return {"status": "Rejected", "status_info": {"reason_code": "UnknownEvse"}}
        ref = self.evses[max(evse_id, 1) - 1]
        phases = ref.spec.phases if ref.spec.current_type == "AC" else 1
        tx = ref.tx if evse_id else None
        schedule = smartcharging.composite_schedule(
            self.profiles, evse_id, self.now(), duration, charging_rate_unit or "W", voltage=ref.spec.voltage, phases=phases,
            station_max_w=ref.spec.max_power_kw * 1000 * (len(self.evses) if evse_id == 0 else 1), tx_id=tx.id if tx else None, tx_start=tx.started_at if tx else None)
        return {"status": "Accepted", "schedule": schedule}

    # ------------------------------------------------------------------------------------ device model
    async def cmd_get_variables(self, get_variable_data: list) -> dict:
        results = []
        for item in get_variable_data:
            status, value, _ = self.model.read(item["component"], item["variable"], item.get("attribute_type"))
            r = {"attribute_status": status, "attribute_type": item.get("attribute_type", "Actual"), "component": item["component"], "variable": item["variable"]}
            if value is not None:
                r["attribute_value"] = value
            results.append(r)
        return {"get_variable_result": results}

    async def cmd_set_variables(self, set_variable_data: list) -> dict:
        results = []
        for item in set_variable_data:
            status, var = self.model.write(item["component"], item["variable"], item["attribute_value"], item.get("attribute_type"))
            results.append({"attribute_status": status, "attribute_type": item.get("attribute_type", "Actual"), "component": item["component"], "variable": item["variable"]})
            if status in ("Accepted", "RebootRequired") and var is not None:
                self.note(f"{var.component}.{var.name} = {item['attribute_value']}")
        self.changed()
        return {"set_variable_result": results}

    async def cmd_get_base_report(self, request_id: int, report_base: str) -> dict:
        self.spawn(self._send_report(request_id, self.model.report_items(report_base)))
        return {"status": "Accepted"}

    async def cmd_get_report(self, request_id: int, component_variable: list | None = None, component_criteria: list | None = None) -> dict:
        items = self.model.report_items("FullInventory", component_criteria, component_variable)
        if not items:
            return {"status": "EmptyResultSet"}
        self.spawn(self._send_report(request_id, items))
        return {"status": "Accepted"}

    async def _send_report(self, request_id: int, items: list[dict]) -> None:
        per = max(1, self.model.get_int("DeviceDataCtrlr", "ItemsPerMessage", 40))
        chunks = [items[i:i + per] for i in range(0, len(items), per)] or [[]]
        for seq, chunk in enumerate(chunks):
            await self.request("NotifyReport", request_id=request_id, generated_at=self.ts(), seq_no=seq, tbc=seq < len(chunks) - 1, **({"report_data": chunk} if chunk else {}))

    # ----------------------------------------------------------------------------------------- triggers
    async def cmd_trigger_message(self, requested_message: str, evse: dict | None = None) -> dict:
        if requested_message not in SUPPORTED_TRIGGERS:
            return {"status": "NotImplemented"}
        if evse and not 1 <= evse["id"] <= len(self.evses):
            return {"status": "Rejected", "status_info": {"reason_code": "UnknownEvse"}}
        if requested_message in ("SignChargingStationCertificate",) and not self.model.get_bool("SecurityCtrlr", "Enabled", True):
            return {"status": "Rejected"}
        self.spawn(self._send_triggered(requested_message, evse))
        return {"status": "Accepted"}

    async def _send_triggered(self, message: str, evse: dict | None) -> None:
        targets = [self.evse(evse["id"])] if evse else self.evses
        if message == "BootNotification":
            self.next_boot_reason = "Triggered"
            self.boot_required = True
            await self._boot()
        elif message == "Heartbeat":
            await self.request("Heartbeat")
        elif message == "StatusNotification":
            for e in targets:
                for c in e.connectors:
                    if evse is None or evse.get("connector_id") in (None, c.id):
                        c.status = self.compute_status(e, c)
                        await self.send_status(e, c)
        elif message == "MeterValues":
            for e in targets:
                mv = self.meter_values(e, e.tx, "AlignedDataCtrlr", "Measurands", "Trigger")
                if mv:
                    await self.request("MeterValues", evse_id=e.id, meter_value=mv)
        elif message == "TransactionEvent":
            for e in targets:
                if e.tx is not None:
                    mv = self.meter_values(e, e.tx, "SampledDataCtrlr", "TxUpdatedMeasurands", "Trigger")
                    async with self.tx_lock:
                        await self.deliver_tx(self._tx_payload(e.tx, "Updated", "Trigger", meter_value=mv, charging_state=e.tx.charging_state))
        elif message == "FirmwareStatusNotification":
            await self.request("FirmwareStatusNotification", status=self.firmware["status"] if self.firmware["status"] != "Installed" else "Idle",
                               **({"request_id": self.firmware["request_id"]} if self.firmware["request_id"] is not None else {}))
        elif message == "LogStatusNotification":
            await self.request("LogStatusNotification", status=self.log_upload["status"] if self.log_upload["status"] != "Uploaded" else "Idle",
                               **({"request_id": self.log_upload["request_id"]} if self.log_upload["request_id"] is not None else {}))
        elif message == "PublishFirmwareStatusNotification":
            await self.request("PublishFirmwareStatusNotification", status="Idle")
        elif message == "SignChargingStationCertificate":
            await self.sign_certificate("ChargingStationCertificate")

    # -------------------------------------------------------------------------------------- local list
    async def cmd_get_local_list_version(self) -> dict:
        return {"version_number": self.local_list["version"]}

    async def cmd_send_local_list(self, version_number: int, update_type: str, local_authorization_list: list | None = None) -> dict:
        if not self.model.get_bool("LocalAuthListCtrlr", "Enabled", True):
            return {"status": "Failed", "status_info": {"reason_code": "NotEnabled"}}
        entries = self.local_list["entries"]
        if update_type == "Differential" and version_number <= self.local_list["version"]:
            return {"status": "VersionMismatch"}
        if update_type == "Full":
            entries = {}
        storage = self.model.get_int("LocalAuthListCtrlr", "Storage", 1000)
        for item in local_authorization_list or []:
            token = item["id_token"]["id_token"]
            if item.get("id_token_info"):
                entries[token] = {**item["id_token_info"], "type": item["id_token"].get("type")}
            else:
                entries.pop(token, None)
        if len(entries) > storage:
            return {"status": "Failed", "status_info": {"reason_code": "StorageFull"}}
        self.local_list = {"version": version_number, "entries": entries}
        self.model.find("LocalAuthListCtrlr", "Entries").value = str(len(entries))
        return {"status": "Accepted"}

    async def cmd_clear_cache(self) -> dict:
        if not self.model.get_bool("AuthCacheCtrlr", "Enabled", True):
            return {"status": "Rejected"}
        self.auth_cache.clear()
        return {"status": "Accepted"}

    # ------------------------------------------------------------------------------- display messages
    async def cmd_set_display_message(self, message: dict) -> dict:
        formats = self.model.get_list("DisplayMessageCtrlr", "SupportedFormats")
        priorities = self.model.get_list("DisplayMessageCtrlr", "SupportedPriorities")
        if (message.get("message") or {}).get("format") not in formats:
            return {"status": "NotSupportedMessageFormat"}
        if message.get("priority") not in priorities:
            return {"status": "NotSupportedPriority"}
        if message.get("state") and message["state"] not in ("Charging", "Faulted", "Idle", "Unavailable"):
            return {"status": "NotSupportedState"}
        if len(self.display_messages) >= self.model.get_int("DisplayMessageCtrlr", "DisplayMessages", 10) and message["id"] not in self.display_messages:
            return {"status": "Rejected", "status_info": {"reason_code": "StorageFull"}}
        self.display_messages[message["id"]] = message
        self.note(f"Display message {message['id']}: {message['message'].get('content')}")
        return {"status": "Accepted"}

    async def cmd_get_display_messages(self, request_id: int, id: list | None = None, priority: str | None = None, state: str | None = None) -> dict:
        found = [m for m in self.display_messages.values() if (not id or m["id"] in id) and (not priority or m["priority"] == priority) and (not state or m.get("state") == state)]
        if not found:
            return {"status": "Unknown"}
        self.spawn(self._notify_display(request_id, found))
        return {"status": "Accepted"}

    async def _notify_display(self, request_id: int, found: list[dict]) -> None:
        await self.request("NotifyDisplayMessages", request_id=request_id, message_info=found, tbc=False)

    async def cmd_clear_display_message(self, id: int) -> dict:
        return {"status": "Accepted" if self.display_messages.pop(id, None) else "Unknown"}

    async def cmd_customer_information(self, request_id: int, report: bool, clear: bool, customer_certificate: dict | None = None,
                                       id_token: dict | None = None, customer_identifier: str | None = None) -> dict:
        if not report and not clear:
            return {"status": "Invalid"}
        token = (id_token or {}).get("id_token") or customer_identifier
        if token is None and customer_certificate is None:
            return {"status": "Invalid"}
        known = token in self.local_list["entries"] or token in self.auth_cache
        if report:
            data = f"Token {token}: " + ("present in the local list or cache" if known else "no stored data") if token else "Certificate lookup: no stored data"
            self.spawn(self._notify_customer(request_id, data))
        if clear and token:
            self.auth_cache.pop(token, None)
            self.local_list["entries"].pop(token, None)
        return {"status": "Accepted"}

    async def _notify_customer(self, request_id: int, data: str) -> None:
        await self.request("NotifyCustomerInformation", data=data, seq_no=0, generated_at=self.ts(), request_id=request_id, tbc=False)

    # ------------------------------------------------------------------------------------- monitoring
    async def cmd_set_monitoring_base(self, monitoring_base: str) -> dict:
        if monitoring_base == "FactoryDefault":
            self.monitors.clear()
        self.model.find("MonitoringCtrlr", "ActiveMonitoringBase").value = monitoring_base
        return {"status": "Accepted"}

    async def cmd_set_monitoring_level(self, severity: int) -> dict:
        if not 0 <= severity <= 9:
            return {"status": "Rejected"}
        self.model.find("MonitoringCtrlr", "ActiveMonitoringLevel").value = str(severity)
        return {"status": "Accepted"}

    async def cmd_set_variable_monitoring(self, set_monitoring_data: list) -> dict:
        results = []
        for item in set_monitoring_data:
            status, mid = "Accepted", item.get("id")
            var = self.model._resolve(item["component"], item["variable"])
            if var is None:
                known = any(v.component == item["component"]["name"] for v in self.model.vars.values())
                status = "UnknownVariable" if known else "UnknownComponent"
            elif not var.supports_monitoring:
                status = "Rejected"
            elif item["type"] not in ("UpperThreshold", "LowerThreshold", "Delta", "Periodic", "PeriodicClockAligned"):
                status = "UnsupportedMonitorType"
            elif any(m["component"] == item["component"] and m["variable"] == item["variable"] and m["type"] == item["type"] and m["id"] != mid and m["value"] == item["value"] for m in self.monitors.values()):
                status = "Duplicate"
            if status == "Accepted":
                if mid is None:
                    self._monitor_ids += 1
                    mid = self._monitor_ids
                self.monitors[mid] = {"id": mid, "component": item["component"], "variable": item["variable"], "type": item["type"],
                                      "value": item["value"], "severity": item["severity"], "transaction": item.get("transaction", False), "active": False}
                self.monitor_last.pop(mid, None)
            r = {"status": status, "type": item["type"], "severity": item["severity"], "component": item["component"], "variable": item["variable"]}
            if mid is not None and status == "Accepted":
                r["id"] = mid
            results.append(r)
        return {"set_monitoring_result": results}

    async def cmd_clear_variable_monitoring(self, id: list) -> dict:
        return {"clear_monitoring_result": [{"status": "Accepted" if self.monitors.pop(i, None) else "NotFound", "id": i} for i in id]}

    async def cmd_get_monitoring_report(self, request_id: int, component_variable: list | None = None, monitoring_criteria: list | None = None) -> dict:
        found = [m for m in self.monitors.values()
                 if (not component_variable or any(cv["component"]["name"] == m["component"]["name"] and (not cv.get("variable") or cv["variable"]["name"] == m["variable"]["name"]) for cv in component_variable))
                 and (not monitoring_criteria or ("ThresholdMonitoring" in monitoring_criteria and m["type"] in ("UpperThreshold", "LowerThreshold"))
                      or ("DeltaMonitoring" in monitoring_criteria and m["type"] == "Delta") or ("PeriodicMonitoring" in monitoring_criteria and m["type"].startswith("Periodic")))]
        if not found:
            return {"status": "EmptyResultSet"}
        self.spawn(self._send_monitoring_report(request_id, found))
        return {"status": "Accepted"}

    async def _send_monitoring_report(self, request_id: int, found: list[dict]) -> None:
        monitor = [{"component": m["component"], "variable": m["variable"], "variable_monitoring": [
            {"id": m["id"], "transaction": m["transaction"], "value": m["value"], "type": m["type"], "severity": m["severity"]}]} for m in found]
        await self.request("NotifyMonitoringReport", request_id=request_id, seq_no=0, generated_at=self.ts(), tbc=False, monitor=monitor)

    def monitor_value(self, comp: dict, var: dict) -> float | None:
        evse_id = (comp.get("evse") or {}).get("id") or 1
        name = (comp["name"], var["name"])
        try:
            evse = self.evse(evse_id)
        except ValueError:
            return None
        if name == ("EVSE", "Temperature"):
            return evse.temperature
        if name == ("EVSE", "Voltage"):
            return self.voltage_now(evse)
        if name == ("EVSE", "Power"):
            return evse.power_w
        v = self.model._resolve(comp, var)
        try:
            return float(v.value) if v is not None and v.value is not None else None
        except ValueError:
            return None

    def evaluate_monitors(self) -> None:
        level = self.model.get_int("MonitoringCtrlr", "ActiveMonitoringLevel", 9)
        for mid, m in list(self.monitors.items()):
            if m["severity"] > level:
                continue
            value = self.monitor_value(m["component"], m["variable"])
            if value is None:
                continue
            kind, limit = m["type"], float(m["value"])
            if kind in ("UpperThreshold", "LowerThreshold"):
                breached = value > limit if kind == "UpperThreshold" else value < limit
                if breached != m["active"]:
                    m["active"] = breached
                    self.spawn(self._monitor_event(m, value, "Alerting", cleared=not breached))
            elif kind == "Delta":
                last = self.monitor_last.setdefault(mid, value)
                if abs(value - last) >= limit:
                    self.monitor_last[mid] = value
                    self.spawn(self._monitor_event(m, value, "Delta"))
            else:
                last = self.monitor_last.setdefault(mid, time.time())
                if time.time() - last >= max(1.0, limit):
                    self.monitor_last[mid] = time.time()
                    self.spawn(self._monitor_event(m, value, "Periodic"))

    async def _monitor_event(self, m: dict, value: float, trigger: str, cleared: bool = False) -> None:
        self.event_ids += 1
        seq, self.event_seq = self.event_seq, self.event_seq + 1
        ev = {"event_id": self.event_ids, "timestamp": self.ts(), "trigger": trigger, "actual_value": f"{value:.2f}", "event_notification_type": "CustomMonitor",
              "component": m["component"], "variable": m["variable"], "variable_monitoring_id": m["id"], "cleared": cleared}
        await self.request("NotifyEvent", generated_at=self.ts(), seq_no=seq, event_data=[ev])

    # ---------------------------------------------------------------------------------- certificates
    @staticmethod
    def _hash_data(pem: str) -> dict | None:
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import serialization

            cert = x509.load_pem_x509_certificate(pem.encode())
            issuer_name = hashlib.sha256(cert.issuer.public_bytes()).hexdigest()
            key = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
            return {"hash_algorithm": "SHA256", "issuer_name_hash": issuer_name, "issuer_key_hash": hashlib.sha256(key).hexdigest(), "serial_number": format(cert.serial_number, "x")}
        except Exception:
            return None

    async def cmd_install_certificate(self, certificate_type: str, certificate: str) -> dict:
        data = self._hash_data(certificate)
        if data is None:
            return {"status": "Rejected", "status_info": {"reason_code": "InvalidCertificate"}}
        if len(self.certs) >= MAX_CERTS:
            return {"status": "Failed", "status_info": {"reason_code": "StorageFull"}}
        self.certs = [c for c in self.certs if c["hash"]["serial_number"] != data["serial_number"] or c["type"] != certificate_type]
        self.certs.append({"type": certificate_type, "pem": certificate, "hash": data})
        self.model.find("SecurityCtrlr", "CertificateEntries").value = str(len(self.certs))
        return {"status": "Accepted"}

    async def cmd_get_installed_certificate_ids(self, certificate_type: list | None = None) -> dict:
        found = [c for c in self.certs if not certificate_type or c["type"] in certificate_type]
        if not found:
            return {"status": "NotFound"}
        return {"status": "Accepted", "certificate_hash_data_chain": [{"certificate_type": c["type"], "certificate_hash_data": c["hash"]} for c in found]}

    async def cmd_delete_certificate(self, certificate_hash_data: dict) -> dict:
        for c in self.certs:
            if c["hash"]["serial_number"] == certificate_hash_data["serial_number"] and c["hash"]["issuer_name_hash"] == certificate_hash_data["issuer_name_hash"]:
                if c["type"] == "ChargingStationCertificate":
                    return {"status": "Failed"}
                self.certs.remove(c)
                self.model.find("SecurityCtrlr", "CertificateEntries").value = str(len(self.certs))
                return {"status": "Accepted"}
        return {"status": "NotFound"}

    async def sign_certificate(self, cert_type: str = "ChargingStationCertificate") -> dict:
        """Station -> CSMS SignCertificate with a real, freshly generated CSR."""
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        key = ec.generate_private_key(ec.SECP256R1())
        csr = (x509.CertificateSigningRequestBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, self.cfg.ocpp_id), x509.NameAttribute(NameOID.ORGANIZATION_NAME, self.model.get_value("SecurityCtrlr", "OrganizationName", "Simulated Operator"))]))
               .sign(key, hashes.SHA256()))
        self.pending_csr_key = key
        res = await self.request("SignCertificate", csr=csr.public_bytes(serialization.Encoding.PEM).decode(), certificate_type=cert_type)
        return {"sent": res.sent, "status": res.status, "error": res.error}

    async def cmd_certificate_signed(self, certificate_chain: str, certificate_type: str | None = None) -> dict:
        if self.pending_csr_key is None:
            return {"status": "Rejected", "status_info": {"reason_code": "NoPendingCSR"}}
        first = certificate_chain.split("-----END CERTIFICATE-----")[0] + "-----END CERTIFICATE-----\n"
        data = self._hash_data(first)
        if data is None:
            return {"status": "Rejected", "status_info": {"reason_code": "InvalidCertificate"}}
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import serialization

            cert = x509.load_pem_x509_certificate(first.encode())
            mine = self.pending_csr_key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
            theirs = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
            if mine != theirs:
                return {"status": "Rejected", "status_info": {"reason_code": "KeyMismatch"}}
        except Exception:
            return {"status": "Rejected", "status_info": {"reason_code": "InvalidCertificate"}}
        self.certs = [c for c in self.certs if c["type"] != "ChargingStationCertificate"] + [{"type": "ChargingStationCertificate", "pem": certificate_chain, "hash": data}]
        self.pending_csr_key = None
        self.model.find("SecurityCtrlr", "CertificateEntries").value = str(len(self.certs))
        return {"status": "Accepted"}

    # ---------------------------------------------------------------------------- network / vendor
    async def cmd_set_network_profile(self, configuration_slot: int, connection_data: dict) -> dict:
        url = connection_data.get("ocpp_csms_url", "")
        profile = connection_data.get("security_profile", 0)
        if connection_data.get("ocpp_version") not in ("OCPP20", "OCPP201"):
            return {"status": "Rejected", "status_info": {"reason_code": "UnsupportedVersion"}}
        if not re.match(r"^wss?://", url) or (profile >= 2 and not url.startswith("wss://")):
            return {"status": "Rejected", "status_info": {"reason_code": "InvalidUrl", "additional_info": "Security profile 2 and 3 need a wss:// URL"}}
        self.network_profiles[configuration_slot] = connection_data
        return {"status": "Accepted"}

    async def cmd_data_transfer(self, vendor_id: str, message_id: str | None = None, data=None) -> dict:
        if vendor_id == "com.simulator.echo":
            return {"status": "Accepted", "data": data}
        if vendor_id == "com.simulator.reject":
            return {"status": "Rejected"}
        if vendor_id == "com.simulator":
            return {"status": "UnknownMessageId"}
        return {"status": "UnknownVendorId"}

    # ------------------------------------------------------------------------- firmware and logs
    @staticmethod
    def _next_version(current: str, location: str) -> str:
        m = re.search(r"(\d+\.\d+\.\d+)", location)
        if m and m.group(1) != current:
            return m.group(1)
        parts = current.split(".")
        parts[-1] = str(int(parts[-1]) + 1) if parts[-1].isdigit() else "1"
        return ".".join(parts)

    async def cmd_update_firmware(self, request_id: int, firmware: dict, retries: int | None = None, retry_interval: int | None = None) -> dict:
        if not re.match(r"^(https?|ftps?|sftp)://", firmware["location"]):
            return {"status": "Rejected", "status_info": {"reason_code": "UnsupportedProtocol"}}
        if self.fw_failure == "signature" and firmware.get("signing_certificate"):
            return {"status": "InvalidCertificate"}
        cancelled = self.fw_task is not None and not self.fw_task.done()
        if cancelled:
            self.fw_task.cancel()
        self.firmware.update({"request_id": request_id, "status": "DownloadScheduled"})
        self.fw_task = self.spawn(self._firmware_flow(request_id, firmware))
        return {"status": "AcceptedCanceled" if cancelled else "Accepted"}

    async def _fw(self, status: str, request_id: int) -> None:
        self.firmware["status"] = status
        await self.request("FirmwareStatusNotification", status=status, request_id=request_id)

    async def _firmware_flow(self, request_id: int, fw: dict) -> None:
        retrieve = smartcharging.parse_dt(fw["retrieve_date_time"])
        wait = (retrieve - self.now()).total_seconds()
        if wait > 0:
            await self._fw("DownloadScheduled", request_id)
            await asyncio.sleep(min(wait, 3600))
        await self._fw("Downloading", request_id)
        await asyncio.sleep(1.5)
        if self.fw_failure == "download":
            await self._fw("DownloadFailed", request_id)
            return
        await self._fw("Downloaded", request_id)
        if fw.get("signature"):
            if self.fw_failure == "signature":
                await self._fw("InvalidSignature", request_id)
                return
            await self._fw("SignatureVerified", request_id)
        install = smartcharging.parse_dt(fw.get("install_date_time"))
        if install and install > self.now():
            await self._fw("InstallScheduled", request_id)
            await asyncio.sleep(min((install - self.now()).total_seconds(), 3600))
        while any(e.tx for e in self.evses):  # a running session is never interrupted by an install
            await asyncio.sleep(1)
        await self._fw("Installing", request_id)
        await asyncio.sleep(1.5)
        if self.fw_failure == "install":
            await self._fw("InstallationFailed", request_id)
            return
        await self._fw("InstallRebooting", request_id)
        new = self._next_version(self.cfg.firmware_version, fw["location"])
        self.cfg.firmware_version = new
        self.model.find("ChargingStation", "FirmwareVersion").value = new
        self.firmware["version_pending_done"] = True
        await self.reboot("FirmwareUpdate", "Reboot")

    async def cmd_get_log(self, log: dict, log_type: str, request_id: int, retries: int | None = None, retry_interval: int | None = None) -> dict:
        if not re.match(r"^(https?|ftps?|sftp)://", log["remote_location"]):
            return {"status": "Rejected", "status_info": {"reason_code": "UnsupportedProtocol"}}
        cancelled = self.log_task is not None and not self.log_task.done()
        if cancelled:
            self.log_task.cancel()
        self.log_upload.update({"request_id": request_id, "status": "Uploading"})
        self.log_task = self.spawn(self._log_flow(request_id))
        return {"status": "AcceptedCanceled" if cancelled else "Accepted", "filename": f"{self.cfg.ocpp_id}-{int(time.time())}.log"}

    async def _log_flow(self, request_id: int) -> None:
        for status in ("Uploading", "UploadFailure" if self.fw_failure == "log" else "Uploaded"):
            self.log_upload["status"] = status
            await self.request("LogStatusNotification", status=status, request_id=request_id)
            await asyncio.sleep(1.5)

    async def cmd_publish_firmware(self, location: str, checksum: str, request_id: int, retries: int | None = None, retry_interval: int | None = None) -> dict:
        if not re.match(r"^(https?|ftps?)://", location):
            return {"status": "Rejected", "status_info": {"reason_code": "UnsupportedProtocol"}}
        self.spawn(self._publish_flow(location, checksum, request_id))
        return {"status": "Accepted"}

    async def _publish_flow(self, location: str, checksum: str, request_id: int) -> None:
        for status in ("DownloadScheduled", "Downloading", "Downloaded", "ChecksumVerified"):
            await self.request("PublishFirmwareStatusNotification", status=status, request_id=request_id)
            await asyncio.sleep(0.8)
        self.firmware["published"][checksum] = location
        await self.request("PublishFirmwareStatusNotification", status="Published", request_id=request_id, location=[f"http://{self.cfg.ocpp_id.lower()}.local/fw/{checksum}"])

    async def cmd_unpublish_firmware(self, checksum: str) -> dict:
        return {"status": "Unpublished" if self.firmware["published"].pop(checksum, None) else "NoFirmware"}

    # ----------------------------------------------------------------------------------------- UI
    def commands_snapshot(self) -> dict:
        return {
            "profiles": [{"id": p["id"], "evse_id": p["_evse_id"], "purpose": p["charging_profile_purpose"], "stack_level": p["stack_level"], "kind": p["charging_profile_kind"],
                          "unit": p["charging_schedule"][0].get("charging_rate_unit"), "periods": p["charging_schedule"][0]["charging_schedule_period"]} for p in self.profiles],
            "reservations": [{"id": r["id"], "evse_id": r["evse_id"], "token": r["id_token"]["id_token"], "expiry": r["expiry"].isoformat()} for r in self.reservations.values()],
            "local_list": {"version": self.local_list["version"], "entries": [{"token": t, **{k: v for k, v in i.items() if k in ("status", "type")}} for t, i in list(self.local_list["entries"].items())[:50]], "count": len(self.local_list["entries"])},
            "cache": [{"token": t, "status": c["status"]} for t, c in list(self.auth_cache.items())[:50]],
            "certificates": [{"type": c["type"], "serial": c["hash"]["serial_number"]} for c in self.certs],
            "display_messages": list(self.display_messages.values()),
            "monitors": [{k: v for k, v in m.items() if k != "active"} | {"breached": m["active"]} for m in self.monitors.values()],
            "firmware": {"status": self.firmware["status"], "version": self.cfg.firmware_version, "published": len(self.firmware["published"])},
            "log_upload": dict(self.log_upload),
            "network_profiles": {str(k): v for k, v in self.network_profiles.items()},
            "tx_queue": [{"event_type": q["event_type"], "seq_no": q["seq_no"], "transaction_id": q["transaction_info"]["transaction_id"][:8], "timestamp": q["timestamp"]} for q in list(self.tx_queue)[:30]],
        }
