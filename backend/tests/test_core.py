"""Connection, boot, connectors and the session lifecycle."""
import asyncio

import pytest

from ocpp_simulator.mock_csms import MockCsms
from tests.conftest import tx_events, wait_until


async def test_boot_reports_every_connector_available(csms, make_station):
    st = await make_station("dc_60_dual")  # 2 EVSEs, 3 connectors in total
    boot = (await csms.wait_for("BootNotification", cp_id="SIM1"))[0]
    assert boot["reason"] == "PowerUp"
    assert boot["charging_station"]["model"] == "SIM-DC-60" and boot["charging_station"]["vendor_name"] == "SimCharge"
    statuses = {(m["evse_id"], m["connector_id"]): m["connector_status"] for m in csms.messages("StatusNotification", "SIM1")}
    assert statuses == {(1, 1): "Available", (1, 2): "Available", (2, 1): "Available"}
    assert st.state == "online" and st.boot_status == "Accepted"
    # the boot interval from the CSMS becomes the heartbeat interval
    assert st.model.get_int("OCPPCommCtrlr", "HeartbeatInterval") == 30
    assert len(csms.messages("StatusNotification", "SIM1")) == 3  # exactly one per connector, no duplicates


async def test_basic_auth_is_sent_and_a_wrong_password_is_refused(make_station, csms):
    csms.passwords["SECURE"] = "s3cret"
    bad = await make_station(ocpp_id="SECURE", connect=False, password="wrong")
    await bad.connect()
    await wait_until(lambda: bad.last_error is not None, message="auth failure")
    assert "refused" in bad.last_error.lower() or "401" in bad.last_error
    assert not bad.online
    await bad.disconnect()

    good = await make_station(ocpp_id="SECURE", connect=False, password="s3cret")
    await good.connect()
    await wait_until(lambda: good.online, message="authenticated")


async def test_pending_boot_keeps_retrying_until_accepted(csms, make_station):
    csms.boot_status, csms.interval = "Pending", 1
    st = await make_station(connect=False)
    await st.connect()
    await wait_until(lambda: len(csms.messages("BootNotification")) >= 2, message="boot retry")
    assert st.boot_status == "Pending" and not st.online
    csms.boot_status = "Accepted"
    await wait_until(lambda: st.online, message="accepted after retry")


async def test_rfid_session_full_lifecycle(csms, make_station):
    st = await make_station()
    await st.plug_in(1, ev="mid", soc=20)
    assert [e["status"] for e in st.snapshot()["evses"][0]["connectors"]] == ["Occupied"]
    assert (await st.tap_card(1, "CARD1"))["status"] == "Accepted"
    await wait_until(lambda: st.evses[0].tx and st.evses[0].tx.energy_wh > 20, message="energy accrues")
    snap = st.snapshot()["evses"][0]
    assert snap["tx"]["charging_state"] == "Charging" and snap["power_kw"] == 11.0  # AC 22 kW pedestal, vehicle limited to 11 kW

    await csms.wait_for("TransactionEvent", predicate=lambda m: m["event_type"] == "Updated" and m["trigger_reason"] == "MeterValuePeriodic")
    assert (await st.tap_card(1, "CARD1"))["status"] == "Stopped"
    await wait_until(lambda: "Ended" in [e for e, _ in tx_events(csms)], message="Ended")

    events = csms.messages("TransactionEvent", "SIM1")
    assert events[0]["event_type"] == "Started" and events[0]["trigger_reason"] == "ChargingStateChanged"
    assert events[0]["id_token"] == {"id_token": "CARD1", "type": "ISO14443"} and events[0]["transaction_info"]["charging_state"] == "Charging"
    assert [e["seq_no"] for e in events] == list(range(len(events)))            # strictly increasing from 0
    assert len({e["transaction_info"]["transaction_id"] for e in events}) == 1  # one transaction id throughout
    ended = events[-1]
    assert ended["event_type"] == "Ended" and ended["transaction_info"]["stopped_reason"] == "Local" and ended["trigger_reason"] == "StopAuthorized"
    energy = [sv["value"] for mv in ended["meter_value"] for sv in mv["sampled_value"] if sv["measurand"] == "Energy.Active.Import.Register"]
    assert energy and energy[0] > events[0]["meter_value"][0]["sampled_value"][0]["value"]  # the register moved during the session
    assert st.evses[0].tx is None and st.snapshot()["evses"][0]["connectors"][0]["status"] == "Occupied"  # still plugged in
    await st.unplug(1)
    await wait_until(lambda: st.snapshot()["evses"][0]["connectors"][0]["status"] == "Available")


