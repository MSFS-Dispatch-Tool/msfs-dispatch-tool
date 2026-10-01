"""Aircraft technical status for a dispatched itinerary: deferred MEL items
and CDL deviations, what they do to the flight, and whether the flight can
be dispatched with them in today's weather.

An MEL item (data/aircraft/<family>/mel_list.json) carries machine-readable
"effects" next to its text:
  max_fl            flight level cap, sent to SimBrief as the cruise level
  extra_fuel_min    MEL fuel in minutes, sent to SimBrief as extra fuel
  ground_support    ["GPU", "ASU"] needed on every stand
  no_wx             weather it must not be dispatched into: TS (thunder-
                    storms), ICING, LVP (low visibility at destination),
                    CONTAM (contaminated destination runway), PRECIP
  performance       take-off/landing performance corrections apply
  cargo_hold_empty  one cargo hold must stay empty
  min_main_tank_fuel_kg  {"takeoff", "landing"}: fuel the affected main tank
                    must hold (737 boost pump items); both mains are loaded
                    alike, so the totals are twice that
  max_fuel_kg       usable fuel limit (centre tank kept empty)
A CDL item (cdl_list.json) adds a fuel-burn percentage and a take-off and
landing weight penalty.

The weather checks read the leg's own METAR and TAF, the same reports the
pilot sees on the Weather tab. A no_wx condition found there makes the leg
NO-GO: dispatch would have to swap the aircraft or the pilot pick another
flight, as a real operations controller would.

The content is representative of typical master MEL provisions, written for
simulation; it is not an approved MEL for real-world use.
"""

import math
import random
import re

from generator import parse_metar

CATEGORY_INTERVAL_DAYS = {"B": 3, "C": 10, "D": 120}

# How many MEL items an aircraft carries at dispatch: most fly with none, a
# few with two or three (airliners routinely do).
MEL_COUNT_WEIGHTS = ((0, 55), (1, 30), (2, 11), (3, 4))
CDL_PROBABILITY = 0.12

GROUND_SUPPORT_NAMES = {"GPU": "a ground power cart", "ASU": "an air starter"}
GROUND_SUPPORT_ORDER = ("GPU", "ASU")
# Plain wording: these lines are read by pilots of all levels, not dispatchers
CONDITION_TEXT = {
    "TS": "thunderstorms",
    "ICING": "icing (cloud, rain or snow near freezing)",
    "LVP": "fog or very low cloud",
    "CONTAM": "snow, ice or heavy rain on the runway",
    "PRECIP": "rain or snow",
}
# Which airports each condition is checked at: thunderstorms, icing and
# precipitation matter at both ends; CAT 1 minima and a contaminated runway
# only for the landing.
CONDITION_SCOPE = {"TS": ("dep", "arr"), "ICING": ("dep", "arr"), "PRECIP": ("dep", "arr"),
                   "LVP": ("arr",), "CONTAM": ("arr",)}

_TS_RE = re.compile(r"(?<![A-Z])(?:[+-]|VC)?TS(?:RA|SN|GR|GS|PL|DZ)*(?![A-Z])|\d{3}CB\b")
_PRECIP_RE = re.compile(r"(?<![A-Z])[+-]?(?:SH|TS|FZ)?(?:RA|DZ|SN|SG|PL|GR|GS)+(?![A-Z])")
_CONTAM_RE = re.compile(r"(?<![A-Z])(?:[+-]?(?:SH|BL|DR)?SN|[+-]?FZ(?:RA|DZ)|[+-]?(?:SH)?PL|[+-]?SG|\+(?:SH|TS)?RA)(?![A-Z])")
_FREEZING_RE = re.compile(r"(?<![A-Z])(?:[+-]?FZ(?:RA|DZ|FG)|[+-]?(?:SH|BL)?SN)(?![A-Z])")
_VIS_RE = re.compile(r"(?<=\s)(\d{4})(?=\s)")
_RVR_RE = re.compile(r"\bR\d{2}[LCR]?/[PM]?(\d{4})")
_LOW_CLOUD_RE = re.compile(r"\b(?:BKN|OVC|VV)(\d{3})")
_CLOUD_RE = re.compile(r"\b(?:BKN|OVC|VV)\d{3}")
_MOISTURE = {"RA", "DZ", "SN", "SG", "PL", "GS", "GR", "IC", "FG", "BR", "UP"}


def _strip_header(report):
    """Drops the station/time header so its digits aren't read as weather."""
    return f" {(report or '').upper()} "


