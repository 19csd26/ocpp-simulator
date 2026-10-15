"""What the commands actually do, not just that they are answered."""
import asyncio
import datetime as dt

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from ocpp_simulator.templates import PROFILE
from tests.conftest import wait_until

V = lambda name, comp="OCPPCommCtrlr": {"component": {"name": comp}, "variable": {"name": name}}  # noqa: E731


def statuses(csms, evse=1):
    return [m["connector_status"] for m in csms.messages("StatusNotification", "SIM1") if m["evse_id"] == evse]


# ------------------------------------------------------------------------------ device model
async def test_get_and_set_variables_with_every_outcome(csms, make_station):
    st = await make_station()
    got = await csms.command("SIM1", "GetVariables", {"getVariableData": [
        {**V("HeartbeatInterval")}, {**V("Model", "ChargingStation")}, {**V("Nope")}, {"component": {"name": "Ghost"}, "variable": {"name": "x"}},
        {**V("HeartbeatInterval"), "attributeType": "MaxSet"}]})
    result = got["getVariableResult"]
    assert [r["attributeStatus"] for r in result] == ["Accepted", "Accepted", "UnknownVariable", "UnknownComponent", "NotSupportedAttributeType"]
    assert result[0]["attributeValue"] == "30" and result[1]["attributeValue"] == "SIM-AC-22D"

    done = await csms.command("SIM1", "SetVariables", {"setVariableData": [
        {"attributeValue": "45", **V("HeartbeatInterval")},               # ok
        {"attributeValue": "abc", **V("HeartbeatInterval")},              # wrong type
        {"attributeValue": "0", **V("HeartbeatInterval")},                # below the minimum (1)
        {"attributeValue": "x", **V("Model", "ChargingStation")},         # read only
        {"attributeValue": "2", **V("SecurityProfile", "SecurityCtrl" + "r")},  # needs a reboot
        {"attributeValue": "maybe", **V("AuthorizeRemoteStart", "AuthCtrlr")},  # boolean
        {"attributeValue": "Foo", **V("TxStartPoint", "TxCtrlr")},        # not in the member list
        {"attributeValue": "Authorized,EVConnected", **V("TxStartPoint", "TxCtrlr")}]})
    assert [r["attributeStatus"] for r in done["setVariableResult"]] == ["Accepted", "Rejected", "Rejected", "Rejected", "RebootRequired", "Rejected", "Rejected", "Accepted"]
    assert st.model.get_int("OCPPCommCtrlr", "HeartbeatInterval") == 45
    assert st.start_points() == {"Authorized", "EVConnected"}


async def test_reports_are_chunked_and_filtered(csms, make_station):
    st = await make_station("dc_60_dual")
    total = len(st.model.report_items("FullInventory"))
    assert (await csms.command("SIM1", "GetBaseReport", {"requestId": 7, "reportBase": "FullInventory"}))["status"] == "Accepted"
    parts = await csms.wait_for("NotifyReport", predicate=lambda m: m["request_id"] == 7, count=3)
    assert [p["seq_no"] for p in parts] == [0, 1, 2] and [p["tbc"] for p in parts] == [True, True, False]  # 40 per message by default
    assert sum(len(p["report_data"]) for p in parts) == total
    config = len(st.model.report_items("ConfigurationInventory"))
    assert 0 < config < total  # read-only variables are left out
    assert (await csms.command("SIM1", "GetReport", {"requestId": 8, "componentVariable": [{"component": {"name": "EVSE", "evse": {"id": 2}}}]}))["status"] == "Accepted"
    only_evse2 = (await csms.wait_for("NotifyReport", predicate=lambda m: m["request_id"] == 8))[0]["report_data"]
    assert {(d["component"]["name"], d["component"]["evse"]["id"]) for d in only_evse2} == {("EVSE", 2)}
    assert (await csms.command("SIM1", "GetReport", {"requestId": 9, "componentVariable": [{"component": {"name": "Ghost"}}]}))["status"] == "EmptyResultSet"


async def test_changing_the_sampling_interval_changes_the_meter_updates(csms, make_station):
    st = await make_station()
    await csms.command("SIM1", "SetVariables", {"setVariableData": [
        {"attributeValue": "Energy.Active.Import.Register,SoC", **V("TxUpdatedMeasurands", "SampledDataCtrlr")}]})
    await st.plug_in(1)
    await st.tap_card(1, "C1")
    update = (await csms.wait_for("TransactionEvent", predicate=lambda m: m["event_type"] == "Updated" and m["trigger_reason"] == "MeterValuePeriodic"))[0]
    measurands = {sv["measurand"] for sv in update["meter_value"][0]["sampled_value"]}
    assert measurands == {"Energy.Active.Import.Register", "SoC"}


