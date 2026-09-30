"""Convert a fetch_airlabs.py schedule CSV into data/carriers/<CODE>/routes.json,
in the same schema as the Ryanair network.

    python tools/easyjet_schedule/build_routes.py            # easyJet
    python tools/easyjet_schedule/build_routes.py --carrier WZZ

AirLabs has no aircraft type for ~99% of flights, so aircraft_type is null
on those routes and the app falls back to the first fleet entry in the
carrier's carrier.json.
"""

import argparse
import csv
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
DATA = os.path.join(TOOLS, "..", "data", "carriers")
SOURCES = {
    "EZY": os.path.join(TOOLS, "easyjet_schedule", "easyjet_schedule.csv"),
    "WZZ": os.path.join(TOOLS, "wizzair_schedule", "wizzair_schedule.csv"),
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
        "distance_nm": int(row["distance_nm"]) if row["distance_nm"] else None,
        "days_operated": [int(d) for d in row["days_operated"]] or None,
        "scheduled_departure_local": row["dep_local"],
        "scheduled_arrival_local": row["arr_local"],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--carrier", default="EZY", choices=sorted(SOURCES))
    carrier = ap.parse_args().carrier
    dest = os.path.join(DATA, carrier, "routes.json")
    with open(SOURCES[carrier], newline="", encoding="utf-8") as f:
        routes = [to_route(r) for r in csv.DictReader(f)
                  if r["departure_icao"] and r["arrival_icao"]]
    numbers = [r["flight_number"] for r in routes]
    assert len(numbers) == len(set(numbers)), "duplicate flight numbers - the app keys routes by them"
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(routes, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {len(routes)} routes to {os.path.relpath(dest)}")


if __name__ == "__main__":
    main()
