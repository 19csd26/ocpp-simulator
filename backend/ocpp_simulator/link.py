"""The per-connection OCPP endpoint: maps every CSMS command to a `cmd_*` method on the Station."""
import json
import re

from ocpp.routing import on
from ocpp.v201 import ChargePoint as BaseChargePoint
from ocpp.v201 import call_result
from ocpp.v201.enums import Action

from ocpp_simulator.rules import REJECT_PAYLOADS

# Every CSMS -> station command of OCPP 2.0.1 (40; DataTransfer is bidirectional).
COMMAND_NAMES = [
    "CancelReservation", "CertificateSigned", "ChangeAvailability", "ClearCache", "ClearChargingProfile", "ClearDisplayMessage",
    "ClearVariableMonitoring", "CostUpdated", "CustomerInformation", "DataTransfer", "DeleteCertificate", "GetBaseReport",
    "GetChargingProfiles", "GetCompositeSchedule", "GetDisplayMessages", "GetInstalledCertificateIds", "GetLocalListVersion", "GetLog",
    "GetMonitoringReport", "GetReport", "GetTransactionStatus", "GetVariables", "InstallCertificate", "PublishFirmware",
    "RequestStartTransaction", "RequestStopTransaction", "ReserveNow", "Reset", "SendLocalList", "SetChargingProfile",
    "SetDisplayMessage", "SetMonitoringBase", "SetMonitoringLevel", "SetNetworkProfile", "SetVariableMonitoring", "SetVariables",
    "TriggerMessage", "UnlockConnector", "UnpublishFirmware", "UpdateFirmware",
]


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower().replace("e_v_", "ev_")


class RuleRouting:
    """Message-level fault rules for incoming CSMS commands, shared by the 2.0.1 and 1.6 endpoints."""

    sim = None  # set by the Station right after construction
    reject_payloads: dict = REJECT_PAYLOADS

    async def route_message(self, raw_msg):
        """Apply message-level fault rules to incoming CSMS commands before the library handles them."""
        try:
            frame = json.loads(raw_msg)
        except (TypeError, ValueError):
            return await super().route_message(raw_msg)
        if isinstance(frame, list) and frame and frame[0] == 2:
            rule = self.sim.rules.match("in", frame[2])
            if rule is not None and await self._apply_rule(rule, frame):
                return None
        return await super().route_message(raw_msg)

    async def _apply_rule(self, rule, frame) -> bool:
        """Returns True when the rule fully handled the message (so the library must not)."""
        _, msg_id, action = frame[0], frame[1], frame[2]
        self.sim.note(f"rule #{rule.id}: {rule.effect} on incoming {action}")
        if rule.effect == "delay":
            import asyncio

            await asyncio.sleep(rule.params.get("delay_ms", 1000) / 1000)
            return False
        if rule.effect == "timeout":
            return True  # never answer
        if rule.effect == "error":
            code = rule.params.get("code", "InternalError")
            await self._connection.send(json.dumps([4, msg_id, code, rule.params.get("description", "Injected failure"), {}]))
            return True
        if rule.effect == "reject":
            payload = self.reject_payloads.get(action)
            if payload is None:
                await self._connection.send(json.dumps([4, msg_id, "NotSupported", f"{action} cannot be rejected", {}]))
            else:
                from ocpp.charge_point import snake_to_camel_case

                await self._connection.send(json.dumps([3, msg_id, snake_to_camel_case(payload)]))
            return True
        if rule.effect == "invalid_response":
            await self._connection.send(json.dumps([3, msg_id, {"bogusField": True}]))
            return True
        return False


class Link(RuleRouting, BaseChargePoint):
    """OCPP 2.0.1 endpoint."""


def _make_handler(name: str):
    async def handler(self, **kwargs):
        result = await self.sim.run_command(name, kwargs)
        return getattr(call_result, name)(**result)

    handler.__name__ = f"on_{snake(name)}"
    return on(Action(name))(handler)


for _name in COMMAND_NAMES:
    setattr(Link, f"on_{snake(_name)}", _make_handler(_name))