# --------------------------------------------------------------------------- remote start / stop
async def test_remote_start_with_vehicle_connected_then_remote_stop(csms, make_station):
    st = await make_station()
    await st.plug_in(1)
    reply = await csms.command("SIM1", "RequestStartTransaction", {"idToken": {"idToken": "user-5", "type": "Central"}, "remoteStartId": 42, "evseId": 1})
    assert reply["status"] == "Accepted" and reply["transactionId"]
    started = (await csms.wait_for("TransactionEvent", predicate=lambda m: m["event_type"] == "Started"))[0]
    assert started["trigger_reason"] == "RemoteStart" and started["transaction_info"]["remote_start_id"] == 42
    assert started["transaction_info"]["transaction_id"] == reply["transactionId"]   # exactly the id promised in the response
    assert started["id_token"] == {"id_token": "user-5", "type": "Central"}
    assert (await csms.command("SIM1", "RequestStopTransaction", {"transactionId": reply["transactionId"]}))["status"] == "Accepted"
    ended = (await csms.wait_for("TransactionEvent", predicate=lambda m: m["event_type"] == "Ended"))[0]
    assert ended["transaction_info"]["stopped_reason"] == "Remote" and ended["trigger_reason"] == "RemoteStop"
    assert (await csms.command("SIM1", "RequestStopTransaction", {"transactionId": reply["transactionId"]}))["status"] == "Rejected"  # already over


async def test_remote_start_waits_for_a_vehicle_and_expires(csms, make_station):
    st = await make_station()
    st.model.write({"name": "TxCtrlr"}, {"name": "EVConnectionTimeOut"}, "2")
    reply = await csms.command("SIM1", "RequestStartTransaction", {"idToken": {"idToken": "R1", "type": "Central"}, "remoteStartId": 1, "evseId": 1})
    assert reply["status"] == "Accepted" and st.evses[0].tx is None and st.evses[0].pending_remote
    await wait_until(lambda: st.evses[0].pending_remote is None, timeout=6, message="remote start expiry")
    # a vehicle that arrives in time does start the session
    reply = await csms.command("SIM1", "RequestStartTransaction", {"idToken": {"idToken": "R2", "type": "Central"}, "remoteStartId": 2, "evseId": 1})
    await st.plug_in(1)
    await wait_until(lambda: st.evses[0].tx is not None, message="session after plug-in")
    assert st.evses[0].tx.id == reply["transactionId"]


async def test_remote_start_refusals(csms, make_station):
    csms.allowed_tokens = {"GOOD"}
    st = await make_station()
    token = lambda t: {"idToken": t, "type": "Central"}  # noqa: E731
    assert (await csms.command("SIM1", "RequestStartTransaction", {"idToken": token("x"), "remoteStartId": 1, "evseId": 9}))["status"] == "Rejected"
    await st.plug_in(1)
    first = await csms.command("SIM1", "RequestStartTransaction", {"idToken": token("a"), "remoteStartId": 1, "evseId": 1})
    await wait_until(lambda: st.evses[0].tx is not None)
    busy = await csms.command("SIM1", "RequestStartTransaction", {"idToken": token("b"), "remoteStartId": 2, "evseId": 1})
    assert busy["status"] == "Rejected" and busy["statusInfo"]["reasonCode"] == "TxInProgress"
    # With AuthorizeRemoteStart on, the station answers Accepted and then asks the CSMS: a refused token means no transaction (F01)
    st.model.write({"name": "AuthCtrlr"}, {"name": "AuthorizeRemoteStart"}, "true")
    await st.plug_in(2)
    denied = await csms.command("SIM1", "RequestStartTransaction", {"idToken": token("NOPE"), "remoteStartId": 3, "evseId": 2})
    assert denied["status"] == "Accepted"
    await csms.wait_for("Authorize", predicate=lambda m: m["id_token"]["id_token"] == "NOPE")
    await wait_until(lambda: st.evses[1].pending_remote is None, message="refused start dropped")
    await asyncio.sleep(0.5)
    assert st.evses[1].tx is None and st.evses[1].auth is None
    ok = await csms.command("SIM1", "RequestStartTransaction", {"idToken": token("GOOD"), "remoteStartId": 4, "evseId": 2})
    assert ok["status"] == "Accepted"
    await wait_until(lambda: st.evses[1].tx is not None, message="authorised remote start begins")
    assert st.evses[1].tx.id == ok["transactionId"]


async def test_remote_start_without_evse_picks_the_connected_one(csms, make_station):
    st = await make_station()
    await st.plug_in(2)
    reply = await csms.command("SIM1", "RequestStartTransaction", {"idToken": {"idToken": "any", "type": "Central"}, "remoteStartId": 1})
    assert reply["status"] == "Accepted"
    await wait_until(lambda: st.evses[1].tx is not None)


# -------------------------------------------------------------------------------- reservations
async def test_reservation_lifecycle(csms, make_station):
    st = await make_station()
    future = (st.now() + dt.timedelta(minutes=5)).isoformat()
    token = {"idToken": "VIP", "type": "ISO14443"}
    assert (await csms.command("SIM1", "ReserveNow", {"id": 5, "expiryDateTime": future, "idToken": token, "evseId": 1}))["status"] == "Accepted"
    await wait_until(lambda: statuses(csms)[-1] == "Reserved", message="Reserved reported")
    assert (await st.tap_card(1, "STRANGER"))["status"] == "ReservedForOther"
    other = await csms.command("SIM1", "ReserveNow", {"id": 6, "expiryDateTime": future, "idToken": token, "evseId": 1})
    assert other["status"] == "Occupied"  # already reserved by id 5
    await st.plug_in(1)
    assert (await st.tap_card(1, "VIP"))["status"] == "Accepted"
    started = (await csms.wait_for("TransactionEvent", predicate=lambda m: m["event_type"] == "Started"))[0]
    assert started["reservation_id"] == 5 and not st.reservations                 # consumed by the session

    assert (await csms.command("SIM1", "ReserveNow", {"id": 7, "expiryDateTime": future, "idToken": token, "evseId": 2}))["status"] == "Accepted"
    assert (await csms.command("SIM1", "CancelReservation", {"reservationId": 7}))["status"] == "Accepted"
    await wait_until(lambda: statuses(csms, 2)[-1] == "Available")
    assert (await csms.command("SIM1", "CancelReservation", {"reservationId": 7}))["status"] == "Rejected"


