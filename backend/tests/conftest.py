import asyncio
import time

import pytest

from ocpp_simulator.config import PRESETS, StationConfig
from ocpp_simulator.hub import Hub
from ocpp_simulator.mock_csms import MockCsms
from ocpp_simulator.station import Station


async def wait_until(cond, timeout: float = 10.0, step: float = 0.05, message: str = "condition"):
    deadline = time.monotonic() + timeout
    while True:
        result = cond()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return result
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {message}")
        await asyncio.sleep(step)


@pytest.fixture
async def csms():
    server = MockCsms()
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
async def make_station(csms):
    created: list[Station] = []

    async def factory(preset: str = "ac_22_dual", ocpp_id: str = "SIM1", *, connect: bool = True, time_scale: float = 60, **overrides) -> Station:
        cfg = StationConfig(**{**PRESETS[preset], "ocpp_id": ocpp_id, "csms_url": csms.url, "time_scale": time_scale, **overrides})
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


def tx_events(csms, cp="SIM1"):
    return [(m["event_type"], m["trigger_reason"]) for m in csms.messages("TransactionEvent", cp)]
