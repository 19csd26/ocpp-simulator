"""OCPP 1.6J: negotiation, the connector/status model, sessions, offline queue, faults and every CSMS command."""
import asyncio

import pytest

from ocpp_simulator.config import PRESETS, StationConfig
from ocpp_simulator.hub import Hub
from ocpp_simulator.mock_csms import MockCsms
from ocpp_simulator.station import Station
from tests.conftest import wait_until


def statuses(csms, cp="SIM1"):
    return [(m["connector_id"], m["status"], m["error_code"]) for m in csms.messages("StatusNotification", cp)]


@pytest.fixture
async def make16(csms):
    created: list[Station] = []

    async def factory(preset="ac_22_dual", ocpp_id="SIM1", *, version="1.6", server=None, connect=True, **overrides) -> Station:
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


async def test_1_6_station_boots_and_reports_every_connector_available(make16, csms):
    st = await make16()
    assert st.version == "1.6" and csms.connections["SIM1"].version == "1.6"
    boot = csms.messages("BootNotification", "SIM1")[0]
    assert boot["charge_point_vendor"] == "SimCharge" and boot["charge_point_model"]
    await wait_until(lambda: len(statuses(csms)) >= 3, message="status notifications")
    assert sorted((c, s) for c, s, _ in statuses(csms)) == [(0, "Available"), (1, "Available"), (2, "Available")]  # connector 0 = the station


async def test_1_6_session_start_meter_values_stop_and_finishing(make16, csms):
    st = await make16()
    await st.plug_in(1, 1, "mid", 20, 100)
    await wait_until(lambda: (1, "Preparing", "NoError") in statuses(csms), message="Preparing")
    await st.tap_card(1, "CARD1")
    start = (await csms.wait_for("StartTransaction", cp_id="SIM1"))[0]
    assert start["connector_id"] == 1 and start["id_tag"] == "CARD1" and isinstance(start["meter_start"], int)
    await wait_until(lambda: (1, "Charging", "NoError") in statuses(csms), message="Charging")
    assert st.evses[0].tx is not None and st.tx16[st.evses[0].tx.id] == 1
    mv = (await csms.wait_for("MeterValues", cp_id="SIM1", predicate=lambda m: m.get("transaction_id") == 1))[0]
    assert mv["connector_id"] == 1
    values = {s["measurand"]: s for s in mv["meter_value"][0]["sampled_value"]}
    assert float(values["Energy.Active.Import.Register"]["value"]) >= start["meter_start"] and values["Energy.Active.Import.Register"]["unit"] == "Wh"
    await st.stop_session(1)
    stop = (await csms.wait_for("StopTransaction", cp_id="SIM1"))[0]
    assert stop["transaction_id"] == 1 and stop["reason"] == "Local" and stop["meter_stop"] >= start["meter_start"]
    await wait_until(lambda: (1, "Finishing", "NoError") in statuses(csms), message="Finishing while the cable is plugged")
    await st.unplug(1)
    await wait_until(lambda: statuses(csms).count((1, "Available", "NoError")) >= 2, message="Available again")


# ----------------------------------------------------------------------------------------------- negotiation
async def test_auto_uses_whatever_the_csms_speaks(make16):
    only201, only16 = MockCsms(versions=("2.0.1",)), MockCsms(versions=("1.6",))
    await only201.start()
    await only16.start()
    try:
        a = await make16(ocpp_id="A1", version="auto", server=only201)
        b = await make16(ocpp_id="B1", version="auto", server=only16)
        assert a.version == "2.0.1" and a.link.__class__.__name__ == "Link"
        assert b.version == "1.6" and b.link.__class__.__name__ == "Link16"
        assert only201.messages("TransactionEvent") == [] and only16.messages("StartTransaction") == []
    finally:
        await only201.stop()
        await only16.stop()


async def test_a_1_6_only_station_is_refused_by_a_2_0_1_only_csms_with_a_clear_message(make16):
    old = MockCsms(versions=("2.0.1",))
    await old.start()
    try:
        st = await make16(ocpp_id="C1", version="1.6", server=old, connect=False)
        await st.connect()
        await wait_until(lambda: st.last_error, message="a connection error")
        assert not st.online and st.last_error
        await st.disconnect()
    finally:
        await old.stop()