async def test_reservation_expires_and_reports_it(csms, make_station):
    st = await make_station()
    soon = (st.now() + dt.timedelta(seconds=2)).isoformat()
    await csms.command("SIM1", "ReserveNow", {"id": 9, "expiryDateTime": soon, "idToken": {"idToken": "T", "type": "ISO14443"}, "evseId": 1})
    update = (await csms.wait_for("ReservationStatusUpdate", timeout=8))[0]
    assert update == {"reservation_id": 9, "reservation_update_status": "Expired"}
    await wait_until(lambda: statuses(csms)[-1] == "Available", timeout=6)
    past = (st.now() - dt.timedelta(minutes=1)).isoformat()
    refused = await csms.command("SIM1", "ReserveNow", {"id": 10, "expiryDateTime": past, "idToken": {"idToken": "T", "type": "ISO14443"}, "evseId": 1})
    assert refused["status"] == "Rejected"


# -------------------------------------------------------------------------------- smart charging
async def test_charging_profile_limits_power_and_reports_the_change(csms, make_station):
    st = await make_station()
    await st.plug_in(1)
    await st.tap_card(1, "C1")
    await wait_until(lambda: st.evses[0].power_w > 10000, message="full power")
    now = st.now().isoformat()
    reply = await csms.command("SIM1", "SetChargingProfile", {"evseId": 1, "chargingProfile": PROFILE(now, limit=8, unit="A", pid=3)})  # 8 A x 3 phases x 230 V
    assert reply["status"] == "Accepted"
    await wait_until(lambda: abs(st.evses[0].power_w - 5520) < 1, message="power limited to 5.52 kW")
    changed = await csms.wait_for("TransactionEvent", predicate=lambda m: m["trigger_reason"] == "ChargingRateChanged")
    assert changed[0]["transaction_info"]["charging_state"] == "Charging"
    sched = (await csms.command("SIM1", "GetCompositeSchedule", {"evseId": 1, "duration": 600, "chargingRateUnit": "W"}))["schedule"]
    assert sched["chargingSchedulePeriod"][0]["limit"] == 5520.0
    assert (await csms.command("SIM1", "ClearChargingProfile", {"chargingProfileId": 3}))["status"] == "Accepted"
    await wait_until(lambda: st.evses[0].power_w > 10000, message="limit lifted")
    assert (await csms.command("SIM1", "ClearChargingProfile", {"chargingProfileId": 3}))["status"] == "Unknown"


async def test_charging_profile_validation(csms, make_station):
    st = await make_station()
    now = st.now().isoformat()
    set_ = lambda evse, profile: csms.command("SIM1", "SetChargingProfile", {"evseId": evse, "chargingProfile": profile})  # noqa: E731
    r = await set_(1, PROFILE(now, purpose="TxProfile"))
    assert r["status"] == "Rejected" and r["statusInfo"]["reasonCode"] == "TxNotFound"          # no running transaction
    r = await set_(1, PROFILE(now, purpose="ChargingStationMaxProfile"))
    assert r["status"] == "Rejected" and r["statusInfo"]["reasonCode"] == "InvalidValue"        # station max must use evse 0
    assert (await set_(0, PROFILE(now, purpose="ChargingStationMaxProfile", limit=30)))["status"] == "Accepted"
    assert (await set_(9, PROFILE(now)))["status"] == "Rejected"                                # unknown EVSE
    bad = PROFILE(now)
    bad["chargingSchedule"][0]["chargingSchedulePeriod"][0]["startPeriod"] = 60
    r = await set_(1, bad)
    assert r["status"] == "Rejected" and r["statusInfo"]["reasonCode"] == "InvalidSchedule"     # must start at 0
    # same purpose + stack level on the same EVSE replaces the older profile
    await set_(1, PROFILE(now, pid=11, limit=10))
    await set_(1, PROFILE(now, pid=12, limit=12))
    assert sorted(p["id"] for p in st.profiles if p["_evse_id"] == 1) == [12]


async def test_get_charging_profiles_reports_them(csms, make_station):
    st = await make_station()
    now = st.now().isoformat()
    await csms.command("SIM1", "SetChargingProfile", {"evseId": 1, "chargingProfile": PROFILE(now, pid=21)})
    reply = await csms.command("SIM1", "GetChargingProfiles", {"requestId": 3, "chargingProfile": {"chargingProfilePurpose": "TxDefaultProfile"}})
    assert reply["status"] == "Accepted"
    report = (await csms.wait_for("ReportChargingProfiles", predicate=lambda m: m["request_id"] == 3))[0]
    assert report["evse_id"] == 1 and [p["id"] for p in report["charging_profile"]] == [21]
    assert (await csms.command("SIM1", "GetChargingProfiles", {"requestId": 4, "chargingProfile": {"chargingProfilePurpose": "TxProfile"}}))["status"] == "NoProfiles"


