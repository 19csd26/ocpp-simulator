"""Valid sample payloads (spec camelCase) for every OCPP 2.0.1 message.

Used by the UI's "send a message" tool and by the test-suite. Payloads are built per station so ids and
timestamps are current.
"""
from datetime import timedelta

PEM_PLACEHOLDER = "-----BEGIN CERTIFICATE-----\nMIIBplaceholder\n-----END CERTIFICATE-----\n"

PROFILE = lambda now, limit=16, unit="A", purpose="TxDefaultProfile", pid=1, stack=0: {  # noqa: E731
    "id": pid, "stackLevel": stack, "chargingProfilePurpose": purpose, "chargingProfileKind": "Absolute",
    "chargingSchedule": [{"id": pid, "startSchedule": now, "chargingRateUnit": unit, "chargingSchedulePeriod": [{"startPeriod": 0, "limit": limit}]}],
}


def message_template(action: str, st) -> dict:
    """Payload for a message the STATION sends to the CSMS."""
    now = st.ts()
    evse = 1
    event = {"eventId": 1, "timestamp": now, "trigger": "Alerting", "actualValue": "true", "eventNotificationType": "HardWiredNotification",
             "component": {"name": "EVSE", "evse": {"id": evse}}, "variable": {"name": "Problem"}, "techCode": "SampleFault"}
    ocsp = {"hashAlgorithm": "SHA256", "issuerNameHash": "0a" * 32, "issuerKeyHash": "0b" * 32, "serialNumber": "01", "responderURL": "http://ocsp.example.com"}
    table = {
        "Authorize": {"idToken": {"idToken": st.cfg.card_id, "type": "ISO14443"}},
        "BootNotification": {"reason": "Triggered", "chargingStation": {"vendorName": st.cfg.vendor, "model": st.cfg.model, "serialNumber": st.cfg.serial_number, "firmwareVersion": st.cfg.firmware_version}},
        "ClearedChargingLimit": {"chargingLimitSource": "EMS", "evseId": evse},
        "DataTransfer": {"vendorId": "com.simulator", "messageId": "hello", "data": {"greeting": "hi"}},
        "FirmwareStatusNotification": {"status": "Installed", "requestId": 1},
        "Get15118EVCertificate": {"iso15118SchemaVersion": "urn:iso:15118:2:2013:MsgDef", "action": "Install", "exiRequest": "QUJD"},
        "GetCertificateStatus": {"ocspRequestData": ocsp},
        "Heartbeat": {},
        "LogStatusNotification": {"status": "Uploaded", "requestId": 1},
        "MeterValues": {"evseId": evse, "meterValue": [{"timestamp": now, "sampledValue": [{"value": 1234.5, "measurand": "Energy.Active.Import.Register", "unitOfMeasure": {"unit": "Wh"}}]}]},
        "NotifyChargingLimit": {"chargingLimit": {"chargingLimitSource": "EMS", "isGridCritical": False}},
        "NotifyCustomerInformation": {"data": "No data held for this customer", "seqNo": 0, "generatedAt": now, "requestId": 1},
        "NotifyDisplayMessages": {"requestId": 1, "messageInfo": [{"id": 1, "priority": "NormalCycle", "message": {"format": "UTF8", "content": "Welcome"}}]},
        "NotifyEVChargingNeeds": {"evseId": evse, "chargingNeeds": {"requestedEnergyTransfer": "AC_three_phase", "acChargingParameters": {"energyAmount": 20000, "evMinCurrent": 6, "evMaxCurrent": 32, "evMaxVoltage": 400}}},
        "NotifyEVChargingSchedule": {"timeBase": now, "evseId": evse, "chargingSchedule": {"id": 1, "chargingRateUnit": "W", "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 7000}]}},
        "NotifyEvent": {"generatedAt": now, "seqNo": 0, "eventData": [event]},
        "NotifyMonitoringReport": {"requestId": 1, "seqNo": 0, "generatedAt": now},
        "NotifyReport": {"requestId": 1, "generatedAt": now, "seqNo": 0},
        "PublishFirmwareStatusNotification": {"status": "Idle"},
        "ReportChargingProfiles": {"requestId": 1, "chargingLimitSource": "CSO", "evseId": evse, "chargingProfile": [PROFILE(now)]},
        "ReservationStatusUpdate": {"reservationId": 1, "reservationUpdateStatus": "Expired"},
        "SecurityEventNotification": {"type": "FirmwareUpdated", "timestamp": now},
        "SignCertificate": {"csr": "-----BEGIN CERTIFICATE REQUEST-----\nMIIBplaceholder\n-----END CERTIFICATE REQUEST-----\n", "certificateType": "ChargingStationCertificate"},
        "StatusNotification": {"timestamp": now, "connectorStatus": "Available", "evseId": evse, "connectorId": 1},
        "TransactionEvent": {"eventType": "Updated", "timestamp": now, "triggerReason": "Trigger", "seqNo": 0, "transactionInfo": {"transactionId": "sample-transaction"}},
    }
    return table[action]


def command_template(action: str, st) -> dict:
    """A valid payload for a command the CSMS sends to the station."""
    now = st.ts()
    future = (st.now() + timedelta(hours=1)).isoformat()
    tok = {"idToken": "SAMPLE1", "type": "ISO14443"}
    hash_data = {"hashAlgorithm": "SHA256", "issuerNameHash": "0a" * 32, "issuerKeyHash": "0b" * 32, "serialNumber": "01"}
    table = {
        "CancelReservation": {"reservationId": 1},
        "CertificateSigned": {"certificateChain": PEM_PLACEHOLDER},
        "ChangeAvailability": {"operationalStatus": "Operative"},
        "ClearCache": {},
        "ClearChargingProfile": {"chargingProfileId": 1},
        "ClearDisplayMessage": {"id": 1},
        "ClearVariableMonitoring": {"id": [1]},
        "CostUpdated": {"totalCost": 12.5, "transactionId": "sample-transaction"},
        "CustomerInformation": {"requestId": 1, "report": True, "clear": False, "customerIdentifier": "CUSTOMER1"},
        "DataTransfer": {"vendorId": "com.simulator.echo", "messageId": "hello", "data": "hi"},
        "DeleteCertificate": {"certificateHashData": hash_data},
        "GetBaseReport": {"requestId": 1, "reportBase": "SummaryInventory"},
        "GetChargingProfiles": {"requestId": 1, "chargingProfile": {"chargingProfilePurpose": "TxDefaultProfile"}},
        "GetCompositeSchedule": {"duration": 600, "evseId": 1},
        "GetDisplayMessages": {"requestId": 1},
        "GetInstalledCertificateIds": {},
        "GetLocalListVersion": {},
        "GetLog": {"log": {"remoteLocation": "https://logs.example.com/upload"}, "logType": "DiagnosticsLog", "requestId": 1},
        "GetMonitoringReport": {"requestId": 1},
        "GetReport": {"requestId": 1, "componentVariable": [{"component": {"name": "EVSE", "evse": {"id": 1}}}]},
        "GetTransactionStatus": {},
        "GetVariables": {"getVariableData": [{"component": {"name": "OCPPCommCtrlr"}, "variable": {"name": "HeartbeatInterval"}}]},
        "InstallCertificate": {"certificateType": "CSMSRootCertificate", "certificate": PEM_PLACEHOLDER},
        "PublishFirmware": {"location": "https://fw.example.com/fw.bin", "checksum": "0123456789abcdef0123456789abcdef", "requestId": 1},
        "RequestStartTransaction": {"idToken": {"idToken": "REMOTE1", "type": "Central"}, "remoteStartId": 1, "evseId": 1},
        "RequestStopTransaction": {"transactionId": "sample-transaction"},
        "ReserveNow": {"id": 1, "expiryDateTime": future, "idToken": tok, "evseId": 1},
        "Reset": {"type": "OnIdle"},
        "SendLocalList": {"versionNumber": 1, "updateType": "Full", "localAuthorizationList": [{"idToken": tok, "idTokenInfo": {"status": "Accepted"}}]},
        "SetChargingProfile": {"evseId": 1, "chargingProfile": PROFILE(now)},
        "SetDisplayMessage": {"message": {"id": 1, "priority": "NormalCycle", "message": {"format": "UTF8", "content": "Hello driver"}}},
        "SetMonitoringBase": {"monitoringBase": "All"},
        "SetMonitoringLevel": {"severity": 5},
        "SetNetworkProfile": {"configurationSlot": 1, "connectionData": {"ocppVersion": "OCPP20", "ocppTransport": "JSON", "ocppCsmsUrl": "wss://csms.example.com/ocpp", "messageTimeout": 30, "securityProfile": 2, "ocppInterface": "Wired0"}},
        "SetVariableMonitoring": {"setMonitoringData": [{"type": "UpperThreshold", "severity": 5, "value": 80, "component": {"name": "EVSE", "evse": {"id": 1}}, "variable": {"name": "Temperature"}}]},
        "SetVariables": {"setVariableData": [{"attributeValue": "20", "component": {"name": "OCPPCommCtrlr"}, "variable": {"name": "HeartbeatInterval"}}]},
        "TriggerMessage": {"requestedMessage": "Heartbeat"},
        "UnlockConnector": {"evseId": 1, "connectorId": 1},
        "UnpublishFirmware": {"checksum": "0123456789abcdef0123456789abcdef"},
        "UpdateFirmware": {"requestId": 1, "firmware": {"location": "https://fw.example.com/fw-2.0.0.bin", "retrieveDateTime": now}},
    }
    return table[action]


# ---------------------------------------------------------------------------------------------------------- OCPP 1.6
def message_template16(action: str, st) -> dict:
    """Payload for a message a 1.6 charge point sends to the CSMS."""
    now = st.ts()
    tx_ids = [n for n in st.tx16.values()]
    table = {
        "Authorize": {"idTag": st.cfg.card_id[:20]},
        "BootNotification": {"chargePointVendor": st.cfg.vendor, "chargePointModel": st.cfg.model, "chargePointSerialNumber": st.cfg.serial_number,
                             "firmwareVersion": st.cfg.firmware_version},
        "DataTransfer": {"vendorId": "com.simulator", "messageId": "hello", "data": "hi"},
        "DiagnosticsStatusNotification": {"status": "Uploaded"},
        "FirmwareStatusNotification": {"status": "Installed"},
        "Heartbeat": {},
        "MeterValues": {"connectorId": 1, **({"transactionId": tx_ids[0]} if tx_ids else {}),
                        "meterValue": [{"timestamp": now, "sampledValue": [{"value": "1234.5", "context": "Sample.Periodic", "measurand": "Energy.Active.Import.Register",
                                                                          "unit": "Wh"}]}]},
        "StartTransaction": {"connectorId": 1, "idTag": st.cfg.card_id[:20], "meterStart": int(st.evses[0].register_wh), "timestamp": now},
        "StatusNotification": {"connectorId": 1, "errorCode": "NoError", "status": "Available", "timestamp": now},
        "StopTransaction": {"transactionId": tx_ids[0] if tx_ids else 1, "meterStop": int(st.evses[0].register_wh), "timestamp": now, "reason": "Local"},
    }
    return table[action]


def command_template16(action: str, st) -> dict:
    """A valid payload for each of the 19 commands a 1.6 CSMS can send."""
    now = st.ts()
    profile = {"chargingProfileId": 1, "stackLevel": 0, "chargingProfilePurpose": "TxDefaultProfile", "chargingProfileKind": "Absolute",
               "chargingSchedule": {"chargingRateUnit": "A", "startSchedule": now, "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 16}]}}
    table = {
        "CancelReservation": {"reservationId": 1}, "ChangeAvailability": {"connectorId": 1, "type": "Operative"},
        "ChangeConfiguration": {"key": "HeartbeatInterval", "value": "30"}, "ClearCache": {}, "ClearChargingProfile": {"id": 1},
        "DataTransfer": {"vendorId": "com.simulator.echo", "data": "hi"}, "GetCompositeSchedule": {"connectorId": 1, "duration": 600},
        "GetConfiguration": {}, "GetDiagnostics": {"location": "ftp://example.com/logs"}, "GetLocalListVersion": {},
        "RemoteStartTransaction": {"idTag": st.cfg.card_id[:20], "connectorId": 1}, "RemoteStopTransaction": {"transactionId": 1},
        "ReserveNow": {"connectorId": 1, "expiryDate": (st.now() + timedelta(hours=1)).isoformat(), "idTag": st.cfg.card_id[:20], "reservationId": 1},
        "Reset": {"type": "Soft"},
        "SendLocalList": {"listVersion": 1, "updateType": "Full", "localAuthorizationList": [{"idTag": st.cfg.card_id[:20], "idTagInfo": {"status": "Accepted"}}]},
        "SetChargingProfile": {"connectorId": 1, "csChargingProfiles": profile}, "TriggerMessage": {"requestedMessage": "StatusNotification"},
        "UnlockConnector": {"connectorId": 1}, "UpdateFirmware": {"location": "http://example.com/firmware.bin", "retrieveDate": now},
    }
    return table[action]
