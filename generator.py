"""
Generator logic for the MSFS immersion tool (step 4, revised for
search/filter/multi-leg selection).

Split into two phases, matching the new flow:
1. find_itineraries() - lists candidate route combinations matching the
   filters (origin, destination, leg count, time budget). Pure route/time
   logic, no randomized conditions yet.
2. generate_conditions() - once the person has picked one itinerary, rolls
   the airframe-level MEL once for the whole rotation, and rolls
   pax/cargo/dangerous-goods/delay/LMC separately per leg, since those are
   sector-specific, not aircraft-specific.
"""

import random
import uuid

MEL_PROBABILITY = 0.35
DELAY_PROBABILITY = 0.45
LMC_PROBABILITY = 0.50

MAX_LEGS = 3          # hard cap - beyond this, search space explodes for
                       # little realistic benefit on a single session
MAX_ITINERARIES = 40  # cap on how many options are returned to the person


def resolve_airport(airports, code):
    """
    Accepts an ICAO or IATA code (case-insensitive) and returns the matching
    airport's ICAO code, or None if it doesn't exist in the dataset.
    """
    if not code:
        return None
    code = code.strip().upper()
    for a in airports:
        if a["icao"] == code or a["iata"] == code:
            return a["icao"]
    return None


def find_itineraries(routes, aircraft_type, available_minutes,
                      origin_icao=None, destination_icao=None, num_legs=None):
    """
    Returns a list of itineraries, each a list of 1..MAX_LEGS route dicts
    chained so each leg's departure equals the previous leg's arrival.

    - origin_icao, if given, constrains the FIRST leg's departure.
    - destination_icao, if given, constrains the LAST leg's arrival.
    - num_legs, if given, requires that exact chain length; if None
      ("auto"), all chain lengths up to MAX_LEGS that fit the time budget
      are considered.
    - Total chain duration must fit within available_minutes.
    """
    by_departure = {}
    for r in routes:
        if r["aircraft_type"] != aircraft_type or r["duration_minutes"] is None:
            continue
        by_departure.setdefault(r["departure_icao"], []).append(r)

    leg_counts_to_try = [num_legs] if num_legs else list(range(1, MAX_LEGS + 1))

    results = []

    def extend(chain, remaining_minutes, target_legs, bucket, bucket_cap):
        if len(bucket) >= bucket_cap:
            return
        if len(chain) == target_legs:
            if destination_icao and chain[-1]["arrival_icao"] != destination_icao:
                return
            bucket.append(list(chain))
            return

        current_airport = chain[-1]["arrival_icao"] if chain else origin_icao
        candidates = by_departure.get(current_airport, []) if current_airport else routes

        for leg in candidates:
            if origin_icao and not chain and leg["departure_icao"] != origin_icao:
                continue
            if leg["duration_minutes"] > remaining_minutes:
                continue
            chain.append(leg)
            extend(chain, remaining_minutes - leg["duration_minutes"], target_legs, bucket, bucket_cap)
            chain.pop()
            if len(bucket) >= bucket_cap:
                return

    # Each leg-count gets its own capped bucket so "auto" mode returns a mix
    # of 1/2/3-leg options instead of one leg-count exhausting the whole cap.
    per_count_cap = MAX_ITINERARIES if num_legs else max(1, MAX_ITINERARIES // len(leg_counts_to_try))
    for legs in leg_counts_to_try:
        bucket = []
        extend([], available_minutes, legs, bucket, per_count_cap)
        results.extend(bucket)

    return results[:MAX_ITINERARIES]


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
        if "pmdg_failure_name" in item and isinstance(item["pmdg_failure_name"], dict):
            resolved["pmdg_failure_name"] = item["pmdg_failure_name"].get(choice)
    else:
        resolved["chosen_component"] = None
        if "pmdg_failure_name" in item and isinstance(item["pmdg_failure_name"], dict):
            resolved["pmdg_failure_name"] = item["pmdg_failure_name"].get("default")
    return resolved


def resolve_lmc(item):
    resolved = dict(item)
    lo, hi = item["delta_range"]
    resolved["delta"] = random.randint(lo, hi)
    return resolved


def pick_cost_index():
    """
    Ryanair is widely reported by pilots/dispatchers as running a very low,
    fuel-conservative cost index - commonly cited as CI 6 for the 737-800
    fleet (the airline has at times been described in pilot forums as
    "the world's largest 737-800 operator running CI 6"). Other anecdotal
    sources put general 737 operators anywhere from 10-40, so there's some
    disagreement out there - this generator leans on the low, Ryanair-
    specific figure the person asked for, with a modest spread around it
    to reflect that a real dispatcher/captain occasionally nudges it up
    for schedule recovery, not a literal fixed constant every flight.
    """
    return round(random.triangular(2, 16, 6))


def generate_leg_conditions(dangerous_goods, delay_codes, lmc_events):
    """Rolls the per-sector conditions for a single leg: pax load, cargo,
    dangerous goods, delay, LMC, cost index. Called once per leg in the
    itinerary."""
    load_factor = round(random.triangular(0.65, 0.98, 0.90), 2)
    pax_count = round(189 * load_factor)
    bags_per_pax = random.uniform(0.5, 0.9)
    cargo_weight_kg = round(pax_count * bags_per_pax * 15)

    return {
        "pax_count": pax_count,
        "load_factor": load_factor,
        "cargo_weight_kg": cargo_weight_kg,
        "cost_index": pick_cost_index(),
        "dangerous_goods": resolve_component(weighted_pick(dangerous_goods)),
        "delay": weighted_pick(delay_codes) if random.random() < DELAY_PROBABILITY else None,
        "lmc_event": resolve_lmc(weighted_pick(lmc_events)) if random.random() < LMC_PROBABILITY else None,
    }


def generate_conditions(itinerary, mels, delay_codes, lmc_events, dangerous_goods):
    """
    Rolls the randomized operational conditions for an already-chosen
    itinerary (a list of route legs).

    MEL is rolled ONCE for the whole itinerary - it's a property of the
    airframe, not the sector, so it follows the aircraft across every leg
    of the rotation. Pax load, cargo, dangerous goods, delay, and LMC are
    rolled separately PER LEG, since those are sector-specific.
    """
    legs = []
    for route_leg in itinerary:
        conditions = generate_leg_conditions(dangerous_goods, delay_codes, lmc_events)
        conditions["flight_number"] = route_leg["flight_number"]
        legs.append(conditions)

    session = {
        "session_id": str(uuid.uuid4()),
        "itinerary": itinerary,
        "legs": legs,
        "mel": resolve_component(weighted_pick(mels)) if random.random() < MEL_PROBABILITY else None,
        "fuel": None,
        "ofp_static_id": None,
    }
    return session
