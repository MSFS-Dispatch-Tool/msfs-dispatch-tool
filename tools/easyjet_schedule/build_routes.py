"""Convert a schedule CSV (fetch_airlabs.py output) into
data/carriers/<CARRIER>/routes.json, in the same schema as the Ryanair network.

    python tools/easyjet_schedule/build_routes.py               # easyJet
    python tools/easyjet_schedule/build_routes.py --carrier WZZ # Wizz Air

AirLabs has no aircraft type for ~99% of easyJet flights, so aircraft_type
is null on those routes and the app falls back to the first fleet entry in
data/carriers/EZY/carrier.json.
"""

import argparse
import csv
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCES = {
    "EZY": os.path.join(HERE, "easyjet_schedule.csv"),
    "WZZ": os.path.join(HERE, "..", "wizzair_schedule", "wizzair_schedule.csv"),
}


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
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--carrier", choices=sorted(SOURCES), default="EZY")
    args = ap.parse_args()
    src = SOURCES[args.carrier]
    dest = os.path.join(HERE, "..", "..", "data", "carriers", args.carrier, "routes.json")
    with open(src, newline="", encoding="utf-8") as f:
        routes = [to_route(r) for r in csv.DictReader(f)]
    numbers = [r["flight_number"] for r in routes]
    assert len(numbers) == len(set(numbers)), "duplicate flight numbers - the app keys routes by them"
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(routes, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {len(routes)} routes to {os.path.relpath(dest)}")


if __name__ == "__main__":
    main()
