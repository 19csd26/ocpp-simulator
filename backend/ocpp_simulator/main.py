"""HTTP + WebSocket API for the simulator UI.

Stations live in memory only (no database, no files): add them in the UI by typing the CSMS WebSocket URL and the
charger id. The UI remembers your entries in the browser.
"""
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from ocpp_simulator.config import CONNECTOR_TYPES, EV_MODELS, PRESETS, StationConfig
from ocpp_simulator.faults import CATALOG as FAULTS
from ocpp_simulator.hub import Hub
from ocpp_simulator.link import COMMAND_NAMES
from ocpp_simulator.rules import ERROR_CODES, IN_EFFECTS, OUT_EFFECTS
from ocpp_simulator.scenarios import CATALOG_SCENARIOS, Runner
from ocpp_simulator.station import Station
from ocpp_simulator.templates import command_template, message_template

UI_DIST = Path(__file__).resolve().parent.parent.parent / "ui" / "dist"


class NewStation(BaseModel):
    name: str = "Simulated station"
    ocpp_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,60}$")
    csms_url: str
    password: str | None = None
    security_profile: Literal[0, 1, 2, 3] = 1
    verify_tls: bool = True
    preset: str = "ac_22_dual"
    evses: list[dict] | None = None  # custom hardware (overrides the preset)
    vendor: str | None = None
    model: str | None = None
    firmware_version: str | None = None
    time_scale: float = Field(1.0, ge=1, le=600)
    boot_on_reconnect: bool = True
    end_on_full: bool = True
    connect: bool = True


class StationPatch(BaseModel):
    name: str | None = None
    time_scale: float | None = Field(None, ge=1, le=600)
    boot_on_reconnect: bool | None = None
    end_on_full: bool | None = None
    password: str | None = None  # "" removes the password
    csms_url: str | None = None
    ocpp_id: str | None = Field(None, pattern=r"^[A-Za-z0-9_.-]{1,60}$")
    security_profile: Literal[0, 1, 2, 3] | None = None
    verify_tls: bool | None = None
    preset: str | None = None
    evses: list[dict] | None = None  # custom hardware (overrides the preset)


class PlugIn(BaseModel):
    connector: int = 1
    ev: str = "mid"
    soc: float = 20
    target_soc: float = 100


class Tap(BaseModel):
    token: str = Field(min_length=1, max_length=36)
    type: str = "ISO14443"


class StopBody(BaseModel):
    reason: str = "Local"


class EvBody(BaseModel):
    ready: bool | None = None
    soc: float | None = None
    target_soc: float | None = None


class FaultBody(BaseModel):
    kind: str
    scope: int | None = None
    params: dict = Field(default_factory=dict)


class SendBody(BaseModel):
    action: str
    payload: dict = Field(default_factory=dict)
    validate_schema: bool = True


class RuleBody(BaseModel):
    direction: Literal["out", "in"]
    action: str = "*"
    effect: str
    params: dict = Field(default_factory=dict)
    times: int | None = None
    probability: float = 1.0


class CustomScenario(BaseModel):
    title: str = "Custom scenario"
    steps: list[dict] = Field(min_length=1, max_length=200)


class VariableBody(BaseModel):
    component: str
    variable: str
    value: str
    evse_id: int | None = None