async def test_the_version_can_be_chosen_per_station_on_one_csms(make16, csms):
    a = await make16(ocpp_id="V201", version="2.0.1")
    b = await make16(ocpp_id="V16", version="1.6")
    assert (a.version, b.version) == ("2.0.1", "1.6")
    assert csms.connections["V201"].version == "2.0.1" and csms.connections["V16"].version == "1.6"


# ----------------------------------------------------------------------------------------- authorisation and numbering
async def test_authorize_uses_id_tag_and_a_refused_card_does_not_start(make16):
    server = MockCsms(allowed_tokens={"GOOD"})
    await server.start()
    try:
        st = await make16(server=server)
        await st.plug_in(1)
        assert (await st.tap_card(1, "BAD"))["status"] == "Invalid"
        assert server.messages("Authorize", "SIM1")[0]["id_tag"] == "BAD" and server.messages("StartTransaction") == []
        assert (await st.tap_card(1, "GOOD"))["status"] == "Accepted"
        await server.wait_for("StartTransaction", cp_id="SIM1")
    finally:
        await server.stop()


async def test_connectors_are_numbered_across_the_whole_charge_point(make16, csms):
    st = await make16("mixed_hub")
    await wait_until(lambda: len(statuses(csms)) >= 4, message="statuses")
    assert sorted(c for c, _, _ in statuses(csms)) == [0, 1, 2, 3]
    await st.plug_in(3, 1)
    await st.tap_card(3, "CARD1")
    assert (await csms.wait_for("StartTransaction", cp_id="SIM1"))[0]["connector_id"] == 3


async def test_a_dual_connector_evse_reports_the_unused_connector_unavailable(make16, csms):
    st = await make16("dc_60_dual")  # EVSE 1 has CCS + CHAdeMO (1.6 connectors 1 and 2), EVSE 2 has CCS (connector 3)
    await st.plug_in(1, 1)
    await wait_until(lambda: (2, "Unavailable", "NoError") in statuses(csms) and (1, "Preparing", "NoError") in statuses(csms), message="2 unavailable")


# --------------------------------------------------------------------------------------------------- remote control
async def test_remote_start_and_remote_stop_use_the_integer_transaction_id(make16, csms):
    st = await make16()
    await st.plug_in(2, 1)
    r = await csms.command("SIM1", "RemoteStartTransaction", {"idTag": "REMOTE1", "connectorId": 2})
    assert r["status"] == "Accepted"
    start = (await csms.wait_for("StartTransaction", cp_id="SIM1"))[0]
    assert start["id_tag"] == "REMOTE1" and start["connector_id"] == 2
    assert (await csms.command("SIM1", "RemoteStopTransaction", {"transactionId": 999}))["status"] == "Rejected"
    assert (await csms.command("SIM1", "RemoteStopTransaction", {"transactionId": 1}))["status"] == "Accepted"
    stop = (await csms.wait_for("StopTransaction", cp_id="SIM1"))[0]
    assert stop["transaction_id"] == 1 and stop["reason"] == "Remote"


async def test_reset_reboots_and_ends_the_session_with_the_reset_reason(make16, csms):
    st = await make16()
    await st.plug_in(1)
    await st.tap_card(1, "CARD1")
    await csms.wait_for("StartTransaction", cp_id="SIM1")
    assert (await csms.command("SIM1", "Reset", {"type": "Soft"}))["status"] == "Accepted"
    assert (await csms.wait_for("StopTransaction", cp_id="SIM1"))[0]["reason"] == "SoftReset"
    await csms.wait_for("BootNotification", cp_id="SIM1", count=2, timeout=20)


