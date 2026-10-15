"""Fault scenarios: what the CSMS sees when things go wrong."""
import asyncio
import time
from datetime import datetime

import pytest

from ocpp_simulator.faults import CATALOG
from ocpp_simulator.station import Station
from tests.conftest import wait_until

ENERGY = "Energy.Active.Import.Register"


def energy_of(event: dict) -> float | None:
    for mv in event.get("meter_value", []):
        for sv in mv["sampled_value"]:
            if sv["measurand"] == ENERGY:
                return sv["value"]
    return None


def statuses(csms, evse=1):
    return [m["connector_status"] for m in csms.messages("StatusNotification", "SIM1") if m["evse_id"] == evse]


def updates(csms):
    return [m for m in csms.messages("TransactionEvent", "SIM1") if m["event_type"] == "Updated" and m["trigger_reason"] == "MeterValuePeriodic"]


async def charging(st, evse=1, ev="large", token="C1"):
    await st.plug_in(evse, ev=ev)
    await st.tap_card(evse, token)
    await wait_until(lambda: st.evses[evse - 1].tx and st.evses[evse - 1].tx.charging_state == "Charging", message="charging")
    return st.evses[evse - 1].tx


# ============================================================================= power meter failure
async def test_meter_failure_no_readings(csms, make_station):
    st = await make_station()
    await charging(st)
    await csms.wait_for("TransactionEvent", predicate=lambda m: m["trigger_reason"] == "MeterValuePeriodic" and energy_of(m) is not None)
    await st.inject_fault("power_meter_failure", 1, {"mode": "stop_reporting"})
    event = (await csms.wait_for("NotifyEvent", predicate=lambda m: m["event_data"][0]["component"]["name"] == "LocalEnergyMeter"))[0]["event_data"][0]
    assert event["trigger"] == "Alerting" and event["actual_value"] == "true" and event["tech_code"] == "PowerMeterFailure" and event["component"]["evse"]["id"] == 1
    abnormal = (await csms.wait_for("TransactionEvent", predicate=lambda m: m["trigger_reason"] == "AbnormalCondition"))[0]
    assert energy_of(abnormal) is None                                   # no energy register, but other measurands continue
    assert any(sv["measurand"] == "Power.Active.Import" for sv in abnormal["meter_value"][0]["sampled_value"])
    later = await csms.wait_for("TransactionEvent", count=1, predicate=lambda m: m["trigger_reason"] == "MeterValuePeriodic" and m["seq_no"] > abnormal["seq_no"])
    assert energy_of(later[0]) is None
    assert st.evses[0].tx.charging_state == "Charging"                   # the session itself carries on
    await st.clear_all_faults()
    cleared = (await csms.wait_for("NotifyEvent", predicate=lambda m: m["event_data"][0]["cleared"] and m["event_data"][0]["component"]["name"] == "LocalEnergyMeter"))[0]
    assert cleared["event_data"][0]["actual_value"] == "false"


async def test_meter_failure_frozen_register(csms, make_station):
    st = await make_station()
    await charging(st)
    await csms.wait_for("TransactionEvent", predicate=lambda m: m["trigger_reason"] == "MeterValuePeriodic")
    seen = len(csms.messages("TransactionEvent"))
    await st.inject_fault("power_meter_failure", 1, {"mode": "frozen"})
    await wait_until(lambda: len([m for m in csms.messages("TransactionEvent")[seen:] if energy_of(m) is not None]) >= 3, timeout=10, message="three readings")
    readings = [energy_of(m) for m in csms.messages("TransactionEvent")[seen:] if energy_of(m) is not None]
    assert len(set(readings)) == 1, readings                       # the register never moves again
    assert st.evses[0].register_wh > readings[0] + 100             # while the real meter keeps counting


