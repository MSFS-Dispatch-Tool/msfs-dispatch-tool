"""
SimDispatch - Flask app.
"""

from flask import Flask, render_template, request, jsonify
import json
import os
import requests
from collections import Counter
from datetime import date
from urllib.parse import urlencode
from generator import (
    resolve_airport, find_round_trip_pairs, find_itineraries,
    generate_leg_conditions, roll_delay, generate_callsign, generate_loadsheet_extras
)
from timeutils import resolve_leg_times, resolve_leg_schedule, format_zulu, turnaround_minutes, simbrief_date_str

app = Flask(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def load_json(filename):
    path = os.path.join(DATA_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


routes = load_json("routes_enriched.json")
mels = load_json("mel_list.json")
delay_codes = load_json("delay_codes.json")
lmc_events = load_json("lmc_events.json")
dangerous_goods = load_json("dangerous_goods.json")
airports = load_json("airports.json")

routes_by_flight_number = {r["flight_number"]: r for r in routes}
country_by_icao = {a["icao"]: a["country"] for a in airports}
tz_by_icao = {a["icao"]: a["tz"] for a in airports}
airports_by_icao = {a["icao"]: a for a in airports}

rt_pairing = find_round_trip_pairs(routes)

# There's no real runway/stand-count data in this dataset, so "large
# airport" (for taxi-time purposes - see timeutils.resolve_leg_schedule)
# is approximated from how many scheduled routes in routes_enriched.json
# touch that airport. Airports at/above the threshold get a 15-minute
# taxi allowance each way; everything else gets 10. This is a heuristic,
# not real airport data - treat STOT/SLDT precision accordingly.
LARGE_AIRPORT_ROUTE_THRESHOLD = 15
_route_touch_counts = Counter()
for _r in routes:
    _route_touch_counts[_r["departure_icao"]] += 1
    _route_touch_counts[_r["arrival_icao"]] += 1
large_airport_lookup = {icao: count >= LARGE_AIRPORT_ROUTE_THRESHOLD for icao, count in _route_touch_counts.items()}

WEATHER_USER_AGENT = "SimDispatch/1.0 (personal MSFS immersion tool; not for real-world ops use)"

# SimBrief dispatch-redirect / OFP fetch-back integration. Both are
# SimBrief's public, no-API-key mechanisms - not the gated "API v1" popup
# flow (that needs an emailed-and-approved key and is out of scope here).
SIMBRIEF_AIRLINE_IATA = "FR"  # Ryanair - hardcoded, this tool is RYR-only
SIMBRIEF_AIRCRAFT_TYPE = {"738": "B738"}


def fetch_weather_batch(icao_list):
    unique = sorted(set(icao_list))
    ids_param = ",".join(unique)
    result = {icao: {"metar": None, "taf": None} for icao in unique}
    try:
        resp = requests.get("https://aviationweather.gov/api/data/metar",
                             params={"ids": ids_param, "format": "json"},
                             headers={"User-Agent": WEATHER_USER_AGENT}, timeout=8)
        resp.raise_for_status()
        for item in resp.json():
            icao = item.get("icaoId")
            if icao in result:
                result[icao]["metar"] = item.get("rawOb")
    except Exception:
        pass
    try:
        resp = requests.get("https://aviationweather.gov/api/data/taf",
                             params={"ids": ids_param, "format": "json"},
                             headers={"User-Agent": WEATHER_USER_AGENT}, timeout=8)
        resp.raise_for_status()
        for item in resp.json():
            icao = item.get("icaoId")
            if icao in result:
                result[icao]["taf"] = item.get("rawTAF")
    except Exception:
        pass
    return result


def airport_info(icao):
    a = airports_by_icao.get(icao, {})
    return {
        "icao": icao,
        "iata": a.get("iata", ""),
        "name": a.get("name", "UNKNOWN"),
        "country": a.get("country", ""),
        "lat": a.get("lat"),
        "lon": a.get("lon"),
    }


def leg_summary_with_times(route, today):
    """Shared by /search (list view) and /select (detail view): airport
    info, cost-agnostic route facts, and Zulu dep/arr times."""
    dep_dt, arr_dt, arr_source = resolve_leg_times(route, tz_by_icao, today)
    return {
        "flight_number": route["flight_number"],
        "departure_info": airport_info(route["departure_icao"]),
        "arrival_info": airport_info(route["arrival_icao"]),
        "duration_minutes": route["duration_minutes"],
        "distance_nm": route["distance_nm"],
        "scheduled_departure_zulu": format_zulu(dep_dt, today),
        "scheduled_arrival_zulu": format_zulu(arr_dt, today),
        "arrival_time_source": arr_source,
        "_dep_dt": dep_dt, "_arr_dt": arr_dt,  # internal, stripped before jsonify
    }


def strip_internal(d):
    return {k: v for k, v in d.items() if not k.startswith("_")}


@app.route("/")
def index():
    counts = {
        "routes": len(routes), "mels": len(mels), "delay_codes": len(delay_codes),
        "lmc_events": len(lmc_events), "dangerous_goods": len(dangerous_goods),
    }
    return render_template("index.html", counts=counts)


@app.route("/airports/search")
def airports_search():
    q = request.args.get("q", "").strip().upper()
    if not q:
        return jsonify([])
    matches = [a for a in airports if a["icao"].startswith(q) or a["iata"].startswith(q) or a["city"].upper().startswith(q)]
    return jsonify(matches[:15])


@app.route("/airports/validate")
def airports_validate():
    code = request.args.get("code", "")
    icao = resolve_airport(airports, code)
    return jsonify({"valid": icao is not None, "icao": icao})


@app.route("/search")
def search():
    available_minutes = request.args.get("minutes", default=180, type=int)
    aircraft_type = request.args.get("aircraft", default="738", type=str)
    origin_raw = request.args.get("origin", default="", type=str)
    destination_raw = request.args.get("destination", default="", type=str)
    trip_type = request.args.get("trip_type", default="random", type=str)
    include_repositioning = request.args.get("repositioning", default="false", type=str) == "true"

    origin_icao = resolve_airport(airports, origin_raw) if origin_raw else None
    if origin_raw and origin_icao is None:
        return jsonify({"error": f"Unknown airport code: {origin_raw}"}), 400
    destination_icao = resolve_airport(airports, destination_raw) if destination_raw else None
    if destination_raw and destination_icao is None:
        return jsonify({"error": f"Unknown airport code: {destination_raw}"}), 400

    itineraries = find_itineraries(
        routes, rt_pairing, airports, aircraft_type, available_minutes,
        origin_icao=origin_icao, destination_icao=destination_icao,
        trip_type=trip_type, include_repositioning=include_repositioning
    )

    today = date.today()
    summaries = []
    for itin in itineraries:
        legs = itin["legs"]
        leg_data = [leg_summary_with_times(leg, today) for leg in legs]

        # turnaround between legs (only meaningful for RT, 2 legs)
        for i in range(1, len(leg_data)):
            leg_data[i]["turnaround_minutes"] = turnaround_minutes(leg_data[i - 1]["_arr_dt"], leg_data[i]["_dep_dt"])

        dep_country = country_by_icao.get(legs[0]["departure_icao"], "")
        # "away" airport for RT = leg[0] arrival; for 1W/RE = leg[-1] arrival
        away_country = country_by_icao.get(legs[0]["arrival_icao"], "")
        # For a round trip the rotation's final airport is always back at
        # origin, so "VIA" in the UI shows the away/turnaround airport
        # instead (the outbound leg's arrival). One-way and repositioning
        # legs fly direct, so there's no via airport at all.
        via_icao = legs[0]["arrival_icao"] if itin["trip_type"] == "RT" else None

        summaries.append({
            "trip_type": itin["trip_type"],
            "flight_numbers": [l["flight_number"] for l in legs],
            "path": [legs[0]["departure_icao"]] + [l["arrival_icao"] for l in legs],
            "total_minutes": sum(l["duration_minutes"] for l in legs),
            "legs": len(legs),
            "departure_icao": legs[0]["departure_icao"],
            "arrival_icao": legs[-1]["arrival_icao"],
            "via_icao": via_icao,
            "departure_country": dep_country,
            "arrival_country": away_country,
            "domestic": dep_country != "" and dep_country == away_country,
            "leg_details": [strip_internal(l) for l in leg_data],
            "first_departure_zulu": leg_data[0]["scheduled_departure_zulu"],
            "last_arrival_zulu": leg_data[-1]["scheduled_arrival_zulu"],
        })

    return jsonify({"count": len(summaries), "itineraries": summaries})


@app.route("/select")
def select():
    flights_raw = request.args.get("flights", default="", type=str)
    flight_numbers = [f.strip() for f in flights_raw.split(",") if f.strip()]

    itinerary = []
    for fn in flight_numbers:
        leg = routes_by_flight_number.get(fn)
        if leg is None:
            # repositioning legs aren't in routes_by_flight_number - the
            # frontend re-sends the full synthesized leg object for those
            # instead of just a flight number (see JS side).
            return jsonify({"error": f"Unknown flight number: {fn}"}), 400
        itinerary.append(leg)

    if not itinerary:
        return jsonify({"error": "No flights specified."}), 400

    today = date.today()
    icao_needed = set()
    for leg in itinerary:
        icao_needed.add(leg["departure_icao"])
        icao_needed.add(leg["arrival_icao"])
    weather = fetch_weather_batch(list(icao_needed))

    legs_out = []
    leg_times = []
    for route_leg in itinerary:
        conditions = generate_leg_conditions(is_repositioning=False, duration_minutes=route_leg["duration_minutes"])
        conditions["delay"] = roll_delay(delay_codes)
        conditions["flight_number"] = route_leg["flight_number"]
        conditions["departure_info"] = airport_info(route_leg["departure_icao"])
        conditions["arrival_info"] = airport_info(route_leg["arrival_icao"])
        conditions["weather"] = {
            "departure": weather.get(route_leg["departure_icao"], {"metar": None, "taf": None}),
            "arrival": weather.get(route_leg["arrival_icao"], {"metar": None, "taf": None}),
        }
        schedule = resolve_leg_schedule(route_leg, tz_by_icao, large_airport_lookup, today)
        conditions["sobt"] = schedule["sobt"]
        conditions["stot"] = schedule["stot"]
        conditions["sldt"] = schedule["sldt"]
        conditions["sibt"] = schedule["sibt"]
        conditions["eet_minutes"] = schedule["eet_minutes"]
        leg_times.append((schedule["_sobt_dt"], schedule["_sibt_dt"]))
        legs_out.append(conditions)

    for i in range(1, len(legs_out)):
        legs_out[i]["turnaround_minutes"] = turnaround_minutes(leg_times[i - 1][1], leg_times[i][0])

    return jsonify({"itinerary": itinerary, "legs": legs_out})


@app.route("/confirm")
def confirm():
    """
    Called when the person presses CONFIRM after reviewing a flight.
    Generates a callsign PER LEG (only happens here, never earlier -
    each flight number/sector gets its own, since real callsigns are
    per-flight, not per-rotation) and rolls dangerous goods + LMC
    together, as the loadsheet-signing moment.
    """
    flights_raw = request.args.get("flights", default="", type=str)
    flight_numbers = [f.strip() for f in flights_raw.split(",") if f.strip()]
    itinerary = [routes_by_flight_number[fn] for fn in flight_numbers if fn in routes_by_flight_number]
    if not itinerary:
        return jsonify({"error": "No flights specified."}), 400

    callsigns = [generate_callsign() for _ in flight_numbers]
    dg, lmc = generate_loadsheet_extras(dangerous_goods, lmc_events)

    return jsonify({
        "confirmation_id": callsigns[0],  # unique enough for this tool's purposes
        "callsigns": callsigns,           # one per flight_numbers[i], same order
        "flight_numbers": flight_numbers,
        "dangerous_goods": dg,
        "lmc_event": lmc,
        "leg_status": [{"flight_number": fn, "status": "pending"} for fn in flight_numbers],
    })


@app.route("/simbrief/redirect-url")
def simbrief_redirect_url():
    """
    Builds a SimBrief "dispatch redirect" URL - a plain link to SimBrief's
    own dispatch form, pre-filled via query string. Opening it just shows
    the pilot a pre-filled form on simbrief.com; they still press Generate
    there themselves. No API key involved.

    Airframe (acdata/reg) is deliberately not passed - that's configured
    once as the pilot's own SimBrief profile default. deph/depm/cargo are
    also left out: their expected units aren't confirmed from public docs,
    and a wrong guess would silently mis-fill the form.
    """
    flights_raw = request.args.get("flights", default="", type=str)
    flight_numbers = [f.strip() for f in flights_raw.split(",") if f.strip()]
    if not flight_numbers:
        return jsonify({"error": "No flights specified."}), 400

    fn = flight_numbers[0]
    route_leg = routes_by_flight_number.get(fn)
    if route_leg is None:
        return jsonify({"error": f"Unknown flight number: {fn}"}), 400

    civalue = request.args.get("civalue", type=int)
    pax = request.args.get("pax", type=int)
    if civalue is None or pax is None:
        # Only hit when the caller doesn't already have confirmed leg
        # values (e.g. a preview, before CONFIRM has committed a
        # cost_index/pax_count). An already-confirmed active leg's
        # frontend call always supplies both, so this never re-rolls
        # numbers the pilot has already seen and confirmed.
        conditions = generate_leg_conditions(is_repositioning=False, duration_minutes=route_leg["duration_minutes"])
        if civalue is None:
            civalue = conditions["cost_index"]
        if pax is None:
            pax = conditions["pax_count"]

    params = {
        "orig": route_leg["departure_icao"],
        "dest": route_leg["arrival_icao"],
        "type": SIMBRIEF_AIRCRAFT_TYPE.get(route_leg.get("aircraft_type", "738"), "B738"),
        "airline": SIMBRIEF_AIRLINE_IATA,
        "fltnum": "".join(ch for ch in fn if ch.isdigit()),
        "date": simbrief_date_str(),
        "civalue": civalue,
        "pax": pax,
    }
    static_id = request.args.get("static_id", default="", type=str).strip()
    if static_id:
        params["static_id"] = static_id

    return jsonify({"url": "https://www.simbrief.com/system/dispatch.php?" + urlencode(params)})


@app.route("/simbrief/ofp")
def simbrief_ofp():
    """
    Reads back the pilot's most recently generated OFP from SimBrief's
    public fetch-back endpoint (json=v2, no API key). This always returns
    whichever OFP is most recent for that SimBrief account - matching
    static_id (echoed back inside the OFP's params block) against the one
    this app sent to dispatch.php is how the frontend confirms it got the
    OFP it just asked for, not a stale one from an earlier session.
    """
    username = request.args.get("username", default="", type=str).strip()
    if not username:
        return jsonify({"error": "SimBrief username is required."}), 400

    try:
        resp = requests.get(
            "https://www.simbrief.com/api/xml.fetcher.php",
            params={"username": username, "json": "v2"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return jsonify({"error": "Could not reach SimBrief, or no OFP is on file for this username."}), 502

    if not isinstance(data, dict):
        return jsonify({"error": "Unexpected response from SimBrief."}), 502
    if str(data.get("fetch", {}).get("status", "")).lower().startswith("error"):
        return jsonify({"error": "SimBrief reported an error for this username - generate an OFP first."}), 502

    origin = data.get("origin", {})
    destination = data.get("destination", {})
    general = data.get("general", {})
    aircraft = data.get("aircraft", {})
    params_block = data.get("params", {})
    fuel = data.get("fuel", {})
    times = data.get("times", {})

    return jsonify({
        "static_id": params_block.get("static_id", ""),
        "origin_icao": origin.get("icao_code", ""),
        "destination_icao": destination.get("icao_code", ""),
        "callsign": f"{general.get('icao_airline', '')}{general.get('flight_number', '')}",
        "route": general.get("route", ""),
        "cost_index": general.get("costindex", ""),
        "initial_altitude_ft": general.get("initial_altitude", ""),
        "registration": aircraft.get("reg", ""),
        "icao_type": aircraft.get("icaocode", ""),
        "block_fuel_kg": fuel.get("plan_ramp", ""),
        "est_time_enroute_sec": times.get("est_time_enroute", ""),
    })


if __name__ == "__main__":
    app.run(debug=True, port=5000)