# ------------------------------------------------------------------------------------------------------ configuration
async def test_get_and_change_configuration_drive_the_real_behaviour(make16, csms):
    st = await make16()
    everything = await csms.command("SIM1", "GetConfiguration", {})
    keys = {k["key"]: k for k in everything["configurationKey"]}
    assert {"HeartbeatInterval", "MeterValueSampleInterval", "NumberOfConnectors", "SupportedFeatureProfiles"} <= keys.keys()
    assert keys["NumberOfConnectors"]["readonly"] is True and keys["NumberOfConnectors"]["value"] == "2"
    some = await csms.command("SIM1", "GetConfiguration", {"key": ["HeartbeatInterval", "Nope"]})
    assert [k["key"] for k in some["configurationKey"]] == ["HeartbeatInterval"] and some["unknownKey"] == ["Nope"]
    assert (await csms.command("SIM1", "ChangeConfiguration", {"key": "HeartbeatInterval", "value": "7"}))["status"] == "Accepted"
    assert st.model.get_int("OCPPCommCtrlr", "HeartbeatInterval") == 7
    assert (await csms.command("SIM1", "ChangeConfiguration", {"key": "NumberOfConnectors", "value": "9"}))["status"] == "Rejected"
    assert (await csms.command("SIM1", "ChangeConfiguration", {"key": "NoSuchKey", "value": "1"}))["status"] == "NotSupported"
    assert (await csms.command("SIM1", "ChangeConfiguration", {"key": "HeartbeatInterval", "value": "abc"}))["status"] == "Rejected"
    assert (await csms.command("SIM1", "ChangeConfiguration", {"key": "LocalAuthListEnabled", "value": "maybe"}))["status"] == "Rejected"
    assert (await csms.command("SIM1", "ChangeConfiguration", {"key": "TransactionMessageAttempts", "value": "5"}))["status"] == "Accepted"
    assert {k["key"]: k["value"] for k in (await csms.command("SIM1", "GetConfiguration", {"key": ["TransactionMessageAttempts"]}))["configurationKey"]} == {"TransactionMessageAttempts": "5"}


# ------------------------------------------------------------------------------------------------- faults and errors
async def test_a_fault_is_reported_as_an_error_code_on_status_notification(make16, csms):
    st = await make16()
    await st.inject_fault("ground_fault", 1)
    await wait_until(lambda: (1, "Faulted", "GroundFailure") in statuses(csms), message="Faulted + GroundFailure")
    await st.clear_all_faults()
    await wait_until(lambda: statuses(csms).count((1, "Available", "NoError")) >= 2, message="recovers")


async def test_power_meter_failure_is_visible_to_a_1_6_csms(make16, csms):
    st = await make16()
    await st.plug_in(1)
    await st.tap_card(1, "CARD1")
    await csms.wait_for("StartTransaction", cp_id="SIM1")
    await st.inject_fault("power_meter_failure", 1, {"mode": "stop_reporting"})
    await wait_until(lambda: any(e == "PowerMeterFailure" for _, _, e in statuses(csms)), message="PowerMeterFailure error code")
    n = len(csms.messages("MeterValues", "SIM1"))
    await asyncio.sleep(2.5)
    for mv in csms.messages("MeterValues", "SIM1")[n:]:  # no register value while the meter is dead
        assert all(s["measurand"] != "Energy.Active.Import.Register" for s in mv["meter_value"][0]["sampled_value"])


async def test_power_loss_ends_the_session_with_power_loss_after_the_reboot(make16, csms):
    st = await make16()
    await st.plug_in(1)
    await st.tap_card(1, "CARD1")
    await csms.wait_for("StartTransaction", cp_id="SIM1")
    await st.inject_fault("power_loss", None, {"duration_s": 3})
    stop = (await csms.wait_for("StopTransaction", cp_id="SIM1", timeout=30))[0]
    assert stop["reason"] == "PowerLoss" and stop["transaction_id"] == 1
    assert len(csms.messages("BootNotification", "SIM1")) >= 2


async def test_session_survives_a_network_outage_and_is_delivered_in_order(make16, csms):
    st = await make16()
    await st.plug_in(1)
    await st.tap_card(1, "CARD1")
    await csms.wait_for("StartTransaction", cp_id="SIM1")
    await st.net_down()
    await st.stop_session(1)  # while offline: StopTransaction is queued, not lost
    assert len(st.tx_queue) >= 1
    assert csms.messages("StopTransaction", "SIM1") == []
    st.net_up()
    stop = (await csms.wait_for("StopTransaction", cp_id="SIM1", timeout=30))[0]
    assert stop["transaction_id"] == 1 and len(st.tx_queue) == 0


