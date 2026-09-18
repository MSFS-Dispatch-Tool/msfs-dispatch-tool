"""
Generator logic for the MSFS immersion tool (step 4).

Pure functions operating on the in-memory datasets loaded by app.py.
No Flask/HTTP concerns in here - that separation makes this testable from
a plain Python shell before it's ever wired to a route.
"""

import random
import uuid

# Tunable probabilities for whether an event fires at all on a given
# generated flight. These are starting guesses, not sourced from anything -
# tune by feel once you're actually using the tool. Dangerous goods doesn't
# need a separate gate: the dataset already has a heavily-weighted
# "no dangerous goods on this sector" entry that serves as the null case.
MEL_PROBABILITY = 0.35
DELAY_PROBABILITY = 0.45
LMC_PROBABILITY = 0.50


def pick_route(routes, aircraft_type, available_minutes):
    """
    Filters routes by aircraft type and whether the flight fits in the
    available time (duration only - doesn't account for turnaround,
    taxi, etc. at this stage). Returns None if nothing fits.
    """
    candidates = [
        r for r in routes
        if r["aircraft_type"] == aircraft_type
        and r["duration_minutes"] is not None
        and r["duration_minutes"] <= available_minutes
    ]
    if not candidates:
        return None
    return random.choice(candidates)


def weighted_pick(items):
    """Picks one item from a list using its 'weight' field."""
    weights = [item["weight"] for item in items]
    return random.choices(items, weights=weights, k=1)[0]


def resolve_component(item):
    """
    If an item has component_options, randomly picks one and substitutes
    it into {side} in description/dispatch_consequence. Returns a copy -
    never mutates the original dataset entry, since that's shared across
    every future generation.
    """
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
    """LMC events use delta_range instead of component_options - roll an
    actual delta within that range rather than substituting text."""
    resolved = dict(item)
    lo, hi = item["delta_range"]
    resolved["delta"] = random.randint(lo, hi)
    return resolved


def generate_flight(routes, mels, delay_codes, lmc_events, dangerous_goods,
                     aircraft_type="738", available_minutes=180):
    """
    Assembles one generated session object per the schema from step 1.
    Returns None (with a reason) if no route fits the time available.
    """
    route = pick_route(routes, aircraft_type, available_minutes)
    if route is None:
        return {"error": "No route fits the given aircraft type and time available."}

    # Passenger load: weighted toward realistic LCC load factors rather
    # than a flat range - see step 1 discussion on why flat random was
    # rejected.
    load_factor = round(random.triangular(0.65, 0.98, 0.90), 2)
    pax_count = round(189 * load_factor)  # 189 seats, standard Ryanair 738 config

    # Cargo: rough bag-per-pax model, not tied to any real dataset - a
    # reasonable placeholder until refined.
    bags_per_pax = random.uniform(0.5, 0.9)
    cargo_weight_kg = round(pax_count * bags_per_pax * 15)  # ~15kg avg checked bag

    session = {
        "session_id": str(uuid.uuid4()),
        "route": route,
        "pax_count": pax_count,
        "load_factor": load_factor,
        "cargo_weight_kg": cargo_weight_kg,
        "dangerous_goods": resolve_component(weighted_pick(dangerous_goods)),
        "mel": resolve_component(weighted_pick(mels)) if random.random() < MEL_PROBABILITY else None,
        "delay": weighted_pick(delay_codes) if random.random() < DELAY_PROBABILITY else None,
        "lmc_event": resolve_lmc(weighted_pick(lmc_events)) if random.random() < LMC_PROBABILITY else None,
        "fuel": None,  # filled in step 5 once SimBrief integration exists
        "ofp_static_id": None,  # filled in step 5
    }
    return session
