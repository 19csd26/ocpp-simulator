"""The HTTP / WebSocket API used by the UI, driven through a real server and the mock CSMS."""
import asyncio
import json

import httpx
import pytest
import uvicorn

from ocpp_simulator.main import create_app
from tests.conftest import wait_until


@pytest.fixture
async def api(csms):
    app = create_app()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
    task = asyncio.create_task(server.serve())
    await wait_until(lambda: server.started, message="api up")
    port = server.servers[0].sockets[0].getsockname()[1]
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=15) as client:
        client.port = port
        client.csms = csms
        yield client
    server.should_exit = True
    await task


def new_station(csms, **kw):
    return {"name": "Bench", "ocpp_id": "API1", "csms_url": csms.url, "preset": "ac_22_dual", "time_scale": 60, **kw}


async def test_meta_describes_everything_the_ui_needs(api):
    meta = (await api.get("/api/meta")).json()
    assert len(meta["station_messages"]) == 25 and len(meta["commands"]) == 40 and len(meta["faults"]) >= 24 and len(meta["scenarios"]) >= 30
    assert {"ac_7", "dc_60_dual"} <= set(meta["presets"]) and "mid" in meta["ev_models"] and "cType2" in meta["connector_types"]
    assert "plug_in" in meta["scenario_steps"]["do"] and "message_sent" in meta["scenario_steps"]["expect"]
    assert (await api.get("/api/scenarios/rfid_session")).json()["steps"]


async def test_add_station_by_url_and_charger_id_then_connect_and_operate(api, csms):
    r = await api.post("/api/stations", json=new_station(csms))
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    snap = r.json()
    assert snap["ocpp_id"] == "API1" and snap["csms_url"] == csms.url and "password" not in snap and len(snap["evses"]) == 2
    await wait_until(lambda: "API1" in csms.connections, message="connected")
    await wait_until(lambda: csms.messages("StatusNotification", "API1"), message="statuses")
    state = {}
    for _ in range(60):
        state = (await api.get(f"/api/stations/{sid}")).json()
        if state["state"] == "online":
            break
        await asyncio.sleep(0.1)
    assert state["state"] == "online" and [c["status"] for e in state["evses"] for c in e["connectors"]] == ["Available", "Available"]

    assert (await api.post(f"/api/stations/{sid}/evse/1/plug-in", json={"ev": "large", "soc": 30})).status_code == 200
    assert (await api.post(f"/api/stations/{sid}/evse/1/tap", json={"token": "CARD9"})).json()["status"] == "Accepted"
    for _ in range(80):
        state = (await api.get(f"/api/stations/{sid}")).json()
        if state["evses"][0]["tx"] and state["evses"][0]["power_kw"] > 10:
            break
        await asyncio.sleep(0.1)
    assert state["evses"][0]["tx"]["charging_state"] == "Charging" and state["evses"][0]["ev"]["label"].startswith("Large")
    assert (await api.post(f"/api/stations/{sid}/evse/1/tap", json={"token": "CARD9"})).json()["status"] == "Stopped"
    assert (await api.post(f"/api/stations/{sid}/evse/1/unplug")).status_code == 200


async def test_validation_errors_are_readable(api, csms):
    assert (await api.post("/api/stations", json=new_station(csms, csms_url="http://nope"))).status_code == 422
    assert (await api.post("/api/stations", json=new_station(csms, ocpp_id="bad id!"))).status_code == 422
    assert (await api.post("/api/stations", json=new_station(csms, preset="nope"))).status_code == 422
    assert (await api.post("/api/stations", json=new_station(csms, security_profile=2))).status_code == 422   # needs wss://
    r = await api.post("/api/stations", json=new_station(csms, connect=False))
    assert r.status_code == 201
    dup = await api.post("/api/stations", json=new_station(csms, connect=False))
    assert dup.status_code == 409 and "already exists" in dup.json()["detail"]
    sid = r.json()["id"]
    bad = await api.post(f"/api/stations/{sid}/evse/9/plug-in", json={})
    assert bad.status_code == 422 and "does not exist" in bad.json()["detail"]
    assert (await api.post(f"/api/stations/{sid}/evse/1/unplug")).status_code == 422
    assert (await api.get("/api/stations/nope")).status_code == 404
    assert (await api.post(f"/api/stations/{sid}/faults", json={"kind": "meteor"})).status_code == 422