async def test_remote_start_with_a_tx_profile(csms, make_station):
    st = await make_station()
    await st.plug_in(1)
    profile = PROFILE(st.now().isoformat(), limit=6, unit="A", purpose="TxProfile", pid=40)
    reply = await csms.command("SIM1", "RequestStartTransaction", {"idToken": {"idToken": "P", "type": "Central"}, "remoteStartId": 9, "evseId": 1, "chargingProfile": profile})
    assert reply["status"] == "Accepted"
    await wait_until(lambda: st.evses[0].tx is not None)
    await wait_until(lambda: abs(st.evses[0].power_w - 4140) < 1, message="6 A x 3 x 230 V")
    assert [p["transaction_id"] for p in st.profiles] == [reply["transactionId"]]


# ------------------------------------------------------------------------------- local list / cache
async def test_local_list_versioning(csms, make_station):
    st = await make_station()
    token = lambda t, s="Accepted": {"idToken": {"idToken": t, "type": "ISO14443"}, "idTokenInfo": {"status": s}}  # noqa: E731
    assert (await csms.command("SIM1", "GetLocalListVersion", {}))["versionNumber"] == 0
    assert (await csms.command("SIM1", "SendLocalList", {"versionNumber": 1, "updateType": "Full", "localAuthorizationList": [token("A"), token("B", "Blocked")]}))["status"] == "Accepted"
    assert (await csms.command("SIM1", "GetLocalListVersion", {}))["versionNumber"] == 1
    assert (await csms.command("SIM1", "SendLocalList", {"versionNumber": 1, "updateType": "Differential", "localAuthorizationList": [token("C")]}))["status"] == "VersionMismatch"
    diff = {"versionNumber": 2, "updateType": "Differential", "localAuthorizationList": [token("C"), {"idToken": {"idToken": "A", "type": "ISO14443"}}]}  # add C, remove A
    assert (await csms.command("SIM1", "SendLocalList", diff))["status"] == "Accepted"
    assert set(st.local_list["entries"]) == {"B", "C"} and st.model.get_int("LocalAuthListCtrlr", "Entries") == 2


async def test_offline_authorisation_uses_local_list_then_cache_then_policy(csms, make_station):
    csms.allowed_tokens = {"ONLINE1"}
    st = await make_station()
    await csms.command("SIM1", "SendLocalList", {"versionNumber": 1, "updateType": "Full", "localAuthorizationList": [
        {"idToken": {"idToken": "LISTED", "type": "ISO14443"}, "idTokenInfo": {"status": "Accepted"}},
        {"idToken": {"idToken": "BLOCKED", "type": "ISO14443"}, "idTokenInfo": {"status": "Blocked"}}]})
    assert (await st.authorize("ONLINE1"))[0] == "Accepted"       # online: asked the CSMS, and cached the answer
    await st.net_down()
    assert st.online is False
    assert await st.authorize("LISTED") == ("Accepted", "local list (offline)")
    assert (await st.authorize("BLOCKED"))[0] == "Blocked"
    assert await st.authorize("ONLINE1") == ("Accepted", "cache (offline)")
    assert (await st.authorize("STRANGER"))[0] == "Invalid"
    st.model.write({"name": "AuthCtrlr"}, {"name": "OfflineTxForUnknownIdEnabled"}, "true")
    assert (await st.authorize("STRANGER"))[0] == "Accepted"
    st.net_up()
    await wait_until(lambda: st.online, message="reconnected")
    assert (await csms.command("SIM1", "ClearCache", {}))["status"] == "Accepted" and not st.auth_cache


# ------------------------------------------------------------------------ availability / reset
async def test_change_availability(csms, make_station):
    st = await make_station()
    assert (await csms.command("SIM1", "ChangeAvailability", {"operationalStatus": "Inoperative", "evse": {"id": 1}}))["status"] == "Accepted"
    await wait_until(lambda: statuses(csms, 1)[-1] == "Unavailable")
    assert statuses(csms, 2)[-1] == "Available"
    assert (await st.tap_card(1, "X"))["status"] == "Unavailable"
    await csms.command("SIM1", "ChangeAvailability", {"operationalStatus": "Operative", "evse": {"id": 1}})
    await wait_until(lambda: statuses(csms, 1)[-1] == "Available")
    bad = await csms.command("SIM1", "ChangeAvailability", {"operationalStatus": "Inoperative", "evse": {"id": 8}})
    assert bad["status"] == "Rejected"
    await csms.command("SIM1", "ChangeAvailability", {"operationalStatus": "Inoperative"})  # the whole station
    await wait_until(lambda: statuses(csms, 1)[-1] == "Unavailable" and statuses(csms, 2)[-1] == "Unavailable")


