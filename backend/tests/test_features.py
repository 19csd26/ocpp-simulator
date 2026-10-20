"""Features taken from the reference simulators: auto charge, smart plug, V2G, identity, finishing mode, meter register,
local list editing, reboot reasons, simulating CSMS commands and raw frames. Each runs on OCPP 1.6 and 2.0.1."""
import asyncio
import json

import pytest

from ocpp_simulator.config import PRESETS, StationConfig
from ocpp_simulator.hub import Hub
from ocpp_simulator.mock_csms import MockCsms
from ocpp_simulator.station import Station
from tests.conftest import wait_until
from tests.test_scenarios import failures, run_scenario

VERSIONS = ["1.6", "2.0.1"]


@pytest.fixture
async def make(csms):
    created: list[Station] = []

    async def factory(version="2.0.1", preset="ac_22_dual", ocpp_id="FT1", *, server=None, connect=True, **overrides) -> Station:
        server = server or csms
        cfg = StationConfig(**{**PRESETS[preset], "ocpp_id": ocpp_id, "csms_url": server.url, "time_scale": 60, "ocpp_version": version, **overrides})
        st = Station(cfg, Hub(), response_timeout=3)
        st.model.write({"name": "SampledDataCtrlr"}, {"name": "TxUpdatedInterval"}, "1")
        created.append(st)
        if connect:
            await st.connect()
            await wait_until(lambda: st.online, message="station online")
            await wait_until(lambda: all(c.status for e in st.evses for c in e.connectors), message="statuses reported")
        return st

    yield factory
    for st in created:
        await st.shutdown()


def statuses(csms, cp="FT1"):
    if csms.connections.get(cp) is not None and csms.connections[cp].version == "1.6":
        return [(m["connector_id"], m["status"]) for m in csms.messages("StatusNotification", cp)]
    return [(m["evse_id"], m["connector_status"]) for m in csms.messages("StatusNotification", cp)]


async def tx_started(csms, version, cp="FT1"):
    if version == "1.6":
        return (await csms.wait_for("StartTransaction", cp_id=cp))[-1]
    return (await csms.wait_for("TransactionEvent", cp_id=cp, predicate=lambda m: m["event_type"] == "Started"))[-1]


# ------------------------------------------------------------------------------------------------------ identity
@pytest.mark.parametrize("version", VERSIONS)
async def test_the_identity_fields_go_into_the_boot_notification(make, csms, version):
    await make(version, vendor="Acme", model="X-9000", serial_number="SER-77", firmware_version="9.8.7", iccid="8900111", imsi="404100",
               charge_box_serial_number="BOX-5", meter_type="EM-3", meter_serial_number="MET-12")
    boot = csms.messages("BootNotification", "FT1")[0]
    if version == "1.6":
        assert (boot["charge_point_vendor"], boot["charge_point_model"], boot["charge_point_serial_number"], boot["firmware_version"]) == ("Acme", "X-9000", "SER-77", "9.8.7")
        assert (boot["iccid"], boot["imsi"], boot["charge_box_serial_number"], boot["meter_type"], boot["meter_serial_number"]) == ("8900111", "404100", "BOX-5", "EM-3", "MET-12")
    else:
        cs = boot["charging_station"]
        assert (cs["vendor_name"], cs["model"], cs["serial_number"], cs["firmware_version"]) == ("Acme", "X-9000", "SER-77", "9.8.7")
        assert cs["modem"] == {"iccid": "8900111", "imsi": "404100"}


# ------------------------------------------------------------------------------------------------ auto charge
@pytest.mark.parametrize("version", VERSIONS)
async def test_auto_charge_starts_a_session_with_the_vehicle_id_and_no_card(make, csms, version):
    st = await make(version, auto_charge=True, vehicle_id="VID:ABC123")
    await st.plug_in(1, 1, "mid")  # nobody taps a card
    started = await tx_started(csms, version)
    token = started["id_tag"] if version == "1.6" else started["id_token"]["id_token"]
    assert token == "VID:ABC123"
    auth = csms.messages("Authorize", "FT1")[0]
    assert (auth["id_tag"] if version == "1.6" else auth["id_token"]["id_token"]) == "VID:ABC123"
    if version != "1.6":
        assert auth["id_token"]["type"] == "eMAID"