async def test_custom_hardware_and_other_urls(api, csms):
    hw = [{"current_type": "DC", "max_power_kw": 350, "voltage": 800, "phases": 3, "connectors": [{"type": "cCCS2"}]}] * 3
    r = await api.post("/api/stations", json=new_station(csms, ocpp_id="BIG", evses=hw, connect=False, model="X-350"))
    assert r.status_code == 201 and len(r.json()["evses"]) == 3 and r.json()["model"] == "X-350" and r.json()["evses"][0]["max_power_kw"] == 350
    assert (await api.post("/api/stations", json=new_station(csms, ocpp_id="BAD", evses=[{"max_power_kw": -5}], connect=False))).status_code == 422
    # pointing at a CSMS that is down just shows an error and keeps retrying
    r = await api.post("/api/stations", json=new_station(csms, ocpp_id="LOST", csms_url="ws://127.0.0.1:1/ocpp"))
    sid = r.json()["id"]
    for _ in range(80):
        s = (await api.get(f"/api/stations/{sid}")).json()
        if s["last_error"]:
            break
        await asyncio.sleep(0.1)
    assert s["last_error"] and s["state"] in ("offline", "connecting")
    assert (await api.delete(f"/api/stations/{sid}")).status_code == 204


async def test_faults_rules_and_messages_over_http(api, csms):
    sid = (await api.post("/api/stations", json=new_station(csms))).json()["id"]
    await wait_until(lambda: csms.messages("StatusNotification", "API1"))
    fault = (await api.post(f"/api/stations/{sid}/faults", json={"kind": "rfid_reader_failure"})).json()
    assert fault["active"] and (await api.get(f"/api/stations/{sid}")).json()["faults"]["active"][0]["id"] == fault["id"]
    assert (await api.delete(f"/api/stations/{sid}/faults/{fault['id']}")).status_code == 200
    assert (await api.delete(f"/api/stations/{sid}/faults/{fault['id']}")).status_code == 422

    rule = (await api.post(f"/api/stations/{sid}/rules", json={"direction": "in", "action": "ClearCache", "effect": "reject"})).json()
    assert (await csms.command("API1", "ClearCache", {}))["status"] == "Rejected"
    assert (await api.delete(f"/api/stations/{sid}/rules/{rule['id']}")).status_code == 204
    assert (await api.post(f"/api/stations/{sid}/rules", json={"direction": "out", "action": "X", "effect": "reject"})).status_code == 422

    tpl = (await api.get(f"/api/stations/{sid}/templates/Heartbeat")).json()
    assert tpl == {}
    sent = (await api.post(f"/api/stations/{sid}/send", json={"action": "Authorize", "payload": (await api.get(f"/api/stations/{sid}/templates/Authorize")).json()})).json()
    assert sent["sent"] and sent["ok"] and sent["status"] == "Accepted"
    bad = (await api.post(f"/api/stations/{sid}/send", json={"action": "Authorize", "payload": {"idToken": {"idToken": "x", "type": "Nope"}}})).json()
    assert not bad["ok"] and bad["error"]
    assert (await api.post(f"/api/stations/{sid}/send", json={"action": "Reset", "payload": {}})).json()["error"].endswith("is not a message a charging station can send in OCPP 2.0.1")
    cmd = (await api.get(f"/api/stations/{sid}/templates/Reset", params={"kind": "command"})).json()
    assert cmd == {"type": "OnIdle"}


