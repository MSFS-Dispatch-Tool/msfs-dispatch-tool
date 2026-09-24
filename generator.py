"""
Generator logic for VirtualDispatch.

Core model: itineraries are built from REAL round-trip pairs (same two
airports, reversed, sequential-ish flight numbers - the fingerprint of
an actual rotation) or genuine one-way legs, not arbitrary chains of
routes that happen to connect. Scheduled routes only - no synthesized
repositioning legs.
"""

import random
import re
import string
import uuid
from collections import defaultdict

MEL_PROBABILITY = 0.35
DELAY_PROBABILITY = 0.45
LMC_PROBABILITY = 0.50
DG_PROBABILITY_ON_LMC = 1.0  # dangerous goods is always "rolled" alongside
                             # LMC at loadsheet-confirm time - see app.py


# ---------------------------------------------------------------------
# Round-trip pairing (computed once at startup from the dataset)
# ---------------------------------------------------------------------

def flight_number_digits(flight_number):
    """The numeric part of a flight number as a string ("FR2016" -> "2016").
    Skips the 2-character airline designator first, since it can contain
    a digit itself - easyJet's is U2, so "U28391" is flight 8391."""
    return "".join(ch for ch in flight_number[2:] if ch.isdigit())


def _numeric_part(flight_number):
    return int(flight_number_digits(flight_number))


def find_round_trip_pairs(routes):
    """
    Returns a dict flight_number -> partner_flight_number for routes that
    form a real round-trip pair: same two airports, reversed direction,
    same operating carrier, and flight numbers within 3 of each other
    (the numbering convention real short-haul rotations use). A route
    with no match stays one-way.

    The same-carrier check matters once `routes` can span more than one
    carrier (see app.py) - without it, two different airlines happening
    to fly the same city pair with nearby flight numbers would get
    paired into a single "rotation", which is nonsense (you can't fly
    out on one airline and have it come back as a different one).
    """
    by_airport_pair = defaultdict(list)
    for r in routes:
        key = tuple(sorted([r["departure_icao"], r["arrival_icao"]]))
        by_airport_pair[key].append(r)

    pairing = {}
    for group in by_airport_pair.values():
        used = set()
        for r1 in group:
            if r1["flight_number"] in used:
                continue
            best, best_diff = None, None
            for r2 in group:
                if r2["flight_number"] in used or r2 is r1:
                    continue
                if r2["departure_icao"] == r1["arrival_icao"] and r2["arrival_icao"] == r1["departure_icao"] \
                        and r2.get("carrier") == r1.get("carrier"):
                    diff = abs(_numeric_part(r1["flight_number"]) - _numeric_part(r2["flight_number"]))
                    if diff <= 3 and (best_diff is None or diff < best_diff):
                        best, best_diff = r2, diff
            if best:
                pairing[r1["flight_number"]] = best["flight_number"]
                pairing[best["flight_number"]] = r1["flight_number"]
                used.add(r1["flight_number"])
                used.add(best["flight_number"])
    return pairing


# ---------------------------------------------------------------------
# Itinerary search
# ---------------------------------------------------------------------

def resolve_airport(airports, code):
    if not code:
        return None
    code = code.strip().upper()
    for a in airports:
        if a["icao"] == code or a["iata"] == code:
            return a["icao"]
    return None


def find_itineraries(routes, rt_pairing, origin_icao=None, destination_icao=None,
                      trip_type="random"):
    """
    trip_type: "random" (mix of RT and 1W), "round_trip" (RT pairs only),
    "one_way" (single legs only).

    Destination filter on a round-trip pair matches the AWAY airport
    (the outbound leg's arrival) - not the rotation's final airport,
    which by definition is always back at the origin. "I want a round
    trip to Barcelona" means the away leg lands in Barcelona.

    No time-available filter here by design - every real scheduled
    itinerary in the dataset is offered, and app.py orders a round trip's
    two legs by actual scheduled departure time (not flight number) once
    it resolves their Zulu times.
    """
    by_flight = {r["flight_number"]: r for r in routes}

    results = []

    if trip_type in ("random", "round_trip"):
        seen_pairs = set()
        for fn, partner_fn in rt_pairing.items():
            pair_key = tuple(sorted([fn, partner_fn]))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            leg_out = by_flight[fn]
            leg_back = by_flight[partner_fn]
            # orient leg_out as the outbound (matches origin filter if given)
            if origin_icao and leg_out["departure_icao"] != origin_icao:
                leg_out, leg_back = leg_back, leg_out

            if origin_icao and leg_out["departure_icao"] != origin_icao:
                continue
            if destination_icao and leg_out["arrival_icao"] != destination_icao:
                continue

            results.append({"legs": [leg_out, leg_back], "trip_type": "RT"})

    if trip_type in ("random", "one_way"):
        for r in routes:
            if origin_icao and r["departure_icao"] != origin_icao:
                continue
            if destination_icao and r["arrival_icao"] != destination_icao:
                continue
            results.append({"legs": [r], "trip_type": "1W"})

    random.shuffle(results)
    return results


