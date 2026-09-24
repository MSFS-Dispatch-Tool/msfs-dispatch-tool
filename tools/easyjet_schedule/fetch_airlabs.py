"""Build the easyJet schedule from the AirLabs Routes API.

One AirLabs "route" row is one flight number with its local/UTC times,
block time, operating weekdays and aircraft type - exactly the fields the
app's routes.json needs. The script pulls every easyJet row (U2 / EC / DS),
matches them against easyjet_routes.csv and writes:

    easyjet_schedule.csv   one row per flight number on a known route
    easyjet_missing.csv    routes from the CSV with no AirLabs flight
    easyjet_extra.csv      AirLabs flights on routes not in the CSV
    routes.json            same schema as data/carriers/RYR/routes.json

Google Colab
------------
    # Key: left sidebar -> key icon (Secrets) -> add AIRLABS_API_KEY,
    # toggle notebook access on. Never paste it into a cell.
    !pip install -q requests
    # upload this file, easyjet_routes.csv and (optional, for distance)
    # data/airports_world.json from the repo

    !python fetch_airlabs.py --probe          # 1 API call: shows fields + plan limits
    !python fetch_airlabs.py --airports airports_world.json

Every API page is cached in --cache-dir, so a rerun (e.g. after tweaking
the matching) costs no calls. --max-calls guards the free 1,000/month quota.
"""

import argparse
import csv
import json
import math
import os
import sys
import time
from collections import Counter, defaultdict

import requests

API_URL = "https://airlabs.co/api/v9/routes"
# easyJet UK, easyJet Europe, easyJet Switzerland. Most flights are sold as
# U2 whoever operates them; the other two are queried so nothing slips by.
EZY_AIRLINES = ["U2", "EC", "DS"]
PAGE_SIZE = 500

# AirLabs gives the ICAO type designator; the app keys fleets by the short
# IATA-style code (Ryanair uses "738").
AIRCRAFT_TYPE = {"A319": "319", "A320": "320", "A20N": "32N", "A321": "321", "A21N": "32Q"}
WEEKDAY = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 7}


def get_key(cli_key):
    if cli_key:
        return cli_key
    if os.environ.get("AIRLABS_API_KEY"):
        return os.environ["AIRLABS_API_KEY"]
    try:
        from google.colab import userdata  # only exists inside Colab
        return userdata.get("AIRLABS_API_KEY")
    except Exception:
        sys.exit("No API key: set the AIRLABS_API_KEY Colab secret / env var, or pass --key")


class Budget:
    def __init__(self, max_calls):
        self.left = max_calls

    def spend(self):
        if self.left <= 0:
            raise RuntimeError("--max-calls reached; rerun with a higher limit (cached pages are free)")
        self.left -= 1


def call(params, key, cache_dir, budget):
    """One API page, cached on disk by its query parameters."""
    name = "_".join(f"{k}-{v}" for k, v in sorted(params.items())) + ".json"
    path = os.path.join(cache_dir, name)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    budget.spend()
    for attempt in range(4):
        r = requests.get(API_URL, params={**params, "api_key": key}, timeout=60)
        if r.status_code == 429:
            time.sleep(30 * (attempt + 1))
            continue
        break
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"AirLabs error for {params}: {data['error']}")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return data


def fetch_airline(airline, key, cache_dir, budget):
    """All rows for one airline. Pages until an empty page rather than a
    short one, since the free plan may cap rows per call below PAGE_SIZE."""
    rows, offset = [], 0
    while True:
        data = call({"airline_iata": airline, "limit": PAGE_SIZE, "offset": offset}, key, cache_dir, budget)
        page = data.get("response") or []
        print(f"  {airline} offset {offset}: {len(page)} rows")
        if not page:
            return rows
        rows.extend(page)
        offset += len(page)


def probe(key, cache_dir, budget):
    data = call({"airline_iata": "U2", "limit": 5, "offset": 0}, key, cache_dir, budget)
    print("request metadata (plan limits etc.):")
    print(json.dumps(data.get("request", {}), indent=2)[:3000])
    print("\nfirst rows:")
    print(json.dumps(data.get("response", [])[:5], indent=2))


