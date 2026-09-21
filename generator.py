"""
Generator logic for SimDispatch.

Core model: itineraries are built from REAL round-trip pairs (same two
airports, reversed, sequential-ish flight numbers - the fingerprint of
an actual rotation) or genuine one-way legs, not arbitrary chains of
routes that happen to connect. Scheduled routes only - no synthesized
repositioning legs.
"""

import random
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

def _numeric_part(flight_number):
    return int("".join(ch for ch in flight_number if ch.isdigit()))


def find_round_trip_pairs(routes):
    """
    Returns a dict flight_number -> partner_flight_number for routes that
    form a real round-trip pair: same two airports, reversed direction,
    and flight numbers within 3 of each other (the numbering convention
    real short-haul rotations use). A route with no match stays one-way.
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
                if r2["departure_icao"] == r1["arrival_icao"] and r2["arrival_icao"] == r1["departure_icao"]:
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


def generate_leg_conditions(duration_minutes=0):
    """Pax/cargo/cost index for one leg.

    Cargo here means checked/hold baggage only (this tool doesn't model
    belly freight), sized off pax_count and sector length with some
    built-in randomness and an occasional high-cargo "surge" leg."""
    if random.random() < FULL_FLIGHT_PROBABILITY:
        load_factor = 1.0
    else:
        load_factor = round(min(1.0, random.triangular(LOAD_FACTOR_LOW, LOAD_FACTOR_HIGH, LOAD_FACTOR_MODE)), 2)
    pax_count = round(189 * load_factor)

    bag_rate = _bag_check_rate(duration_minutes)
    expected_cargo_kg = pax_count * bag_rate * AVG_CHECKED_BAG_KG
    cargo_weight_kg = round(random.triangular(
        expected_cargo_kg * 0.75, expected_cargo_kg * 1.3, expected_cargo_kg
    ))
    if random.random() < CARGO_SURGE_PROBABILITY:
        cargo_weight_kg = round(cargo_weight_kg * random.uniform(*CARGO_SURGE_MULTIPLIER_RANGE))

    return {
        "pax_count": pax_count,
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


def roll_delay(delay_codes, delay_settings=None):
    pool = _filter_enabled(delay_codes, delay_settings, "iata_code", "disabled_codes")
    if not pool:
        return None
    return weighted_pick(pool) if random.random() < DELAY_PROBABILITY else None


def roll_mel(mels, mel_settings=None):
    pool = _filter_enabled(mels, mel_settings, "id", "disabled_ids")
    if not pool:
        return None
    return resolve_component(weighted_pick(pool)) if random.random() < MEL_PROBABILITY else None


def generate_callsign():
    """Generate RYR followed by one or two digits and one or two letters."""
    digits = "".join(random.choices(string.digits, k=random.randint(1, 2)))
    letters = "".join(random.choices(string.ascii_uppercase, k=random.randint(1, 2)))
    return f"RYR{digits}{letters}"


def generate_loadsheet_extras(dangerous_goods, lmc_events, lmc_settings=None):
    """Dangerous goods + last-minute change, rolled together at loadsheet
    sign-off (CONFIRM), not during route browsing - see app.py /confirm.
    Dangerous goods isn't user-toggleable (out of scope of the LMC/MEL/
    delay settings) - only LMC is filtered by lmc_settings."""
    dg = resolve_component(weighted_pick(dangerous_goods))
    pool = _filter_enabled(lmc_events, lmc_settings, "id", "disabled_ids")
    lmc = resolve_lmc(weighted_pick(pool)) if pool and random.random() < LMC_PROBABILITY else None
    return dg, lmc
