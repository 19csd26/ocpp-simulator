"""OCPP 2.0.1 device model: components, variables, attributes, validation, and report generation."""
from dataclasses import dataclass, field

from ocpp_simulator.config import StationConfig

READ_ONLY, READ_WRITE, WRITE_ONLY = "ReadOnly", "ReadWrite", "WriteOnly"


@dataclass
class Variable:
    component: str
    name: str
    value: str | None = None
    data_type: str = "string"  # string | integer | decimal | boolean | OptionList | MemberList | dateTime
    mutability: str = READ_WRITE
    evse_id: int | None = None
    connector_id: int | None = None
    instance: str | None = None
    unit: str | None = None
    supports_monitoring: bool = False
    min_limit: float | None = None
    max_limit: float | None = None
    values_list: str | None = None  # comma separated, for OptionList / MemberList
    reboot_required: bool = False
    attributes: dict[str, str] = field(default_factory=dict)  # Target / MinSet / MaxSet values (Actual is `value`)

    @property
    def key(self) -> tuple:
        return (self.component, self.instance, self.evse_id, self.connector_id, self.name)


def _is_bool(v: str) -> bool:
    return v.lower() in ("true", "false")


class DeviceModel:
    def __init__(self) -> None:
        self.vars: dict[tuple, Variable] = {}

    # ---- population ------------------------------------------------------------------------
    def add(self, component: str, name: str, value, data_type="string", mutability=READ_WRITE, **kw) -> Variable:
        if isinstance(value, bool):
            value = "true" if value else "false"
            data_type = "boolean"
        var = Variable(component=component, name=name, value=None if value is None else str(value), data_type=data_type, mutability=mutability, **kw)
        self.vars[var.key] = var
        return var

    def find(self, component: str, name: str, evse_id: int | None = None, connector_id: int | None = None, instance: str | None = None) -> Variable | None:
        return self.vars.get((component, instance, evse_id, connector_id, name))

    def get_value(self, component: str, name: str, default=None, evse_id: int | None = None):
        var = self.find(component, name, evse_id)
        return default if var is None or var.value is None else var.value

    def get_int(self, component: str, name: str, default: int = 0) -> int:
        try:
            return int(float(self.get_value(component, name, default)))
        except (TypeError, ValueError):
            return default

    def get_bool(self, component: str, name: str, default: bool = False) -> bool:
        v = self.get_value(component, name, None)
        return default if v is None else str(v).lower() == "true"

    def get_list(self, component: str, name: str) -> list[str]:
        v = self.get_value(component, name, "")
        return [x.strip() for x in str(v).split(",") if x.strip()]

    # ---- OCPP semantics --------------------------------------------------------------------
    def read(self, component: dict, variable: dict, attribute_type: str | None = None) -> tuple[str, str | None, Variable | None]:
        """GetVariables: returns (attributeStatus, value, variable)."""
        var = self._resolve(component, variable)
        if var is None:
            known = any(v.component == component["name"] for v in self.vars.values())
            return ("UnknownVariable" if known else "UnknownComponent"), None, None
        attr = attribute_type or "Actual"
        if var.mutability == WRITE_ONLY:
            return "Rejected", None, var
        if attr == "Actual":
            return "Accepted", var.value, var
        if attr in var.attributes:
            return "Accepted", var.attributes[attr], var
        return "NotSupportedAttributeType", None, var

    def write(self, component: dict, variable: dict, value: str, attribute_type: str | None = None) -> tuple[str, Variable | None]:
        """SetVariables: returns (attributeStatus, variable)."""
        var = self._resolve(component, variable)
        if var is None:
            known = any(v.component == component["name"] for v in self.vars.values())
            return ("UnknownVariable" if known else "UnknownComponent"), None
        attr = attribute_type or "Actual"
        if var.mutability == READ_ONLY:
            return "Rejected", var
        if attr != "Actual":
            if attr not in ("Target", "MinSet", "MaxSet"):
                return "NotSupportedAttributeType", var
            var.attributes[attr] = value
            return "Accepted", var
        problem = self.validate(var, value)
        if problem:
            return "Rejected", var
        var.value = value
        return ("RebootRequired" if var.reboot_required else "Accepted"), var

    @staticmethod
    def validate(var: Variable, value: str) -> str | None:
        t = var.data_type
        try:
            if t == "boolean" and not _is_bool(value):
                return "must be true or false"
            if t == "integer":
                n = int(value)
            elif t == "decimal":
                n = float(value)
            else:
                n = None
            if n is not None:
                if var.min_limit is not None and n < var.min_limit:
                    return f"below minimum {var.min_limit}"
                if var.max_limit is not None and n > var.max_limit:
                    return f"above maximum {var.max_limit}"
            if t == "OptionList" and var.values_list and value not in var.values_list.split(","):
                return "not one of the allowed options"
            if t == "MemberList" and var.values_list:
                allowed = set(var.values_list.split(","))
                if any(x.strip() not in allowed for x in value.split(",") if x.strip()):
                    return "contains a value that is not allowed"
        except ValueError:
            return "wrong data type"
        return None

    def _resolve(self, component: dict, variable: dict) -> Variable | None:
        evse = component.get("evse") or {}
        return self.vars.get((component["name"], component.get("instance"), evse.get("id"), evse.get("connector_id"), variable["name"]))

    # ---- reports ----------------------------------------------------------------------------
    def report_items(self, base: str = "FullInventory", component_criteria: list[str] | None = None, component_variable: list[dict] | None = None) -> list[dict]:
        out = []
        for var in sorted(self.vars.values(), key=lambda v: (v.component, v.evse_id or 0, v.connector_id or 0, v.name)):
            if component_variable:
                if not any(self._matches(var, cv) for cv in component_variable):
                    continue
            elif base == "ConfigurationInventory" and var.mutability == READ_ONLY:
                continue
            elif base == "SummaryInventory" and var.name not in ("Available", "AvailabilityState", "Enabled"):
                continue
            if component_criteria and not self._criteria_match(var, component_criteria):
                continue
            out.append(self.report_data(var))
        return out

    @staticmethod
    def _matches(var: Variable, cv: dict) -> bool:
        comp = cv.get("component") or {}
        evse = comp.get("evse") or {}
        if comp.get("name") != var.component:
            return False
        if evse.get("id") is not None and evse.get("id") != var.evse_id:
            return False
        v = cv.get("variable")
        return v is None or v.get("name") == var.name

    @staticmethod
    def _criteria_match(var: Variable, criteria: list[str]) -> bool:
        if var.name == "Available" and "Available" in criteria:
            return True
        if var.name == "Enabled" and "Enabled" in criteria:
            return True
        return any(c in ("Active", "Problem") and var.name in ("Available", "Enabled") for c in criteria)

    def report_data(self, var: Variable) -> dict:
        comp: dict = {"name": var.component}
        if var.instance:
            comp["instance"] = var.instance
        if var.evse_id is not None:
            comp["evse"] = {"id": var.evse_id, **({"connector_id": var.connector_id} if var.connector_id is not None else {})}
        variable: dict = {"name": var.name}
        attrs = []
        if var.value is not None:
            attrs.append({"type": "Actual", "value": var.value, "mutability": var.mutability})
        for t, v in var.attributes.items():
            attrs.append({"type": t, "value": v, "mutability": var.mutability})
        if not attrs:
            attrs.append({"type": "Actual", "mutability": var.mutability})
        chars: dict = {"data_type": var.data_type, "supports_monitoring": var.supports_monitoring}
        for key, attr in (("unit", var.unit), ("min_limit", var.min_limit), ("max_limit", var.max_limit), ("values_list", var.values_list)):
            if attr is not None:
                chars[key] = attr
        return {"component": comp, "variable": variable, "variable_attribute": attrs, "variable_characteristics": chars}

    def snapshot(self) -> list[dict]:
        return [
            {"component": v.component, "evse_id": v.evse_id, "connector_id": v.connector_id, "variable": v.name, "value": v.value,
             "data_type": v.data_type, "mutability": v.mutability, "unit": v.unit, "values_list": v.values_list}
            for v in sorted(self.vars.values(), key=lambda v: (v.component, v.evse_id or 0, v.connector_id or 0, v.name))
        ]