# ---------------------------------------------------------------------
# Randomized per-leg conditions (rolled at /select time)
# ---------------------------------------------------------------------

def pick_cost_index():
    return round(random.triangular(2, 16, 6))


def weighted_pick(items):
    weights = [item["weight"] for item in items]
    return random.choices(items, weights=weights, k=1)[0]


def resolve_component(item):
    resolved = dict(item)
    options = item.get("component_options")
    if options:
        choice = random.choice(options)
        resolved["chosen_component"] = choice
        resolved["description"] = item["description"].replace("{side}", str(choice))
        if "dispatch_consequence" in item:
            resolved["dispatch_consequence"] = item["dispatch_consequence"].replace("{side}", str(choice))
    else:
        resolved["chosen_component"] = None
    return resolved


def resolve_lmc(item):
    resolved = dict(item)
    lo, hi = item["delta_range"]
    resolved["delta"] = random.randint(lo, hi)
    return resolved


LOAD_FACTOR_LOW = 0.82
LOAD_FACTOR_MODE = 0.94   # Ryanair's reported ~2025 full-year load factor
LOAD_FACTOR_HIGH = 0.99
# A plain triangular(0.82, 0.99, 0.94) tapers to ~zero density right at
# capacity, so a genuinely full house almost never came up - which
# doesn't match Ryanair's own reputation for pushing flights to (and
# past, via overbooking) capacity. A flat chance of a full 189/189 house
# on top of the triangular distribution fixes that without touching the
# realistic spread for the rest.
FULL_FLIGHT_PROBABILITY = 0.30

AVG_CHECKED_BAG_KG = 15
CARGO_SURGE_PROBABILITY = 0.12   # e.g. summer beach-route demand spike
CARGO_SURGE_MULTIPLIER_RANGE = (1.3, 1.9)

TAXI_OUT_MIN_MINUTES = 10
TAXI_OUT_MAX_MINUTES = 15


def _bag_check_rate(duration_minutes):
    """Rough, not-derived-from-real-data heuristic: longer sectors see a
    higher share of passengers checking a bag (more likely to be a
    leisure/holiday trip needing luggage, vs a short business hop with
    carry-on only). Calibrated so a ~160min sector lands close to the
    ~1000kg 'normal' cargo figure requested, capped so it doesn't run
    away on the longest sectors in the dataset."""
    return min(0.60, 0.25 + (duration_minutes / 300) * 0.30)


def pax_and_cargo(seat_capacity, duration_minutes, load_factor=None):
    """Pax count + cargo weight for a given aircraft's seat capacity.

    load_factor is normally rolled fresh here (None - the /select path),
    but the aircraft-reassignment path (app.py's /select/reassign-aircraft)
    passes the ALREADY-shown load factor back in, so changing which
    aircraft is assigned re-derives pax/cargo for the new capacity
    without also silently re-rolling how full the flight is - the pilot
    already saw and reviewed that number.

    Cargo here means checked/hold baggage only (this tool doesn't model
    belly freight), sized off pax_count and sector length with some
    built-in randomness and an occasional high-cargo "surge" leg."""
    if load_factor is None:
        if random.random() < FULL_FLIGHT_PROBABILITY:
            load_factor = 1.0
        else:
            load_factor = round(min(1.0, random.triangular(LOAD_FACTOR_LOW, LOAD_FACTOR_HIGH, LOAD_FACTOR_MODE)), 2)
    pax_count = round(seat_capacity * load_factor)

    bag_rate = _bag_check_rate(duration_minutes)
    expected_cargo_kg = pax_count * bag_rate * AVG_CHECKED_BAG_KG
    cargo_weight_kg = round(random.triangular(
        expected_cargo_kg * 0.75, expected_cargo_kg * 1.3, expected_cargo_kg
    ))
    if random.random() < CARGO_SURGE_PROBABILITY:
        cargo_weight_kg = round(cargo_weight_kg * random.uniform(*CARGO_SURGE_MULTIPLIER_RANGE))

    return pax_count, load_factor, cargo_weight_kg