def weather_conditions(metar, taf, scope):
    """Conditions present in one airport's METAR and TAF. `scope` is "dep"
    or "arr"; low visibility and runway contamination are only judged at
    the destination. Returns a set of condition keys."""
    found = set()
    texts = [t for t in (metar, taf) if t]
    if not texts:
        return found
    joined = " ".join(_strip_header(t) for t in texts)

    if _TS_RE.search(joined):
        found.add("TS")
    if _PRECIP_RE.search(joined):
        found.add("PRECIP")

    station = parse_metar(metar)
    temp = station["temp_c"]
    moisture = bool(station["phenomena"] & _MOISTURE) or bool(metar and _CLOUD_RE.search(metar.upper()))
    if (temp is not None and temp <= 10 and moisture) or _FREEZING_RE.search(joined):
        found.add("ICING")

    if scope == "arr":
        if _CONTAM_RE.search(joined):
            found.add("CONTAM")
        visibilities = [int(v) for v in _VIS_RE.findall(joined) if int(v) < 9999]
        rvrs = [int(v) for v in _RVR_RE.findall(joined)]
        ceilings = [int(c) for c in _LOW_CLOUD_RE.findall(joined)]
        if any(v < 550 for v in visibilities + rvrs) or any(c < 2 for c in ceilings):
            found.add("LVP")
    return found


def _pick_distinct(pool, count, rng):
    """Up to `count` weighted picks without repeats, at most one item per
    "group" (e.g. not both air conditioning packs on one aircraft)."""
    chosen, groups = [], set()
    candidates = [p for p in pool if p.get("weight", 0) > 0]
    while candidates and len(chosen) < count:
        pick = rng.choices(candidates, weights=[c["weight"] for c in candidates], k=1)[0]
        chosen.append(pick)
        groups.add(pick.get("group") or pick["id"])
        candidates = [c for c in candidates if c is not pick and (c.get("group") or c["id"]) not in groups]
    return chosen


def _resolve(item, rng):
    resolved = dict(item)
    options = item.get("component_options")
    side = rng.choice(options) if options else None
    resolved["chosen_component"] = side
    for key in ("description", "dispatch_consequence"):
        if key in resolved and side is not None:
            resolved[key] = resolved[key].replace("{side}", str(side))
        elif key in resolved:
            resolved[key] = resolved[key].replace("{side} ", "").replace("{side}", "")
    # Category A items carry their own interval (e.g. 1 flight-day)
    if "interval_days" not in item and item.get("mel_category") in CATEGORY_INTERVAL_DAYS:
        resolved["interval_days"] = CATEGORY_INTERVAL_DAYS[item["mel_category"]]
    return resolved


def _enabled(items, category_settings, key="id"):
    if category_settings is None:
        return items
    if not category_settings.get("enabled", True):
        return []
    disabled = set(category_settings.get("disabled_ids") or [])
    return [i for i in items if i[key] not in disabled]


def _custom(category_settings):
    value = (category_settings or {}).get("probability")
    return None if value is None else max(0.0, min(1.0, float(value) / 100))


def mel_default_percent():
    return round(100 * sum(w for n, w in MEL_COUNT_WEIGHTS if n) / sum(w for _, w in MEL_COUNT_WEIGHTS))


def roll_tech_status(mels, cdls, mel_settings=None, cdl_settings=None, exclude_ids=(), rng=random):
    """The aircraft's deferred items for one itinerary: {"mels": [...],
    "cdl": [...]}. exclude_ids keeps items out (used when dispatch swaps
    the aircraft because of one of them)."""
    mel_pool = [m for m in _enabled(mels, mel_settings) if m["id"] not in exclude_ids]
    cdl_pool = [c for c in _enabled(cdls, cdl_settings) if c["id"] not in exclude_ids]
    counts, weights = zip(*MEL_COUNT_WEIGHTS)
    mel_chance = _custom(mel_settings)
    if mel_chance is not None:
        # The pilot's chance of at least one item; how many follows the
        # realistic 1/2/3 split.
        busy = sum(weights[1:])
        weights = (1 - mel_chance,) + tuple(mel_chance * w / busy for w in weights[1:])
    n = rng.choices(counts, weights=weights, k=1)[0]
    chosen_mels = [_resolve(m, rng) for m in _pick_distinct(mel_pool, n, rng)]
    cdl_chance = _custom(cdl_settings)
    cdl_chance = CDL_PROBABILITY if cdl_chance is None else cdl_chance
    chosen_cdl = [_resolve(c, rng) for c in _pick_distinct(cdl_pool, 1, rng)] if rng.random() < cdl_chance else []
    return {"mels": chosen_mels, "cdl": chosen_cdl}


