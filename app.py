"""
SimDispatch - Flask app.
"""

from flask import Flask, render_template, request, jsonify
import json
import os
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
    return jsonify(session)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