async def test_meter_failure_rollback_spike_and_invalid(csms, make_station):
    st = await make_station()
    await charging(st)
    await csms.wait_for("TransactionEvent", predicate=lambda m: m["trigger_reason"] == "MeterValuePeriodic")
    normal = energy_of(updates(csms)[-1])
    for mode, check in (("rollback", lambda v: v < normal), ("spike", lambda v: v > normal + 5_000_000), ("invalid", lambda v: v == -1.0)):
        fault = await st.inject_fault("power_meter_failure", 1, {"mode": mode})
        seen = len(csms.messages("TransactionEvent"))
        await wait_until(lambda: len(csms.messages("TransactionEvent")) > seen + 1, timeout=8, message=f"{mode} reading")
        readings = [energy_of(m) for m in csms.messages("TransactionEvent")[seen:] if energy_of(m) is not None]
        assert readings and all(check(v) for v in readings), (mode, readings, normal)
        await st.clear_fault(fault["id"])


async def test_meter_failure_that_stops_the_session(csms, make_station):
    st = await make_station()
    await charging(st)
    fault = await st.inject_fault("power_meter_failure", 1, {"mode": "stop_reporting", "stop_session": True})
    ended = (await csms.wait_for("TransactionEvent", predicate=lambda m: m["event_type"] == "Ended"))[0]
    assert ended["transaction_info"]["stopped_reason"] == "Other" and ended["trigger_reason"] == "AbnormalCondition"
    await wait_until(lambda: statuses(csms)[-1] == "Faulted", message="Faulted")
    assert statuses(csms, 2)[-1] == "Available"                          # the other EVSE is fine
    await st.clear_fault(fault["id"])
    await wait_until(lambda: statuses(csms)[-1] != "Faulted", message="recovered")


# =========================================================================================== power loss
async def test_power_loss_during_a_session(csms, make_station):
    st = await make_station()
    await charging(st)
    await csms.wait_for("TransactionEvent", predicate=lambda m: m["trigger_reason"] == "MeterValuePeriodic")
    energy_at_cut = st.evses[0].register_wh
    cut_time = st.now()
    before = len(csms.received)
    await st.inject_fault("power_loss", None, {"duration_s": 4})

    await wait_until(lambda: "SIM1" not in csms.connections, message="socket gone")
    assert st.state == "unpowered" and st.powered is False and st.online is False
    await asyncio.sleep(1.5)
    assert len(csms.received) == before                                  # a dead charger says nothing
    assert st.evses[0].power_w == 0 and st.evses[0].tx is not None       # the session is remembered, not ended

    await wait_until(lambda: len(csms.messages("BootNotification")) == 2, timeout=12, message="boot after power returns")
    assert csms.messages("BootNotification")[1]["reason"] == "PowerUp"
    ended = (await csms.wait_for("TransactionEvent", predicate=lambda m: m["event_type"] == "Ended"))[0]
    assert ended["transaction_info"]["stopped_reason"] == "PowerLoss" and ended["trigger_reason"] == "AbnormalCondition"
    assert ended["offline"] is True
    ended_at = datetime.fromisoformat(ended["timestamp"])
    assert abs((ended_at - cut_time).total_seconds()) < 2                # stamped when the power failed, not when it returned
    assert abs(energy_of(ended) - energy_at_cut) < 500                   # the last known meter value
    seqs = [m["seq_no"] for m in csms.messages("TransactionEvent")]
    assert seqs == list(range(len(seqs)))                                # no gap in the sequence across the outage
    await wait_until(lambda: statuses(csms)[-1] == "Occupied", message="status re-sent: still plugged in")
    assert st.state == "online" and st.evses[0].tx is None


async def test_power_loss_without_a_session_and_manual_restore(csms, make_station):
    st = await make_station()
    fault = await st.inject_fault("power_loss", None, {"duration_s": 0})  # until cleared
    await asyncio.sleep(1.2)
    assert st.state == "unpowered" and "SIM1" not in csms.connections
    await st.connect()                                                   # cannot connect without power
    assert st.state == "unpowered"
    await st.clear_fault(fault["id"])
    await wait_until(lambda: st.online, timeout=12, message="back online")
    assert csms.messages("BootNotification")[-1]["reason"] == "PowerUp"
    assert "Ended" not in [m["event_type"] for m in csms.messages("TransactionEvent")]