async def test_a_session_started_offline_gets_its_id_when_the_connection_returns(make16, csms):
    st = await make16()
    await st.net_down()
    await st.plug_in(1)
    await st.tap_card(1, "CARD1")  # accepted from the offline rules? unknown token offline -> needs the policy
    st.model.write({"name": "AuthCtrlr"}, {"name": "OfflineTxForUnknownIdEnabled"}, "true")
    await st.tap_card(1, "CARD1")
    await wait_until(lambda: st.evses[0].tx is not None, message="offline transaction")
    await st.stop_session(1)
    st.net_up()
    await csms.wait_for("StartTransaction", cp_id="SIM1", timeout=30)
    stop = (await csms.wait_for("StopTransaction", cp_id="SIM1", timeout=30))[0]
    assert stop["transaction_id"] == 1
    actions = [a for a in csms.actions("SIM1") if a in ("StartTransaction", "StopTransaction")]
    assert actions == ["StartTransaction", "StopTransaction"]


# ------------------------------------------------------------------------------------------------ the 19 CSMS commands
async def test_every_1_6_command_is_answered_with_a_valid_response(make16, csms):
    from ocpp_simulator.v16 import COMMAND_NAMES_16

    st = await make16()
    future = "2099-01-01T00:00:00Z"
    profile = {"chargingProfileId": 1, "stackLevel": 0, "chargingProfilePurpose": "TxDefaultProfile", "chargingProfileKind": "Absolute",
               "chargingSchedule": {"chargingRateUnit": "A", "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 16}], "startSchedule": "2026-01-01T00:00:00Z"}}
    sent = {
        "CancelReservation": {"reservationId": 99}, "ChangeAvailability": {"connectorId": 1, "type": "Operative"},
        "ChangeConfiguration": {"key": "HeartbeatInterval", "value": "30"}, "ClearCache": {}, "ClearChargingProfile": {"id": 1},
        "DataTransfer": {"vendorId": "com.simulator.echo", "data": "hi"}, "GetCompositeSchedule": {"connectorId": 1, "duration": 600},
        "GetConfiguration": {}, "GetDiagnostics": {"location": "ftp://example.com/logs"}, "GetLocalListVersion": {},
        "RemoteStartTransaction": {"idTag": "R1", "connectorId": 1}, "RemoteStopTransaction": {"transactionId": 5},
        "ReserveNow": {"connectorId": 2, "expiryDate": future, "idTag": "R2", "reservationId": 7}, "Reset": {"type": "Soft"},
        "SendLocalList": {"listVersion": 1, "updateType": "Full", "localAuthorizationList": [{"idTag": "LOCAL1", "idTagInfo": {"status": "Accepted"}}]},
        "SetChargingProfile": {"connectorId": 1, "csChargingProfiles": profile}, "TriggerMessage": {"requestedMessage": "Heartbeat"},
        "UnlockConnector": {"connectorId": 1}, "UpdateFirmware": {"location": "http://example.com/fw.bin", "retrieveDate": "2026-01-01T00:00:00Z"},
    }
    assert set(sent) == set(COMMAND_NAMES_16) and len(COMMAND_NAMES_16) == 19
    for name in ("Reset",):
        sent.pop(name)  # Reset is covered on its own (it reboots the station)
    for name, payload in sent.items():
        response = await csms.command("SIM1", name, payload)  # the library validates both directions against the 1.6 JSON schemas
        assert isinstance(response, dict), name


async def test_local_list_reservations_and_availability_behave(make16, csms):
    st = await make16()
    assert (await csms.command("SIM1", "GetLocalListVersion", {}))["listVersion"] == 0
    full = {"listVersion": 3, "updateType": "Full", "localAuthorizationList": [{"idTag": "L1", "idTagInfo": {"status": "Accepted"}}]}
    assert (await csms.command("SIM1", "SendLocalList", full))["status"] == "Accepted"
    assert (await csms.command("SIM1", "SendLocalList", {**full, "updateType": "Differential"}))["status"] == "VersionMismatch"
    assert (await csms.command("SIM1", "GetLocalListVersion", {}))["listVersion"] == 3
    future = "2099-01-01T00:00:00Z"
    r = {"connectorId": 1, "expiryDate": future, "idTag": "RES1", "reservationId": 11}
    assert (await csms.command("SIM1", "ReserveNow", r))["status"] == "Accepted"
    await wait_until(lambda: (1, "Reserved", "NoError") in statuses(csms), message="Reserved")
    assert (await csms.command("SIM1", "CancelReservation", {"reservationId": 11}))["status"] == "Accepted"
    assert (await csms.command("SIM1", "CancelReservation", {"reservationId": 11}))["status"] == "Rejected"
    assert (await csms.command("SIM1", "ChangeAvailability", {"connectorId": 2, "type": "Inoperative"}))["status"] == "Accepted"
    await wait_until(lambda: (2, "Unavailable", "NoError") in statuses(csms), message="Unavailable")
    assert (await csms.command("SIM1", "ChangeAvailability", {"connectorId": 0, "type": "Operative"}))["status"] == "Accepted"
    assert (await csms.command("SIM1", "ChangeAvailability", {"connectorId": 9, "type": "Operative"}))["status"] == "Rejected"
    assert (await csms.command("SIM1", "UnlockConnector", {"connectorId": 9}))["status"] == "NotSupported"
    assert (await csms.command("SIM1", "UnlockConnector", {"connectorId": 1}))["status"] == "Unlocked"


async def test_charging_profiles_limit_the_power_and_the_composite_schedule_reports_it(make16, csms):
    st = await make16()
    await st.plug_in(1)
    await st.tap_card(1, "CARD1")
    await csms.wait_for("StartTransaction", cp_id="SIM1")
    profile = {"chargingProfileId": 5, "stackLevel": 1, "chargingProfilePurpose": "ChargePointMaxProfile", "chargingProfileKind": "Absolute",
               "chargingSchedule": {"chargingRateUnit": "W", "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 3000}], "startSchedule": "2026-01-01T00:00:00Z"}}
    assert (await csms.command("SIM1", "SetChargingProfile", {"connectorId": 0, "csChargingProfiles": profile}))["status"] == "Accepted"
    await wait_until(lambda: st.evses[0].limit_w <= 3000, message="power limited to 3 kW")
    comp = await csms.command("SIM1", "GetCompositeSchedule", {"connectorId": 1, "duration": 600, "chargingRateUnit": "W"})
    assert comp["status"] == "Accepted" and comp["chargingSchedule"]["chargingSchedulePeriod"][0]["limit"] <= 3000
    bad = {**profile, "chargingProfilePurpose": "TxProfile", "transactionId": 424242, "chargingProfileId": 6}
    assert (await csms.command("SIM1", "SetChargingProfile", {"connectorId": 1, "csChargingProfiles": bad}))["status"] == "Rejected"
    assert (await csms.command("SIM1", "ClearChargingProfile", {"id": 5}))["status"] == "Accepted"
    assert (await csms.command("SIM1", "ClearChargingProfile", {"id": 5}))["status"] == "Unknown"
    await wait_until(lambda: st.evses[0].limit_w > 3000, message="limit lifted")


async def test_trigger_message_firmware_and_diagnostics_flows(make16, csms):
    st = await make16()
    n = len(csms.messages("StatusNotification", "SIM1"))
    assert (await csms.command("SIM1", "TriggerMessage", {"requestedMessage": "StatusNotification"}))["status"] == "Accepted"
    await wait_until(lambda: len(csms.messages("StatusNotification", "SIM1")) >= n + 3, message="status notifications triggered")
    assert (await csms.command("SIM1", "TriggerMessage", {"requestedMessage": "Heartbeat"}))["status"] == "Accepted"
    await csms.wait_for("Heartbeat", cp_id="SIM1")
    assert (await csms.command("SIM1", "TriggerMessage", {"requestedMessage": "MeterValues", "connectorId": 1}))["status"] == "Accepted"
    assert (await csms.command("SIM1", "TriggerMessage", {"requestedMessage": "FirmwareStatusNotification"}))["status"] == "Accepted"
    assert (await csms.command("SIM1", "UpdateFirmware", {"location": "http://x/fw.bin", "retrieveDate": "2020-01-01T00:00:00Z"})) == {}
    await csms.wait_for("FirmwareStatusNotification", cp_id="SIM1", predicate=lambda m: m["status"] == "Installed", timeout=20)
    seen = [m["status"] for m in csms.messages("FirmwareStatusNotification", "SIM1")]
    assert seen.index("Downloading") < seen.index("Downloaded") < seen.index("Installing") < seen.index("Installed")
    d = await csms.command("SIM1", "GetDiagnostics", {"location": "ftp://example.com/logs"})
    assert d["fileName"].endswith(".zip")
    await csms.wait_for("DiagnosticsStatusNotification", cp_id="SIM1", predicate=lambda m: m["status"] == "Uploaded", timeout=15)


async def test_a_failing_firmware_download_is_reported(make16, csms):
    st = await make16()
    st.fw_failure = "download"
    await csms.command("SIM1", "UpdateFirmware", {"location": "http://x/fw.bin", "retrieveDate": "2020-01-01T00:00:00Z"})
    await csms.wait_for("FirmwareStatusNotification", cp_id="SIM1", predicate=lambda m: m["status"] == "DownloadFailed", timeout=15)


# ------------------------------------------------------------------------------------------------ the station -> CSMS side
async def test_send_raw_speaks_1_6_and_refuses_2_0_1_messages(make16, csms):
    st = await make16()
    ok = await st.send_raw("DataTransfer", {"vendorId": "jio", "messageId": "GetChargeInstruction", "data": ""})
    assert ok.ok and ok.status == "Accepted"
    assert (await st.send_raw("Heartbeat", {})).ok
    no = await st.send_raw("TransactionEvent", {})
    assert not no.sent and "OCPP 1.6" in no.error
    bad = await st.send_raw("StatusNotification", {"connectorId": 1, "status": "NotAStatus", "errorCode": "NoError"})
    assert not bad.ok  # the schema check refuses it before it is sent


async def test_station_messages_without_a_1_6_equivalent_are_quietly_skipped(make16, csms):
    st = await make16()
    res = await st.request("NotifyEvent", generated_at=st.ts(), seq_no=0, event_data=[])
    assert res.sent and res.ok and csms.messages("NotifyEvent") == []


# ------------------------------------------------------------------------------------------------------- scenarios
from tests.test_scenarios import FAST, SLOW, failures, run_scenario  # noqa: E402


@pytest.mark.parametrize("sid", FAST)
async def test_builtin_scenarios_also_pass_on_ocpp_1_6(csms, make16, sid):
    st = await make16(connect=(sid != "smoke_boot"), time_scale=300 if sid == "battery_full" else 60)
    run = await run_scenario(st, sid)
    assert run.status == "passed", failures(run)
    assert any(s.status == "passed" for s in run.steps)


@pytest.mark.slow
@pytest.mark.parametrize("sid", SLOW)
async def test_slow_builtin_scenarios_also_pass_on_ocpp_1_6(csms, make16, sid):
    st = await make16()
    run = await run_scenario(st, sid, timeout=180)
    assert run.status == "passed", failures(run)


async def test_the_remaining_catalog_scenarios_are_version_aware(make16):
    from ocpp_simulator.scenarios import CATALOG_SCENARIOS

    st = await make16(connect=False)
    for sid, sc in CATALOG_SCENARIOS.items():
        for step in sc["steps"]:
            resolved = st.runner.for_version(step)
            if resolved is None:
                continue
            assert resolved.get("action") not in ("TransactionEvent", "NotifyEvent", "NotifyChargingLimit", "ClearedChargingLimit", "SecurityEventNotification",
                                                  "RequestStartTransaction", "RequestStopTransaction"), (sid, step)
            assert resolved.get("status") != "Occupied", (sid, step)


async def test_a_csms_that_answers_with_a_protocol_the_charger_did_not_offer_gets_a_helpful_error(make16):
    """The reported failure: 'CSMS refused the connection: unsupported subprotocol: ocpp1.6'."""
    stubborn = MockCsms(versions=("1.6",), answer_with="ocpp1.6")
    await stubborn.start()
    try:
        fixed = await make16(ocpp_id="D1", version="2.0.1", server=stubborn, connect=False)
        await fixed.connect()
        await wait_until(lambda: fixed.last_error, message="a refusal")
        assert "unsupported subprotocol: ocpp1.6" in fixed.last_error and "Auto or 1.6" in fixed.last_error
        await fixed.disconnect()
        auto = await make16(ocpp_id="D2", version="auto", server=stubborn)
        assert auto.version == "1.6" and auto.online  # offering both protocols is the fix
    finally:
        await stubborn.stop()