async def test_unknown_token_is_refused_and_no_transaction_starts(csms, make_station):
    csms.allowed_tokens = {"GOOD"}
    st = await make_station()
    await st.plug_in(1)
    assert (await st.tap_card(1, "BAD"))["status"] == "Invalid"
    assert st.evses[0].tx is None and "TransactionEvent" not in csms.actions("SIM1")
    assert (await st.tap_card(1, "GOOD"))["status"] == "Accepted"
    await wait_until(lambda: st.evses[0].tx is not None)


async def test_authorize_first_then_plug_in(csms, make_station):
    st = await make_station()
    assert (await st.tap_card(1, "EARLY"))["status"] == "Accepted"
    assert st.evses[0].tx is None  # PowerPathClosed start point: nothing until a vehicle is connected
    await st.plug_in(1)
    await wait_until(lambda: st.evses[0].tx is not None, message="session starts after plug-in")
    started = (await csms.wait_for("TransactionEvent", predicate=lambda m: m["event_type"] == "Started"))[0]
    assert started["id_token"]["id_token"] == "EARLY"


async def test_unplugging_ends_the_session_with_ev_disconnected(csms, make_station):
    st = await make_station()
    await st.plug_in(1)
    await st.tap_card(1, "C1")
    await wait_until(lambda: st.evses[0].tx and st.evses[0].tx.charging_state == "Charging")
    await st.unplug(1)
    ended = (await csms.wait_for("TransactionEvent", predicate=lambda m: m["event_type"] == "Ended"))[0]
    assert ended["transaction_info"]["stopped_reason"] == "EVDisconnected" and ended["trigger_reason"] == "EVDeparted"
    assert st.snapshot()["evses"][0]["connectors"][0]["status"] == "Available"


async def test_full_battery_ends_the_session_by_the_vehicle(csms, make_station):
    st = await make_station(time_scale=600)
    await st.plug_in(1, ev="scooter", soc=95)  # a 3 kWh scooter at 1.5 kW fills up in minutes of simulated time
    await st.tap_card(1, "C1")
    ended = (await csms.wait_for("TransactionEvent", timeout=30, predicate=lambda m: m["event_type"] == "Ended"))[0]
    assert ended["transaction_info"]["stopped_reason"] == "StoppedByEV"
    states = [m["transaction_info"].get("charging_state") for m in csms.messages("TransactionEvent")]
    assert "SuspendedEV" in states
    assert st.evses[0].ev.soc >= 99.9


async def test_each_evse_runs_its_own_session_at_the_same_time(csms, make_station):
    st = await make_station()
    for evse in (1, 2):
        await st.plug_in(evse)
        await st.tap_card(evse, f"CARD{evse}")
    await wait_until(lambda: all(e.tx and e.tx.charging_state == "Charging" for e in st.evses))
    ids = {e.tx.id for e in st.evses}
    assert len(ids) == 2
    await st.stop_session(1)
    assert st.evses[1].tx is not None and st.evses[0].tx is None  # the other session is untouched


async def test_only_one_connector_of_an_evse_can_be_used(make_station):
    st = await make_station("dc_60_dual")
    await st.plug_in(1, connector_id=1)
    with pytest.raises(ValueError, match="already has a vehicle"):
        await st.plug_in(1, connector_id=2)
    statuses = [c["status"] for c in st.snapshot()["evses"][0]["connectors"]]
    assert statuses == ["Occupied", "Unavailable"]


async def test_heartbeats_follow_the_configured_interval(csms, make_station):
    st = await make_station()
    st.model.write({"name": "OCPPCommCtrlr"}, {"name": "HeartbeatInterval"}, "1")
    await csms.wait_for("Heartbeat", count=2, timeout=8)


async def test_disconnect_and_reconnect(csms, make_station):
    st = await make_station()
    await st.disconnect()
    await wait_until(lambda: "SIM1" not in csms.connections, message="closed")
    assert st.state == "stopped"
    await st.connect()
    await wait_until(lambda: st.online)
    assert len(csms.messages("BootNotification")) == 2