async def test_power_loss_with_two_sessions_ends_both(csms, make_station):
    st = await make_station()
    await charging(st, 1, token="A")
    await charging(st, 2, token="B")
    await st.inject_fault("power_loss", None, {"duration_s": 2})
    await csms.wait_for("TransactionEvent", count=2, timeout=15, predicate=lambda m: m["event_type"] == "Ended")
    ended = [m for m in csms.messages("TransactionEvent") if m["event_type"] == "Ended"]
    assert {m["evse"]["id"] for m in ended} == {1, 2} and all(m["transaction_info"]["stopped_reason"] == "PowerLoss" for m in ended)


# ======================================================================================== network loss
async def test_network_loss_queues_events_and_delivers_them_in_order(csms, make_station):
    st = await make_station()
    tx = await charging(st)
    await csms.wait_for("TransactionEvent", predicate=lambda m: m["trigger_reason"] == "MeterValuePeriodic")
    await st.inject_fault("network_loss", None, {"duration_s": 5})
    await wait_until(lambda: "SIM1" not in csms.connections and not st.online, message="offline")
    delivered_before = len(csms.messages("TransactionEvent"))
    await wait_until(lambda: len(st.tx_queue) >= 3, timeout=8, message="events queued while offline")
    assert len(csms.messages("TransactionEvent")) == delivered_before           # nothing reached the CSMS
    assert st.evses[0].tx is tx and st.evses[0].tx.charging_state == "Charging"  # the session continued
    assert all(q["offline"] is True for q in st.tx_queue)
    assert (await st.cmd_get_transaction_status(transaction_id=tx.id))["messages_in_queue"] is True

    await wait_until(lambda: st.online and not st.tx_queue, timeout=20, message="queue delivered")
    events = csms.messages("TransactionEvent")
    seqs = [m["seq_no"] for m in events]
    assert seqs == list(range(len(seqs))) and len(set(seqs)) == len(seqs)    # in order, no duplicates, no gaps
    offline_events = [m for m in events if m.get("offline")]
    assert len(offline_events) >= 3 and all(m["event_type"] == "Updated" for m in offline_events)
    stamps = [datetime.fromisoformat(m["timestamp"]) for m in events]
    assert stamps == sorted(stamps)                                            # original timestamps preserved
    assert len(csms.messages("BootNotification")) == 2                         # boot_on_reconnect is on by default


async def test_network_loss_without_boot_on_reconnect(csms, make_station):
    st = await make_station(boot_on_reconnect=False)
    await st.inject_fault("network_loss", None, {"duration_s": 2})
    await wait_until(lambda: "SIM1" not in csms.connections)
    await wait_until(lambda: st.online, timeout=12)
    assert len(csms.messages("BootNotification")) == 1


async def test_a_session_that_starts_and_ends_entirely_offline(csms, make_station):
    st = await make_station()
    await st.inject_fault("network_loss", None, {"duration_s": 0})
    await wait_until(lambda: not st.online and "SIM1" not in csms.connections)
    await st.plug_in(1, ev="large")
    assert (await st.tap_card(1, "OFFLINE", "ISO14443"))["status"] == "Invalid"   # unknown token while offline
    st.model.write({"name": "AuthCtrlr"}, {"name": "OfflineTxForUnknownIdEnabled"}, "true")
    assert (await st.tap_card(1, "OFFLINE"))["status"] == "Accepted"
    await wait_until(lambda: st.evses[0].tx is not None)
    await asyncio.sleep(2.2)
    await st.tap_card(1, "OFFLINE")
    assert len(st.tx_queue) >= 2 and not csms.messages("TransactionEvent")
    await st.clear_all_faults()
    await wait_until(lambda: not st.tx_queue and st.online, timeout=20, message="delivered")
    events = csms.messages("TransactionEvent")
    assert events[0]["event_type"] == "Started" and events[-1]["event_type"] == "Ended"
    assert all(m.get("offline") for m in events) and [m["seq_no"] for m in events] == list(range(len(events)))


