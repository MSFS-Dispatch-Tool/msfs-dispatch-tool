"""Build the easyJet schedule from the AirLabs Routes API.

An AirLabs "route" row is one flight number on one weekday pattern with
its local/UTC times and block time - U24636 ABZ-CDG can be four rows, one
per departure time. The script collects every row for the routes in
easyjet_routes.csv, folds each flight number's rows into one entry, and
writes:

    easyjet_schedule.csv   one row per flight number on a known route
    easyjet_missing.csv    routes from the CSV with no AirLabs flight
    easyjet_extra.csv      AirLabs flights on routes not in the CSV
    routes.json            same schema as data/carriers/RYR/routes.json

Free-plan limits (seen in --probe): 50 rows per call, and no query returns
more than its first 300 rows. easyJet has ~14k rows, so the script queries
per airport instead of per airline:
  1. departures of each CSV airport (first page reports total_items);
  2. for airports over the cap, arrivals into their destinations instead;
  3. single-route queries for whatever is still not covered.
Expect ~400-700 calls. Every page is cached in --cache-dir, so a stopped
run resumes for free; --max-calls stops before the monthly quota is gone.

Google Colab
------------
    # Key: left sidebar -> key icon (Secrets) -> add AIRLABS_API_KEY,
    # toggle notebook access on. Never paste it into a cell. Secrets are
    # only readable inside the notebook, not from a `!python` subprocess,
    # so copy it into an env var (inherited by `!python`) in a cell first:
    import os
    from google.colab import userdata
    os.environ["AIRLABS_API_KEY"] = userdata.get("AIRLABS_API_KEY")
    !pip install -q requests
    # upload this file, easyjet_routes.csv and (optional, for distance)
    # data/airports_world.json from the repo

    !python fetch_airlabs.py --probe            # 1 call: fields + plan limits
    !python fetch_airlabs.py --probe-aircraft   # 2 calls: do live endpoints carry aircraft type?
    !python fetch_airlabs.py --airports airports_world.json
"""

import argparse
import csv
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict

import requests

API_BASE = "https://airlabs.co/api/v9/"
PAGE_SIZE = 500   # requested; the free plan silently returns 50
ROW_CAP = 300     # free plan: offsets past this come back empty

# Operating carrier -> ICAO callsign prefix. easyJet sells nearly everything
# as U2; AirLabs names the operator in cs_airline_iata (EC = easyJet Europe,
# DS = easyJet Switzerland).
OPERATOR_ICAO = {"U2": "EZY", "EC": "EJU", "DS": "EZS"}

# AirLabs gives the ICAO type designator; the app keys fleets by the short
# IATA-style code (Ryanair uses "738").
AIRCRAFT_TYPE = {"A319": "319", "A320": "320", "A20N": "32N", "A321": "321", "A21N": "32Q"}
WEEKDAY = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 7}


def get_key(cli_key):
    if cli_key:
        return cli_key
    if os.environ.get("AIRLABS_API_KEY"):
        return os.environ["AIRLABS_API_KEY"]
    sys.exit("No API key. In Colab, run this in a notebook cell first (secrets aren't "
             "visible to `!python` subprocesses):\n"
             "  import os; from google.colab import userdata\n"
             "  os.environ['AIRLABS_API_KEY'] = userdata.get('AIRLABS_API_KEY')\n"
             "Elsewhere: export AIRLABS_API_KEY=... or pass --key")


class OutOfCalls(Exception):
    pass


