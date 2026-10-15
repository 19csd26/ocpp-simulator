"""All 40 CSMS commands against a live station, and all 25 station messages against the mock CSMS.
The ocpp library validates every payload against the official OCPP 2.0.1 JSON schemas in both directions."""
import pytest

from ocpp_simulator.link import COMMAND_NAMES
from ocpp_simulator.station import Station
from ocpp_simulator.templates import command_template, message_template
from tests.conftest import wait_until

# a CALLRESULT (not a CALLERROR) is the pass criterion; most commands also have a definite status
EXPECTED_STATUS = {
    "CancelReservation": "Rejected",            # nothing reserved yet
    "CertificateSigned": "Rejected",            # no CSR is pending
    "ChangeAvailability": "Accepted", "ClearCache": "Accepted", "ClearChargingProfile": "Unknown", "ClearDisplayMessage": "Unknown",
    "CustomerInformation": "Accepted", "DataTransfer": "Accepted", "DeleteCertificate": "NotFound", "GetBaseReport": "Accepted",
    "GetChargingProfiles": "NoProfiles", "GetCompositeSchedule": "Accepted", "GetDisplayMessages": "Unknown", "GetInstalledCertificateIds": "NotFound",
    "GetLog": "Accepted", "GetMonitoringReport": "EmptyResultSet", "GetReport": "Accepted", "InstallCertificate": "Rejected",  # placeholder PEM is not a certificate
    "PublishFirmware": "Accepted", "RequestStartTransaction": "Accepted", "RequestStopTransaction": "Rejected", "ReserveNow": "Accepted",
    "Reset": "Accepted", "SendLocalList": "Accepted", "SetChargingProfile": "Accepted", "SetDisplayMessage": "Accepted", "SetMonitoringBase": "Accepted",
    "SetMonitoringLevel": "Accepted", "SetNetworkProfile": "Accepted", "TriggerMessage": "Accepted", "UnlockConnector": "Unlocked",
    "UnpublishFirmware": "NoFirmware", "UpdateFirmware": "Accepted",
}


@pytest.mark.parametrize("action", COMMAND_NAMES)
async def test_every_command_is_answered_and_schema_valid(csms, make_station, action):
    st = await make_station()
    reply = await csms.command("SIM1", action, command_template(action, st))
    assert isinstance(reply, dict)
    if action in EXPECTED_STATUS:
        assert reply.get("status") == EXPECTED_STATUS[action], reply


@pytest.mark.parametrize("action", sorted(Station.CP_ACTIONS))
async def test_every_station_message_is_accepted_by_a_csms(csms, make_station, action):
    st = await make_station()
    res = await st.send_raw(action, message_template(action, st))
    assert res.sent and res.ok, res
    assert action in csms.actions("SIM1")


async def test_commands_that_the_csms_gets_wrong_are_reported_not_crashed(csms, make_station):
    st = await make_station()
    with pytest.raises(Exception, match="(?i)validation|property|required|type"):
        await csms.command("SIM1", "Reset", {"type": "Maybe"})  # not a valid enum value: the library refuses before sending
    assert st.online