async def test_inoperative_while_charging_is_scheduled_until_the_session_ends(csms, make_station):
    st = await make_station()
    await st.plug_in(1)
    await st.tap_card(1, "C")
    await wait_until(lambda: st.evses[0].tx is not None)
    assert (await csms.command("SIM1", "ChangeAvailability", {"operationalStatus": "Inoperative", "evse": {"id": 1}}))["status"] == "Scheduled"
    assert st.evses[0].operative and st.evses[0].tx is not None   # still charging
    await st.tap_card(1, "C")
    await wait_until(lambda: not st.evses[0].operative, timeout=6, message="applied after the session")


async def test_station_reset_reboots_with_remote_reset_reason(csms, make_station):
    st = await make_station()
    await st.plug_in(1)
    await st.tap_card(1, "C")
    await wait_until(lambda: st.evses[0].tx is not None)
    assert (await csms.command("SIM1", "Reset", {"type": "Immediate"}))["status"] == "Accepted"
    ended = (await csms.wait_for("TransactionEvent", predicate=lambda m: m["event_type"] == "Ended"))[0]
    assert ended["transaction_info"]["stopped_reason"] == "ImmediateReset" and ended["trigger_reason"] == "ResetCommand"
    boots = await csms.wait_for("BootNotification", count=2, timeout=15)
    assert boots[1]["reason"] == "RemoteReset"
    await wait_until(lambda: st.online, timeout=15)


async def test_reset_on_idle_waits_for_the_session(csms, make_station):
    st = await make_station()
    await st.plug_in(1)
    await st.tap_card(1, "C")
    await wait_until(lambda: st.evses[0].tx is not None)
    assert (await csms.command("SIM1", "Reset", {"type": "OnIdle"}))["status"] == "Scheduled"
    await asyncio.sleep(1.5)
    assert len(csms.messages("BootNotification")) == 1 and st.evses[0].tx is not None
    await st.tap_card(1, "C")
    await csms.wait_for("BootNotification", count=2, timeout=15)


async def test_evse_reset_only_cycles_that_evse(csms, make_station):
    st = await make_station()
    assert (await csms.command("SIM1", "Reset", {"type": "Immediate", "evse": 1} if False else {"type": "Immediate", "evseId": 1}))["status"] == "Accepted"
    await wait_until(lambda: "Unavailable" in statuses(csms, 1), message="EVSE 1 cycled")
    await wait_until(lambda: statuses(csms, 1)[-1] == "Available", timeout=8)
    assert "Unavailable" not in statuses(csms, 2) and len(csms.messages("BootNotification")) == 1
    assert (await csms.command("SIM1", "Reset", {"type": "Immediate", "evseId": 6}))["status"] == "Rejected"


# ----------------------------------------------------------------------------- misc commands
async def test_unlock_connector_outcomes(csms, make_station):
    st = await make_station()
    unlock = lambda e, c: csms.command("SIM1", "UnlockConnector", {"evseId": e, "connectorId": c})  # noqa: E731
    assert (await unlock(1, 1))["status"] == "Unlocked"
    assert (await unlock(1, 5))["status"] == "UnknownConnector"
    await st.plug_in(1)
    await st.tap_card(1, "C")
    await wait_until(lambda: st.evses[0].tx is not None)
    assert (await unlock(1, 1))["status"] == "OngoingAuthorizedTransaction"


@pytest.mark.parametrize("message, expect", [
    ("Heartbeat", "Heartbeat"), ("StatusNotification", "StatusNotification"), ("MeterValues", "MeterValues"), ("BootNotification", "BootNotification"),
    ("FirmwareStatusNotification", "FirmwareStatusNotification"), ("LogStatusNotification", "LogStatusNotification"),
    ("PublishFirmwareStatusNotification", "PublishFirmwareStatusNotification"), ("SignChargingStationCertificate", "SignCertificate")])
async def test_trigger_message(csms, make_station, message, expect):
    st = await make_station()
    before = len(csms.messages(expect))
    reply = await csms.command("SIM1", "TriggerMessage", {"requestedMessage": message})
    assert reply["status"] == "Accepted"
    await wait_until(lambda: len(csms.messages(expect)) > before, message=f"{expect} after trigger")
    if message == "BootNotification":
        assert csms.messages("BootNotification")[-1]["reason"] == "Triggered"


async def test_trigger_message_edge_cases(csms, make_station):
    st = await make_station()
    assert (await csms.command("SIM1", "TriggerMessage", {"requestedMessage": "SignV2GCertificate"}))["status"] == "NotImplemented"
    assert (await csms.command("SIM1", "TriggerMessage", {"requestedMessage": "StatusNotification", "evse": {"id": 7, "connectorId": 1}}))["status"] == "Rejected"
    await st.plug_in(1)
    await st.tap_card(1, "C")
    await wait_until(lambda: st.evses[0].tx is not None)
    await csms.command("SIM1", "TriggerMessage", {"requestedMessage": "TransactionEvent", "evse": {"id": 1}})
    trig = await csms.wait_for("TransactionEvent", predicate=lambda m: m["trigger_reason"] == "Trigger")
    assert trig[0]["event_type"] == "Updated"


