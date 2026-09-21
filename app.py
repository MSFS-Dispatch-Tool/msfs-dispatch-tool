"""
SimDispatch - Flask app.
"""

from flask import Flask, render_template, request, jsonify
import json
import os
import requests
from datetime import date
from generator import (
    resolve_airport, find_round_trip_pairs, find_itineraries,
    generate_leg_conditions, roll_delay, generate_callsign, generate_loadsheet_extras
)
from timeutils import resolve_leg_times, format_zulu, turnaround_minutes

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

WEATHER_USER_AGENT = "SimDispatch/1.0 (personal MSFS immersion tool; not for real-world ops use)"


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

        summaries.append({
            "trip_type": itin["trip_type"],
            "flight_numbers": [l["flight_number"] for l in legs],
            "path": [legs[0]["departure_icao"]] + [l["arrival_icao"] for l in legs],
            "total_minutes": sum(l["duration_minutes"] for l in legs),
            "legs": len(legs),
            "departure_icao": legs[0]["departure_icao"],
            "arrival_icao": legs[-1]["arrival_icao"],
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
        conditions = generate_leg_conditions(is_repositioning=False)
        conditions["delay"] = roll_delay(delay_codes)
        conditions["flight_number"] = route_leg["flight_number"]
        conditions["departure_info"] = airport_info(route_leg["departure_icao"])
        conditions["arrival_info"] = airport_info(route_leg["arrival_icao"])
        conditions["weather"] = {
            "departure": weather.get(route_leg["departure_icao"], {"metar": None, "taf": None}),
            "arrival": weather.get(route_leg["arrival_icao"], {"metar": None, "taf": None}),
        }
        dep_dt, arr_dt, arr_source = resolve_leg_times(route_leg, tz_by_icao, today)
        conditions["scheduled_departure_zulu"] = format_zulu(dep_dt, today)
        conditions["scheduled_arrival_zulu"] = format_zulu(arr_dt, today)
        conditions["arrival_time_source"] = arr_source
        leg_times.append((dep_dt, arr_dt))
        legs_out.append(conditions)

    for i in range(1, len(legs_out)):
        legs_out[i]["turnaround_minutes"] = turnaround_minutes(leg_times[i - 1][1], leg_times[i][0])

    return jsonify({"itinerary": itinerary, "legs": legs_out})


@app.route("/confirm")
def confirm():
    """
    Called when the person presses CONFIRM after reviewing a flight.
    Generates the callsign (only happens here, never earlier) and rolls
    dangerous goods + LMC together, as the loadsheet-signing moment.
    """
    flights_raw = request.args.get("flights", default="", type=str)
    flight_numbers = [f.strip() for f in flights_raw.split(",") if f.strip()]
    itinerary = [routes_by_flight_number[fn] for fn in flight_numbers if fn in routes_by_flight_number]
    if not itinerary:
        return jsonify({"error": "No flights specified."}), 400

    callsign = generate_callsign()
    dg, lmc = generate_loadsheet_extras(dangerous_goods, lmc_events)

    return jsonify({
        "confirmation_id": callsign,  # unique enough for this tool's purposes
        "callsign": callsign,
        "flight_numbers": flight_numbers,
        "dangerous_goods": dg,
        "lmc_event": lmc,
        "leg_status": [{"flight_number": fn, "status": "pending"} for fn in flight_numbers],
    })


if __name__ == "__main__":
    app.run(debug=True, port=5000)