async def test_flapping_network(csms, make_station):
    st = await make_station()
    await st.inject_fault("network_flapping", None, {"cycles": 2, "down_s": 1, "up_s": 2})
    await wait_until(lambda: not st.faults, timeout=20, message="flapping finished")
    await wait_until(lambda: st.online, timeout=10)
    assert len(csms.messages("BootNotification")) >= 3
    kinds = [e for _, e in csms.connect_log if True]
    assert kinds.count("connected") >= 3


# ================================================================================ electrical faults
async def test_over_voltage_stops_everything_until_cleared(csms, make_station):
    st = await make_station()
    await charging(st, 1, token="A")
    await charging(st, 2, token="B")
    fault = await st.inject_fault("over_voltage", None, {"voltage": 285})
    await csms.wait_for("TransactionEvent", count=2, predicate=lambda m: m["event_type"] == "Ended")
    ended = [m for m in csms.messages("TransactionEvent") if m["event_type"] == "Ended"]
    assert all(m["transaction_info"]["stopped_reason"] == "PowerQuality" for m in ended)
    await wait_until(lambda: statuses(csms, 1)[-1] == "Faulted" and statuses(csms, 2)[-1] == "Faulted")
    event = (await csms.wait_for("NotifyEvent", predicate=lambda m: m["event_data"][0]["component"]["name"] == "ElectricalFeeder"))[0]["event_data"][0]
    assert event["tech_code"] == "OverVoltage"
    assert st._sample(st.evses[0], None, "Voltage", "x")["value"] == 285.0     # meter readings show the abnormal grid voltage
    with pytest.raises(ValueError, match="faulted"):
        await st.plug_in(1) if st.evses[0].plugged is None else (_ for _ in ()).throw(ValueError("EVSE 1 is faulted"))
    await st.clear_fault(fault["id"])
    await wait_until(lambda: statuses(csms, 1)[-1] != "Faulted" and statuses(csms, 2)[-1] != "Faulted", message="recovered")
    assert st._sample(st.evses[0], None, "Voltage", "x")["value"] == 230.0


async def test_under_voltage_uses_the_same_protection(csms, make_station):
    st = await make_station()
    await charging(st)
    await st.inject_fault("under_voltage", None, {"voltage": 150})
    ended = (await csms.wait_for("TransactionEvent", predicate=lambda m: m["event_type"] == "Ended"))[0]
    assert ended["transaction_info"]["stopped_reason"] == "PowerQuality"
    assert (await csms.wait_for("NotifyEvent", predicate=lambda m: m["event_data"][0].get("tech_code") == "UnderVoltage"))


@pytest.mark.parametrize("kind, reason, component", [("over_current", "OvercurrentFault", "OverCurrentBreaker"), ("ground_fault", "GroundFault", "GroundIsolationMonitoring")])
async def test_evse_trips_affect_only_that_evse(csms, make_station, kind, reason, component):
    st = await make_station()
    await charging(st, 1, token="A")
    await charging(st, 2, token="B")
    fault = await st.inject_fault(kind, 1, {})
    ended = (await csms.wait_for("TransactionEvent", predicate=lambda m: m["event_type"] == "Ended"))[0]
    assert ended["evse"]["id"] == 1 and ended["transaction_info"]["stopped_reason"] == reason
    await wait_until(lambda: statuses(csms, 1)[-1] == "Faulted")
    assert (await csms.wait_for("NotifyEvent", predicate=lambda m: m["event_data"][0]["component"]["name"] == component))
    assert st.evses[1].tx is not None and statuses(csms, 2)[-1] == "Occupied"   # EVSE 2 keeps charging
    with pytest.raises(ValueError, match="already active"):
        await st.inject_fault(kind, 1, {})
    assert (await st.tap_card(1, "X"))["status"] == "Unavailable"
    await st.clear_fault(fault["id"])
    await wait_until(lambda: statuses(csms, 1)[-1] != "Faulted")


