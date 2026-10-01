"""Operational constraints around a dispatched rotation: airport curfews,
crew flight duty limits and ATFM (air traffic flow management) slots.

Sources:
- Curfews (data/curfews.json): published night restrictions of the
  curfew airports in this app's network, local times. Only airports whose
  hours could be confirmed are listed.
- Flight duty period: EASA ORO.FTL.205(b) Table 2, maximum daily FDP for
  acclimatised crew members, and ORO.FTL.205(f) commander's discretion (up
  to 2 hours for a non-augmented crew).
- Slots: Eurocontrol Network Manager practice - a regulated flight gets a
  calculated take-off time (CTOT) and must take off within -5/+10 minutes
  of it.
"""

import json
import os
import random
from datetime import timedelta
from zoneinfo import ZoneInfo

from timeutils import local_time_to_zulu_dt

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
with open(os.path.join(DATA_DIR, "curfews.json"), encoding="utf-8") as f:
    CURFEWS = {c["icao"]: c for c in json.load(f)}

# ---------------------------------------------------------------------
# Crew duty
# ---------------------------------------------------------------------

REPORT_BEFORE_SOBT_MINUTES = 45   # typical short-haul report time
DISCRETION_MINUTES = 120          # ORO.FTL.205(f), two-pilot crew
MIN_FDP_MINUTES = 9 * 60

# ORO.FTL.205(b) Table 2: (band start "HHMM" local, max FDP for 1-2 sectors
# in minutes). Each sector after the second takes 30 minutes off, down to
# 9:00.
_FDP_BANDS = [
    ("0500", 12 * 60), ("0515", 12 * 60 + 15), ("0530", 12 * 60 + 30), ("0545", 12 * 60 + 45),
    ("0600", 13 * 60), ("1330", 12 * 60 + 45), ("1400", 12 * 60 + 30), ("1430", 12 * 60 + 15),
    ("1500", 12 * 60), ("1530", 11 * 60 + 45), ("1600", 11 * 60 + 30), ("1630", 11 * 60 + 15),
    ("1700", 11 * 60),
]


def max_fdp_minutes(report_local_minutes, sectors):
    """Maximum daily FDP (minutes) for an acclimatised crew reporting at
    report_local_minutes (minutes after local midnight) for `sectors`
    sectors. 17:00-04:59 is the 11:00 band."""
    hhmm = f"{report_local_minutes // 60:02d}{report_local_minutes % 60:02d}"
    base = 11 * 60
    for start, minutes in _FDP_BANDS:
        if hhmm >= start:
            base = minutes
    if hhmm < "0500":
        base = 11 * 60
    return max(MIN_FDP_MINUTES, base - 30 * max(0, sectors - 2))


def crew_duty(first_sobt_dt, dep_tz, sectors):
    """Report time and FDP limits for a rotation starting at first_sobt_dt
    (aware UTC) from an airport in dep_tz. The FDP itself (report to the
    last on-blocks) is measured in the browser against the expected and
    actual times."""
    if first_sobt_dt is None or not dep_tz:
        return None
    report = first_sobt_dt - timedelta(minutes=REPORT_BEFORE_SOBT_MINUTES)
    local = report.astimezone(ZoneInfo(dep_tz))
    max_fdp = max_fdp_minutes(local.hour * 60 + local.minute, sectors)
    return {
        "report_utc": report.strftime("%H:%M") + "Z",
        "report_local": local.strftime("%H:%M"),
        "report_before_sobt": REPORT_BEFORE_SOBT_MINUTES,
        "sectors": sectors,
        "max_fdp_minutes": max_fdp,
        "discretion_minutes": DISCRETION_MINUTES,
    }


# ---------------------------------------------------------------------
# Curfews
# ---------------------------------------------------------------------

def _zulu(local_hhmm, tz, ref_date):
    dt = local_time_to_zulu_dt(local_hhmm, tz, ref_date)
    return dt.strftime("%H:%M") + "Z" if dt else None