async def test_auto_charge_is_refused_when_the_csms_does_not_know_the_vehicle(make):
    strict = MockCsms(allowed_tokens={"CARD1"})
    await strict.start()
    try:
        st = await make("2.0.1", server=strict, auto_charge=True, vehicle_id="VID:UNKNOWN")
        await st.plug_in(1, 1, "mid")
        await strict.wait_for("Authorize", cp_id="FT1")
        await asyncio.sleep(1)
        assert st.evses[0].tx is None and strict.messages("TransactionEvent", "FT1") == []
    finally:
        await strict.stop()


async def test_auto_charge_off_means_a_card_is_needed(make, csms):
    st = await make("2.0.1")
    await st.plug_in(1, 1, "mid")
    await asyncio.sleep(1.5)
    assert st.evses[0].tx is None and csms.messages("Authorize", "FT1") == []


# --------------------------------------------------------------------------------------------------- smart plug
@pytest.mark.parametrize("version", VERSIONS)
async def test_smart_plug_stays_available_until_a_remote_start_then_charges_without_a_cable(make, csms, version):
    st = await make(version, smart_plug=True)
    remote = {"idTag": "REMOTE1", "connectorId": 1} if version == "1.6" else {"idToken": {"idToken": "REMOTE1", "type": "Central"}, "remoteStartId": 1, "evseId": 1}
    result = await csms.command("FT1", "RemoteStartTransaction" if version == "1.6" else "RequestStartTransaction", remote)
    assert result["status"] == "Accepted"
    await tx_started(csms, version)
    await wait_until(lambda: st.evses[0].tx is not None and st.evses[0].tx.charging_state == "Charging", message="charging")
    await wait_until(lambda: (1, "Charging" if version == "1.6" else "Occupied") in statuses(csms), message="the session shows in the status")
    seen = statuses(csms)
    assert (1, "Preparing") not in seen and (1, "Finishing") not in seen  # a smart plug never reports a cable
    await st.stop_session(1)
    await wait_until(lambda: statuses(csms)[-1:] == [(1, "Available")] or (1, "Available") in statuses(csms)[-3:], message="Available after the session")
    assert st.evses[0].plugged is None and st.evses[0].tx is None


async def test_smart_plug_ignores_a_card_because_nothing_is_connected(make, csms):
    st = await make("1.6", smart_plug=True)
    res = await st.tap_card(1, "CARD1")
    await asyncio.sleep(1)
    assert st.evses[0].tx is None and csms.messages("StartTransaction", "FT1") == []
    assert res["status"] in ("Accepted", "Invalid", "Unavailable")


# ------------------------------------------------------------------------------------------- finishing mode
@pytest.mark.parametrize("finishing_mode,after", [(True, "Finishing"), (False, "Preparing")])
async def test_finishing_mode_chooses_the_status_after_a_session_with_the_cable_still_in(make, csms, finishing_mode, after):
    st = await make("1.6", finishing_mode=finishing_mode)
    await st.plug_in(1, 1, "mid")
    await st.tap_card(1, "CARD1")
    await tx_started(csms, "1.6")
    n = len(statuses(csms))
    await st.stop_session(1)
    await wait_until(lambda: (1, after) in statuses(csms)[n:], message=f"{after} after the stop")
    assert (1, "Finishing" if after == "Preparing" else "Preparing") not in statuses(csms)[n:]


# -------------------------------------------------------------------------------------------- measurands
@pytest.mark.parametrize("version", ["1.6", "2.0.1"])
async def test_temperature_power_factor_and_export_measurands_can_be_reported(make, csms, version):
    st = await make(version)
    st.model.write({"name": "SampledDataCtrlr"}, {"name": "TxUpdatedMeasurands"},
                   "Energy.Active.Import.Register,Temperature,Power.Factor,Energy.Active.Export.Register,Power.Active.Export,Current.Export")
    await st.plug_in(1, 1, "mid")
    await st.tap_card(1, "CARD1")
    await tx_started(csms, version)
    await wait_until(lambda: st.evses[0].tx.energy_wh > 5, message="energy")
    if version == "1.6":
        frames = await csms.wait_for("MeterValues", cp_id="FT1", predicate=lambda m: any(s["measurand"] == "Power.Factor" for s in m["meter_value"][0]["sampled_value"]))
        samples = {s["measurand"]: s for mv in frames[-1]["meter_value"] for s in mv["sampled_value"]}
        assert float(samples["Temperature"]["value"]) > 20 and samples["Temperature"]["unit"] == "Celsius"  # a measurand in OCPP 1.6
    else:
        frames = await csms.wait_for("TransactionEvent", cp_id="FT1", predicate=lambda m: any(
            s["measurand"] == "Power.Factor" for mv in m.get("meter_value") or [] for s in mv["sampled_value"]))
        samples = {s["measurand"]: s for mv in frames[-1]["meter_value"] for s in mv["sampled_value"]}
        assert "Temperature" not in samples  # 2.0.1: not a measurand, so it is never sent as one (the CSMS would reject the message)
    assert float(samples["Power.Factor"]["value"]) == 1.0 and "unit" not in samples["Power.Factor"] and "unit_of_measure" not in samples["Power.Factor"]
    assert float(samples["Power.Active.Export"]["value"]) == 0  # charging, not discharging


