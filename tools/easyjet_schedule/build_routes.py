"""Convert easyjet_schedule.csv (fetch_airlabs.py output) into
data/carriers/EZY/routes.json, in the same schema as the Ryanair network.

    python tools/easyjet_schedule/build_routes.py

AirLabs has no aircraft type for ~99% of easyJet flights, so aircraft_type
is null on those routes and the app falls back to the first fleet entry in
data/carriers/EZY/carrier.json.
"""

import csv
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "easyjet_schedule.csv")
DEST = os.path.join(HERE, "..", "..", "data", "carriers", "EZY", "routes.json")


def to_route(row):
    return {
        "flight_number": row["flight_number"],
        "callsign_prefix": row["operator_icao"] or None,
        "departure_iata": row["departure_iata"],
        "departure_icao": row["departure_icao"],
        "arrival_iata": row["arrival_iata"],
        "arrival_icao": row["arrival_icao"],
        "aircraft_type": row["aircraft_type"] or None,
        "duration_minutes": int(row["duration_minutes"]),
        "distance_nm": int(row["distance_nm"]),
        "days_operated": [int(d) for d in row["days_operated"]] or None,
        "scheduled_departure_local": row["dep_local"],
        "scheduled_arrival_local": row["arr_local"],
    }


def main():
    with open(SRC, newline="", encoding="utf-8") as f:
        routes = [to_route(r) for r in csv.DictReader(f)]
    numbers = [r["flight_number"] for r in routes]
    assert len(numbers) == len(set(numbers)), "duplicate flight numbers - the app keys routes by them"
    with open(DEST, "w", encoding="utf-8") as f:
        json.dump(routes, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {len(routes)} routes to {os.path.relpath(DEST)}")


if __name__ == "__main__":
    main()