def distance_nm(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return round(2 * 3440.065 * math.asin(math.sqrt(h)))


def to_flight(row):
    days = row.get("days") or []
    icao_type = (row.get("aircraft_icao") or "").upper() or None
    return {
        "flight_number": row["flight_iata"].upper(),
        "departure_iata": row["dep_iata"],
        "arrival_iata": row["arr_iata"],
        "aircraft_icao": icao_type,
        "aircraft_type": AIRCRAFT_TYPE.get(icao_type, icao_type),
        "dep_local": row.get("dep_time"),
        "arr_local": row.get("arr_time"),
        "dep_utc": row.get("dep_time_utc"),
        "arr_utc": row.get("arr_time_utc"),
        "duration_minutes": row.get("duration"),
        "days_operated": sorted(WEEKDAY[d] for d in days if d in WEEKDAY) or None,
        "updated": row.get("updated"),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", help="AirLabs API key (prefer the AIRLABS_API_KEY secret)")
    ap.add_argument("--routes", default="easyjet_routes.csv")
    ap.add_argument("--airports", help="data/airports_world.json from the repo (for distance_nm)")
    ap.add_argument("--cache-dir", default="airlabs_cache")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--max-calls", type=int, default=200)
    ap.add_argument("--probe", action="store_true", help="one call, print fields and plan limits")
    args = ap.parse_args()

    os.makedirs(args.cache_dir, exist_ok=True)
    os.makedirs(args.out_dir, exist_ok=True)
    key = get_key(args.key)
    budget = Budget(args.max_calls)

    if args.probe:
        probe(key, args.cache_dir, budget)
        return

    raw = []
    for airline in EZY_AIRLINES:
        raw.extend(fetch_airline(airline, key, args.cache_dir, budget))
    print(f"{len(raw)} raw rows, {args.max_calls - budget.left} API calls used this run")

    # Codeshare rows describe another carrier's flight sold under an easyJet
    # number (or vice versa) - keep only flights easyJet operates itself.
    flights, seen = [], set()
    for row in raw:
        if row.get("cs_flight_iata") or not row.get("flight_iata") or not row.get("dep_iata"):
            continue
        f = to_flight(row)
        k = (f["flight_number"], f["departure_iata"], f["arrival_iata"])
        if k in seen:
            continue
        seen.add(k)
        flights.append(f)

    with open(args.routes, newline="", encoding="utf-8") as fh:
        routes = list(csv.DictReader(fh))
    route_by_pair = {(r["departure_iata"], r["arrival_iata"]): r for r in routes}
    airports = {}
    if args.airports:
        with open(args.airports, encoding="utf-8") as fh:
            airports = {a["icao"]: a for a in json.load(fh) if a.get("icao")}

    matched, extra = [], []
    for f in flights:
        route = route_by_pair.get((f["departure_iata"], f["arrival_iata"]))
        if route is None:
            extra.append(f)
            continue
        a, b = airports.get(route["departure_icao"]), airports.get(route["arrival_icao"])
        matched.append({**route, **f, "distance_nm": distance_nm(a, b) if a and b else None})

    # The app keys routes by flight number. A number on two routes is a
    # multi-sector flight (A-B-C): keep the leg that operates most days.
    by_fn = defaultdict(list)
    for m in matched:
        by_fn[m["flight_number"]].append(m)
    unique = [max(v, key=lambda m: len(m["days_operated"] or [])) for v in by_fn.values()]
    unique.sort(key=lambda m: (m["departure_iata"], m["dep_local"] or ""))

    covered = {(m["departure_iata"], m["arrival_iata"]) for m in unique}
    missing = [r for r in routes if (r["departure_iata"], r["arrival_iata"]) not in covered]

    fields = ["flight_number", "departure_city", "departure_iata", "departure_icao",
              "arrival_city", "arrival_iata", "arrival_icao", "aircraft_type", "aircraft_icao",
              "dep_local", "arr_local", "dep_utc", "arr_utc", "duration_minutes", "distance_nm",
              "days_operated", "updated"]

    def write_csv(name, rows, cols):
        with open(os.path.join(args.out_dir, name), "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                if isinstance(r.get("days_operated"), list):
                    r = {**r, "days_operated": "".join(map(str, r["days_operated"]))}
                w.writerow(r)

    write_csv("easyjet_schedule.csv", unique, fields)
    write_csv("easyjet_missing.csv", missing, list(routes[0].keys()))
    write_csv("easyjet_extra.csv", extra, [c for c in fields if c in extra[0]] if extra else ["flight_number"])

    app_routes = [{
        "flight_number": m["flight_number"],
        "callsign_prefix": None,
        "departure_iata": m["departure_iata"],
        "departure_icao": m["departure_icao"],
        "arrival_iata": m["arrival_iata"],
        "arrival_icao": m["arrival_icao"],
        "aircraft_type": m["aircraft_type"],
        "duration_minutes": m["duration_minutes"],
        "distance_nm": m["distance_nm"],
        "days_operated": m["days_operated"],
        "scheduled_departure_local": m["dep_local"],
        "scheduled_arrival_local": m["arr_local"],
    } for m in unique]
    with open(os.path.join(args.out_dir, "routes.json"), "w", encoding="utf-8") as fh:
        json.dump(app_routes, fh, indent=2)

    # Quality report - read this before trusting the data.
    n = len(unique) or 1
    print(f"\n{len(unique)} flights on {len(covered)}/{len(routes)} routes from the CSV")
    print(f"{len(missing)} CSV routes with no flight, {len(extra)} AirLabs flights on routes not in the CSV")
    print(f"multi-sector duplicates dropped: {len(matched) - len(unique)}")
    print("aircraft:", dict(Counter(m["aircraft_icao"] for m in unique).most_common()))
    for field in ("aircraft_icao", "dep_local", "arr_local", "duration_minutes", "days_operated"):
        empty = sum(1 for m in unique if not m[field])
        print(f"  {field:17s} missing on {empty} ({100 * empty / n:.0f}%)")
    updated = sorted(m["updated"] for m in unique if m["updated"])
    if updated:
        print(f"row 'updated' dates: oldest {updated[0]}, median {updated[len(updated) // 2]}, newest {updated[-1]}")


if __name__ == "__main__":
    main()