async def test_the_gun_gets_warmer_while_charging(make):
    st = await make("2.0.1", preset="dc_150")
    start = st.evses[0].temperature
    await st.plug_in(1, 1, "large", 20)
    await st.tap_card(1, "CARD1")
    await wait_until(lambda: st.evses[0].temperature > start + 5, timeout=20, message="the connector warms up")


# ------------------------------------------------------------------------------------------ V2G discharge
@pytest.mark.parametrize("version", ["1.6", "2.0.1"])
async def test_v2g_discharge_feeds_energy_back_and_stops_at_the_battery_floor(make, csms, version):
    st = await make(version, preset="dc_150")
    st.model.write({"name": "SampledDataCtrlr"}, {"name": "TxUpdatedMeasurands"}, "Energy.Active.Import.Register,Energy.Active.Export.Register,Power.Active.Export,SoC")
    await st.plug_in(1, 1, "large", 14, 100)
    await st.tap_card(1, "CARD1")
    await wait_until(lambda: st.evses[0].tx is not None and st.evses[0].tx.charging_state == "Charging", message="charging")
    imported = st.evses[0].register_wh
    await st.set_v2x(1, "discharge", 20)
    await wait_until(lambda: st.evses[0].export_power_w == 20000, message="20 kW flowing back")
    assert st.evses[0].power_w == 0 and st.evses[0].register_wh == imported  # nothing is imported while exporting
    export = st.evses[0].export_register_wh
    await wait_until(lambda: st.evses[0].export_register_wh > export + 20, message="export register grows")
    await wait_until(lambda: st.evses[0].v2x == "charge", timeout=60, message="the battery floor ends the discharge")
    assert 9.5 <= st.evses[0].ev.soc <= 11
    await wait_until(lambda: st.evses[0].power_w > 0, message="charging resumes")


async def test_v2g_rules(make):
    st = await make("2.0.1", preset="dc_150")
    with pytest.raises(ValueError, match="needs a vehicle"):
        await st.set_v2x(1, "discharge", 5)
    await st.plug_in(1, 1, "city", 80)  # a city EV cannot discharge
    await st.tap_card(1, "CARD1")
    await wait_until(lambda: st.evses[0].tx is not None, message="session")
    with pytest.raises(ValueError, match="not a bidirectional"):
        await st.set_v2x(1, "discharge", 5)
    with pytest.raises(ValueError, match="'charge' or 'discharge'"):
        await st.set_v2x(1, "sideways")


# -------------------------------------------------------------------------------------------- meter register
@pytest.mark.parametrize("version", VERSIONS)
async def test_the_meter_register_can_be_set_by_hand_and_the_csms_sees_it(make, csms, version):
    st = await make(version)
    await st.set_meter_register(1, import_kwh=555.5)
    assert st.evses[0].register_wh == 555_500
    await st.plug_in(1, 1, "mid")
    await st.tap_card(1, "CARD1")
    started = await tx_started(csms, version)
    if version == "1.6":
        assert started["meter_start"] == 555_500
    else:
        sample = started["meter_value"][0]["sampled_value"][0]
        assert sample["measurand"] == "Energy.Active.Import.Register" and sample["value"] == 555_500
    with pytest.raises(ValueError):
        await st.set_meter_register(1, import_kwh=-1)


