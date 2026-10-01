"""Flight statistics: turns filed PIREPs into compact logbook rows.

The Stats page filters and aggregates in the browser, so the server sends
each flight once as a small flat row (see logbook_row) plus the airports
those rows reference. Everything derived from the raw PIREP - flight time,
air time, delays, distance - is computed here, in one tested place.

Definitions:
- flight time: actual off-block to in-block (AOBT -> ABIT), the logbook
  "flight time". PIREPs without those times fall back to the scheduled
  block time (SOBT -> SIBT) and are flagged ft_est.
- air time: actual take-off to landing (ATOT -> ALDT).
- departure/arrival delay: AOBT - SOBT and ABIT - SIBT, in minutes.
- distance: great-circle between the two airports, in nautical miles.
- fuel: converted to kilograms whatever unit the pilot's SimBrief profile
  uses, so flights planned in kg and lb can be compared and summed.
"""

import math
import re

# A flight number's 2-character designator -> the carrier it belongs to,
# for PIREPs filed before legs recorded their carrier (all Ryanair then).
DESIGNATOR_CARRIER = {"FR": "RYR", "RK": "RYR", "U2": "EZY", "W6": "WZZ", "W4": "WZZ", "W9": "WZZ"}

_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})")
EARTH_RADIUS_NM = 3440.065
MAX_BLOCK_MINUTES = 20 * 60   # anything longer is a typo, not a flight
LB_TO_KG = 0.45359237


def minutes_of_day(value):
    """"HH:MM" or "HH:MMZ" (optionally followed by e.g. " (+1d)") -> minutes
    after midnight; None for anything else ("N/A", "", None)."""
    if not isinstance(value, str):
        return None
    m = _TIME_RE.match(value)
    if not m:
        return None
    h, mm = int(m.group(1)), int(m.group(2))
    if h > 23 or mm > 59:
        return None
    return h * 60 + mm


def elapsed(start, end):
    """Minutes from start to end (minutes of day), wrapping past midnight.
    None if either is missing or the result isn't a plausible flight."""
    if start is None or end is None:
        return None
    minutes = (end - start) % 1440
    return minutes if 0 < minutes <= MAX_BLOCK_MINUTES else None


def delay(scheduled, actual):
    """Actual minus scheduled, in minutes, normalised to -12h..+12h so a
    departure just after midnight against a schedule just before it reads
    as a small delay, not a 23-hour one."""
    if scheduled is None or actual is None:
        return None
    return (actual - scheduled + 720) % 1440 - 720


def great_circle_nm(a, b):
    try:
        lat1, lon1, lat2, lon2 = map(math.radians, (float(a["lat"]), float(a["lon"]), float(b["lat"]), float(b["lon"])))
    except (TypeError, ValueError, KeyError):
        return None
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return round(2 * EARTH_RADIUS_NM * math.asin(min(1.0, math.sqrt(h))))


def _num(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def fuel_kg(value, unit):
    """A SimBrief/PIREP fuel figure in the OFP's unit ("kgs"/"lbs") -> kg."""
    n = _num(value)
    if n is None:
        return None
    if isinstance(unit, str) and unit.strip().lower().startswith("lb"):
        n *= LB_TO_KG
    return round(n)


def logbook_row(record, airport):
    """One PIREP record (db._row_to_record shape) -> a compact flat row.
    `airport(icao)` returns {"lat", "lon", ...} or None."""
    leg = record.get("leg") or {}
    ofp = record.get("ofp") or {}
    pirep = record.get("pirep") or {}
    fn = record.get("flight_number") or leg.get("flight_number") or ""
    dep, arr = record.get("departure_icao") or "", record.get("arrival_icao") or ""
    callsign = record.get("callsign") or ""

    sobt, sibt = minutes_of_day(leg.get("sobt")), minutes_of_day(leg.get("sibt"))
    aobt, atot = minutes_of_day(pirep.get("aobt")), minutes_of_day(pirep.get("atot"))
    aldt, abit = minutes_of_day(pirep.get("aldt")), minutes_of_day(pirep.get("abit"))
    block, air, sched_block = elapsed(aobt, abit), elapsed(atot, aldt), elapsed(sobt, sibt)
    if block is None and sched_block is None and isinstance(leg.get("eet_minutes"), (int, float)):
        sched_block = int(leg["eet_minutes"])
    flight_time = block if block is not None else sched_block

    dep_ap, arr_ap = airport(dep), airport(arr)
    date = record.get("flight_date") or (record.get("submitted_at") or "")[:10] or None
    # The delay cause: what the pilot coded in the PIREP (the main code,
    # i.e. the one with the most minutes) when they did, otherwise the
    # delay dispatch predicted for the leg.
    coded = [c for c in (pirep.get("delay_codes") or []) if isinstance(c, dict) and c.get("code")]
    if coded:
        main = max(coded, key=lambda c: _num(c.get("minutes")) or 0)
        delay_info = {"iata_code": main["code"], "description": main.get("description")}
    else:
        delay_info = leg.get("delay") or {}
    mel = leg.get("mel") or {}
    unit = ofp.get("weight_unit")
    seats = _num(leg.get("seat_capacity"))
    pax = _num(leg.get("pax_count"))

    return {
        "id": record.get("id"),
        "date": date,
        "fn": fn,
        "cs": callsign,
        "op": callsign[:3].upper() if re.match(r"^[A-Za-z]{3}", callsign) else "",
        "carrier": leg.get("carrier") or DESIGNATOR_CARRIER.get(fn[:2].upper(), ""),
        "type": leg.get("aircraft_type") or "",
        "dep": dep,
        "arr": arr,
        "dist": great_circle_nm(dep_ap, arr_ap) if dep_ap and arr_ap else None,
        "ft": flight_time,
        "ft_est": block is None and flight_time is not None,
        "block": block,
        "air": air,
        "sched_block": sched_block,
        "sobt": sobt,
        "dep_delay": delay(sobt, aobt),
        "arr_delay": delay(sibt, abit),
        "pax": int(pax) if pax is not None else None,
        "seats": int(seats) if seats is not None else None,
        "lf": _num(leg.get("load_factor")),
        "cargo": _num(leg.get("cargo_weight_kg")),
        "ci": _num(leg.get("cost_index")),
        "delay_code": str(delay_info.get("iata_code") or "") or None,
        "delay_desc": str(delay_info.get("description") or "")[:120] or None,
        "delay_coded": bool(coded),
        "mel": (str(mel.get("system") or mel.get("id") or "") or None) if mel else None,
        # All fuel in kg (see fuel_kg). AFAD is entered in the OFP's unit.
        "block_fuel": fuel_kg(ofp.get("block_fuel"), unit),
        "plan_trip_fuel": fuel_kg(ofp.get("plan_trip_fuel"), unit),
        "plan_landing_fuel": fuel_kg(ofp.get("plan_landing_fuel"), unit),
        "afad": fuel_kg(pirep.get("afad"), unit),
    }


def logbook(records, airport):
    """All of an account's PIREPs -> {"flights": [rows, oldest first],
    "airports": {icao: {name, city, country, lat, lon}}}."""
    rows = [logbook_row(r, airport) for r in records]
    rows.sort(key=lambda r: (r["date"] or "", r["id"] or ""))
    airports = {}
    for row in rows:
        for icao in (row["dep"], row["arr"]):
            if icao and icao not in airports:
                info = airport(icao) or {}
                airports[icao] = {k: info.get(k) for k in ("name", "city", "country", "lat", "lon")}
    return {"flights": rows, "airports": airports}
