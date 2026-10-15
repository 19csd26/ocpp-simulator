"""Station configuration, hardware presets and EV models."""
import uuid
from typing import Literal

from pydantic import BaseModel, Field, field_validator

CONNECTOR_TYPES = [
    "cCCS1", "cCCS2", "cG105", "cTesla", "cType1", "cType2", "s309-1P-16A", "s309-1P-32A", "s309-3P-16A",
    "s309-3P-32A", "sBS1361", "sCEE-7-7", "sType2", "sType3", "Other1PhMax16A", "Other1PhOver16A", "Other3Ph",
    "Pan", "wInductive", "wResonant", "Undetermined", "Unknown",
]


class ConnectorSpec(BaseModel):
    type: str = "cType2"

    @field_validator("type")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in CONNECTOR_TYPES:
            raise ValueError(f"Unknown connector type '{v}'")
        return v


class EvseSpec(BaseModel):
    current_type: Literal["AC", "DC"] = "AC"
    max_power_kw: float = Field(7.4, gt=0, le=1000)
    voltage: float = Field(230, gt=0, le=1000)
    phases: Literal[1, 3] = 1
    connectors: list[ConnectorSpec] = Field(default_factory=lambda: [ConnectorSpec()], min_length=1, max_length=4)

    @property
    def max_current_a(self) -> float:
        return round(self.max_power_kw * 1000 / (self.voltage * (self.phases if self.current_type == "AC" else 1)), 1)


class StationConfig(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:10])  # local id inside the simulator
    name: str = "Simulated station"
    ocpp_id: str = Field("SIM001", pattern=r"^[A-Za-z0-9_.-]{1,60}$")  # identity in the CSMS URL
    csms_url: str = "ws://localhost:8000/ocpp"  # base URL; the station id is appended
    password: str | None = None  # HTTP Basic password (security profile 1 and 2)
    security_profile: Literal[0, 1, 2, 3] = 1
    verify_tls: bool = True
    ca_file: str | None = None
    client_cert: str | None = None  # security profile 3 (mutual TLS)
    client_key: str | None = None
    vendor: str = "SimCharge"
    model: str = "SIM-AC-7"
    serial_number: str = Field(default_factory=lambda: "SN" + uuid.uuid4().hex[:8].upper())
    firmware_version: str = "1.0.0"
    iccid: str = "8991101200003204510"
    imsi: str = "310150123456789"
    evses: list[EvseSpec] = Field(default_factory=lambda: [EvseSpec()], min_length=1, max_length=8)
    time_scale: float = Field(1.0, ge=1, le=600)  # speeds up charging only (energy / SoC), never protocol timers
    boot_on_reconnect: bool = True  # send BootNotification again after every WebSocket reconnect
    auto_connect: bool = False
    end_on_full: bool = True  # end the session (StoppedByEV) when the vehicle battery is full


PRESETS: dict[str, dict] = {
    "ac_7": {"label": "AC wallbox · 7.4 kW · 1 connector", "model": "SIM-AC-7", "evses": [{"current_type": "AC", "max_power_kw": 7.4, "phases": 1, "voltage": 230, "connectors": [{"type": "cType2"}]}]},
    "ac_22_dual": {"label": "AC pedestal · 2 × 22 kW", "model": "SIM-AC-22D", "evses": [{"current_type": "AC", "max_power_kw": 22, "phases": 3, "voltage": 230, "connectors": [{"type": "cType2"}]}] * 2},
    "dc_60_dual": {"label": "DC fast · 2 × 60 kW (CCS2 + CHAdeMO on one EVSE)", "model": "SIM-DC-60", "evses": [
        {"current_type": "DC", "max_power_kw": 60, "phases": 3, "voltage": 400, "connectors": [{"type": "cCCS2"}, {"type": "cG105"}]},
        {"current_type": "DC", "max_power_kw": 60, "phases": 3, "voltage": 400, "connectors": [{"type": "cCCS2"}]}]},
    "dc_150": {"label": "DC ultra-fast · 150 kW · CCS2", "model": "SIM-DC-150", "evses": [{"current_type": "DC", "max_power_kw": 150, "phases": 3, "voltage": 400, "connectors": [{"type": "cCCS2"}]}]},
    "mixed_hub": {"label": "Hub · 2 AC 22 kW + 1 DC 60 kW", "model": "SIM-HUB", "evses": [
        {"current_type": "AC", "max_power_kw": 22, "phases": 3, "voltage": 230, "connectors": [{"type": "cType2"}]},
        {"current_type": "AC", "max_power_kw": 22, "phases": 3, "voltage": 230, "connectors": [{"type": "cType2"}]},
        {"current_type": "DC", "max_power_kw": 60, "phases": 3, "voltage": 400, "connectors": [{"type": "cCCS2"}]}]},
}

EV_MODELS: dict[str, dict] = {
    "city": {"label": "City EV · 30 kWh (AC 7.4 kW / DC 50 kW)", "capacity_kwh": 30, "max_ac_kw": 7.4, "max_dc_kw": 50},
    "mid": {"label": "Mid-size EV · 60 kWh (AC 11 kW / DC 100 kW)", "capacity_kwh": 60, "max_ac_kw": 11, "max_dc_kw": 100},
    "large": {"label": "Large EV · 100 kWh (AC 11 kW / DC 150 kW)", "capacity_kwh": 100, "max_ac_kw": 11, "max_dc_kw": 150},
    "scooter": {"label": "E-scooter · 3 kWh (AC 1.5 kW)", "capacity_kwh": 3, "max_ac_kw": 1.5, "max_dc_kw": 0},
}
