"""Guard: every message of OCPP 2.0.1 is either handled (CSMS -> station) or sendable (station -> CSMS)."""
import pytest
from ocpp.v201.enums import Action

from ocpp_simulator.config import PRESETS, StationConfig
from ocpp_simulator.hub import Hub
from ocpp_simulator.link import COMMAND_NAMES, Link, snake
from ocpp_simulator.mock_csms import CP_INITIATED
from ocpp_simulator.station import Station
from ocpp_simulator.templates import command_template, message_template


def test_all_64_messages_are_covered_exactly():
    everything = {a.value for a in Action}
    assert len(everything) == 64
    commands, sendable = set(COMMAND_NAMES), set(Station.CP_ACTIONS)
    assert len(commands) == 40 and len(sendable) == 25
    assert commands | sendable == everything
    assert commands & sendable == {"DataTransfer"}  # the only message either side may start
    assert set(CP_INITIATED) == sendable


def test_every_command_has_a_handler_registered_on_the_link():
    class Dummy:
        async def recv(self): ...
        async def send(self, m): ...
    link = Link("x", Dummy())
    assert {a for a in link.route_map} >= set(COMMAND_NAMES)
    st = Station(StationConfig(**PRESETS["ac_7"]), Hub())
    for name in COMMAND_NAMES:
        assert hasattr(st, f"cmd_{snake(name)}"), name


@pytest.mark.parametrize("action", sorted(Station.CP_ACTIONS))
def test_message_templates_exist_for_every_station_message(action):
    st = Station(StationConfig(**PRESETS["ac_7"]), Hub())
    assert isinstance(message_template(action, st), dict)


@pytest.mark.parametrize("action", COMMAND_NAMES)
def test_command_templates_exist_for_every_command(action):
    st = Station(StationConfig(**PRESETS["ac_7"]), Hub())
    assert isinstance(command_template(action, st), dict)
