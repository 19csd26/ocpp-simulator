"""The scripted test cases, run against the mock CSMS."""
import asyncio

import pytest

from ocpp_simulator.scenarios import CATALOG_SCENARIOS, matches, validate_catalog
from tests.conftest import wait_until

FAST = ["smoke_boot", "rfid_session", "authorize_then_plug", "two_sessions", "battery_full", "meter_failure", "meter_frozen", "meter_failure_stops_session",
        "power_loss_session", "power_loss_idle", "network_loss_session", "emergency_stop", "over_current", "ground_fault", "over_voltage", "tamper",
        "contactor_failure", "reader_failure", "ev_comm_loss", "cable_pulled", "watchdog_reboot", "all_messages"]
SLOW = ["meter_rollback", "meter_spike", "meter_invalid", "unstable_network", "high_latency", "clock_drift", "over_temperature", "grid_overload"]


async def run_scenario(st, sid: str, timeout: float = 120):
    sc = CATALOG_SCENARIOS[sid]
    run = st.runner.start(sid, sc["title"], sc["steps"])
    await asyncio.wait_for(st.runner.task, timeout)
    return run


def failures(run):
    return [(s.label, s.detail) for s in run.steps if s.status == "failed"]


@pytest.mark.parametrize("sid", FAST)
async def test_builtin_scenario_passes_against_a_cooperative_csms(csms, make_station, sid):
    st = await make_station(connect=(sid != "smoke_boot"), time_scale=300 if sid == "battery_full" else 60)
    run = await run_scenario(st, sid)
    assert run.status == "passed", failures(run)
    assert all(s.status == "passed" for s in run.steps)


@pytest.mark.slow
@pytest.mark.parametrize("sid", SLOW)
async def test_slow_builtin_scenarios(csms, make_station, sid):
    st = await make_station()
    run = await run_scenario(st, sid, timeout=180)
    assert run.status == "passed", failures(run)


async def test_unknown_token_scenario_detects_a_csms_that_accepts_everything(csms, make_station):
    st = await make_station()
    run = await run_scenario(st, "unknown_token")                       # the mock accepts every token: the scenario must FAIL
    assert run.status == "failed"
    failed = failures(run)
    assert failed and "Invalid" in failed[0][0] or "tap_status" in failed[0][0] or failed
    assert [s.status for s in run.steps][-1] == "skipped"               # later steps do not run after a failure
    csms.allowed_tokens = {"CARD1"}                                       # a strict CSMS makes it pass
    st2 = await make_station(ocpp_id="SIM2")
    assert (await run_scenario(st2, "unknown_token")).status == "passed"


async def test_all_messages_scenario_reveals_a_csms_that_errors(csms, make_station):
    st = await make_station()
    st.rules.add("in", "*", "delay", {"delay_ms": 1})                    # irrelevant: only incoming commands
    csms.refuse.add("NotifyEvent")
    original = csms.reply
    def broken(cp, action, payload):
        if action == "NotifyEvent":
            raise RuntimeError("csms bug")
        return original(cp, action, payload)
    csms.reply = broken
    run = await run_scenario(st, "all_messages")
    assert run.status == "failed" and any("NotifyEvent" in label for label, _ in failures(run))


async def test_expectations_time_out_with_a_clear_message(csms, make_station):
    st = await make_station()
    run = st.runner.start("custom", "x", [{"expect": "tx_state", "state": "Charging", "timeout": 0.5}])
    await st.runner.task
    assert run.status == "failed" and "timed out after 0.5s" in run.steps[0].detail


async def test_custom_scenarios_are_validated_before_running(make_station):
    st = await make_station(connect=False)
    with pytest.raises(ValueError, match="needs either"):
        st.runner.start("c", "c", [{"label": "nothing"}])
    with pytest.raises(ValueError, match="unknown action"):
        st.runner.start("c", "c", [{"do": "teleport"}])
    with pytest.raises(ValueError, match="unknown expectation"):
        st.runner.start("c", "c", [{"expect": "vibes"}])


async def test_only_one_scenario_runs_at_a_time_and_it_can_be_stopped(csms, make_station):
    st = await make_station()
    run = st.runner.start("a", "a", [{"do": "wait", "seconds": 30}, {"do": "wait", "seconds": 1}])
    with pytest.raises(ValueError, match="already running"):
        st.runner.start("b", "b", [{"do": "wait", "seconds": 1}])
    await wait_until(lambda: run.steps[0].status == "running")
    st.runner.stop()
    await wait_until(lambda: run.status == "stopped", message="stopped")
    assert [s.status for s in run.steps] == ["skipped", "skipped"] and st.runner.history[-1] is run
    again = st.runner.start("c", "c", [{"do": "wait", "seconds": 0.1}])
    await st.runner.task
    assert again.status == "passed"


async def test_a_step_can_continue_after_failing(csms, make_station):
    st = await make_station()
    run = st.runner.start("c", "c", [{"do": "clear", "fault": "ground_fault", "continue_on_fail": True}, {"do": "wait", "seconds": 0.1}])
    await st.runner.task
    assert [s.status for s in run.steps] == ["failed", "passed"] and run.status == "failed"


async def test_runner_publishes_progress_to_the_ui_hub(csms, make_station):
    st = await make_station()
    queue = st.hub.subscribe()
    st.runner.start("c", "c", [{"do": "wait", "seconds": 0.1}])
    await st.runner.task
    kinds = []
    while not queue.empty():
        ev = queue.get_nowait()
        if ev["type"] == "scenario":
            kinds.append(ev["run"]["status"])
    assert kinds[0] == "running" and kinds[-1] == "passed"


def test_catalog_is_consistent_and_message_matching_works():
    validate_catalog()
    assert len(CATALOG_SCENARIOS) >= 30 and set(FAST + SLOW) <= set(CATALOG_SCENARIOS)
    assert matches({"a": {"b": [{"c": 1}]}}, {"a.b.0.c": 1}) and not matches({"a": 1}, {"a": 2}) and not matches({}, {"x.y": 1})