def _short(text, limit):
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def leg_tech(status, dep_icao, arr_icao, dep_wx, arr_wx, eet_minutes):
    """What the aircraft's items mean for one leg: SimBrief inputs, ground
    needs and the dispatch checks. dep_wx/arr_wx are {"metar", "taf"}."""
    mels, cdl = status.get("mels") or [], status.get("cdl") or []
    effects = [m.get("effects") or {} for m in mels]

    caps = [e["max_fl"] for e in effects if e.get("max_fl")]
    mel_fuel = sum(e.get("extra_fuel_min") or 0 for e in effects)
    cdl_pct = sum((c.get("effects") or {}).get("fuel_burn_pct") or 0 for c in cdl)
    cdl_fuel = math.ceil((eet_minutes or 0) * cdl_pct / 100) if cdl_pct else 0
    weight_penalty = sum((c.get("effects") or {}).get("weight_penalty_kg") or 0 for c in cdl)
    tank_minimums = [e["min_main_tank_fuel_kg"] for e in effects if e.get("min_main_tank_fuel_kg")]
    min_takeoff_fuel = 2 * max((t["takeoff"] for t in tank_minimums), default=0)
    min_landing_fuel = 2 * max((t["landing"] for t in tank_minimums), default=0)
    fuel_caps = [e["max_fuel_kg"] for e in effects if e.get("max_fuel_kg")]
    needed = {g for e in effects for g in (e.get("ground_support") or [])}
    ground = [g for g in GROUND_SUPPORT_ORDER if g in needed]

    wx = {"dep": dep_wx or {}, "arr": arr_wx or {}}
    present = {scope: weather_conditions(wx[scope].get("metar"), wx[scope].get("taf"), scope) for scope in ("dep", "arr")}
    missing = {scope for scope in ("dep", "arr") if not (wx[scope].get("metar") or wx[scope].get("taf"))}
    names = {"dep": dep_icao, "arr": arr_icao}

    checks = []
    for m, e in zip(mels, effects):
        label = m.get("name") or m["system"]
        for cond in e.get("no_wx") or []:
            scopes = CONDITION_SCOPE.get(cond, ("dep", "arr"))
            hit = [names[s] for s in scopes if cond in present[s]]
            unknown = [names[s] for s in scopes if s in missing]
            if hit:
                checks.append({"level": "nogo", "item": m["id"],
                               "text": f"{label} broken and {CONDITION_TEXT[cond]} at {' and '.join(hit)}: this aircraft can't fly the trip"})
            elif unknown:
                checks.append({"level": "caution", "item": m["id"],
                               "text": f"{label}: no weather report for {' and '.join(unknown)}, so check for {CONDITION_TEXT[cond]} before you go"})
            else:
                checks.append({"level": "ok", "item": m["id"],
                               "text": f"{label}: no {CONDITION_TEXT[cond]} expected at the {' or '.join('departure' if s == 'dep' else 'destination' for s in scopes)}, good to go"})
        if e.get("performance"):
            checks.append({"level": "caution", "item": m["id"], "text": f"{label}: take-off and landing need more runway than usual"})
        if e.get("cargo_hold_empty"):
            checks.append({"level": "caution", "item": m["id"], "text": f"{label}: keep the {(m.get('chosen_component') or 'affected').lower()} cargo hold empty and put the bags in the other one"})
    if min_takeoff_fuel:
        checks.append({"level": "caution", "item": None,
                       "text": f"Fuel pump out: take off with at least {min_takeoff_fuel:,} kg of fuel "
                               f"({min_takeoff_fuel // 2:,} kg in each wing) and land with at least {min_landing_fuel:,} kg"})
    if fuel_caps:
        checks.append({"level": "caution", "item": None,
                       "text": f"Centre fuel tank can't be used: you can carry about {min(fuel_caps):,} kg of fuel at most"})
    if ground:
        checks.append({"level": "caution", "item": None,
                       "text": f"You'll need {' and '.join(GROUND_SUPPORT_NAMES[g] for g in ground)} at {dep_icao} and {arr_icao}"})
    if weight_penalty:
        checks.append({"level": "caution", "item": None,
                       "text": f"Missing panel: the aircraft's maximum take-off and landing weights are {weight_penalty} kg lower"})

    remarks = [f"MEL {m['ata']} {m['system'].upper()}" + (f" {m['chosen_component']}".upper() if m.get("chosen_component") else "") for m in mels]
    if caps:
        remarks.append(f"MAX FL{min(caps)}")
    remarks += [f"CDL {c['ata']} {c['part'].upper()}" for c in cdl]

    return {
        "max_fl": min(caps) if caps else None,
        "extra_fuel_min": mel_fuel + cdl_fuel,
        "mel_fuel_min": mel_fuel,
        "cdl_fuel_min": cdl_fuel,
        "cdl_fuel_pct": round(cdl_pct, 2),
        "weight_penalty_kg": weight_penalty,
        "min_takeoff_fuel_kg": min_takeoff_fuel or None,
        "min_landing_fuel_kg": min_landing_fuel or None,
        "max_fuel_kg": min(fuel_caps) if fuel_caps else None,
        "ground_support": ground,
        "checks": checks,
        "nogo": any(c["level"] == "nogo" for c in checks),
        "remarks": _short(" / ".join(remarks), 180),
    }


def nogo_items(legs_tech):
    """Item ids that make any leg NO-GO."""
    return {c["item"] for t in legs_tech for c in t["checks"] if c["level"] == "nogo" and c["item"]}