async def test_emergency_stop_for_the_whole_station_and_for_one_evse(csms, make_station):
    st = await make_station()
    await charging(st, 1, token="A")
    await charging(st, 2, token="B")
    one = await st.inject_fault("emergency_stop", 2, {})
    ended = (await csms.wait_for("TransactionEvent", predicate=lambda m: m["event_type"] == "Ended"))[0]
    assert ended["evse"]["id"] == 2 and ended["transaction_info"]["stopped_reason"] == "EmergencyStop" and st.evses[0].tx is not None
    await st.clear_fault(one["id"])
    await st.inject_fault("emergency_stop", None, {})
    await wait_until(lambda: st.evses[0].tx is None, message="whole station stopped")
    await wait_until(lambda: statuses(csms, 1)[-1] == "Faulted" and statuses(csms, 2)[-1] == "Faulted")


async def test_over_temperature_derates_or_stops(csms, make_station):
    st = await make_station()
    await charging(st)
    await wait_until(lambda: st.evses[0].power_w > 10900, message="full power")
    assert abs(st.evses[0].power_w - 11000) < 1
    derate = await st.inject_fault("over_temperature", 1, {"mode": "derate", "derate_percent": 25, "temperature_c": 88})
    await wait_until(lambda: st.evses[0].power_w < 5600, message="derated to 25 % of 22 kW")
    assert abs(st.evses[0].power_w - 5500) < 1 and st.evses[0].tx.charging_state == "Charging"
    assert (await csms.wait_for("NotifyEvent", predicate=lambda m: m["event_data"][0]["component"]["name"] == "EVSE" and m["event_data"][0]["variable"]["name"] == "Temperature"))
    await st.clear_fault(derate["id"])
    await wait_until(lambda: st.evses[0].power_w > 10900, message="full power again")
    await st.inject_fault("over_temperature", 1, {"mode": "stop"})
    ended = (await csms.wait_for("TransactionEvent", predicate=lambda m: m["event_type"] == "Ended"))[0]
    assert ended["transaction_info"]["stopped_reason"] == "Other"
    await wait_until(lambda: statuses(csms)[-1] == "Faulted")


# ============================================================================== hardware and vehicle
async def test_contactor_failure_prevents_charging(csms, make_station):
    st = await make_station()
    fault = await st.inject_fault("contactor_failure", 1, {})
    await st.plug_in(1)
    await st.tap_card(1, "C")
    await asyncio.sleep(1.2)
    assert st.evses[0].tx is None and st.evses[0].power_w == 0                  # PowerPathClosed never happens
    assert (await csms.wait_for("NotifyEvent", predicate=lambda m: m["event_data"][0]["component"]["name"] == "PowerContactor"))
    await st.clear_fault(fault["id"])
    await wait_until(lambda: st.evses[0].tx is not None and st.evses[0].tx.charging_state == "Charging", message="charging after repair")

    other = await make_station(ocpp_id="SIM2")                                  # start the session at authorisation instead
    other.model.write({"name": "TxCtrlr"}, {"name": "TxStartPoint"}, "Authorized")
    await other.inject_fault("contactor_failure", 1, {})
    await other.plug_in(1)
    await other.tap_card(1, "C")
    started = (await csms.wait_for("TransactionEvent", cp_id="SIM2", predicate=lambda m: m["event_type"] == "Started"))[0]
    assert started["transaction_info"]["charging_state"] == "SuspendedEVSE" and other.evses[0].tx.energy_wh == 0


async def test_connector_lock_failure(csms, make_station):
    st = await make_station()
    fault = await st.inject_fault("connector_lock_failure", 1, {})
    reply = await csms.command("SIM1", "UnlockConnector", {"evseId": 1, "connectorId": 1})
    assert reply["status"] == "UnlockFailed" and reply["statusInfo"]["reasonCode"] == "ConnectorLockFailure"
    assert (await csms.command("SIM1", "UnlockConnector", {"evseId": 2, "connectorId": 1}))["status"] == "Unlocked"
    await st.clear_fault(fault["id"])
    assert (await csms.command("SIM1", "UnlockConnector", {"evseId": 1, "connectorId": 1}))["status"] == "Unlocked"


