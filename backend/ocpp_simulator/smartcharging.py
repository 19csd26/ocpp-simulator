"""Charging profiles: validation, the effective limit at a given time, and composite schedules.

A profile is kept exactly as received from the CSMS (snake_case dict). Rules implemented (OCPP 2.0.1 part 2, K01-K05):
* purposes: ChargingStationMaxProfile (EVSE 0 only), TxDefaultProfile (EVSE 0 or a specific EVSE), TxProfile (a running transaction)
* within a purpose, the valid profile with the highest stackLevel wins
* effective limit = min(station max, TxProfile if present else TxDefaultProfile)
* kinds: Absolute, Relative (starts with the transaction), Recurring (Daily / Weekly)
"""
from datetime import datetime, timedelta, timezone

MAX_STACK_LEVEL = 20
MAX_PERIODS = 24


def parse_dt(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def validate_profile(profile: dict, evse_id: int, evse_count: int, active_tx_ids: dict[int, str]) -> tuple[bool, str | None, str | None]:
    """Returns (ok, reasonCode, additionalInfo) in the format of StatusInfo."""
    purpose = profile.get("charging_profile_purpose")
    if evse_id < 0 or evse_id > evse_count:
        return False, "UnknownEvse", f"EVSE {evse_id} does not exist"
    if purpose == "ChargingStationMaxProfile" and evse_id != 0:
        return False, "InvalidValue", "ChargingStationMaxProfile applies to the whole station (evseId 0)"
    if purpose == "TxProfile":
        if evse_id == 0:
            return False, "InvalidValue", "TxProfile needs a specific EVSE"
        tx = profile.get("transaction_id")
        if not tx or active_tx_ids.get(evse_id) != tx:
            return False, "TxNotFound", "No running transaction with that id on this EVSE"
    if not 0 <= profile.get("stack_level", 0) <= MAX_STACK_LEVEL:
        return False, "InvalidStackLevel", f"stackLevel must be 0..{MAX_STACK_LEVEL}"
    kind = profile.get("charging_profile_kind")
    schedules = profile.get("charging_schedule") or []
    if not schedules:
        return False, "InvalidSchedule", "A charging schedule is required"
    for sched in schedules:
        periods = sched.get("charging_schedule_period") or []
        if not periods or periods[0].get("start_period") != 0:
            return False, "InvalidSchedule", "The first period must start at 0"
        if len(periods) > MAX_PERIODS:
            return False, "InvalidSchedule", f"At most {MAX_PERIODS} periods"
        starts = [p.get("start_period", 0) for p in periods]
        if starts != sorted(set(starts)):
            return False, "InvalidSchedule", "Periods must be in strictly increasing order"
        if any(p.get("limit", 0) < 0 for p in periods):
            return False, "InvalidValue", "A limit cannot be negative"
        if kind in ("Absolute", "Recurring") and not sched.get("start_schedule"):
            return False, "MissingParam", "startSchedule is required for Absolute and Recurring profiles"
    if kind == "Recurring" and not profile.get("recurrency_kind"):
        return False, "MissingParam", "recurrencyKind is required for Recurring profiles"
    if kind == "Relative" and any(s.get("start_schedule") for s in schedules):
        return False, "InvalidValue", "startSchedule must not be set for Relative profiles"
    return True, None, None


def _anchor(profile: dict, sched: dict, tx_start: datetime | None, now: datetime) -> datetime | None:
    kind = profile.get("charging_profile_kind")
    if kind == "Relative":
        return tx_start or now
    start = parse_dt(sched.get("start_schedule"))
    if kind == "Recurring" and start:
        span = timedelta(days=1 if profile.get("recurrency_kind") == "Daily" else 7)
        if now >= start:
            start += span * int((now - start) / span)
        else:
            start -= span * (int((start - now) / span) + 1)
    return start


def _in_validity(profile: dict, t: datetime) -> bool:
    vf, vt = parse_dt(profile.get("valid_from")), parse_dt(profile.get("valid_to"))
    return (vf is None or t >= vf) and (vt is None or t < vt)


def _period_limit(profile: dict, t: datetime, tx_start: datetime | None) -> tuple[float, str] | None:
    """(limit, unit) of the profile's first schedule at time t, or None if not applicable."""
    if not _in_validity(profile, t):
        return None
    sched = (profile.get("charging_schedule") or [None])[0]
    if not sched:
        return None
    start = _anchor(profile, sched, tx_start, t)
    if start is None or t < start:
        return None
    offset = (t - start).total_seconds()
    duration = sched.get("duration")
    if duration is not None and offset >= duration:
        return None
    limit = None
    for period in sched["charging_schedule_period"]:
        if period["start_period"] <= offset:
            limit = period["limit"]
    return (limit, sched.get("charging_rate_unit", "W")) if limit is not None else None


def _to_watts(limit: float, unit: str, voltage: float, phases: int) -> float:
    return limit if unit == "W" else limit * voltage * phases


def limit_at(profiles: list[dict], evse_id: int, t: datetime, *, voltage: float, phases: int, tx_id: str | None, tx_start: datetime | None) -> float | None:
    """Effective power limit in watts for an EVSE at time t (None = unrestricted)."""
    def best(purpose: str, wanted: set[int]) -> float | None:
        candidates = [(p["stack_level"], p) for p in profiles if p.get("charging_profile_purpose") == purpose and p["_evse_id"] in wanted]
        for _, p in sorted(candidates, key=lambda x: -x[0]):
            if purpose == "TxProfile" and p.get("transaction_id") != tx_id:
                continue
            hit = _period_limit(p, t, tx_start)
            if hit:
                return _to_watts(hit[0], hit[1], voltage, phases)
        return None

    station = best("ChargingStationMaxProfile", {0})
    tx_limit = best("TxProfile", {evse_id}) if tx_id else None
    if tx_limit is None:
        tx_limit = best("TxDefaultProfile", {evse_id})
        if tx_limit is None:
            tx_limit = best("TxDefaultProfile", {0})
    values = [v for v in (station, tx_limit) if v is not None]
    return min(values) if values else None


def composite_schedule(profiles: list[dict], evse_id: int, start: datetime, duration: int, unit: str, *, voltage: float, phases: int,
                       station_max_w: float, tx_id: str | None, tx_start: datetime | None) -> dict:
    """GetCompositeSchedule: the limit a station will follow over [start, start + duration)."""
    cuts = {0}
    for p in profiles:
        if p["_evse_id"] not in (0, evse_id):
            continue
        for sched in p.get("charging_schedule") or []:
            anchor = _anchor(p, sched, tx_start, start)
            if anchor is None:
                continue
            for period in sched["charging_schedule_period"]:
                off = int((anchor + timedelta(seconds=period["start_period"]) - start).total_seconds())
                if 0 < off < duration:
                    cuts.add(off)
            if sched.get("duration") is not None:
                off = int((anchor + timedelta(seconds=sched["duration"]) - start).total_seconds())
                if 0 < off < duration:
                    cuts.add(off)
    periods = []
    for off in sorted(cuts):
        at = start + timedelta(seconds=off)
        watts = limit_at(profiles, evse_id, at, voltage=voltage, phases=phases, tx_id=tx_id, tx_start=tx_start)
        watts = station_max_w if watts is None else min(watts, station_max_w)
        value = watts if unit == "W" else watts / (voltage * phases)
        if not periods or periods[-1]["limit"] != round(value, 1):
            periods.append({"start_period": off, "limit": round(value, 1)})
    return {"evse_id": evse_id, "duration": duration, "schedule_start": start.isoformat(), "charging_rate_unit": unit, "charging_schedule_period": periods}