async def test_scenarios_log_and_variables_over_http(api, csms):
    sid = (await api.post("/api/stations", json=new_station(csms))).json()["id"]
    await wait_until(lambda: csms.messages("StatusNotification", "API1"))
    run = (await api.post(f"/api/stations/{sid}/scenarios/rfid_session/run")).json()
    assert run["status"] == "running" and (await api.post(f"/api/stations/{sid}/scenarios/rfid_session/run")).status_code == 422
    for _ in range(300):
        runs = (await api.get(f"/api/stations/{sid}/scenarios/runs")).json()
        if runs and runs[0]["status"] != "running":
            break
        await asyncio.sleep(0.2)
    assert runs[0]["status"] == "passed"
    custom = await api.post(f"/api/stations/{sid}/scenarios/custom", json={"title": "mine", "steps": [{"do": "wait", "seconds": 0.1}, {"expect": "online"}]})
    assert custom.status_code == 202
    assert (await api.post(f"/api/stations/{sid}/scenarios/custom", json={"steps": [{"do": "teleport"}]})).status_code in (422,)
    assert (await api.post(f"/api/stations/{sid}/scenarios/nope/run")).status_code == 404

    log = (await api.get(f"/api/stations/{sid}/log")).json()
    assert log["entries"] and {"dir", "kind", "action", "summary", "payload"} <= set(log["entries"][0])
    assert any(e["action"] == "BootNotification" and e["kind"] == "result" for e in log["entries"])      # replies are matched to their request
    newer = (await api.get(f"/api/stations/{sid}/log", params={"after_id": log["last_id"]})).json()
    assert all(e["id"] > log["last_id"] for e in newer["entries"])
    exported = await api.get(f"/api/stations/{sid}/log/export")
    assert "attachment" in exported.headers["content-disposition"] and exported.json()

    variables = (await api.get(f"/api/stations/{sid}/variables")).json()
    assert len(variables) > 100
    assert (await api.put(f"/api/stations/{sid}/variables", json={"component": "OCPPCommCtrlr", "variable": "HeartbeatInterval", "value": "77"})).status_code == 200
    assert (await api.put(f"/api/stations/{sid}/variables", json={"component": "ChargingStation", "variable": "Model", "value": "x"})).status_code == 422
    assert (await api.delete(f"/api/stations/{sid}/log")).status_code == 204


async def test_websocket_streams_state_and_log(api, csms):
    import websockets

    async with websockets.connect(f"ws://127.0.0.1:{api.port}/ws") as ws:
        first = __import__("json").loads(await ws.recv())
        assert first["type"] == "init" and first["stations"] == []
        sid = (await api.post("/api/stations", json=new_station(csms))).json()["id"]
        kinds, deadline = set(), asyncio.get_running_loop().time() + 8
        while asyncio.get_running_loop().time() < deadline and not {"log", "state"} <= kinds:
            ev = __import__("json").loads(await asyncio.wait_for(ws.recv(), 5))
            kinds.add(ev["type"])
        assert {"log", "state"} <= kinds
        await api.delete(f"/api/stations/{sid}")


async def test_ui_sees_rule_changes_and_a_removed_station_stays_removed(api, csms):
    import json

    import websockets

    sid = (await api.post("/api/stations", json=new_station(csms))).json()["id"]
    async with websockets.connect(f"ws://127.0.0.1:{api.port}/ws") as ws:
        await ws.recv()  # init

        async def next_state(predicate, timeout=6):
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                try:
                    ev = json.loads(await asyncio.wait_for(ws.recv(), 1))
                except asyncio.TimeoutError:
                    continue
                if ev["type"] == "state" and ev["station_id"] == sid and predicate(ev["state"]):
                    return ev["state"]
            raise AssertionError("no matching state event")

        rule = (await api.post(f"/api/stations/{sid}/rules", json={"direction": "out", "action": "Heartbeat", "effect": "delay"})).json()
        await next_state(lambda s: [r["id"] for r in s["rules"]] == [rule["id"]])
        await api.delete(f"/api/stations/{sid}/rules/{rule['id']}")
        await next_state(lambda s: s["rules"] == [])

        await api.delete(f"/api/stations/{sid}")
        seen = []
        end = asyncio.get_running_loop().time() + 1.5
        while asyncio.get_running_loop().time() < end:
            try:
                seen.append(json.loads(await asyncio.wait_for(ws.recv(), 0.4)))
            except asyncio.TimeoutError:
                pass
        removed_at = next(i for i, e in enumerate(seen) if e["type"] == "removed" and e["station_id"] == sid)
        assert not [e for e in seen[removed_at:] if e.get("station_id") == sid and e["type"] in ("state", "log")]   # nothing resurrects it