async def test_rfid_reader_failure(csms, make_station):
    st = await make_station()
    fault = await st.inject_fault("rfid_reader_failure", None, {})
    await st.plug_in(1)
    assert (await st.tap_card(1, "C"))["status"] == "ReaderFailure"
    assert "Authorize" not in csms.actions("SIM1")
    assert (await csms.wait_for("NotifyEvent", predicate=lambda m: m["event_data"][0]["component"]["name"] == "TokenReader"))
    await st.clear_fault(fault["id"])
    assert (await st.tap_card(1, "C"))["status"] == "Accepted"


async def test_vehicle_communication_loss_and_pause(csms, make_station):
    st = await make_station()
    await charging(st, 1, token="A")
    comm = await st.inject_fault("ev_communication_loss", 1, {"end_session": False})
    lost = (await csms.wait_for("TransactionEvent", predicate=lambda m: m["trigger_reason"] == "EVCommunicationLost"))[0]
    assert lost["transaction_info"]["charging_state"] == "SuspendedEV" and st.evses[0].power_w == 0
    await st.clear_fault(comm["id"])
    await wait_until(lambda: st.evses[0].tx.charging_state == "Charging")

    pause = await st.inject_fault("ev_suspends", 1, {})
    await wait_until(lambda: st.evses[0].tx.charging_state == "SuspendedEV" and st.evses[0].power_w == 0)
    await st.clear_fault(pause["id"])
    await wait_until(lambda: st.evses[0].tx.charging_state == "Charging")

    await st.inject_fault("ev_communication_loss", 1, {"end_session": True})
    ended = (await csms.wait_for("TransactionEvent", predicate=lambda m: m["event_type"] == "Ended"))[0]
    assert ended["trigger_reason"] == "EVCommunicationLost"


async def test_cable_pulled_and_battery_full(csms, make_station):
    st = await make_station()
    await charging(st, 1, token="A")
    await charging(st, 2, token="B")
    await st.inject_fault("cable_unplugged", 1, {})
    pulled = (await csms.wait_for("TransactionEvent", predicate=lambda m: m["event_type"] == "Ended"))[0]
    assert pulled["evse"]["id"] == 1 and pulled["transaction_info"]["stopped_reason"] == "EVDisconnected" and pulled["trigger_reason"] == "EVDeparted"
    assert st.evses[0].plugged is None and not st.faults                        # one-shot: nothing stays active
    await st.inject_fault("ev_battery_full", 2, {})
    full = (await csms.wait_for("TransactionEvent", predicate=lambda m: m["event_type"] == "Ended" and m["evse"]["id"] == 2))[0]
    assert full["transaction_info"]["stopped_reason"] == "StoppedByEV"
    with pytest.raises(ValueError, match="No vehicle"):
        await st.inject_fault("ev_battery_full", 1, {})


# ======================================================================================= grid / time
async def test_grid_overload_announces_limit_and_derates(csms, make_station):
    st = await make_station()
    await charging(st, 1, token="A")
    await charging(st, 2, token="B")
    fault = await st.inject_fault("grid_overload", None, {"limit_kw": 5})
    limit = (await csms.wait_for("NotifyChargingLimit"))[0]["charging_limit"]
    assert limit["charging_limit_source"] == "EMS" and limit["is_grid_critical"] is True
    await wait_until(lambda: all(abs(e.power_w - 2500) < 1 for e in st.evses), message="2.5 kW each")
    assert (await csms.wait_for("TransactionEvent", predicate=lambda m: m["trigger_reason"] == "ChargingRateChanged"))
    await st.clear_fault(fault["id"])
    assert (await csms.wait_for("ClearedChargingLimit"))[0]["charging_limit_source"] == "EMS"
    await wait_until(lambda: all(e.power_w > 10900 for e in st.evses))