def leg_curfews(dep_icao, arr_icao, tz_lookup, ref_date):
    """Curfew windows that matter for one leg, in Zulu clock times for the
    leg's date: the departure airport's last take-off (or off-block) time
    and the destination's last landing time. The browser compares them with
    the leg's current expected times, so delays added later still count."""
    out = []
    for icao, kind in ((dep_icao, "departure"), (arr_icao, "arrival")):
        c = CURFEWS.get(icao)
        tz = tz_lookup.get(icao)
        if not c or not tz:
            continue
        until_local = c["landing_until"] if kind == "arrival" else c["takeoff_until"]
        check = "landing" if kind == "arrival" else c.get("departure_check", "takeoff")
        out.append({
            "icao": icao, "name": c["name"], "kind": kind, "check": check,
            "until_local": until_local, "opens_local": c["opens"],
            "until_utc": _zulu(until_local, tz, ref_date), "opens_utc": _zulu(c["opens"], tz, ref_date),
            "note": c["note"],
        })
    return out


# ---------------------------------------------------------------------
# ATFM slots
# ---------------------------------------------------------------------

ATFM_CODES = {"81", "82", "83", "84", "89"}
SLOT_WINDOW = (-5, 10)

# Area control centres a regulation can sit in, with a rough centre point:
# an en-route regulation is put in the one nearest the middle of the leg.
_ACCS = [
    ("EGTT", "London ACC", 52.0, -1.0), ("EGPX", "Scottish ACC", 56.0, -4.0), ("EISN", "Shannon ACC", 53.0, -9.0),
    ("LFFF", "Paris ACC", 48.5, 2.5), ("LFRR", "Brest ACC", 47.5, -2.5), ("LFBB", "Bordeaux ACC", 44.5, 0.0),
    ("LFMM", "Marseille ACC", 43.8, 5.0), ("LFEE", "Reims ACC", 48.8, 5.5), ("EDYY", "Maastricht UAC", 51.0, 6.0),
    ("EDUU", "Karlsruhe UAC", 49.0, 9.0), ("EDGG", "Langen ACC", 50.0, 8.5), ("EDWW", "Bremen ACC", 53.0, 9.0),
    ("EDMM", "Munich ACC", 48.5, 11.5), ("LSAS", "Swiss ACC", 46.8, 8.0), ("LIMM", "Milan ACC", 45.5, 9.5),
    ("LIPP", "Padua ACC", 45.0, 12.0), ("LIRR", "Rome ACC", 42.0, 12.5), ("LIBB", "Brindisi ACC", 40.5, 17.0),
    ("LECB", "Barcelona ACC", 41.5, 2.0), ("LECM", "Madrid ACC", 40.5, -3.7), ("LECS", "Seville ACC", 37.4, -6.0),
    ("LPPC", "Lisbon ACC", 39.0, -9.0), ("LOVV", "Vienna ACC", 48.0, 16.0), ("LKAA", "Prague ACC", 50.0, 14.5),
    ("EPWW", "Warsaw ACC", 52.0, 20.0), ("LHCC", "Budapest ACC", 47.4, 19.0), ("LRBB", "Bucharest ACC", 44.5, 26.0),
    ("LGGG", "Athens ACC", 38.0, 23.7), ("ESAA", "Sweden ACC", 59.5, 17.5), ("EKDK", "Copenhagen ACC", 55.6, 12.6),
]
_REASONS = {
    "81": "ATC capacity", "82": "ATC staffing", "83": "aerodrome capacity",
    "84": "weather at the destination", "89": "aerodrome capacity at departure",
}


def _nearest_acc(lat, lon):
    return min(_ACCS, key=lambda a: (a[2] - lat) ** 2 + ((a[3] - lon) * 0.7) ** 2)


def atfm_slot(delay, dep_info, arr_info, etot_text, rng=random):
    """A CTOT for a leg whose delay is an ATFM code. etot_text is the leg's
    expected take-off time ("HH:MMZ ..."); the CTOT is that time, i.e. the
    delay the regulation imposes. Returns None for other delays."""
    if not delay or delay.get("iata_code") not in ATFM_CODES or not etot_text:
        return None
    code = delay["iata_code"]
    if code in ("83", "84"):
        where = f"{arr_info.get('icao')} arrivals"
    elif code == "89":
        where = f"{dep_info.get('icao')} departures"
    else:
        try:
            lat = (float(dep_info["lat"]) + float(arr_info["lat"])) / 2
            lon = (float(dep_info["lon"]) + float(arr_info["lon"])) / 2
            icao, name, _, _ = _nearest_acc(lat, lon)
            where = f"{name} ({icao})"
        except (KeyError, TypeError, ValueError):
            where = "en route"
    return {"ctot": etot_text[:6], "reason": _REASONS[code], "where": where, "window": list(SLOT_WINDOW)}