async def test_edit_a_stopped_station_changes_identity_url_and_hardware(api, csms):
    sid = (await api.post("/api/stations", json=new_station(csms, connect=False))).json()["id"]
    r = await api.patch(f"/api/stations/{sid}", json={"name": "Renamed", "ocpp_id": "API2", "preset": "dc_150", "security_profile": 0, "password": "pw"})
    assert r.status_code == 200, r.text
    snap = r.json()
    assert snap["name"] == "Renamed" and snap["ocpp_id"] == "API2" and len(snap["evses"]) == 1
    assert snap["config"]["has_password"] is True and snap["config"]["security_profile"] == 0 and snap["model"] == "SIM-DC-150"
    assert (await api.patch(f"/api/stations/{sid}", json={"password": ""})).json()["config"]["has_password"] is False
    await api.post(f"/api/stations/{sid}/connect")
    await wait_until(lambda: "API2" in csms.connections, message="connects with the new id")


async def test_edit_while_connected_only_allows_harmless_fields(api, csms):
    sid = (await api.post("/api/stations", json=new_station(csms))).json()["id"]
    await wait_until(lambda: "API1" in csms.connections, message="connected")
    ok = await api.patch(f"/api/stations/{sid}", json={"name": "Live rename", "time_scale": 120, "end_on_full": False})
    assert ok.status_code == 200 and ok.json()["name"] == "Live rename" and ok.json()["config"]["end_on_full"] is False
    for body in ({"ocpp_id": "OTHER"}, {"csms_url": csms.url + "/x"}, {"preset": "ac_7"}):
        assert (await api.patch(f"/api/stations/{sid}", json=body)).status_code == 409
    # resending the unchanged values (as the edit form does) is not a change
    same = await api.patch(f"/api/stations/{sid}", json={"ocpp_id": "API1", "csms_url": csms.url, "security_profile": 1})
    assert same.status_code == 200


async def test_edit_validation_and_hardware_guards(api, csms):
    a = (await api.post("/api/stations", json=new_station(csms, connect=False))).json()["id"]
    await api.post("/api/stations", json=new_station(csms, ocpp_id="API9", connect=False))
    assert (await api.patch(f"/api/stations/{a}", json={"ocpp_id": "API9"})).status_code == 409
    assert (await api.patch(f"/api/stations/{a}", json={"csms_url": "http://nope"})).status_code == 422
    assert (await api.patch(f"/api/stations/{a}", json={"security_profile": 2})).status_code == 422
    assert (await api.patch(f"/api/stations/{a}", json={"preset": "nope"})).status_code == 422
    assert (await api.patch(f"/api/stations/{a}", json={"ocpp_id": "bad id!"})).status_code == 422
    await api.post(f"/api/stations/{a}/evse/1/plug-in", json={})
    r = await api.patch(f"/api/stations/{a}", json={"preset": "ac_7"})
    assert r.status_code == 409 and "Unplug" in r.json()["detail"]
    assert (await api.patch(f"/api/stations/{a}", json={"name": "still fine"})).status_code == 200


# --------------------------------------------------------------------------------------------- new endpoints
async def test_signature_about_and_response_header(api):
    r = await api.get("/api/about")
    assert r.status_code == 200 and r.json()["owner"] == "Raghav" and r.json()["github"] == "19csd26" and "All rights reserved" in r.json()["copyright"]
    meta = (await api.get("/api/meta")).json()
    assert meta["about"]["signature"] == r.json()["signature"] and set(meta["versions"]) == {"1.6", "2.0.1"}
    assert (len(meta["versions"]["2.0.1"]["station_messages"]), len(meta["versions"]["2.0.1"]["commands"])) == (25, 40)
    assert (len(meta["versions"]["1.6"]["station_messages"]), len(meta["versions"]["1.6"]["commands"])) == (10, 19)
    assert r.headers["x-created-by"] == r.json()["signature"]


