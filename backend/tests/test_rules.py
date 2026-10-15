"""Message-level fault injection."""
import asyncio
import time

import pytest

from ocpp_simulator.rules import REJECT_PAYLOADS, RuleSet
from tests.conftest import wait_until


# ------------------------------------------------------------------- outgoing (station -> CSMS)
async def test_delay_outgoing_messages(csms, make_station):
    st = await make_station()
    st.rules.add("out", "Heartbeat", "delay", {"delay_ms": 1000})
    t0 = time.monotonic()
    assert (await st.request("Heartbeat")).ok
    assert time.monotonic() - t0 >= 0.95
    t0 = time.monotonic()
    await st.request("StatusNotification", timestamp=st.ts(), connector_status="Available", evse_id=1, connector_id=1)
    assert time.monotonic() - t0 < 0.5                                  # other messages are not affected


async def test_drop_outgoing_message_times_out(csms, make_station):
    st = await make_station()
    st.rules.add("out", "Heartbeat", "drop", times=1)
    before = len(csms.messages("Heartbeat"))
    res = await st.request("Heartbeat")
    assert res.sent and res.error == "timeout" and len(csms.messages("Heartbeat")) == before
    assert (await st.request("Heartbeat")).ok                           # the rule switched itself off after one hit
    assert not st.rules.rules[0].enabled and st.rules.rules[0].hits == 1


async def test_duplicate_outgoing_message(csms, make_station):
    st = await make_station()
    before = len(csms.messages("Heartbeat"))
    st.rules.add("out", "Heartbeat", "duplicate", times=1)
    await st.request("Heartbeat")
    await wait_until(lambda: len(csms.messages("Heartbeat")) == before + 2, message="two copies")


async def test_corrupt_outgoing_message_is_refused_by_a_strict_csms(csms, make_station):
    st = await make_station()
    st.rules.add("out", "Heartbeat", "corrupt", times=1)
    res = await st.request("Heartbeat")
    assert res.sent and not res.ok and res.error and "FormatViolation" in res.error or "PropertyConstraint" in res.error or "Formation" in res.error
    assert st.stats["errors"] >= 1
    assert (await st.request("Heartbeat")).ok


async def test_wildcard_rule_and_probability(csms, make_station):
    st = await make_station()
    st.rules.add("out", "*", "delay", {"delay_ms": 300}, times=2)
    t0 = time.monotonic()
    await st.request("Heartbeat")
    await st.request("Heartbeat")
    assert time.monotonic() - t0 >= 0.55
    st.rules.clear()
    st.rules.add("out", "Heartbeat", "drop", probability=0.0001)
    assert (await st.request("Heartbeat")).ok                           # almost never fires


# ------------------------------------------------------------------- incoming (CSMS -> station)
async def test_reject_a_command(csms, make_station):
    st = await make_station()
    await st.plug_in(1)
    st.rules.add("in", "RequestStartTransaction", "reject")
    reply = await csms.command("SIM1", "RequestStartTransaction", {"idToken": {"idToken": "x", "type": "Central"}, "remoteStartId": 1, "evseId": 1})
    assert reply["status"] == "Rejected" and st.evses[0].tx is None and st.evses[0].pending_remote is None
    st.rules.clear()
    st.rules.add("in", "Reset", "reject")
    assert (await csms.command("SIM1", "Reset", {"type": "Immediate"}))["status"] == "Rejected"
    assert len(csms.messages("BootNotification")) == 1                  # it was really refused, not just reported so


async def test_error_a_command_with_a_chosen_code(csms, make_station):
    st = await make_station()
    st.rules.add("in", "GetVariables", "error", {"code": "NotImplemented", "description": "Not supported by this unit"})
    with pytest.raises(Exception, match="NotImplemented"):
        await csms.command("SIM1", "GetVariables", {"getVariableData": [{"component": {"name": "OCPPCommCtrlr"}, "variable": {"name": "HeartbeatInterval"}}]})


async def test_timeout_a_command(csms, make_station):
    st = await make_station()
    st.rules.add("in", "ClearCache", "timeout", times=1)
    with pytest.raises(asyncio.TimeoutError):
        await csms.command("SIM1", "ClearCache", {}, timeout=1.5)
    assert (await csms.command("SIM1", "ClearCache", {}))["status"] == "Accepted"   # the rule applied once


async def test_delay_a_command(csms, make_station):
    st = await make_station()
    st.rules.add("in", "ClearCache", "delay", {"delay_ms": 1200}, times=1)
    t0 = time.monotonic()
    assert (await csms.command("SIM1", "ClearCache", {}))["status"] == "Accepted"
    assert time.monotonic() - t0 >= 1.1


async def test_invalid_response_to_a_command(csms, make_station):
    st = await make_station()
    st.rules.add("in", "ClearCache", "invalid_response", times=1)
    with pytest.raises(Exception):
        await csms.command("SIM1", "ClearCache", {})                    # the CSMS must cope with a malformed CALLRESULT
    assert st.online


# ----------------------------------------------------------------------------- rule bookkeeping
def test_rule_validation_and_management():
    rules = RuleSet()
    with pytest.raises(ValueError, match="direction"):
        rules.add("sideways", "X", "delay")
    with pytest.raises(ValueError, match="not available for direction"):
        rules.add("out", "Heartbeat", "reject")
    with pytest.raises(ValueError, match="not available for direction"):
        rules.add("in", "Reset", "drop")
    with pytest.raises(ValueError, match="probability"):
        rules.add("out", "*", "drop", probability=0)
    with pytest.raises(ValueError, match="times"):
        rules.add("out", "*", "drop", times=0)
    a = rules.add("out", "Heartbeat", "drop")
    b = rules.add("in", "*", "delay")
    assert [r["id"] for r in rules.snapshot()] == [a.id, b.id]
    assert rules.match("out", "Heartbeat") is a and rules.match("out", "Authorize") is None
    assert rules.match("in", "Anything") is b
    a.enabled = False
    assert rules.match("out", "Heartbeat") is None
    assert rules.remove(a.id) and not rules.remove(a.id)
    rules.clear()
    assert rules.snapshot() == []


def test_every_rejectable_command_has_a_valid_refusal():
    from ocpp_simulator.link import COMMAND_NAMES

    assert set(REJECT_PAYLOADS) <= set(COMMAND_NAMES)
    assert len(REJECT_PAYLOADS) >= 35