async def test_display_messages(csms, make_station):
    st = await make_station()
    msg = lambda i, fmt="UTF8", pr="NormalCycle": {"message": {"id": i, "priority": pr, "message": {"format": fmt, "content": f"Hello {i}"}}}  # noqa: E731
    assert (await csms.command("SIM1", "SetDisplayMessage", msg(1)))["status"] == "Accepted"
    assert (await csms.command("SIM1", "SetDisplayMessage", msg(2, fmt="HTML")))["status"] == "NotSupportedMessageFormat"
    assert (await csms.command("SIM1", "GetDisplayMessages", {"requestId": 5}))["status"] == "Accepted"
    shown = (await csms.wait_for("NotifyDisplayMessages", predicate=lambda m: m["request_id"] == 5))[0]
    assert [m["id"] for m in shown["message_info"]] == [1]
    assert (await csms.command("SIM1", "ClearDisplayMessage", {"id": 1}))["status"] == "Accepted"
    assert (await csms.command("SIM1", "ClearDisplayMessage", {"id": 1}))["status"] == "Unknown"
    assert (await csms.command("SIM1", "GetDisplayMessages", {"requestId": 6}))["status"] == "Unknown"


async def test_customer_information_cost_and_data_transfer(csms, make_station):
    st = await make_station()
    assert (await csms.command("SIM1", "CustomerInformation", {"requestId": 2, "report": False, "clear": False, "customerIdentifier": "X"}))["status"] == "Invalid"
    assert (await csms.command("SIM1", "CustomerInformation", {"requestId": 3, "report": True, "clear": False, "customerIdentifier": "X"}))["status"] == "Accepted"
    info = (await csms.wait_for("NotifyCustomerInformation", predicate=lambda m: m["request_id"] == 3))[0]
    assert "X" in info["data"] and info["tbc"] is False
    await st.plug_in(1)
    await st.tap_card(1, "C")
    await wait_until(lambda: st.evses[0].tx is not None)
    await csms.command("SIM1", "CostUpdated", {"totalCost": 7.25, "transactionId": st.evses[0].tx.id})
    assert st.evses[0].tx.total_cost == 7.25
    assert (await csms.command("SIM1", "DataTransfer", {"vendorId": "com.simulator.echo", "data": {"a": 1}})) == {"status": "Accepted", "data": {"a": 1}}
    assert (await csms.command("SIM1", "DataTransfer", {"vendorId": "com.other"}))["status"] == "UnknownVendorId"
    assert (await csms.command("SIM1", "DataTransfer", {"vendorId": "com.simulator", "messageId": "zzz"}))["status"] == "UnknownMessageId"
    assert (await csms.command("SIM1", "DataTransfer", {"vendorId": "com.simulator.reject"}))["status"] == "Rejected"


async def test_get_transaction_status(csms, make_station):
    st = await make_station()
    await st.plug_in(1)
    await st.tap_card(1, "C")
    await wait_until(lambda: st.evses[0].tx is not None)
    tx = st.evses[0].tx.id
    assert await csms.command("SIM1", "GetTransactionStatus", {"transactionId": tx}) == {"messagesInQueue": False, "ongoingIndicator": True}
    assert await csms.command("SIM1", "GetTransactionStatus", {"transactionId": "nope"}) == {"messagesInQueue": False, "ongoingIndicator": False}
    assert await csms.command("SIM1", "GetTransactionStatus", {}) == {"messagesInQueue": False}


async def test_network_profile_validation(csms, make_station):
    await make_station()
    data = lambda url, prof, ver="OCPP20": {"configurationSlot": 1, "connectionData": {"ocppVersion": ver, "ocppTransport": "JSON", "ocppCsmsUrl": url, "messageTimeout": 30, "securityProfile": prof, "ocppInterface": "Wired0"}}  # noqa: E731
    ok = await csms.command("SIM1", "SetNetworkProfile", data("wss://csms.example.com/ocpp", 2))
    assert ok["status"] == "Accepted"
    plain = await csms.command("SIM1", "SetNetworkProfile", data("ws://csms.example.com/ocpp", 2))
    assert plain["status"] == "Rejected" and plain["statusInfo"]["reasonCode"] == "InvalidUrl"      # profile 2 needs TLS
    assert (await csms.command("SIM1", "SetNetworkProfile", data("ftp://x", 1)))["status"] == "Rejected"
    assert (await csms.command("SIM1", "SetNetworkProfile", data("ws://csms.example.com/ocpp", 1, "OCPP16")))["status"] == "Rejected"