def default_model(cfg: StationConfig) -> DeviceModel:
    m = DeviceModel()
    RO = READ_ONLY
    # --- ChargingStation
    m.add("ChargingStation", "Model", cfg.model, mutability=RO)
    m.add("ChargingStation", "VendorName", cfg.vendor, mutability=RO)
    m.add("ChargingStation", "SerialNumber", cfg.serial_number, mutability=RO)
    m.add("ChargingStation", "FirmwareVersion", cfg.firmware_version, mutability=RO)
    m.add("ChargingStation", "Available", True, mutability=RO)
    m.add("ChargingStation", "AvailabilityState", "Available", "OptionList", RO, values_list="Available,Occupied,Reserved,Unavailable,Faulted")
    m.add("ChargingStation", "SupplyPhases", 3, "integer", RO)
    # --- OCPPCommCtrlr
    m.add("OCPPCommCtrlr", "HeartbeatInterval", 30, "integer", unit="s", min_limit=1, max_limit=86400)
    m.add("OCPPCommCtrlr", "OfflineThreshold", 60, "integer", unit="s", min_limit=0)
    m.add("OCPPCommCtrlr", "MessageAttempts", 3, "integer", min_limit=1, max_limit=20)
    m.add("OCPPCommCtrlr", "MessageAttemptInterval", 5, "integer", unit="s", min_limit=1)
    m.add("OCPPCommCtrlr", "RetryBackOffRepeatTimes", 3, "integer", min_limit=0)
    m.add("OCPPCommCtrlr", "RetryBackOffRandomRange", 2, "integer", unit="s", min_limit=0)
    m.add("OCPPCommCtrlr", "RetryBackOffWaitMinimum", 2, "integer", unit="s", min_limit=1)
    m.add("OCPPCommCtrlr", "WebSocketPingInterval", 30, "integer", unit="s", min_limit=0)
    m.add("OCPPCommCtrlr", "UnlockOnEVSideDisconnect", True)
    m.add("OCPPCommCtrlr", "FileTransferProtocols", "HTTP,HTTPS,FTP", "MemberList", RO, values_list="HTTP,HTTPS,FTP,FTPS,SFTP")
    m.add("OCPPCommCtrlr", "NetworkConfigurationPriority", "0", "string")
    # --- SampledDataCtrlr / AlignedDataCtrlr
    measurands = "Energy.Active.Import.Register,Power.Active.Import,Current.Import,Voltage,SoC,Power.Offered,Current.Offered,Frequency"
    m.add("SampledDataCtrlr", "Available", True, mutability=RO)
    m.add("SampledDataCtrlr", "Enabled", True)
    m.add("SampledDataCtrlr", "TxStartedMeasurands", "Energy.Active.Import.Register", "MemberList", values_list=measurands)
    m.add("SampledDataCtrlr", "TxUpdatedMeasurands", "Energy.Active.Import.Register,Power.Active.Import,Current.Import,Voltage,SoC", "MemberList", values_list=measurands)
    m.add("SampledDataCtrlr", "TxEndedMeasurands", "Energy.Active.Import.Register", "MemberList", values_list=measurands)
    m.add("SampledDataCtrlr", "TxUpdatedInterval", 10, "integer", unit="s", min_limit=0)
    m.add("SampledDataCtrlr", "TxEndedInterval", 0, "integer", unit="s", min_limit=0)
    m.add("AlignedDataCtrlr", "Available", True, mutability=RO)
    m.add("AlignedDataCtrlr", "Enabled", True)
    m.add("AlignedDataCtrlr", "Measurands", "Energy.Active.Import.Register,Power.Active.Import", "MemberList", values_list=measurands)
    m.add("AlignedDataCtrlr", "Interval", 0, "integer", unit="s", min_limit=0)
    m.add("AlignedDataCtrlr", "TxEndedMeasurands", "Energy.Active.Import.Register", "MemberList", values_list=measurands)
    m.add("AlignedDataCtrlr", "TxEndedInterval", 0, "integer", unit="s", min_limit=0)
    # --- TxCtrlr
    points = "EVConnected,Authorized,PowerPathClosed,DataSigned"
    m.add("TxCtrlr", "TxStartPoint", "PowerPathClosed", "MemberList", values_list=points)
    m.add("TxCtrlr", "TxStopPoint", "EVConnected,Authorized", "MemberList", values_list=points)
    m.add("TxCtrlr", "EVConnectionTimeOut", 60, "integer", unit="s", min_limit=1)
    m.add("TxCtrlr", "StopTxOnEVSideDisconnect", True)
    m.add("TxCtrlr", "StopTxOnInvalidId", True)
    m.add("TxCtrlr", "MaxEnergyOnInvalidId", 0, "integer", unit="Wh", min_limit=0)
    m.add("TxCtrlr", "TxBeforeAcceptedEnabled", False)
    # --- Authorization
    m.add("AuthCtrlr", "AuthorizeRemoteStart", False)
    m.add("AuthCtrlr", "LocalAuthorizeOffline", True)
    m.add("AuthCtrlr", "LocalPreAuthorize", False)
    m.add("AuthCtrlr", "OfflineTxForUnknownIdEnabled", False)
    m.add("AuthCtrlr", "AuthEnabled", True)
    m.add("AuthCacheCtrlr", "Available", True, mutability=RO)
    m.add("AuthCacheCtrlr", "Enabled", True)
    m.add("AuthCacheCtrlr", "LifeTime", 86400, "integer", unit="s", min_limit=0)
    m.add("AuthCacheCtrlr", "Storage", 1000, "integer", RO)
    m.add("AuthCacheCtrlr", "Policy", "LRU", "OptionList", values_list="LRU,LFU,FIFO,CUSTOM")
    m.add("LocalAuthListCtrlr", "Available", True, mutability=RO)
    m.add("LocalAuthListCtrlr", "Enabled", True)
    m.add("LocalAuthListCtrlr", "Entries", 0, "integer", RO)
    m.add("LocalAuthListCtrlr", "Storage", 1000, "integer", RO)
    m.add("LocalAuthListCtrlr", "ItemsPerMessage", 250, "integer", RO)
    m.add("LocalAuthListCtrlr", "BytesPerMessage", 20000, "integer", RO)
    # --- Reservation, smart charging, display, monitoring, security, clock, device data, ISO 15118
    m.add("ReservationCtrlr", "Available", True, mutability=RO)
    m.add("ReservationCtrlr", "Enabled", True)
    m.add("ReservationCtrlr", "NonEvseSpecific", False)
    m.add("SmartChargingCtrlr", "Available", True, mutability=RO)
    m.add("SmartChargingCtrlr", "Enabled", True)
    m.add("SmartChargingCtrlr", "ACPhaseSwitchingSupported", False, mutability=RO)
    m.add("SmartChargingCtrlr", "ProfileStackLevel", 20, "integer", RO)
    m.add("SmartChargingCtrlr", "PeriodsPerSchedule", 24, "integer", RO)
    m.add("SmartChargingCtrlr", "EntriesChargingProfiles", 100, "integer", RO)
    m.add("SmartChargingCtrlr", "RateUnit", "A,W", "MemberList", RO, values_list="A,W")
    m.add("SmartChargingCtrlr", "LimitChangeSignificance", 0, "decimal", min_limit=0)
    m.add("DisplayMessageCtrlr", "Available", True, mutability=RO)
    m.add("DisplayMessageCtrlr", "Enabled", True)
    m.add("DisplayMessageCtrlr", "DisplayMessages", 10, "integer", RO)
    m.add("DisplayMessageCtrlr", "SupportedFormats", "ASCII,UTF8", "MemberList", RO, values_list="ASCII,HTML,URI,UTF8")
    m.add("DisplayMessageCtrlr", "SupportedPriorities", "NormalCycle,InFront,AlwaysFront", "MemberList", RO, values_list="AlwaysFront,InFront,NormalCycle")
    m.add("MonitoringCtrlr", "Available", True, mutability=RO)
    m.add("MonitoringCtrlr", "Enabled", True)
    m.add("MonitoringCtrlr", "ActiveMonitoringBase", "All", "OptionList", RO, values_list="All,FactoryDefault,HardWiredOnly")
    m.add("MonitoringCtrlr", "ActiveMonitoringLevel", 9, "integer", min_limit=0, max_limit=9)
    m.add("MonitoringCtrlr", "ItemsPerMessage", 50, "integer", RO)
    m.add("MonitoringCtrlr", "BytesPerMessage", 20000, "integer", RO)
    m.add("SecurityCtrlr", "Identity", cfg.ocpp_id, mutability=RO)
    m.add("SecurityCtrlr", "OrganizationName", "Simulated Operator")
    m.add("SecurityCtrlr", "SecurityProfile", cfg.security_profile, "integer", min_limit=0, max_limit=3, reboot_required=True)
    m.add("SecurityCtrlr", "CertificateEntries", 0, "integer", RO)
    m.add("ClockCtrlr", "DateTime", None, "dateTime")
    m.add("ClockCtrlr", "TimeSource", "Heartbeat", "MemberList", values_list="Heartbeat,NTP,GPS,RealTimeClock,MobileNetwork,RadioTimeTransmitter")
    m.add("ClockCtrlr", "TimeOffset", "+00:00")
    m.add("DeviceDataCtrlr", "ItemsPerMessage", 40, "integer", min_limit=1)
    m.add("DeviceDataCtrlr", "BytesPerMessage", 20000, "integer", RO)
    m.add("ISO15118Ctrlr", "Enabled", False)
    m.add("ISO15118Ctrlr", "PnCEnabled", False)
    m.add("ISO15118Ctrlr", "ContractCertificateInstallationEnabled", False)
    # --- per EVSE / connector
    for i, evse in enumerate(cfg.evses, start=1):
        m.add("EVSE", "Available", True, mutability=RO, evse_id=i)
        m.add("EVSE", "AvailabilityState", "Available", "OptionList", RO, evse_id=i, values_list="Available,Occupied,Reserved,Unavailable,Faulted")
        m.add("EVSE", "Power", evse.max_power_kw * 1000, "decimal", RO, evse_id=i, unit="W", supports_monitoring=True)
        m.add("EVSE", "SupplyPhases", evse.phases if evse.current_type == "AC" else 3, "integer", RO, evse_id=i)
        m.add("EVSE", "Temperature", 30, "decimal", RO, evse_id=i, unit="Celsius", supports_monitoring=True)
        m.add("EVSE", "Voltage", evse.voltage, "decimal", RO, evse_id=i, unit="V", supports_monitoring=True)
        for c, conn in enumerate(evse.connectors, start=1):
            m.add("Connector", "Available", True, mutability=RO, evse_id=i, connector_id=c)
            m.add("Connector", "ConnectorType", conn.type, mutability=RO, evse_id=i, connector_id=c)
            m.add("Connector", "SupplyPhases", evse.phases if evse.current_type == "AC" else 3, "integer", RO, evse_id=i, connector_id=c)
    return m