def create_app() -> FastAPI:
    hub = Hub()
    stations: dict[str, Station] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        for st in list(stations.values()):
            await st.shutdown()

    app = FastAPI(title="OCPP 2.0.1 Charging Station Simulator", version="1.0.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origin_regex=r"^http://(localhost|127\.0\.0\.1)(:\d+)?$", allow_methods=["*"], allow_headers=["*"], expose_headers=["Content-Disposition"])
    app.state.hub, app.state.stations = hub, stations

    def get(station_id: str) -> Station:
        st = stations.get(station_id)
        if st is None:
            raise HTTPException(404, "Unknown station")
        return st

    @app.exception_handler(ValueError)
    async def value_error(_, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    async def guarded(coro):
        try:
            return await coro
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    # ------------------------------------------------------------------------------------ catalogue
    @app.get("/api/meta")
    async def meta():
        return {
            "presets": {k: {"label": v["label"], "evses": v["evses"], "model": v["model"]} for k, v in PRESETS.items()},
            "ev_models": EV_MODELS, "connector_types": CONNECTOR_TYPES,
            "faults": FAULTS,
            "scenarios": [{k: v for k, v in s.items() if k != "steps"} | {"steps": len(s["steps"])} for s in CATALOG_SCENARIOS.values()],
            "station_messages": sorted(Station.CP_ACTIONS), "commands": COMMAND_NAMES,
            "rule_effects": {"out": sorted(OUT_EFFECTS), "in": sorted(IN_EFFECTS)}, "error_codes": ERROR_CODES,
            "stop_reasons": sorted({"DeAuthorized", "EmergencyStop", "EnergyLimitReached", "EVDisconnected", "GroundFault", "ImmediateReset", "Local", "LocalOutOfCredit",
                                    "MasterPass", "Other", "OvercurrentFault", "PowerLoss", "PowerQuality", "Reboot", "Remote", "SOCLimitReached", "StoppedByEV", "TimeLimitReached", "Timeout"}),
            "scenario_steps": {"do": sorted(Runner.ACTIONS), "expect": sorted(Runner.EXPECTS)},
        }

    @app.get("/api/scenarios/{scenario_id}")
    async def scenario(scenario_id: str):
        if scenario_id not in CATALOG_SCENARIOS:
            raise HTTPException(404, "Unknown scenario")
        return CATALOG_SCENARIOS[scenario_id]

    # ------------------------------------------------------------------------------------- stations
    @app.get("/api/stations")
    async def list_stations():
        return [s.snapshot() for s in stations.values()]

    @app.post("/api/stations", status_code=201)
    async def add_station(body: NewStation):
        if body.preset not in PRESETS:
            raise HTTPException(422, f"Unknown preset '{body.preset}'")
        if not body.csms_url.startswith(("ws://", "wss://")):
            raise HTTPException(422, "The CSMS URL must start with ws:// or wss://")
        if body.security_profile >= 2 and not body.csms_url.startswith("wss://"):
            raise HTTPException(422, "Security profile 2 and 3 need a wss:// URL")
        if any(s.cfg.ocpp_id == body.ocpp_id and s.cfg.csms_url == body.csms_url for s in stations.values()):
            raise HTTPException(409, "A simulated station with this charger id and URL already exists; two would fight over one connection")
        preset = PRESETS[body.preset]
        data = body.model_dump(exclude={"preset", "connect", "evses"}, exclude_none=True)
        try:
            cfg = StationConfig(**{"evses": body.evses or preset["evses"], "model": preset["model"], **data})
        except ValidationError as exc:
            raise HTTPException(422, "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()))
        station = Station(cfg, hub)
        stations[cfg.id] = station
        if body.connect:
            await station.connect()
        hub.state_changed(station)
        return station.snapshot()

    @app.get("/api/stations/{station_id}")
    async def get_station(station_id: str):
        return get(station_id).snapshot()

    @app.patch("/api/stations/{station_id}")
    async def patch_station(station_id: str, body: StationPatch):
        st = get(station_id)
        changes = body.model_dump(exclude_none=True)
        preset = changes.pop("preset", None)
        if preset is not None:
            if preset not in PRESETS:
                raise HTTPException(422, f"Unknown preset '{preset}'")
            changes.setdefault("evses", PRESETS[preset]["evses"])
            changes.setdefault("model", PRESETS[preset]["model"])
        if "password" in changes:
            changes["password"] = changes["password"] or None
        # Only what is really different counts as a change (the edit form sends every field).
        current = st.cfg.model_dump()
        changes = {k: v for k, v in changes.items() if current.get(k) != v}
        locked = {"ocpp_id", "csms_url", "password", "security_profile", "verify_tls", "evses", "model"} & changes.keys()
        if locked and st.want_connected:
            raise HTTPException(409, "Disconnect the station before changing its charger id, URL, credentials or hardware")
        if "evses" in changes:
            busy = [e.id for e in st.evses if e.plugged is not None or e.tx is not None]
            if busy:
                raise HTTPException(409, f"Unplug the vehicle on EVSE {', '.join(map(str, busy))} before changing the hardware")
            if st.faults and any(f.active for f in st.faults.values()):
                raise HTTPException(409, "Clear the active faults before changing the hardware")
            if st.tx_queue:
                raise HTTPException(409, "The offline queue is not empty; reconnect and let it flush before changing the hardware")
        try:
            new = StationConfig(**{**current, **changes})
        except ValidationError as exc:
            raise HTTPException(422, "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()))
        if not new.csms_url.startswith(("ws://", "wss://")):
            raise HTTPException(422, "The CSMS URL must start with ws:// or wss://")
        if new.security_profile >= 2 and not new.csms_url.startswith("wss://"):
            raise HTTPException(422, "Security profile 2 and 3 need a wss:// URL")
        if any(o is not st and o.cfg.ocpp_id == new.ocpp_id and o.cfg.csms_url == new.csms_url for o in stations.values()):
            raise HTTPException(409, "Another simulated station already uses this charger id and URL")
        st.apply_config(new, hardware_changed="evses" in changes)
        return st.snapshot()

    @app.delete("/api/stations/{station_id}", status_code=204)
    async def delete_station(station_id: str):
        st = get(station_id)
        st.removed = True  # stop any queued snapshot from being published
        await st.shutdown()
        stations.pop(station_id, None)
        hub.forget(station_id)
        hub.publish({"type": "removed", "station_id": station_id})

    @app.post("/api/stations/{station_id}/connect")
    async def connect(station_id: str):
        await get(station_id).connect()
        return get(station_id).snapshot()

    @app.post("/api/stations/{station_id}/disconnect")
    async def disconnect(station_id: str):
        await get(station_id).disconnect()
        return get(station_id).snapshot()

    # ---------------------------------------------------------------------------------- operations
    @app.post("/api/stations/{station_id}/evse/{evse_id}/plug-in")
    async def plug_in(station_id: str, evse_id: int, body: PlugIn):
        return await guarded(get(station_id).plug_in(evse_id, body.connector, body.ev, body.soc, body.target_soc))

    @app.post("/api/stations/{station_id}/evse/{evse_id}/unplug")
    async def unplug(station_id: str, evse_id: int):
        return await guarded(get(station_id).unplug(evse_id))

    @app.post("/api/stations/{station_id}/evse/{evse_id}/tap")
    async def tap(station_id: str, evse_id: int, body: Tap):
        return await guarded(get(station_id).tap_card(evse_id, body.token, body.type))

    @app.post("/api/stations/{station_id}/evse/{evse_id}/stop")
    async def stop(station_id: str, evse_id: int, body: StopBody):
        return await guarded(get(station_id).stop_session(evse_id, body.reason, "StopAuthorized"))

    @app.post("/api/stations/{station_id}/evse/{evse_id}/ev")
    async def ev(station_id: str, evse_id: int, body: EvBody):
        return await guarded(get(station_id).set_ev(evse_id, ready=body.ready, soc=body.soc, target_soc=body.target_soc))

    # ------------------------------------------------------------------------------------- faults
    @app.post("/api/stations/{station_id}/faults", status_code=201)
    async def inject(station_id: str, body: FaultBody):
        return await guarded(get(station_id).inject_fault(body.kind, body.scope, body.params))

    @app.delete("/api/stations/{station_id}/faults/{fault_id}")
    async def clear(station_id: str, fault_id: int):
        return await guarded(get(station_id).clear_fault(fault_id))

    @app.delete("/api/stations/{station_id}/faults")
    async def clear_all(station_id: str):
        return {"cleared": await get(station_id).clear_all_faults()}

    # ---------------------------------------------------------------------------------- messaging
    @app.post("/api/stations/{station_id}/send")
    async def send(station_id: str, body: SendBody):
        res = await get(station_id).send_raw(body.action, body.payload, validate=body.validate_schema)
        return {"sent": res.sent, "ok": res.ok, "status": res.status, "response": res.response, "error": res.error}

    @app.get("/api/stations/{station_id}/templates/{action}")
    async def template(station_id: str, action: str, kind: Literal["message", "command"] = "message"):
        st = get(station_id)
        try:
            return message_template(action, st) if kind == "message" else command_template(action, st)
        except KeyError:
            raise HTTPException(404, f"No {kind} named '{action}'")

    @app.get("/api/stations/{station_id}/rules")
    async def rules(station_id: str):
        return get(station_id).rules.snapshot()

    @app.post("/api/stations/{station_id}/rules", status_code=201)
    async def add_rule(station_id: str, body: RuleBody):
        st = get(station_id)
        rule = st.rules.add(body.direction, body.action, body.effect, body.params, body.times, body.probability).public()
        st.changed()
        return rule

    @app.delete("/api/stations/{station_id}/rules/{rule_id}", status_code=204)
    async def remove_rule(station_id: str, rule_id: int):
        st = get(station_id)
        if not st.rules.remove(rule_id):
            raise HTTPException(404, "Unknown rule")
        st.changed()

    @app.delete("/api/stations/{station_id}/rules", status_code=204)
    async def clear_rules(station_id: str):
        st = get(station_id)
        st.rules.clear()
        st.changed()

    # ------------------------------------------------------------------------------------ scenarios
    @app.post("/api/stations/{station_id}/scenarios/stop")
    async def stop_scenario(station_id: str):
        get(station_id).runner.stop()
        return {"ok": True}

    @app.post("/api/stations/{station_id}/scenarios/custom", status_code=202)
    async def run_custom(station_id: str, body: CustomScenario):
        return (await guarded(_start(get(station_id), "custom", body.title, body.steps))).public()

    @app.post("/api/stations/{station_id}/scenarios/{scenario_id}/run", status_code=202)
    async def run_scenario(station_id: str, scenario_id: str):
        sc = CATALOG_SCENARIOS.get(scenario_id)
        if sc is None:
            raise HTTPException(404, "Unknown scenario")
        return (await guarded(_start(get(station_id), scenario_id, sc["title"], [{"do": "reset", "label": "Start from a clean station"}, *sc["steps"]]))).public()

    async def _start(st: Station, sid: str, title: str, steps: list[dict]):
        return st.runner.start(sid, title, steps)

    @app.get("/api/stations/{station_id}/scenarios/runs")
    async def runs(station_id: str):
        return [r.public() for r in reversed(get(station_id).runner.history)]

    # ------------------------------------------------------------------------------------ inspection
    @app.get("/api/stations/{station_id}/log")
    async def log(station_id: str, after_id: int = 0, limit: int = Query(500, le=5000)):
        get(station_id)
        entries = hub.history(station_id, after_id, limit)
        return {"entries": entries, "last_id": entries[-1]["id"] if entries else after_id}

    @app.delete("/api/stations/{station_id}/log", status_code=204)
    async def clear_log(station_id: str):
        get(station_id)
        hub.logs[station_id].clear()

    @app.get("/api/stations/{station_id}/log/export")
    async def export_log(station_id: str):
        st = get(station_id)
        body = json.dumps(list(hub.logs[station_id]), indent=2)
        return Response(body, media_type="application/json", headers={"Content-Disposition": f'attachment; filename="{st.cfg.ocpp_id}-ocpp-log.json"'})

    @app.get("/api/stations/{station_id}/variables")
    async def variables(station_id: str):
        return get(station_id).model.snapshot()

    @app.put("/api/stations/{station_id}/variables")
    async def set_variable(station_id: str, body: VariableBody):
        st = get(station_id)
        comp = {"name": body.component, **({"evse": {"id": body.evse_id}} if body.evse_id is not None else {})}
        status, _ = st.model.write(comp, {"name": body.variable}, body.value)
        if status not in ("Accepted", "RebootRequired"):
            raise HTTPException(422, f"Cannot set {body.component}.{body.variable}: {status}")
        st.changed()
        return {"status": status}

    # --------------------------------------------------------------------------------- live updates
    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        queue = hub.subscribe()
        try:
            await socket.send_json({"type": "init", "stations": [s.snapshot() for s in stations.values()]})

            async def pump():
                while True:
                    await socket.send_json(await queue.get())

            sender = asyncio.create_task(pump())
            try:
                while True:
                    await socket.receive_text()  # keep-alive pings from the browser
            finally:
                sender.cancel()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.unsubscribe(queue)

    if UI_DIST.exists():
        app.mount("/", StaticFiles(directory=UI_DIST, html=True), name="ui")
    return app


app = create_app()