# ---------------------------------------------------------------------------------- monitoring
async def test_variable_monitoring_raises_events(csms, make_station):
    st = await make_station()
    res = await csms.command("SIM1", "SetVariableMonitoring", {"setMonitoringData": [
        {"type": "UpperThreshold", "severity": 3, "value": 60, "component": {"name": "EVSE", "evse": {"id": 1}}, "variable": {"name": "Temperature"}},
        {"type": "UpperThreshold", "severity": 3, "value": 1, "component": {"name": "EVSE", "evse": {"id": 1}}, "variable": {"name": "Model"}},      # not monitorable
        {"type": "UpperThreshold", "severity": 3, "value": 1, "component": {"name": "Ghost"}, "variable": {"name": "x"}}]})
    assert [r["status"] for r in res["setMonitoringResult"]] == ["Accepted", "UnknownVariable", "UnknownComponent"]
    mid = res["setMonitoringResult"][0]["id"]
    assert (await csms.command("SIM1", "GetMonitoringReport", {"requestId": 1}))["status"] == "Accepted"
    rep = (await csms.wait_for("NotifyMonitoringReport"))[0]
    assert rep["monitor"][0]["variable_monitoring"][0] == {"id": mid, "transaction": False, "value": 60.0, "type": "UpperThreshold", "severity": 3}
    await st.inject_fault("over_temperature", 1, {"mode": "derate", "temperature_c": 90})
    alert = (await csms.wait_for("NotifyEvent", timeout=10, predicate=lambda m: m["event_data"][0].get("variable_monitoring_id") == mid))[0]["event_data"][0]
    assert alert["trigger"] == "Alerting" and alert["event_notification_type"] == "CustomMonitor" and alert["cleared"] is False
    await st.clear_all_faults()
    cleared = await csms.wait_for("NotifyEvent", timeout=10, predicate=lambda m: m["event_data"][0].get("variable_monitoring_id") == mid and m["event_data"][0]["cleared"])
    assert cleared
    assert (await csms.command("SIM1", "ClearVariableMonitoring", {"id": [mid, 999]}))["clearMonitoringResult"] == [{"status": "Accepted", "id": mid}, {"status": "NotFound", "id": 999}]


async def test_monitoring_base_and_level(csms, make_station):
    st = await make_station()
    assert (await csms.command("SIM1", "SetMonitoringLevel", {"severity": 2}))["status"] == "Accepted"
    assert st.model.get_int("MonitoringCtrlr", "ActiveMonitoringLevel") == 2
    assert (await csms.command("SIM1", "SetMonitoringBase", {"monitoringBase": "FactoryDefault"}))["status"] == "Accepted"
    assert st.model.get_value("MonitoringCtrlr", "ActiveMonitoringBase") == "FactoryDefault"


# -------------------------------------------------------------------------------- certificates
def make_cert(cn="Test Root", key=None, issuer=None, ca=True):
    key = key or ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = dt.datetime.now(dt.timezone.utc)
    builder = (x509.CertificateBuilder().subject_name(name).issuer_name(issuer or name).public_key(key.public_key())
               .serial_number(x509.random_serial_number()).not_valid_before(now).not_valid_after(now + dt.timedelta(days=30)))
    return key, builder.sign(key, hashes.SHA256())


async def test_certificate_install_list_delete(csms, make_station):
    st = await make_station()
    _, cert = make_cert()
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    assert (await csms.command("SIM1", "InstallCertificate", {"certificateType": "CSMSRootCertificate", "certificate": pem}))["status"] == "Accepted"
    listed = await csms.command("SIM1", "GetInstalledCertificateIds", {"certificateType": ["CSMSRootCertificate"]})
    chain = listed["certificateHashDataChain"]
    assert len(chain) == 1 and chain[0]["certificateType"] == "CSMSRootCertificate" and chain[0]["certificateHashData"]["hashAlgorithm"] == "SHA256"
    assert st.model.get_int("SecurityCtrlr", "CertificateEntries") == 1
    assert (await csms.command("SIM1", "GetInstalledCertificateIds", {"certificateType": ["V2GRootCertificate"]}))["status"] == "NotFound"
    assert (await csms.command("SIM1", "DeleteCertificate", {"certificateHashData": chain[0]["certificateHashData"]}))["status"] == "Accepted"
    assert (await csms.command("SIM1", "DeleteCertificate", {"certificateHashData": chain[0]["certificateHashData"]}))["status"] == "NotFound"
    assert (await csms.command("SIM1", "InstallCertificate", {"certificateType": "CSMSRootCertificate", "certificate": "garbage"}))["status"] == "Rejected"


async def test_sign_certificate_flow_with_a_real_csr(csms, make_station):
    st = await make_station()
    result = await st.sign_certificate()
    assert result["sent"] and result["status"] == "Accepted"
    csr_pem = csms.messages("SignCertificate")[0]["csr"]
    csr = x509.load_pem_x509_csr(csr_pem.encode())
    assert csr.is_signature_valid and csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "SIM1"

    ca_key, ca = make_cert("Test CA")
    leaf = (x509.CertificateBuilder().subject_name(csr.subject).issuer_name(ca.subject).public_key(csr.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(dt.datetime.now(dt.timezone.utc)).not_valid_after(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30)).sign(ca_key, hashes.SHA256()))
    chain = leaf.public_bytes(serialization.Encoding.PEM).decode() + ca.public_bytes(serialization.Encoding.PEM).decode()
    assert (await csms.command("SIM1", "CertificateSigned", {"certificateChain": chain}))["status"] == "Accepted"
    assert [c["type"] for c in st.certs] == ["ChargingStationCertificate"]
    # a certificate for somebody else's key is refused, and so is one without a pending CSR
    assert (await csms.command("SIM1", "CertificateSigned", {"certificateChain": chain}))["statusInfo"]["reasonCode"] == "NoPendingCSR"
    await st.sign_certificate()
    _, other = make_cert("Other")
    refused = await csms.command("SIM1", "CertificateSigned", {"certificateChain": other.public_bytes(serialization.Encoding.PEM).decode()})
    assert refused["status"] == "Rejected" and refused["statusInfo"]["reasonCode"] == "KeyMismatch"