# ------------------------------------------------------------------------------------------------ local list
async def test_the_local_list_can_be_edited_by_hand_and_bumps_its_version(make, csms):
    st = await make("2.0.1")
    assert st.local_list_add("TOKEN-A")["version"] == 1
    assert st.local_list_add("TOKEN-B", "Blocked")["version"] == 2
    assert {e["token"]: e["status"] for e in st.snapshot()["local_list"]["entries"]} == {"TOKEN-A": "Accepted", "TOKEN-B": "Blocked"}
    assert (await csms.command("FT1", "GetLocalListVersion", {}))["versionNumber"] == 2
    assert st.model.find("LocalAuthListCtrlr", "Entries").value == "2"
    assert st.local_list_remove("TOKEN-A")["version"] == 3
    with pytest.raises(ValueError, match="not in the local list"):
        st.local_list_remove("NOPE")
    with pytest.raises(ValueError):
        st.local_list_add("x" * 40)
    with pytest.raises(ValueError):
        st.local_list_add("OK", "Nonsense")


# ----------------------------------------------------------------------------------------------- reboot reason
@pytest.mark.parametrize("version", ["2.0.1"])
async def test_reboot_with_a_chosen_boot_reason(make, csms, version):
    st = await make(version)
    await st.reboot_with("Watchdog")
    boots = await csms.wait_for("BootNotification", cp_id="FT1", count=2, timeout=20)
    assert boots[-1]["reason"] == "Watchdog"
    with pytest.raises(ValueError, match="Unknown boot reason"):
        await st.reboot_with("Whenever")


async def test_reboot_on_1_6_boots_again(make, csms):
    st = await make("1.6")
    await st.reboot_with("RemoteReset")
    await csms.wait_for("BootNotification", cp_id="FT1", count=2, timeout=20)


# ------------------------------------------------------------------------------------ simulate a CSMS command
@pytest.mark.parametrize("version,action,payload,key", [
    ("2.0.1", "GetVariables", {"getVariableData": [{"component": {"name": "OCPPCommCtrlr"}, "variable": {"name": "HeartbeatInterval"}}]}, "getVariableResult"),
    ("2.0.1", "GetBaseReport", {"requestId": 1, "reportBase": "SummaryInventory"}, "status"),
    ("1.6", "GetConfiguration", {"key": ["HeartbeatInterval"]}, "configurationKey"),
    ("1.6", "ChangeConfiguration", {"key": "HeartbeatInterval", "value": "44"}, "status"),
])
async def test_a_command_can_be_simulated_without_a_csms(make, version, action, payload, key):
    st = await make(version, connect=False)  # not even connected
    body = await st.simulate_command(action, payload)
    assert key in body
    if action == "ChangeConfiguration":
        assert body["status"] == "Accepted" and st.model.get_int("OCPPCommCtrlr", "HeartbeatInterval") == 44


async def test_a_simulated_command_shows_up_in_the_log_and_can_start_a_session(make, csms):
    st = await make("2.0.1")
    await st.plug_in(1, 1, "mid")
    body = await st.simulate_command("RequestStartTransaction", {"idToken": {"idToken": "LOCALREMOTE", "type": "Central"}, "remoteStartId": 7, "evseId": 1})
    assert body["status"] == "Accepted"
    await wait_until(lambda: st.evses[0].tx is not None, message="session from the simulated remote start")
    log = [e for e in st.hub.logs[st.cfg.id] if "simulated" in (e.get("note") or "")]
    assert [e["dir"] for e in log] == ["in", "out"] and log[0]["action"] == "RequestStartTransaction"
    with pytest.raises(ValueError, match="not a command"):
        await st.simulate_command("BootNotification", {})
    with pytest.raises(ValueError, match="not a command"):
        await st.simulate_command("GetConfiguration", {})  # a 1.6 command: this is a 2.0.1 charger


# ------------------------------------------------------------------------------------------------- raw frames
async def test_a_raw_frame_is_sent_as_given_even_when_malformed(make, csms):
    st = await make("2.0.1")
    await st.send_raw_frame(json.dumps([2, "raw-1", "Heartbeat", {}]))
    await csms.wait_for("Heartbeat", cp_id="FT1")
    await st.send_raw_frame('[2, "raw-2", "Heartbeat", {')  # truncated JSON: the CSMS has to cope
    await st.send_raw_frame(json.dumps([4, "raw-3", "InternalError", "made up", {}]))
    await asyncio.sleep(0.5)
    assert st.online  # nothing broke the connection on our side
    with pytest.raises(ValueError, match="Nothing to send"):
        await st.send_raw_frame("  ")
    await st.disconnect()
    with pytest.raises(ValueError, match="not connected"):
        await st.send_raw_frame("[]")