class Api:
    def __init__(self, key, cache_dir, max_calls):
        self.key, self.cache_dir, self.left, self.used = key, cache_dir, max_calls, 0
        self.exhausted = False

    def get(self, params, endpoint="routes"):
        """One API page, cached on disk by endpoint + query parameters."""
        prefix = "" if endpoint == "routes" else endpoint + "_"
        name = prefix + "_".join(f"{k}-{v}" for k, v in sorted(params.items())) + ".json"
        path = os.path.join(self.cache_dir, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        if self.left <= 0:
            raise OutOfCalls()
        self.left -= 1
        self.used += 1
        for attempt in range(4):
            r = requests.get(API_BASE + endpoint, params={**params, "api_key": self.key}, timeout=60)
            if r.status_code != 429:
                break
            time.sleep(30 * (attempt + 1))
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"AirLabs error for {endpoint} {params}: {data['error']}")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return data

    def query(self, **filters):
        """All rows for a routes query. Returns (rows, complete): complete is
        False when the query holds more rows than the free plan will page
        through - its first page is still returned so nothing fetched is
        wasted, but the caller has to cover it with narrower queries."""
        # Don't trust has_more: AirLabs computes it from the *requested*
        # limit (500) although the free plan serves 50 rows, so a query with
        # 51-500 rows reports has_more=false after its first page. Page on
        # total_items instead. (limit stays 500 so cached pages keep their
        # file names.)
        rows, offset = [], 0
        while True:
            try:
                data = self.get({**filters, "limit": PAGE_SIZE, "offset": offset})
            except OutOfCalls:
                # Only this query goes unfinished - later queries may still
                # be served from the cache, so the run carries on.
                self.exhausted = True
                return rows, False
            page = data.get("response") or []
            total = (data.get("request") or {}).get("total_items")
            rows.extend(page)
            if total is not None and total > ROW_CAP:
                return rows, False
            if not page:
                return rows, (len(rows) >= total) if total is not None else offset < ROW_CAP
            if total is not None and len(rows) >= total:
                return rows, True
            offset += len(page)


def collect(api, routes):
    """Fetches rows until every CSV route is covered by a complete query
    or --max-calls runs out. Returns (rows, uncovered_routes, stopped);
    rows fetched before a stop are kept so the outputs still get written."""
    pool = []
    pairs = {(r["departure_iata"], r["arrival_iata"]) for r in routes}
    covered = set()
    _collect_steps(api, pairs, pool, covered)
    return pool, pairs - covered, api.exhausted


def _collect_steps(api, pairs, pool, covered):

    def run(**filters):
        rows, complete = api.query(airline_iata="U2", **filters)
        pool.extend(rows)
        return complete

    # 1. Departures per airport.
    deps = sorted({d for d, _ in pairs})
    capped_deps = []
    for i, dep in enumerate(deps, 1):
        if run(dep_iata=dep):
            covered.update(p for p in pairs if p[0] == dep)
        else:
            capped_deps.append(dep)
        print(f"  [1/3 departures {i}/{len(deps)}] {dep}{' (over cap)' if dep in capped_deps else ''}"
              f" - calls used {api.used}")
    print(f"step 1 done: {len(covered)}/{len(pairs)} routes covered, "
          f"{len(capped_deps)} airports over the cap: {', '.join(capped_deps)}")

    # 2. Arrivals into the destinations of the over-cap airports, busiest
    #    first - one arrivals query can cover several missing routes.
    todo = defaultdict(set)
    for p in pairs - covered:
        todo[p[1]].add(p)
    arrs = sorted(todo, key=lambda a: -len(todo[a]))
    for i, arr in enumerate(arrs, 1):
        still = todo[arr] - covered
        if not still:
            continue
        ok = run(arr_iata=arr)
        if ok:
            covered.update(still)
        print(f"  [2/3 arrivals {i}/{len(arrs)}] {arr}{'' if ok else ' (over cap)'} - calls used {api.used}")

    # 3. Single routes - routes with no rows at all yet first, so a run
    #    that stops at --max-calls has spent its calls where they add most.
    seen = {(r.get("dep_iata"), r.get("arr_iata")) for r in pool}
    rest = sorted(pairs - covered, key=lambda p: (p in seen, p))
    for i, (dep, arr) in enumerate(rest, 1):
        if run(dep_iata=dep, arr_iata=arr):
            covered.add((dep, arr))
        print(f"  [3/3 routes {i}/{len(rest)}] {dep}-{arr} - calls used {api.used}")


