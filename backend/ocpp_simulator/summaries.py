"""One-line, human readable summaries of OCPP frames for the message log."""


def _kwh(meter_value: list | None) -> float | None:
    for mv in meter_value or []:
        for sv in mv.get("sampledValue", []):
            if sv.get("measurand", "Energy.Active.Import.Register") == "Energy.Active.Import.Register":
                v = float(sv["value"])
                unit = (sv.get("unitOfMeasure") or {}).get("unit") or sv.get("unit") or "Wh"  # 2.0.1: unitOfMeasure.unit, 1.6: unit
                return v / 1000 if unit == "Wh" else v
    return None


def summarize_call(action: str | None, p: dict) -> str:
    p = p if isinstance(p, dict) else {}
    if action == "TransactionEvent":
        text = f"{p.get('eventType')} · seq {p.get('seqNo')} · {p.get('triggerReason')}"
        state = (p.get("transactionInfo") or {}).get("chargingState")
        if state:
            text += f" · {state}"
        if (k := _kwh(p.get("meterValue"))) is not None:
            text += f" · {k:.3f} kWh"
        if reason := (p.get("transactionInfo") or {}).get("stoppedReason"):
            text += f" · stopped: {reason}"
        if p.get("offline"):
            text += " · OFFLINE"
        return text
    if action == "MeterValues" and "connectorId" in p:  # OCPP 1.6
        tx = f" · tx {p['transactionId']}" if p.get("transactionId") is not None else ""
        return f"Connector {p.get('connectorId')}{tx}" + (f" · {k:.3f} kWh" if (k := _kwh(p.get("meterValue"))) is not None else "")
    if action == "StartTransaction":
        return f"Start · connector {p.get('connectorId')} · tag {p.get('idTag')} · meter {p.get('meterStart')} Wh"
    if action == "StopTransaction":
        return f"Stop · tx {p.get('transactionId')} · {p.get('reason', 'Local')} · meter {p.get('meterStop')} Wh"
    if action == "MeterValues":
        return f"EVSE {p.get('evseId')}" + (f" · {k:.3f} kWh" if (k := _kwh(p.get("meterValue"))) is not None else "")
    if action == "StatusNotification" and "errorCode" in p:  # OCPP 1.6
        code = p.get("errorCode")
        return f"Connector {p.get('connectorId')} → {p.get('status')}" + (f" · {code}" if code and code != "NoError" else "")
    if action == "StatusNotification":
        return f"Connector {p.get('evseId')}/{p.get('connectorId')} → {p.get('connectorStatus')}"
    if action == "BootNotification" and "chargePointModel" in p:  # OCPP 1.6
        return f"Boot · {p.get('chargePointVendor')} {p.get('chargePointModel')}"
    if action == "BootNotification":
        return f"Boot ({p.get('reason')}) · {(p.get('chargingStation') or {}).get('model', '')}"
    if action == "Authorize":
        return f"Authorize {(p.get('idToken') or {}).get('idToken') or p.get('idTag')}"
    if action == "RemoteStartTransaction":
        return f"Start · connector {p.get('connectorId', 'any')} · tag {p.get('idTag')}"
    if action == "RemoteStopTransaction":
        return f"Stop · tx {p.get('transactionId')}"
    if action == "ChangeConfiguration":
        return f"Set {p.get('key')} = {p.get('value')}"
    if action == "GetConfiguration":
        return "Get " + (", ".join(p.get("key") or []) or "all configuration keys")
    if action == "DiagnosticsStatusNotification":
        return f"Diagnostics → {p.get('status')}"
    if action in {"RequestStartTransaction"}:
        return f"Start · EVSE {p.get('evseId')} · token {(p.get('idToken') or {}).get('idToken')}"
    if action == "RequestStopTransaction":
        return f"Stop · {p.get('transactionId')}"
    if action == "Reset":
        return f"Reset {p.get('type')}"
    if action in {"NotifyEvent"}:
        ev = (p.get("eventData") or [{}])[0]
        return f"{(ev.get('component') or {}).get('name')}.{(ev.get('variable') or {}).get('name')} = {ev.get('actualValue')} ({ev.get('trigger')})"
    if action == "SecurityEventNotification":
        return f"Security event · {p.get('type')}"
    if action in {"FirmwareStatusNotification", "LogStatusNotification", "PublishFirmwareStatusNotification"}:
        return f"{action.replace('Notification', '')} → {p.get('status')}"
    if action in {"GetVariables", "SetVariables"}:
        data = p.get("getVariableData") or p.get("setVariableData") or []
        names = ", ".join(f"{(d.get('component') or {}).get('name')}.{(d.get('variable') or {}).get('name')}" for d in data[:3])
        return f"{action} · {names}{'…' if len(data) > 3 else ''}"
    pairs = [f"{k}={v}" for k, v in p.items() if isinstance(v, (str, int, float, bool))][:3]
    return (action or "message") + (" · " + ", ".join(pairs) if pairs else "")


def summarize_result(action: str | None, p) -> str:
    p = p if isinstance(p, dict) else {}
    status = p.get("status") or (p.get("idTokenInfo") or p.get("idTagInfo") or {}).get("status")
    if action == "StartTransaction" and p.get("transactionId") is not None:
        return f"StartTransaction → tx {p['transactionId']} · {status}"
    if action == "GetConfiguration" and "configurationKey" in p:
        return f"GetConfiguration → {len(p['configurationKey'])} key(s)" + (f", {len(p['unknownKey'])} unknown" if p.get("unknownKey") else "")
    return f"{action or 'reply'} → {status}" if status else f"{action or 'reply'} acknowledged"