def assign_aircraft_type(fleet_by_type, owned_types=None, explicit_type=None):
    """Picks which aircraft type in a carrier's fleet operates a leg.

    explicit_type wins outright when the route data itself specifies one
    and it's a real fleet member (true for 100% of Ryanair's routes,
    ~1% of easyJet's - AirLabs doesn't report aircraft type for most of
    its schedule data).

    Otherwise, picks randomly from whichever of the pilot's owned
    aircraft (see db.DEFAULT_SETTINGS' profile.aircraft_owned) are in
    this carrier's fleet, weighted by each type's real-world fleet
    prevalence (carrier.json's fleet[].weight) - e.g. easyJet flying far
    more A320s/A319s than A321neos should show up far more often than
    not. Falls back to the carrier's WHOLE fleet, same weighting, when
    the pilot hasn't told the app which aircraft they fly (or owns none
    that carrier flies) - so assignment always produces something
    plausible rather than erroring out."""
    if explicit_type and explicit_type in fleet_by_type:
        return explicit_type
    owned_types = owned_types or ()
    candidates = [t for t in fleet_by_type if t in owned_types] or list(fleet_by_type.keys())
    weights = [fleet_by_type[t].get("weight", 1) for t in candidates]
    return random.choices(candidates, weights=weights, k=1)[0]


def generate_leg_conditions(duration_minutes=0, seat_capacity=189):
    """Cost index (+ pax/cargo via pax_and_cargo) for one leg.

    seat_capacity comes from the operating carrier's fleet entry for
    this route's assigned aircraft type (see app.py's assign_aircraft_type
    and seat_capacity_for) - it defaults to 189 (the 737-800's seating)
    as a fallback only, not because this function assumes a single type."""
    pax_count, load_factor, cargo_weight_kg = pax_and_cargo(seat_capacity, duration_minutes)

    return {
        "pax_count": pax_count,
        "seat_capacity": seat_capacity,
        "load_factor": load_factor,
        "cargo_weight_kg": cargo_weight_kg,
        "cost_index": pick_cost_index(),
        "delay": None,  # rolled below, per leg, separately
        # Taxi-out is the only taxi figure that varies (10-15min); taxi-in
        # is a fixed 10 minutes - see timeutils.TAXI_IN_MINUTES.
        "taxi_out_minutes": random.randint(TAXI_OUT_MIN_MINUTES, TAXI_OUT_MAX_MINUTES),
    }


def _filter_enabled(items, category_settings, field_name, settings_key):
    """Applies a generation-settings category dict ({"enabled": bool,
    settings_key: [...disabled item ids...]}) to an items list: None (no
    settings, e.g. DATABASE_URL unset) means "everything on", matching
    the app's long-standing default behavior."""
    if category_settings is None:
        return items
    if not category_settings.get("enabled", True):
        return []
    disabled = set(category_settings.get(settings_key) or [])
    return [item for item in items if item[field_name] not in disabled]


# ---------------------------------------------------------------------
# Weather-gated delay codes - a handful of the IATA delay codes only
# make sense under specific weather (can't de-ice at 20C), so before
# they can be rolled at all the leg's own METAR has to actually support
# them. This is a best-effort METAR reader, not a full decoder: it only
# pulls the few fields these three codes need, and leaves a field None
# rather than guessing when it can't confidently find it.
# ---------------------------------------------------------------------

_METAR_WIND_RE = re.compile(r'\b(?:\d{3}|VRB)(\d{2,3})(?:G\d{2,3})?(KT|MPS)\b')
_METAR_TEMP_RE = re.compile(r'\s(M?\d{2})/(M?\d{2})\s')
_METAR_SM_VIS_RE = re.compile(r'\b(\d+)SM\b')
_METAR_SIG_WX_RE = re.compile(
    r'\b[-+]?(?:VC)?(?:MI|PR|BC|DR|BL|SH|TS|FZ)?'
    r'(DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|PO|SQ|FC|SS|DS)\b'
)

_DEICING_TEMP_THRESHOLD_C = 5
_LOW_VIS_THRESHOLD_M = 5000
_STRONG_WIND_THRESHOLD_KT = 25
_EXTREME_COLD_THRESHOLD_C = -10
# Weather-gated delay codes, and which station's METAR each one checks -
# keyed by IATA delay code since there are only three of these and each
# has genuinely different semantics, not worth a data-file schema for.
WEATHER_GATED_CODES = {"71": "departure", "72": "arrival", "75": "departure"}


