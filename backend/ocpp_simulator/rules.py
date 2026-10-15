"""Message-level fault injection: change how individual OCPP messages are sent or answered.

direction "out": messages the simulated station SENDS  -> delay | drop | duplicate | corrupt
direction "in" : commands the CSMS sends to the station -> delay | timeout | error | reject | invalid_response
"""
import random
from dataclasses import asdict, dataclass, field
from itertools import count

OUT_EFFECTS = {"delay", "drop", "duplicate", "corrupt"}
IN_EFFECTS = {"delay", "timeout", "error", "reject", "invalid_response"}
ERROR_CODES = ["NotImplemented", "NotSupported", "InternalError", "ProtocolError", "SecurityError", "FormationViolation", "PropertyConstraintViolation", "OccurenceConstraintViolation", "TypeConstraintViolation", "GenericError"]

# Valid "refusal" payloads for commands that have a Rejected-style status.
REJECT_PAYLOADS: dict[str, dict] = {
    "RequestStartTransaction": {"status": "Rejected"}, "RequestStopTransaction": {"status": "Rejected"},
    "Reset": {"status": "Rejected"}, "ChangeAvailability": {"status": "Rejected"}, "UnlockConnector": {"status": "UnlockFailed"},
    "TriggerMessage": {"status": "Rejected"}, "SetChargingProfile": {"status": "Rejected"}, "ClearChargingProfile": {"status": "Unknown"},
    "GetChargingProfiles": {"status": "NoProfiles"}, "GetCompositeSchedule": {"status": "Rejected"}, "ReserveNow": {"status": "Rejected"},
    "CancelReservation": {"status": "Rejected"}, "ClearCache": {"status": "Rejected"}, "UpdateFirmware": {"status": "Rejected"},
    "GetLog": {"status": "Rejected"}, "SendLocalList": {"status": "Failed"}, "SetNetworkProfile": {"status": "Rejected"},
    "InstallCertificate": {"status": "Rejected"}, "DeleteCertificate": {"status": "Failed"}, "CertificateSigned": {"status": "Rejected"},
    "GetBaseReport": {"status": "Rejected"}, "GetReport": {"status": "Rejected"}, "SetDisplayMessage": {"status": "Rejected"},
    "GetDisplayMessages": {"status": "Unknown"}, "ClearDisplayMessage": {"status": "Unknown"}, "SetMonitoringBase": {"status": "Rejected"},
    "SetMonitoringLevel": {"status": "Rejected"}, "GetMonitoringReport": {"status": "Rejected"}, "ClearVariableMonitoring": {"clear_monitoring_result": []},
    "CustomerInformation": {"status": "Rejected"}, "PublishFirmware": {"status": "Rejected"}, "UnpublishFirmware": {"status": "NoFirmware"},
    "GetInstalledCertificateIds": {"status": "NotFound"}, "DataTransfer": {"status": "Rejected"}, "GetTransactionStatus": {"messages_in_queue": False},
}


@dataclass
class Rule:
    id: int
    direction: str
    action: str  # "*" matches every message
    effect: str
    params: dict = field(default_factory=dict)  # delay_ms | code | description
    times: int | None = None  # None = every time, otherwise this many matches then it switches itself off
    probability: float = 1.0
    enabled: bool = True
    hits: int = 0

    def public(self) -> dict:
        return asdict(self)


class RuleSet:
    def __init__(self) -> None:
        self.rules: list[Rule] = []
        self._ids = count(1)

    def add(self, direction: str, action: str, effect: str, params: dict | None = None, times: int | None = None, probability: float = 1.0) -> Rule:
        allowed = OUT_EFFECTS if direction == "out" else IN_EFFECTS if direction == "in" else None
        if allowed is None:
            raise ValueError("direction must be 'out' or 'in'")
        if effect not in allowed:
            raise ValueError(f"effect '{effect}' is not available for direction '{direction}' (use one of {sorted(allowed)})")
        if not 0 < probability <= 1:
            raise ValueError("probability must be in (0, 1]")
        if times is not None and times < 1:
            raise ValueError("times must be at least 1")
        rule = Rule(next(self._ids), direction, action or "*", effect, params or {}, times, probability)
        self.rules.append(rule)
        return rule

    def remove(self, rule_id: int) -> bool:
        before = len(self.rules)
        self.rules = [r for r in self.rules if r.id != rule_id]
        return len(self.rules) != before

    def clear(self) -> None:
        self.rules.clear()

    def match(self, direction: str, action: str | None) -> Rule | None:
        for rule in self.rules:
            if not rule.enabled or rule.direction != direction or rule.action not in ("*", action):
                continue
            if rule.probability < 1 and random.random() > rule.probability:
                continue
            rule.hits += 1
            if rule.times is not None and rule.hits >= rule.times:
                rule.enabled = False
            return rule
        return None

    def snapshot(self) -> list[dict]:
        return [r.public() for r in self.rules]
