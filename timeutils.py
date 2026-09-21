"""
Local-to-Zulu time conversion for scheduled departure/arrival times.

IMPORTANT CAVEAT: a scraped schedule time like "14:30" is a recurring
timetable time with no attached date, but converting to UTC needs to know
the date (because of DST). This module anchors every conversion to TODAY's
date in each airport's local timezone - meaning the Zulu time shown is
correct for flying it today, but could be off by an hour if you're
mentally planning around a different season than what's currently in
effect at that airport. There's no fix for this without the tool tracking
an actual flight date, which it deliberately doesn't (see the Option A/
stateless decision from earlier in the build).
"""

from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")


def local_time_to_zulu_dt(time_str, iana_tz, reference_date=None):
    """
    Converts a local "HH:MM" string at the given IANA timezone into a
    timezone-aware UTC datetime, anchored to reference_date (defaults to
    today). Returns None if time_str or iana_tz is missing/invalid.
    """
    if not time_str or not iana_tz:
        return None
    try:
        hh, mm = map(int, time_str.strip().split(":"))
    except (ValueError, AttributeError):
        return None

    ref = reference_date or date.today()
    try:
        local_dt = datetime(ref.year, ref.month, ref.day, hh, mm, tzinfo=ZoneInfo(iana_tz))
    except Exception:
        return None
    return local_dt.astimezone(UTC)


def format_zulu(utc_dt, reference_date=None):
    """Formats a UTC datetime as 'HH:MMZ', with a '+1'/'-1' day suffix if
    it fell on a different calendar day than the reference date."""
    if utc_dt is None:
        return None
    ref = reference_date or date.today()
    offset = (utc_dt.date() - ref).days
    suffix = f" ({'+' if offset > 0 else ''}{offset}d)" if offset != 0 else ""
    return f"{utc_dt.strftime('%H:%M')}Z{suffix}"


def resolve_leg_times(route, tz_lookup, reference_date=None):
    """
    Returns (departure_zulu_dt, arrival_zulu_dt, arrival_source) for a
    route leg. Uses the REAL scraped scheduled_arrival_local when present;
    falls back to departure + duration_minutes when it isn't (arrival
    data is incomplete for some flights) - arrival_source tells you which
    happened, so the UI can be honest about it rather than presenting a
    computed figure as if it were scraped.
    """
    dep_tz = tz_lookup.get(route["departure_icao"], "")
    dep_dt = local_time_to_zulu_dt(route.get("scheduled_departure_local"), dep_tz, reference_date)
    if dep_dt is None:
        return None, None, None

    arr_tz = tz_lookup.get(route["arrival_icao"], "")
    if route.get("scheduled_arrival_local"):
        arr_dt = local_time_to_zulu_dt(route["scheduled_arrival_local"], arr_tz, reference_date)
        if arr_dt is not None:
            return dep_dt, arr_dt, "scraped"

    arr_dt = dep_dt + timedelta(minutes=route["duration_minutes"])
    return dep_dt, arr_dt, "computed"


def taxi_minutes(icao, large_airport_lookup):
    """15 minutes at a 'large' airport, 10 at a 'small' one. There's no
    real runway/stand-count data in this dataset, so 'large' is a proxy:
    airports touched by many scheduled routes in routes_enriched.json
    (see LARGE_AIRPORT_THRESHOLD in app.py) are treated as large hubs.
    An airport with no scheduled routes at all (e.g. only ever used as a
    repositioning endpoint) defaults to small."""
    return 15 if large_airport_lookup.get(icao, False) else 10


def resolve_leg_schedule(route, tz_lookup, large_airport_lookup, reference_date=None):
    """
    Returns SOBT/STOT/SLDT/SIBT (all Zulu strings) plus EET in minutes,
    and the underlying aware datetimes (for turnaround math) under the
    _sobt_dt/_sibt_dt keys. All fields are None if the route has no
    resolvable scheduled_departure_local (true for repositioning legs,
    which have no timetable at all).

    SOBT = scheduled off-block (gate departure) time - the scraped/derived
           departure time, unchanged from before.
    SIBT = scheduled in-block (gate arrival) time - the scraped arrival
           time if present, otherwise SOBT + duration_minutes.
    STOT = SOBT + taxi-out time at the departure airport.
    SLDT = SIBT - taxi-in time at the arrival airport.
    EET  = SLDT - STOT, i.e. the airborne portion only.
    """
    dep_tz = tz_lookup.get(route["departure_icao"], "")
    sobt_dt = local_time_to_zulu_dt(route.get("scheduled_departure_local"), dep_tz, reference_date)
    if sobt_dt is None:
        return {"sobt": None, "stot": None, "sldt": None, "sibt": None,
                "eet_minutes": None, "_sobt_dt": None, "_sibt_dt": None}

    arr_tz = tz_lookup.get(route["arrival_icao"], "")
    sibt_dt = None
    if route.get("scheduled_arrival_local"):
        sibt_dt = local_time_to_zulu_dt(route["scheduled_arrival_local"], arr_tz, reference_date)
    if sibt_dt is None:
        sibt_dt = sobt_dt + timedelta(minutes=route["duration_minutes"])

    taxi_out = taxi_minutes(route["departure_icao"], large_airport_lookup)
    taxi_in = taxi_minutes(route["arrival_icao"], large_airport_lookup)
    stot_dt = sobt_dt + timedelta(minutes=taxi_out)
    sldt_dt = sibt_dt - timedelta(minutes=taxi_in)
    eet_minutes = round((sldt_dt - stot_dt).total_seconds() / 60)

    return {
        "sobt": format_zulu(sobt_dt, reference_date),
        "stot": format_zulu(stot_dt, reference_date),
        "sldt": format_zulu(sldt_dt, reference_date),
        "sibt": format_zulu(sibt_dt, reference_date),
        "eet_minutes": eet_minutes,
        "_sobt_dt": sobt_dt,
        "_sibt_dt": sibt_dt,
    }


_SIMBRIEF_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def simbrief_date_str(reference_date=None):
    """Formats a date the way SimBrief's dispatch-form 'date' field
    expects: DDMMMYY, e.g. 21SEP26."""
    d = reference_date or date.today()
    return f"{d.day:02d}{_SIMBRIEF_MONTHS[d.month - 1]}{d.year % 100:02d}"


def turnaround_minutes(prev_arrival_zulu_dt, next_departure_zulu_dt):
    """Minutes between one leg's arrival and the next leg's departure.
    Returns None if either side is missing or the numbers don't make
    sense (negative turnaround - almost certainly bad/missing data)."""
    if prev_arrival_zulu_dt is None or next_departure_zulu_dt is None:
        return None
    delta = (next_departure_zulu_dt - prev_arrival_zulu_dt).total_seconds() / 60
    if delta < 0:
        return None
    return round(delta)
