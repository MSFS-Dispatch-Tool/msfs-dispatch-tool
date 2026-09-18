"""
SimDispatch - Flask app.
"""

from flask import Flask, render_template, request, jsonify
import json
import os
import requests
from generator import (
    resolve_airport, find_itineraries, generate_conditions
)

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

# Fast lookup for the /select step, keyed by flight number.
routes_by_flight_number = {r["flight_number"]: r for r in routes}

# Fast country lookup by ICAO code.
country_by_icao = {a["icao"]: a["country"] for a in airports}
airports_by_icao = {a["icao"]: a for a in airports}

# aviationweather.gov is the official NOAA/NWS public API - no key needed,
# but it does require a custom User-Agent and has no CORS support, so this
# MUST be called server-side (a browser calling it directly would be
# blocked). Failures degrade gracefully - the frontend just shows
# "unavailable" rather than the whole request failing.
WEATHER_USER_AGENT = "SimDispatch/1.0 (personal MSFS immersion tool; not for real-world ops use)"


def fetch_weather_batch(icao_list):
    unique = sorted(set(icao_list))
    ids_param = ",".join(unique)
    result = {icao: {"metar": None, "taf": None} for icao in unique}

    try:
        resp = requests.get(
            "https://aviationweather.gov/api/data/metar",
            params={"ids": ids_param, "format": "json"},
            headers={"User-Agent": WEATHER_USER_AGENT},
            timeout=8,
        )
        resp.raise_for_status()
        for item in resp.json():
            icao = item.get("icaoId")
            if icao in result:
                result[icao]["metar"] = item.get("rawOb")
    except Exception:
        pass  # leave as None - frontend shows "unavailable"

    try:
        resp = requests.get(
            "https://aviationweather.gov/api/data/taf",
            params={"ids": ids_param, "format": "json"},
            headers={"User-Agent": WEATHER_USER_AGENT},
            timeout=8,
        )
        resp.raise_for_status()
        for item in resp.json():
            icao = item.get("icaoId")
            if icao in result:
                result[icao]["taf"] = item.get("rawTAF")
    except Exception:
        pass

    return result


@app.route("/")
def index():
    counts = {
        "routes": len(routes),
        "mels": len(mels),
        "delay_codes": len(delay_codes),
        "lmc_events": len(lmc_events),
        "dangerous_goods": len(dangerous_goods),
    }
    return render_template("index.html", counts=counts)


@app.route("/airports/search")
def airports_search():
    """Autocomplete source: matches ICAO/IATA/city prefix, case-insensitive."""
    q = request.args.get("q", "").strip().upper()
    if not q:
        return jsonify([])
    matches = [
        a for a in airports
        if a["icao"].startswith(q) or a["iata"].startswith(q) or a["city"].upper().startswith(q)
    ]
    return jsonify(matches[:15])


@app.route("/airports/validate")
def airports_validate():
    """Used on blur to confirm a typed code actually exists."""
    code = request.args.get("code", "")
    icao = resolve_airport(airports, code)
    return jsonify({"valid": icao is not None, "icao": icao})


@app.route("/search")
def search():
    available_minutes = request.args.get("minutes", default=180, type=int)
    aircraft_type = request.args.get("aircraft", default="738", type=str)
    origin_raw = request.args.get("origin", default="", type=str)
    destination_raw = request.args.get("destination", default="", type=str)
    legs_raw = request.args.get("legs", default="", type=str)

    origin_icao = resolve_airport(airports, origin_raw) if origin_raw else None
    if origin_raw and origin_icao is None:
        return jsonify({"error": f"Unknown airport code: {origin_raw}"}), 400

    destination_icao = resolve_airport(airports, destination_raw) if destination_raw else None
    if destination_raw and destination_icao is None:
        return jsonify({"error": f"Unknown airport code: {destination_raw}"}), 400

    num_legs = int(legs_raw) if legs_raw.isdigit() else None

    itineraries = find_itineraries(
        routes, aircraft_type, available_minutes,
        origin_icao=origin_icao, destination_icao=destination_icao, num_legs=num_legs
    )

    # Return lightweight summaries for the list view - full route objects
    # only get sent once something is actually selected.
    summaries = []
    for itin in itineraries:
        total_minutes = sum(leg["duration_minutes"] for leg in itin)
        dep_country = country_by_icao.get(itin[0]["departure_icao"], "")
        arr_country = country_by_icao.get(itin[-1]["arrival_icao"], "")
        summaries.append({
            "flight_numbers": [leg["flight_number"] for leg in itin],
            "path": [itin[0]["departure_icao"]] + [leg["arrival_icao"] for leg in itin],
            "total_minutes": total_minutes,
            "legs": len(itin),
            "departure_icao": itin[0]["departure_icao"],
            "arrival_icao": itin[-1]["arrival_icao"],
            "departure_country": dep_country,
            "arrival_country": arr_country,
            "domestic": dep_country != "" and dep_country == arr_country,
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
            return jsonify({"error": f"Unknown flight number: {fn}"}), 400
        itinerary.append(leg)

    if not itinerary:
        return jsonify({"error": "No flights specified."}), 400

    session = generate_conditions(itinerary, mels, delay_codes, lmc_events, dangerous_goods)

    # Enrich each leg with airport detail and weather - one batched METAR
    # call and one batched TAF call for the whole itinerary, not one per
    # airport, to keep this reasonable on the free NOAA API's rate limits.
    icao_needed = set()
    for leg in itinerary:
        icao_needed.add(leg["departure_icao"])
        icao_needed.add(leg["arrival_icao"])
    weather = fetch_weather_batch(list(icao_needed))

    def airport_info(icao):
        a = airports_by_icao.get(icao, {})
        return {
            "icao": icao,
            "iata": a.get("iata", ""),
            "name": a.get("name", "UNKNOWN"),
            "country": a.get("country", ""),
        }

    for route_leg, conditions in zip(itinerary, session["legs"]):
        conditions["departure_info"] = airport_info(route_leg["departure_icao"])
        conditions["arrival_info"] = airport_info(route_leg["arrival_icao"])
        conditions["weather"] = {
            "departure": weather.get(route_leg["departure_icao"], {"metar": None, "taf": None}),
            "arrival": weather.get(route_leg["arrival_icao"], {"metar": None, "taf": None}),
        }

    return jsonify(session)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