def canonical_flight(row):
    """Marketing U2 number when there is one, plus the operating carrier."""
    airline, cs = row.get("airline_iata"), row.get("cs_airline_iata")
    if airline == "U2":
        return row["flight_iata"], OPERATOR_ICAO.get(cs, "EZY")
    if cs == "U2" and row.get("cs_flight_iata"):
        return row["cs_flight_iata"], OPERATOR_ICAO.get(airline, row.get("airline_icao"))
    return row["flight_iata"], OPERATOR_ICAO.get(airline, row.get("airline_icao"))


def fold(rows):
    """Collapses the per-weekday rows of each (flight number, route) into
    one entry: days = union of all variants; times/aircraft from the variant
    flown on most days (ties -> most recently updated)."""
    groups = defaultdict(dict)
    for row in rows:
        if not row.get("flight_iata") or not row.get("dep_iata"):
            continue
        airline, cs = row.get("airline_iata"), row.get("cs_airline_iata")
        if airline not in OPERATOR_ICAO and cs not in OPERATOR_ICAO:
            continue  # another carrier's flight
        fn, operator = canonical_flight(row)
        if not re.fullmatch(r"[A-Z0-9]{2}\d{1,4}", fn.upper()):
            continue  # e.g. U22029D - suffixed numbers are diversions/ad-hoc ops
        key = (fn.upper(), row["dep_iata"], row["arr_iata"])
        variant = (row.get("dep_time"), row.get("arr_time"), tuple(sorted(row.get("days") or [])))
        groups[key][variant] = {**row, "_operator": operator}  # same variant via 2 queries

    flights = []
    for (fn, dep, arr), variants in groups.items():
        vs = list(variants.values())
        main = max(vs, key=lambda r: (len(r.get("days") or []), r.get("updated") or ""))
        days = sorted({WEEKDAY[d] for r in vs for d in (r.get("days") or []) if d in WEEKDAY})
        icao_type = next((r["aircraft_icao"].upper() for r in [main] + vs if r.get("aircraft_icao")), None)
        flights.append({
            "flight_number": fn,
            "operator_icao": main["_operator"],
            "departure_iata": dep,
            "arrival_iata": arr,
            "aircraft_icao": icao_type,
            "aircraft_type": AIRCRAFT_TYPE.get(icao_type, icao_type),
            "dep_local": main.get("dep_time"),
            "arr_local": main.get("arr_time"),
            "dep_utc": main.get("dep_time_utc"),
            "arr_utc": main.get("arr_time_utc"),
            "duration_minutes": main.get("duration"),
            "days_operated": days or None,
            "time_variants": len(vs),
            "updated": max(r.get("updated") or "" for r in vs) or None,
        })
    return flights


