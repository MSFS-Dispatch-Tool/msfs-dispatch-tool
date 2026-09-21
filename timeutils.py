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
            # The scraped arrival local time has no attached date, just
            # anchored to the same reference_date as departure - for a
            # long enough sector, or a big enough timezone gap between
            # the two airports, that naive same-date conversion can land
            # BEFORE departure in absolute Zulu terms (e.g. an evening
            # London departure landing at a "23:xx" local Canaries time
            # that's actually the same real moment shifted a day back).
            # Roll it forward a day at a time until it's after departure -
            # correct for any single leg under 24h, true for this dataset.
            while arr_dt <= dep_dt:
                arr_dt += timedelta(days=1)
            return dep_dt, arr_dt, "scraped"

    arr_dt = dep_dt + timedelta(minutes=route["duration_minutes"])
    return dep_dt, arr_dt, "computed"


# Taxi-in is fixed; taxi-out is the variable 10-15min figure rolled in
# generator.py (taxi_out_minutes), since it's random per leg like
# cost_index/pax_count rather than derived from static schedule data.
TAXI_IN_MINUTES = 10


def resolve_leg_schedule(route, tz_lookup, reference_date, taxi_out_minutes):
    """
    Returns the underlying aware SOBT/SIBT datetimes (under _sobt_dt/
    _sibt_dt) plus EET in minutes. All fields are None if the route has
    no resolvable scheduled_departure_local - a defensive fallback,
    since every route in the current dataset has one.

    Callers derive STOT/SLDT themselves from _sobt_dt/_sibt_dt plus
    taxi_out_minutes/TAXI_IN_MINUTES (see app.py's /select) - this
    function doesn't format Zulu strings itself since STOT/SLDT/SIBT's
    day-offset suffix needs to be anchored to SOBT's own date, not this
    function's reference_date (see format_zulu call sites in app.py).

    SOBT = scheduled off-block (gate departure) time - the scraped/derived
           departure time.
    SIBT = scheduled in-block (gate arrival) time - the scraped arrival
           time if present, otherwise SOBT + duration_minutes.
    STOT = SOBT + taxi-out time (taxi_out_minutes).
    SLDT = SIBT - taxi-in time (TAXI_IN_MINUTES).
    EET  = SLDT - STOT, i.e. the airborne portion only.
    """
    dep_tz = tz_lookup.get(route["departure_icao"], "")
    sobt_dt = local_time_to_zulu_dt(route.get("scheduled_departure_local"), dep_tz, reference_date)
    if sobt_dt is None:
        return {"eet_minutes": None, "_sobt_dt": None, "_sibt_dt": None}

    arr_tz = tz_lookup.get(route["arrival_icao"], "")
    sibt_dt = None
    if route.get("scheduled_arrival_local"):
        sibt_dt = local_time_to_zulu_dt(route["scheduled_arrival_local"], arr_tz, reference_date)
        if sibt_dt is not None:
            # See the matching comment in resolve_leg_times - same-date
            # anchoring can otherwise land the scraped arrival before the
            # departure it belongs to.
            while sibt_dt <= sobt_dt:
                sibt_dt += timedelta(days=1)
    if sibt_dt is None:
        sibt_dt = sobt_dt + timedelta(minutes=route["duration_minutes"])

    stot_dt = sobt_dt + timedelta(minutes=taxi_out_minutes)
    sldt_dt = sibt_dt - timedelta(minutes=TAXI_IN_MINUTES)
    eet_minutes = round((sldt_dt - stot_dt).total_seconds() / 60)

    return {
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


MIN_TURNAROUND_MINUTES = 30


def turnaround_shift(prev_arrival_zulu_dt, next_departure_zulu_dt, minimum_minutes=MIN_TURNAROUND_MINUTES):
    """
    How many minutes the next leg's whole schedule needs to be pushed
    back to guarantee at least `minimum_minutes` of turnaround after the
    previous leg's arrival. Zero if the gap is already sufficient, or if
    either time is missing.

    Scraped schedule times occasionally leave an unrealistically short
    (or even negative) turnaround between two legs of the same rotation.
    This doesn't invent a different flight - it shifts the second leg's
    entire schedule later by the deficit, so a real turnaround fits.
    """
    if prev_arrival_zulu_dt is None or next_departure_zulu_dt is None:
        return timedelta(0)
    gap = next_departure_zulu_dt - prev_arrival_zulu_dt
    minimum = timedelta(minutes=minimum_minutes)
    if gap >= minimum:
        return timedelta(0)
    return minimum - gap