# ------------------------------------------------------------------------ firmware and logs
async def test_firmware_update_installs_and_reboots(csms, make_station):
    st = await make_station()
    fw = {"location": "https://fw.example.com/charger-2.4.1.bin", "retrieveDateTime": st.now().isoformat()}
    assert (await csms.command("SIM1", "UpdateFirmware", {"requestId": 11, "firmware": fw}))["status"] == "Accepted"
    await csms.wait_for("FirmwareStatusNotification", count=5, timeout=15)
    await csms.wait_for("BootNotification", count=2, timeout=20)
    await csms.wait_for("FirmwareStatusNotification", timeout=20, predicate=lambda m: m["status"] == "Installed")
    seen = [m["status"] for m in csms.messages("FirmwareStatusNotification")]
    assert seen[:5] == ["Downloading", "Downloaded", "Installing", "InstallRebooting", "Installed"] or seen[-5:] == ["Downloading", "Downloaded", "Installing", "InstallRebooting", "Installed"]
    assert all(m["request_id"] == 11 for m in csms.messages("FirmwareStatusNotification"))
    assert csms.messages("BootNotification")[1]["reason"] == "FirmwareUpdate"
    assert csms.messages("BootNotification")[1]["charging_station"]["firmware_version"] == "2.4.1"
    assert st.model.get_value("ChargingStation", "FirmwareVersion") == "2.4.1"


@pytest.mark.parametrize("stage, final, with_signature", [("download", "DownloadFailed", False), ("signature", "InvalidSignature", True), ("install", "InstallationFailed", False)])
async def test_firmware_update_failures(csms, make_station, stage, final, with_signature):
    st = await make_station()
    await st.inject_fault("firmware_update_failure", None, {"stage": stage})
    fw = {"location": "https://fw.example.com/x.bin", "retrieveDateTime": st.now().isoformat()}
    if with_signature:
        fw["signature"] = "c2ln"
    await csms.command("SIM1", "UpdateFirmware", {"requestId": 12, "firmware": fw})
    await csms.wait_for("FirmwareStatusNotification", timeout=15, predicate=lambda m: m["status"] == final)
    await asyncio.sleep(0.5)
    assert len(csms.messages("BootNotification")) == 1  # a failed update never reboots the station


async def test_firmware_update_input_checks_and_cancelling(csms, make_station):
    st = await make_station()
    bad = await csms.command("SIM1", "UpdateFirmware", {"requestId": 1, "firmware": {"location": "file:///etc/passwd", "retrieveDateTime": st.now().isoformat()}})
    assert bad["status"] == "Rejected"
    later = (st.now() + dt.timedelta(hours=1)).isoformat()
    assert (await csms.command("SIM1", "UpdateFirmware", {"requestId": 2, "firmware": {"location": "https://f.example.com/a.bin", "retrieveDateTime": later}}))["status"] == "Accepted"
    again = await csms.command("SIM1", "UpdateFirmware", {"requestId": 3, "firmware": {"location": "https://f.example.com/b.bin", "retrieveDateTime": later}})
    assert again["status"] == "AcceptedCanceled"


async def test_get_log_uploads_or_fails(csms, make_station):
    st = await make_station()
    reply = await csms.command("SIM1", "GetLog", {"log": {"remoteLocation": "https://logs.example.com/u"}, "logType": "SecurityLog", "requestId": 4})
    assert reply["status"] == "Accepted" and reply["filename"].startswith("SIM1-")
    await csms.wait_for("LogStatusNotification", count=2, timeout=8)
    assert [m["status"] for m in csms.messages("LogStatusNotification")][:2] == ["Uploading", "Uploaded"]
    assert (await csms.command("SIM1", "GetLog", {"log": {"remoteLocation": "gopher://x"}, "logType": "SecurityLog", "requestId": 5}))["status"] == "Rejected"
    await st.inject_fault("firmware_update_failure", None, {"stage": "log"})
    await csms.command("SIM1", "GetLog", {"log": {"remoteLocation": "https://logs.example.com/u"}, "logType": "DiagnosticsLog", "requestId": 6})
    await csms.wait_for("LogStatusNotification", timeout=8, predicate=lambda m: m["status"] == "UploadFailure")


async def test_publish_and_unpublish_firmware(csms, make_station):
    st = await make_station()
    checksum = "0123456789abcdef0123456789abcdef"
    assert (await csms.command("SIM1", "PublishFirmware", {"location": "https://f.example.com/a.bin", "checksum": checksum, "requestId": 1}))["status"] == "Accepted"
    await csms.wait_for("PublishFirmwareStatusNotification", timeout=10, predicate=lambda m: m["status"] == "Published")
    states = [m["status"] for m in csms.messages("PublishFirmwareStatusNotification")]
    assert states == ["DownloadScheduled", "Downloading", "Downloaded", "ChecksumVerified", "Published"]
    assert (await csms.command("SIM1", "UnpublishFirmware", {"checksum": checksum}))["status"] == "Unpublished"
    assert (await csms.command("SIM1", "UnpublishFirmware", {"checksum": checksum}))["status"] == "NoFirmware"