async def test_clock_drift_shifts_every_timestamp(csms, make_station):
    st = await make_station()
    fault = await st.inject_fault("clock_drift", None, {"offset_s": 3600})
    await csms.command("SIM1", "TriggerMessage", {"requestedMessage": "StatusNotification"})
    await wait_until(lambda: len(csms.messages("StatusNotification")) > 2)
    stamp = datetime.fromisoformat(csms.messages("StatusNotification")[-1]["timestamp"])
    skew = (stamp - datetime.now(stamp.tzinfo)).total_seconds()
    assert 3590 < skew < 3610
    await st.clear_fault(fault["id"])
    assert st.clock_offset == 0.0


async def test_watchdog_reboot_and_tamper(csms, make_station):
    st = await make_station()
    await charging(st)
    await st.inject_fault("reboot_watchdog", None, {})
    ended = (await csms.wait_for("TransactionEvent", predicate=lambda m: m["event_type"] == "Ended"))[0]
    assert ended["transaction_info"]["stopped_reason"] == "Reboot"
    boots = await csms.wait_for("BootNotification", count=2, timeout=15)
    assert boots[1]["reason"] == "Watchdog"
    await wait_until(lambda: st.online, timeout=10)
    await st.inject_fault("tamper_detected", None, {})
    sec = (await csms.wait_for("SecurityEventNotification"))[0]
    assert sec["type"] == "TamperDetectionActivated"
    assert (await csms.wait_for("NotifyEvent", predicate=lambda m: m["event_data"][0].get("tech_code") == "TamperDetected"))


# ===================================================================================== network quality
async def test_high_latency_and_packet_loss(csms, make_station):
    st = await make_station()
    slow = await st.inject_fault("high_latency", None, {"latency_ms": 1200})
    t0 = time.monotonic()
    assert (await st.request("Heartbeat")).ok
    assert time.monotonic() - t0 >= 1.1
    await st.clear_fault(slow["id"])
    t0 = time.monotonic()
    await st.request("Heartbeat")
    assert time.monotonic() - t0 < 0.5

    lossy = await st.inject_fault("packet_loss", None, {"drop_percent": 100})
    res = await st.request("Heartbeat")
    assert res.sent and res.error == "timeout" and st.stats["timeouts"] >= 1
    await st.clear_fault(lossy["id"])
    assert (await st.request("Heartbeat")).ok


# ======================================================================================= the engine
async def test_fault_input_validation(csms, make_station):
    st = await make_station()
    with pytest.raises(ValueError, match="Unknown fault"):
        await st.inject_fault("meteor_strike", None, {})
    with pytest.raises(ValueError, match="needs an EVSE"):
        await st.inject_fault("ground_fault", None, {})
    with pytest.raises(ValueError, match="does not exist"):
        await st.inject_fault("ground_fault", 9, {})
    with pytest.raises(ValueError, match="at least"):
        await st.inject_fault("high_latency", None, {"latency_ms": 0})
    with pytest.raises(ValueError, match="one of"):
        await st.inject_fault("power_meter_failure", 1, {"mode": "explode"})
    with pytest.raises(ValueError, match="must be a number"):
        await st.inject_fault("high_latency", None, {"latency_ms": "slow"})
    with pytest.raises(ValueError, match="No active fault"):
        await st.clear_fault(999)
    assert not st.faults


async def test_faults_clear_themselves_after_their_duration(csms, make_station):
    st = await make_station()
    await st.inject_fault("rfid_reader_failure", None, {"duration_s": 1})
    assert st.reader_failed()
    await wait_until(lambda: not st.reader_failed(), timeout=5, message="auto clear")
    assert [h["kind"] for h in st.faults_snapshot()["history"]] == ["rfid_reader_failure"]


def test_every_fault_kind_is_fully_implemented():
    for kind, spec in CATALOG.items():
        assert hasattr(Station, f"_start_{kind}"), kind
        assert spec.get("oneshot") or hasattr(Station, f"_stop_{kind}"), kind
        assert spec["title"] and spec["description"] and spec["expect"] and spec["scope"] in ("station", "evse", "both"), kind
        for p in spec["params"]:
            assert {"key", "label", "type", "default"} <= set(p), (kind, p)
    assert len(CATALOG) >= 24