def distance_nm(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return round(2 * 3440.065 * math.asin(math.sqrt(h)))


def probe(api):
    data = api.get({"airline_iata": "U2", "limit": 5, "offset": 0})
    req = data.get("request", {})
    if isinstance(req.get("key"), dict):
        req = {**req, "key": {**req["key"], "api_key": "<redacted>"}}
    print("request metadata (plan limits etc.):")
    print(json.dumps(req, indent=2)[:3000])
    print("\nfirst rows:")
    print(json.dumps(data.get("response", [])[:5], indent=2))


def probe_aircraft(api):
    """The routes table has no aircraft for easyJet. Checks whether the
    live endpoints (airport board, airborne flights) carry a type."""
    for endpoint, params in (("schedules", {"dep_iata": "LGW", "airline_iata": "U2", "limit": 10}),
                             ("flights", {"airline_iata": "U2", "limit": 10})):
        data = api.get(params, endpoint)
        rows = data.get("response") or []
        req = data.get("request") or {}
        typed = sum(1 for r in rows if r.get("aircraft_icao"))
        print(f"\n== {endpoint}: {len(rows)} rows, {typed} with aircraft_icao, "
              f"total_items={req.get('total_items')}")
        for r in rows[:10]:
            print({k: r.get(k) for k in ("flight_iata", "dep_iata", "arr_iata", "dep_time",
                                         "aircraft_icao", "reg_number", "status")})
        if "error" in data:
            print(data["error"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", help="AirLabs API key (prefer the AIRLABS_API_KEY secret)")
    ap.add_argument("--routes", default="easyjet_routes.csv")
    ap.add_argument("--airports", help="data/airports_world.json from the repo (for distance_nm)")
    ap.add_argument("--cache-dir", default="airlabs_cache")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--max-calls", type=int, default=900,
                    help="stop after this many uncached calls (free plan: 1,000/month)")
    ap.add_argument("--probe", action="store_true", help="one call, print fields and plan limits")
    ap.add_argument("--probe-aircraft", action="store_true", help="two calls to the live endpoints")
    args = ap.parse_args()

    os.makedirs(args.cache_dir, exist_ok=True)
    os.makedirs(args.out_dir, exist_ok=True)
    api = Api(get_key(args.key), args.cache_dir, args.max_calls)

    if args.probe:
        return probe(api)
    if args.probe_aircraft:
        return probe_aircraft(api)

    with open(args.routes, newline="", encoding="utf-8") as fh:
        routes = list(csv.DictReader(fh))

    # Never leave last run's files behind to be mistaken for this run's.
    for name in ("easyjet_schedule.csv", "easyjet_missing.csv", "easyjet_extra.csv", "routes.json"):
        if os.path.exists(os.path.join(args.out_dir, name)):
            os.remove(os.path.join(args.out_dir, name))

    rows, uncovered, stopped = collect(api, routes)
    print(f"\n{len(rows)} raw rows, {api.used} API calls used this run")
    if stopped:
        print(f"STOPPED at --max-calls={args.max_calls}: {len(uncovered)} routes not fully fetched. "
              "Writing what we have; everything is cached, so rerun later (quota resets monthly) to finish.")

    flights = fold(rows)
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

    with_flights = {(m["departure_iata"], m["arrival_iata"]) for m in matched}
    missing = [{**r, "status": "not_fetched" if (r["departure_iata"], r["arrival_iata"]) in uncovered
                else "no_flights"}
               for r in routes if (r["departure_iata"], r["arrival_iata"]) not in with_flights]

    fields = ["flight_number", "operator_icao", "departure_city", "departure_iata", "departure_icao",
              "arrival_city", "arrival_iata", "arrival_icao", "aircraft_type", "aircraft_icao",
              "dep_local", "arr_local", "dep_utc", "arr_utc", "duration_minutes", "distance_nm",
              "days_operated", "time_variants", "updated"]

    def write_csv(name, rows_out, cols):
        with open(os.path.join(args.out_dir, name), "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows_out:
                if isinstance(r.get("days_operated"), list):
                    r = {**r, "days_operated": "".join(map(str, r["days_operated"]))}
                w.writerow(r)

    write_csv("easyjet_schedule.csv", unique, fields)
    write_csv("easyjet_missing.csv", missing, list(routes[0].keys()) + ["status"])
    write_csv("easyjet_extra.csv", extra, list(extra[0].keys()) if extra else ["flight_number"])

    app_routes = [{
        "flight_number": m["flight_number"],
        "callsign_prefix": m["operator_icao"],
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
    print(f"{len(unique)} flights on {len(with_flights)}/{len(routes)} CSV routes")
    print(f"{len(missing)} CSV routes without flights ({len(uncovered)} not fully fetched), "
          f"{len(extra)} AirLabs flights on routes not in the CSV, "
          f"{len(matched) - len(unique)} multi-sector duplicates dropped")
    print("operators:", dict(Counter(m["operator_icao"] for m in unique).most_common()))
    print("aircraft:", dict(Counter(m["aircraft_icao"] for m in unique).most_common()))
    for field in ("aircraft_icao", "dep_local", "arr_local", "duration_minutes", "days_operated"):
        empty = sum(1 for m in unique if not m[field])
        print(f"  {field:17s} missing on {empty} ({100 * empty / n:.0f}%)")
    updated = sorted(m["updated"] for m in unique if m["updated"])
    if updated:
        print(f"'updated' dates: oldest {updated[0]}, median {updated[len(updated) // 2]}, newest {updated[-1]}")


if __name__ == "__main__":
    main()
