"""
Generator logic for SimDispatch.

Core model, revised: itineraries are built from REAL round-trip pairs
(same two airports, reversed, sequential-ish flight numbers - the
fingerprint of an actual rotation) or genuine one-way legs, not arbitrary
chains of routes that happen to connect. Repositioning legs are
synthesized separately, only between airports with no scheduled service
in the dataset at all.
"""

import math
import random
import string
import uuid
from collections import defaultdict

MEL_PROBABILITY = 0.35
DELAY_PROBABILITY = 0.45
LMC_PROBABILITY = 0.50
DG_PROBABILITY_ON_LMC = 1.0  # dangerous goods is always "rolled" alongside
                             # LMC at loadsheet-confirm time - see app.py

MAX_ITINERARIES = 40
REPOSITIONING_PROBABILITY = 0.06   # low, per the person's own spec
REPOSITIONING_CANDIDATES = 3
REPOSITIONING_CARGO_CHANCE = 0.20  # "some cargo, if realistic"


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


def _haversine_nm(lat1, lon1, lat2, lon2):
    R_km = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R_km * c * 0.539957


def _synthesize_repositioning(airports, existing_pairs, aircraft_type,
                               available_minutes, origin_icao, destination_icao,
                               count=REPOSITIONING_CANDIDATES):
    """
    Builds synthetic empty-leg options between airports NOT connected by
    any real route in the dataset. Not a real scheduled flight, so it has
    no flight number, no scheduled times - just distance/duration derived
    from great-circle geometry, same as the original route enrichment.
    """
    candidates = []
    pool = [a for a in airports if a.get("lat") is not None]
    attempts = 0
    while len(candidates) < count and attempts < 300:
        attempts += 1
        a1, a2 = random.sample(pool, 2)
        if origin_icao and a1["icao"] != origin_icao:
            continue
        if destination_icao and a2["icao"] != destination_icao:
            continue
        key = tuple(sorted([a1["icao"], a2["icao"]]))
        if key in existing_pairs:
            continue  # a real scheduled route already covers this pair
        dist_nm = _haversine_nm(a1["lat"], a1["lon"], a2["lat"], a2["lon"])
        duration_minutes = round(dist_nm / 420 * 60) + 25  # rough cruise speed + taxi/climb pad
        if duration_minutes > available_minutes:
            continue
        candidates.append({
            "flight_number": "REPO" + "".join(random.choices(string.digits, k=3)),
            "callsign_prefix": None,
            "departure_iata": a1["iata"], "departure_icao": a1["icao"],
            "arrival_iata": a2["iata"], "arrival_icao": a2["icao"],
            "aircraft_type": aircraft_type,
            "duration_minutes": duration_minutes,
            "distance_nm": round(dist_nm),
            "scheduled_departure_local": None,
            "scheduled_arrival_local": None,
        })
    return candidates


def find_itineraries(routes, rt_pairing, airports, aircraft_type, available_minutes,
                      origin_icao=None, destination_icao=None,
                      trip_type="random", include_repositioning=False):
    """
    trip_type: "random" (mix of RT and 1W), "round_trip" (RT pairs only),
    "one_way" (single legs only).

    Destination filter on a round-trip pair matches the AWAY airport
    (the outbound leg's arrival) - not the rotation's final airport,
    which by definition is always back at the origin. "I want a round
    trip to Barcelona" means the away leg lands in Barcelona.
    """
    by_flight = {r["flight_number"]: r for r in routes}
    existing_pairs = {tuple(sorted([r["departure_icao"], r["arrival_icao"]])) for r in routes}

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

            total_minutes = leg_out["duration_minutes"] + leg_back["duration_minutes"]
            if total_minutes > available_minutes:
                continue

            results.append({"legs": [leg_out, leg_back], "trip_type": "RT"})

    if trip_type in ("random", "one_way"):
        for r in routes:
            if origin_icao and r["departure_icao"] != origin_icao:
                continue
            if destination_icao and r["arrival_icao"] != destination_icao:
                continue
            if r["duration_minutes"] > available_minutes:
                continue
            results.append({"legs": [r], "trip_type": "1W"})

    if include_repositioning and random.random() < REPOSITIONING_PROBABILITY:
        for repo in _synthesize_repositioning(
            airports, existing_pairs, aircraft_type, available_minutes, origin_icao, destination_icao
        ):
            results.append({"legs": [repo], "trip_type": "RE"})

    random.shuffle(results)
    return results[:MAX_ITINERARIES]


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

AVG_CHECKED_BAG_KG = 15
CARGO_SURGE_PROBABILITY = 0.12   # e.g. summer beach-route demand spike
CARGO_SURGE_MULTIPLIER_RANGE = (1.3, 1.9)


def _bag_check_rate(duration_minutes):
    """Rough, not-derived-from-real-data heuristic: longer sectors see a
    higher share of passengers checking a bag (more likely to be a
    leisure/holiday trip needing luggage, vs a short business hop with
    carry-on only). Calibrated so a ~160min sector lands close to the
    ~1000kg 'normal' cargo figure requested, capped so it doesn't run
    away on the longest sectors in the dataset."""
    return min(0.60, 0.25 + (duration_minutes / 300) * 0.30)


def generate_leg_conditions(is_repositioning=False, duration_minutes=0):
    """Pax/cargo/cost index for one leg. Repositioning legs fly empty,
    with a small chance of some cargo, per spec.

    Cargo here means checked/hold baggage only (this tool doesn't model
    belly freight), sized off pax_count and sector length with some
    built-in randomness and an occasional high-cargo "surge" leg."""
    if is_repositioning:
        pax_count, load_factor = 0, 0.0
        cargo_weight_kg = round(random.uniform(50, 400)) if random.random() < REPOSITIONING_CARGO_CHANCE else 0
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
    }


def roll_delay(delay_codes):
    return weighted_pick(delay_codes) if random.random() < DELAY_PROBABILITY else None


def generate_callsign():
    """Generate RYR followed by one or two digits and one or two letters."""
    digits = "".join(random.choices(string.digits, k=random.randint(1, 2)))
    letters = "".join(random.choices(string.ascii_uppercase, k=random.randint(1, 2)))
    return f"RYR{digits}{letters}"


def generate_loadsheet_extras(dangerous_goods, lmc_events):
    """Dangerous goods + last-minute change, rolled together at loadsheet
    sign-off (CONFIRM), not during route browsing - see app.py /confirm."""
    dg = resolve_component(weighted_pick(dangerous_goods))
    lmc = resolve_lmc(weighted_pick(lmc_events)) if random.random() < LMC_PROBABILITY else None
    return dg, lmc
