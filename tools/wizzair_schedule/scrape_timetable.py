"""Wizz Air route list + weekly timetable from Wizz's own public website API.

Paste into a Colab cell (after `!pip install -q curl_cffi`) or run as a script.

Gives, per route: the operating weekdays and local departure times, for a
6-week window. It does NOT give flight numbers, arrival times or aircraft:
those come from Wizz's /search/search endpoint, which sits behind Kasada bot
protection - this script deliberately doesn't touch it. Flight numbers and
arrival times are to be added later from AirLabs, matched on route +
departure time.

Outputs:
    wizzair_routes.csv      every direct route on Wizz's route map
    wizzair_timetable.csv   one row per (route, local departure time) with
                            the weekdays it operates in the window
Requests are cached in CACHE_DIR, so an interrupted run resumes for free.
If Wizz starts answering the timetable with a bot challenge, the script
stops instead of retrying - rerun later.
"""

import csv
import datetime as dt
import json
import os
import random
import re
import time
from collections import defaultdict

from curl_cffi import requests as http

WINDOW_FROM = "2026-10-26"   # first full week of the IATA winter season
WINDOW_TO = "2026-12-06"     # 6 weeks - the timetable endpoint's usual maximum
CACHE_DIR = "wizz_timetable_cache"   # point at Drive to survive a runtime reset
DELAY = (3.0, 6.0)                   # seconds between requests, randomised

S = http.Session(impersonate="chrome")
H = {"Origin": "https://wizzair.com", "Referer": "https://wizzair.com/",
     "Accept": "application/json, text/plain, */*", "Content-Type": "application/json"}


def api_base():
    """Wizz rotates the API's version path; the homepage references the current one."""
    html = S.get("https://www.wizzair.com/en-gb", headers=H, timeout=30).text
    m = re.search(r"https://be\.wizzair\.com/(\d+\.\d+\.\d+)", html)
    if not m:
        raise SystemExit("API version not found on the homepage - the site layout changed")
    return f"https://be.wizzair.com/{m.group(1)}/Api"


class Blocked(Exception):
    pass


def timetable(base, legs):
    """One timetable request for 1-2 legs (a route and its reverse), cached."""
    key = "_".join(f"{a}-{b}" for a, b in legs)
    path = os.path.join(CACHE_DIR, key + ".json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    body = {"flightList": [{"departureStation": a, "arrivalStation": b,
                            "from": WINDOW_FROM, "to": WINDOW_TO} for a, b in legs],
            "priceType": "regular", "adultCount": 1, "childCount": 0, "infantCount": 0}
    for attempt in range(3):
        time.sleep(random.uniform(*DELAY))
        r = S.post(f"{base}/search/timetable", json=body, headers=H, timeout=30)
        if r.status_code == 200:
            data = r.json()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            return data
        if any(h.lower().startswith("x-kpsdk") for h in r.headers):
            raise Blocked(f"bot challenge on {key} (HTTP {r.status_code})")
        if r.status_code == 429:
            time.sleep(60 * (attempt + 1))
            continue
        if r.status_code in (400, 404):   # route not bookable in the window
            data = {}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            return data
        r.raise_for_status()
    raise Blocked(f"still rate-limited on {key}")


def main():
    os.makedirs(CACHE_DIR, exist_ok=True)
    base = api_base()
    print("API base:", base)

    cities = S.get(f"{base}/asset/map?languageCode=en-gb", headers=H, timeout=30).json()["cities"]
    name = {c["iata"]: c.get("shortName", "") for c in cities}
    routes = sorted({(c["iata"], k["iata"]) for c in cities for k in c.get("connections", [])
                     if k.get("isDirectFlight")})
    icao = {}
    if os.path.exists("airports_world.json"):
        with open("airports_world.json", encoding="utf-8") as f:
            icao = {a["iata"]: a["icao"] for a in json.load(f) if a.get("iata")}
    with open("wizzair_routes.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["departure_city", "departure_iata", "departure_icao",
                    "arrival_city", "arrival_iata", "arrival_icao"])
        for d, a in routes:
            w.writerow([name.get(d, ""), d, icao.get(d, ""), name.get(a, ""), a, icao.get(a, "")])
    print(f"{len(cities)} airports, {len(routes)} direct routes -> wizzair_routes.csv")

    # One request per airport pair, both directions together.
    route_set = set(routes)
    pairs = sorted({tuple(sorted(r)) for r in routes})
    # (dep, arr, HH:MM) -> set of dates it departs on
    seen = defaultdict(set)
    blocked = None
    for i, (a, b) in enumerate(pairs, 1):
        legs = [leg for leg in ((a, b), (b, a)) if leg in route_set]
        try:
            data = timetable(base, legs)
        except Blocked as e:
            blocked = e
            break
        for f in (data.get("outboundFlights") or []) + (data.get("returnFlights") or []):
            for t in f.get("departureDates") or []:
                # Stations come from the response, not the request: a London
                # request can return LTN and LGW flights (Wizz groups them).
                seen[(f["departureStation"], f["arrivalStation"], t[11:16])].add(t[:10])
        if i % 50 == 0 or i == len(pairs):
            print(f"  {i}/{len(pairs)} airport pairs, {len(seen)} departures so far")

    with open("wizzair_timetable.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["departure_iata", "departure_icao", "arrival_iata", "arrival_icao", "dep_local",
                    "days_operated", "dates_in_window", "first_date", "last_date"])
        for (d, a, hhmm), dates in sorted(seen.items()):
            days = sorted({dt.date.fromisoformat(x).isoweekday() for x in dates})
            w.writerow([d, icao.get(d, ""), a, icao.get(a, ""), hhmm, "".join(map(str, days)),
                        len(dates), min(dates), max(dates)])
    covered = {(d, a) for d, a, _ in seen}
    print(f"\n{len(seen)} scheduled departures on {len(covered)}/{len(routes)} routes "
          f"({WINDOW_FROM} to {WINDOW_TO}) -> wizzair_timetable.csv")
    if blocked:
        print(f"STOPPED: {blocked}. Everything fetched is cached - rerun later to continue.")


if __name__ == "__main__":
    main()