def parse_metar(metar_text):
    """Extracts temperature, visibility, wind speed, and significant
    weather phenomena from a raw METAR string. Every field is None (or
    an empty set) when not confidently found, including when metar_text
    itself is empty/unavailable - callers treat "unknown" as "can't
    confirm this weather code is warranted", not as "assume the worst"."""
    result = {"temp_c": None, "visibility_m": None, "wind_kt": None, "phenomena": set(), "cavok": False}
    if not metar_text:
        return result
    text = f' {metar_text.upper()} '

    if re.search(r'\bCAVOK\b', text):
        result["cavok"] = True
        result["visibility_m"] = 10000

    wind_match = _METAR_WIND_RE.search(text)
    if wind_match:
        speed, unit = wind_match.groups()
        speed = int(speed)
        result["wind_kt"] = round(speed * 1.94384) if unit == "MPS" else speed

    if not result["cavok"]:
        vis_match = re.search(r'\s(\d{4})\s', text)
        sm_match = _METAR_SM_VIS_RE.search(text)
        if vis_match:
            result["visibility_m"] = int(vis_match.group(1))
        elif sm_match:
            result["visibility_m"] = round(int(sm_match.group(1)) * 1609.34)

        for m in _METAR_SIG_WX_RE.finditer(text):
            result["phenomena"].add(m.group(1))

    temp_match = _METAR_TEMP_RE.search(text)
    if temp_match:
        raw_temp, _ = temp_match.groups()
        result["temp_c"] = -int(raw_temp[1:]) if raw_temp.startswith('M') else int(raw_temp)

    return result


_SIGNIFICANT_WX_PHENOMENA = {
    "RA", "DZ", "SN", "SG", "IC", "PL", "GR", "GS",
    "BR", "FG", "FU", "VA", "SA", "HZ", "PY", "PO", "SQ", "FC", "SS", "DS",
}


def _weather_supports_delay(iata_code, dep_metar, arr_metar):
    station_key = WEATHER_GATED_CODES.get(iata_code)
    if station_key is None:
        return True
    station = parse_metar(dep_metar if station_key == "departure" else arr_metar)

    if iata_code == "75":  # De-icing - cold-triggered, departure conditions
        return station["temp_c"] is not None and station["temp_c"] <= _DEICING_TEMP_THRESHOLD_C

    # 71/72 - weather at departure/destination station
    if station["cavok"]:
        return False
    return (
        (station["visibility_m"] is not None and station["visibility_m"] < _LOW_VIS_THRESHOLD_M)
        or (station["wind_kt"] is not None and station["wind_kt"] >= _STRONG_WIND_THRESHOLD_KT)
        or bool(station["phenomena"] & _SIGNIFICANT_WX_PHENOMENA)
        or (station["temp_c"] is not None and station["temp_c"] <= _EXTREME_COLD_THRESHOLD_C)
    )


def roll_delay(delay_codes, delay_settings=None, dep_metar=None, arr_metar=None):
    pool = _filter_enabled(delay_codes, delay_settings, "iata_code", "disabled_codes")
    pool = [d for d in pool if _weather_supports_delay(d["iata_code"], dep_metar, arr_metar)]
    if not pool:
        return None
    return weighted_pick(pool) if random.random() < DELAY_PROBABILITY else None


def roll_mel(mels, mel_settings=None):
    pool = _filter_enabled(mels, mel_settings, "id", "disabled_ids")
    if not pool:
        return None
    return resolve_component(weighted_pick(pool)) if random.random() < MEL_PROBABILITY else None


def generate_callsign(prefix="RYR"):
    """Generate <prefix> followed by one or two digits and one or two
    letters (e.g. RYR7K, EZY42XY) - the carrier's own callsign_prefix
    (see app.py's carrier.json) drives this, so a second carrier gets
    its own callsigns for free."""
    digits = "".join(random.choices(string.digits, k=random.randint(1, 2)))
    letters = "".join(random.choices(string.ascii_uppercase, k=random.randint(1, 2)))
    return f"{prefix}{digits}{letters}"


def generate_loadsheet_extras(dangerous_goods, lmc_events, lmc_settings=None):
    """Dangerous goods + last-minute change, rolled together at loadsheet
    sign-off (CONFIRM), not during route browsing - see app.py /confirm.
    Dangerous goods isn't user-toggleable (out of scope of the LMC/MEL/
    delay settings) - only LMC is filtered by lmc_settings."""
    dg = resolve_component(weighted_pick(dangerous_goods))
    pool = _filter_enabled(lmc_events, lmc_settings, "id", "disabled_ids")
    lmc = resolve_lmc(weighted_pick(pool)) if pool and random.random() < LMC_PROBABILITY else None
    return dg, lmc