async def test_new_station_endpoints_over_http(api, csms):
    created = await api.post("/api/stations", json=new_station(csms, ocpp_version="auto", vendor="Acme", model="M1", serial_number="SER9", iccid="89001", imsi="40401",
                                                               auto_charge=True, vehicle_id="VID:HTTP", smart_plug=False, finishing_mode=False, preset="dc_150"))
    assert created.status_code == 201, created.text
    snap = created.json()
    sid = snap["id"]
    assert snap["config"]["vendor"] == "Acme" and snap["config"]["serial_number"] == "SER9" and snap["config"]["auto_charge"] is True
    assert snap["config"]["vehicle_id"] == "VID:HTTP" and snap["config"]["finishing_mode"] is False and snap["config"]["ocpp_version"] == "auto"
    await wait_until(lambda: "API1" in csms.connections, message="connected")
    assert (await csms.wait_for("BootNotification", cp_id="API1"))[0]["charging_station"]["vendor_name"] == "Acme"
    st = (await api.get(f"/api/stations/{sid}")).json()
    assert st["version"] == "2.0.1" and st["negotiated"] == "2.0.1"  # auto recognises 1.6 / 2.0.1 and prefers 2.0.1
    # behaviour switches change live, identity does not
    assert (await api.patch(f"/api/stations/{sid}", json={"auto_charge": False, "finishing_mode": True})).status_code == 200
    assert (await api.patch(f"/api/stations/{sid}", json={"vendor": "Other"})).status_code == 409
    assert (await api.patch(f"/api/stations/{sid}", json={"smart_plug": True})).status_code == 409

    assert (await api.post(f"/api/stations/{sid}/local-list", json={"token": "TOK1"})).status_code == 201
    assert [e["token"] for e in (await api.get(f"/api/stations/{sid}")).json()["local_list"]["entries"]] == ["TOK1"]
    assert (await api.delete(f"/api/stations/{sid}/local-list/TOK1")).status_code == 200
    assert (await api.delete(f"/api/stations/{sid}/local-list/NOPE")).status_code == 422

    assert (await api.post(f"/api/stations/{sid}/evse/1/meter", json={"import_kwh": 321.5})).status_code == 200
    assert (await api.get(f"/api/stations/{sid}")).json()["evses"][0]["register_kwh"] == 321.5
    assert (await api.post(f"/api/stations/{sid}/evse/1/v2x", json={"mode": "discharge", "power_kw": 5})).status_code == 422  # no session yet
    assert (await api.post(f"/api/stations/{sid}/evse/1/plug-in", json={"ev": "large"})).status_code == 200
    assert (await api.post(f"/api/stations/{sid}/evse/1/tap", json={"token": "CARD1", "type": "eMAID"})).json()["status"] == "Accepted"
    await wait_until(lambda: csms.messages("TransactionEvent", "API1"), message="session")
    assert (await api.post(f"/api/stations/{sid}/evse/1/v2x", json={"mode": "discharge", "power_kw": 5})).json()["mode"] == "discharge"
    assert (await api.post(f"/api/stations/{sid}/evse/1/v2x", json={"mode": "charge"})).json()["mode"] == "charge"

    sim = await api.post(f"/api/stations/{sid}/inject", json={"action": "GetLocalListVersion", "payload": {}})
    assert sim.status_code == 200 and "versionNumber" in sim.json()
    assert (await api.post(f"/api/stations/{sid}/inject", json={"action": "GetConfiguration", "payload": {}})).status_code == 422  # a 1.6 command
    assert (await api.post(f"/api/stations/{sid}/inject", json={"action": "BootNotification", "payload": {}})).status_code == 422
    template = (await api.get(f"/api/stations/{sid}/templates/RequestStartTransaction", params={"kind": "command"})).json()
    assert "idToken" in template and (await api.get(f"/api/stations/{sid}/templates/NotifyEvent")).json()["eventData"]

    assert (await api.post(f"/api/stations/{sid}/raw", json={"frame": json.dumps([2, "r1", "Heartbeat", {}])})).status_code == 200
    await csms.wait_for("Heartbeat", cp_id="API1")
    assert (await api.post(f"/api/stations/{sid}/reboot", json={"reason": "Nope"})).status_code == 422
    assert (await api.post(f"/api/stations/{sid}/reboot", json={"reason": "LocalReset"})).json()["reason"] == "LocalReset"
    await csms.wait_for("BootNotification", cp_id="API1", count=2, timeout=20)