# ------------------------------------------------------------------------------------------------- scenarios
@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("sid", ["auto_charge", "smart_plug_remote_start", "v2g_discharge"])
async def test_the_new_scenarios_pass_on_every_version(make, csms, version, sid):
    st = await make(version, preset="dc_60_dual", time_scale=60)
    run = await run_scenario(st, sid)
    assert run.status == "passed", failures(run)
    assert st.cfg.auto_charge is False and st.cfg.smart_plug is False  # temporary settings are put back


# ----------------------------------------------------------------- defaults stay exactly as they were
async def test_a_default_1_6_boot_notification_carries_no_extra_fields(make, csms):
    await make("1.6")
    boot = csms.messages("BootNotification", "FT1")[0]
    assert set(boot) <= {"charge_point_vendor", "charge_point_model", "charge_point_serial_number", "firmware_version", "iccid", "imsi"}


@pytest.mark.parametrize("version", ["1.6", "2.0.1"])
async def test_default_behaviour_of_1_6_and_2_0_1_is_unchanged(make, csms, version):
    """No auto charge, no smart plug, Finishing after a session: the original behaviour until you switch something on."""
    st = await make(version)
    assert (st.cfg.auto_charge, st.cfg.smart_plug, st.cfg.finishing_mode) == (False, False, True)
    await st.plug_in(1, 1, "mid")
    await asyncio.sleep(1.2)
    assert st.evses[0].tx is None  # still waiting for a card
    await st.tap_card(1, "CARD1")
    await tx_started(csms, version)
    await st.stop_session(1)
    if version == "1.6":
        await wait_until(lambda: (1, "Finishing") in statuses(csms), message="Finishing after the session")


# --------------------------------------------------------------- the cards a charger uses (your CSMS's own cards)
@pytest.mark.parametrize("version", VERSIONS)
async def test_scenarios_use_the_cards_set_on_the_charger(make, version):
    """A CSMS that only knows MYCARD / MYCARD2 (and refuses the old defaults CARD1 / CARD2) still passes the session scenarios."""
    strict = MockCsms(allowed_tokens={"MYCARD", "MYCARD2"})
    await strict.start()
    try:
        st = await make(version, server=strict, card_id="MYCARD", card_id_2="MYCARD2")
        for sid in ("rfid_session", "two_sessions"):
            run = await run_scenario(st, sid)
            assert run.status == "passed", (sid, failures(run))
        sent = [m["id_tag"] if version == "1.6" else m["id_token"]["id_token"] for m in strict.messages("Authorize", "FT1")]
        assert set(sent) == {"MYCARD", "MYCARD2"}  # never the hard-coded CARD1 / CARD2
    finally:
        await strict.stop()


async def test_a_refused_card_is_named_in_the_scenario_failure(make):
    strict = MockCsms(allowed_tokens={"ONLYTHIS"})
    await strict.start()
    try:
        st = await make("2.0.1", server=strict)  # default cards CARD1 / CARD2: the CSMS does not know them
        run = await run_scenario(st, "rfid_session")
        assert run.status == "failed"
        message = " ".join(d for _, d in failures(run))
        assert "refused the card 'CARD1'" in message and "Invalid" in message and "Cards used by tests" in message
    finally:
        await strict.stop()


async def test_the_unknown_token_scenario_still_passes_and_does_not_use_the_charger_cards(make):
    strict = MockCsms(allowed_tokens={"MYCARD"})
    await strict.start()
    try:
        st = await make("1.6", server=strict, card_id="MYCARD")
        run = await run_scenario(st, "unknown_token")
        assert run.status == "passed", failures(run)
    finally:
        await strict.stop()


async def test_templates_carry_the_configured_card(make, csms):
    st = await make("2.0.1", card_id="TPL-CARD")
    assert __import__("ocpp_simulator.templates", fromlist=["x"]).message_template("Authorize", st)["idToken"]["idToken"] == "TPL-CARD"
    st16 = await make("1.6", ocpp_id="FT16", card_id="TPL-CARD-16-LONGER-THAN-TWENTY")
    assert __import__("ocpp_simulator.templates", fromlist=["x"]).message_template16("Authorize", st16)["idTag"] == "TPL-CARD-16-LONGER-T"[:20]
