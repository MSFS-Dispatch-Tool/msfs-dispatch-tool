"""Enrich the easyJet route list with flight numbers, aircraft type and
scheduled times scraped from flightsfrom.com route pages.

Input : easyjet_routes.csv  (departure_city, departure_iata, departure_icao,
                             arrival_city, arrival_iata, arrival_icao)
Output: easyjet_schedule.csv   one row per easyJet flight number found
        easyjet_missing.csv    routes with no easyJet flight on the page
        routes.json            same schema as data/carriers/RYR/routes.json

Google Colab
------------
    !pip install -q curl_cffi beautifulsoup4 lxml timezonefinder
    # upload this file, easyjet_routes.csv and (optional, for distance and
    # duration fallback) data/airports_world.json from the repo

    # 1. Probe one route first - it saves the raw HTML and prints what the
    #    parser extracted, so a layout change shows up before a 2h run.
    !python scrape_flightsfrom.py --probe LGW-GVA

    # 2. Full run. Pages are cached in --cache-dir, so a disconnected Colab
    #    session resumes where it stopped. Point it at Drive to survive a
    #    runtime reset:
    #    from google.colab import drive; drive.mount('/content/drive')
    !python scrape_flightsfrom.py --airports airports_world.json \
        --cache-dir /content/drive/MyDrive/ezy_cache

Notes
-----
* flightsfrom.com publishes airline timetables (times are local). It only
  shows flights for the current/next season, so seasonal routes that are
  not on sale yet land in easyjet_missing.csv - rerun later for those.
* The page layout is not an API. The parser looks for any element that
  holds exactly one easyJet flight number plus two HH:MM times instead of
  relying on CSS class names, and also scans embedded JSON. If --probe
  finds nothing, the page layout has changed (or Cloudflare served a
  challenge page): send the saved HTML file back and adapt parse_page().
* Be polite: the default 2-4 s delay between requests keeps the full run
  at roughly 2 hours. Don't lower it much.
"""

import argparse
import csv
import datetime as dt
import json
import math
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict

from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as http  # browser TLS fingerprint, gets past basic Cloudflare
    IMPERSONATE = {"impersonate": "chrome"}
except ImportError:  # plain requests works only if the site isn't fingerprinting
    import requests as http
    IMPERSONATE = {}

BASE_URL = "https://www.flightsfrom.com/{dep}-{arr}"

# easyJet UK (U2), easyJet Europe (EC), easyJet Switzerland (DS). Timetables
# sell almost everything as U2; the other two show up on some pages.
EZY_FLIGHT_RE = re.compile(r"\b(U2|EC|DS)\s?-?(\d{1,4})\b")
TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
DURATION_RE = re.compile(r"\b(\d{1,2})\s*h(?:ours?|rs?)?\s*(\d{1,2})\s*m(?:in(?:utes?)?)?\b", re.I)

# Most specific first: "A320neo" must not be read as "A320".
AIRCRAFT_PATTERNS = [
    (re.compile(r"A\s?321\s?-?\s?neo|A21N|\b32Q\b", re.I), "32Q", "A21N"),
    (re.compile(r"A\s?320\s?-?\s?neo|A20N|\b32N\b", re.I), "32N", "A20N"),
    (re.compile(r"A\s?321|\b321\b", re.I), "321", "A321"),
    (re.compile(r"A\s?320|\b320\b", re.I), "320", "A320"),
    (re.compile(r"A\s?319|\b319\b", re.I), "319", "A319"),
]

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
INACTIVE_CLASS_RE = re.compile(r"inactive|disabled|off|not|grey|gray|empty|no-?fl", re.I)

CHALLENGE_MARKERS = ("cf-chl", "Just a moment...", "challenge-platform", "Attention Required")


# ---------------------------------------------------------------- fetching

def fetch(dep, arr, cache_dir, delay):
    """Returns the route page HTML, from cache when possible. Returns None
    on 404 (no such route page). Raises on a Cloudflare challenge so the
    run stops instead of hammering the site with blocked requests."""
    path = os.path.join(cache_dir, f"{dep}-{arr}.html")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    missing_marker = path + ".404"
    if os.path.exists(missing_marker):
        return None

    url = BASE_URL.format(dep=dep, arr=arr)
    headers = {"Accept-Language": "en-GB,en;q=0.9"}
    for attempt in range(4):
        time.sleep(delay * random.uniform(1.0, 2.0))
        try:
            r = http.get(url, headers=headers, timeout=30, **IMPERSONATE)
        except Exception as e:  # network blip - back off and retry
            print(f"  {dep}-{arr}: {e!r}, retrying", file=sys.stderr)
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code == 404:
            open(missing_marker, "w").close()
            return None
        if r.status_code in (429, 503):
            time.sleep(60 * (attempt + 1))
            continue
        if any(m in r.text for m in CHALLENGE_MARKERS):
            raise RuntimeError(
                f"Cloudflare challenge on {url} (HTTP {r.status_code}). "
                "Wait a while / raise --delay, or run from a home connection "
                "instead of Colab.")
        r.raise_for_status()
        with open(path, "w", encoding="utf-8") as f:
            f.write(r.text)
        return r.text
    raise RuntimeError(f"giving up on {url}")


# ----------------------------------------------------------------- parsing

def norm_flight(prefix, digits):
    return f"{prefix.upper()}{int(digits)}"


def match_aircraft(text):
    for pattern, iata_code, icao_code in AIRCRAFT_PATTERNS:
        if pattern.search(text):
            return iata_code, icao_code
    return None, None


def hhmm(h, m):
    return f"{int(h):02d}:{m}"


def days_from_element(row):
    """Looks for a group of exactly seven day markers (M T W T F S S) and
    reads which ones are active from their CSS classes. Returns a list of
    ISO weekdays (1=Mon) or None when it can't tell."""
    for container in row.find_all(True):
        kids = [k for k in container.find_all(True, recursive=False)]
        if len(kids) != 7:
            continue
        labels = [k.get_text(strip=True).lower() for k in kids]
        if not all(1 <= len(l) <= 3 and l.isalpha() for l in labels):
            continue
        classes = [" ".join(k.get("class") or []) for k in kids]
        if len(set(classes)) < 2:
            return None  # every day styled the same: either all 7 or unknowable
        active = [i + 1 for i, c in enumerate(classes) if not INACTIVE_CLASS_RE.search(c)]
        return active or None
    # Text form: "Mon, Wed, Fri" / "Daily"
    text = row.get_text(" ", strip=True).lower()
    if re.search(r"\bdaily\b", text):
        return [1, 2, 3, 4, 5, 6, 7]
    found = [i + 1 for i, d in enumerate(DAY_NAMES) if re.search(rf"\b{d}", text)]
    return found or None


def parse_row_text(text):
    """Extracts one flight from the text of a single schedule row."""
    fm = EZY_FLIGHT_RE.search(text)
    times = TIME_RE.findall(text)
    if not fm or len(times) < 2:
        return None
    dur = DURATION_RE.search(text)
    iata_type, icao_type = match_aircraft(text)
    return {
        "flight_number": norm_flight(*fm.groups()),
        "dep_local": hhmm(*times[0]),
        "arr_local": hhmm(*times[1]),
        "duration_minutes": int(dur.group(1)) * 60 + int(dur.group(2)) if dur else None,
        "aircraft_type": iata_type,
        "aircraft_icao": icao_type,
        "arrives_next_day": "+1" in text,
    }


def parse_html_rows(soup):
    """Finds the smallest elements that contain exactly one easyJet flight
    number and at least two times: those are the schedule rows, whatever
    the site's markup looks like."""
    flights = []
    seen_rows = set()
    for node in soup.find_all(string=EZY_FLIGHT_RE):
        row = node.parent
        while row is not None and row.name not in ("body", "html"):
            text = row.get_text(" ", strip=True)
            n_flights = len(EZY_FLIGHT_RE.findall(text))
            if n_flights > 1:
                row = None  # climbed past the row into the table
                break
            if len(TIME_RE.findall(text)) >= 2:
                break
            row = row.parent
        if row is None or row.name in ("body", "html") or id(row) in seen_rows:
            continue
        seen_rows.add(id(row))
        f = parse_row_text(row.get_text(" ", strip=True))
        if f:
            f["days_operated"] = days_from_element(row)
            flights.append(f)
    return flights


def walk_json(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk_json(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_json(v)


def parse_embedded_json(soup):
    """Some pages ship their schedule as JSON (ld+json, __NEXT_DATA__ ...).
    Flattens every dict that mentions an easyJet flight number and two
    times, same as a HTML row."""
    flights = []
    for script in soup.find_all("script"):
        raw = script.string or ""
        if "U2" not in raw and "EZY" not in raw and "easyJet" not in raw:
            continue
        start = raw.find("{")
        candidates = [raw] + ([raw[start:raw.rfind("}") + 1]] if start >= 0 else [])
        for c in candidates:
            try:
                data = json.loads(c)
                break
            except ValueError:
                data = None
        if data is None:
            continue
        for d in walk_json(data):
            flat = " ".join(str(v) for v in d.values() if not isinstance(v, (dict, list)))
            if len(EZY_FLIGHT_RE.findall(flat)) == 1:
                f = parse_row_text(flat)
                if f:
                    f["days_operated"] = None
                    flights.append(f)
    return flights


def parse_page(html):
    soup = BeautifulSoup(html, "lxml")
    flights = parse_html_rows(soup) or parse_embedded_json(soup)
    # A flight number can appear once per season/period on the page -
    # collapse to the most common variant of each.
    by_fn = defaultdict(list)
    for f in flights:
        by_fn[f["flight_number"]].append(f)
    merged = []
    for fn, variants in by_fn.items():
        key = Counter((v["dep_local"], v["arr_local"]) for v in variants).most_common(1)[0][0]
        best = next(v for v in variants if (v["dep_local"], v["arr_local"]) == key)
        best = dict(best)
        for field in ("aircraft_type", "aircraft_icao", "duration_minutes"):
            if best[field] is None:
                best[field] = next((v[field] for v in variants if v[field] is not None), None)
        days = set()
        for v in variants:
            if v["days_operated"]:
                days.update(v["days_operated"])
        best["days_operated"] = sorted(days) or None
        merged.append(best)
    return merged


# ------------------------------------------------ distance / duration fill

def load_airports(path):
    if not path:
        return {}
    with open(path, encoding="utf-8") as f:
        return {a["icao"]: a for a in json.load(f) if a.get("icao")}


def distance_nm(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return round(2 * 3440.065 * math.asin(math.sqrt(h)))


_tf = None


def tz_for(airport):
    global _tf
    if _tf is None:
        from timezonefinder import TimezoneFinder
        _tf = TimezoneFinder()
    return _tf.timezone_at(lat=airport["lat"], lng=airport["lon"])


def duration_from_local_times(dep_local, arr_local, dep_ap, arr_ap):
    """Block time from two local clock times: convert both to UTC on a
    reference date using each airport's timezone, rolling to the next day
    if the arrival ends up before the departure."""
    from zoneinfo import ZoneInfo
    ref = dt.date.today()
    dep = dt.datetime.combine(ref, dt.time.fromisoformat(dep_local), ZoneInfo(tz_for(dep_ap)))
    arr = dt.datetime.combine(ref, dt.time.fromisoformat(arr_local), ZoneInfo(tz_for(arr_ap)))
    minutes = (arr - dep).total_seconds() / 60
    while minutes <= 0:
        minutes += 24 * 60
    return round(minutes)


# -------------------------------------------------------------------- main

def probe(route, cache_dir, delay):
    dep, arr = route.upper().split("-")
    html = fetch(dep, arr, cache_dir, delay)
    if html is None:
        print(f"{route}: HTTP 404, no page for this route")
        return
    path = os.path.join(cache_dir, f"{dep}-{arr}.html")
    print(f"saved {len(html):,} bytes to {path}")
    flights = parse_page(html)
    if not flights:
        print("parser found NO easyJet flights - open the saved HTML and check the layout")
        print("easyJet flight numbers present in raw HTML:", sorted(set(
            norm_flight(*m) for m in EZY_FLIGHT_RE.findall(html)))[:20])
    for f in flights:
        print(json.dumps(f))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--routes", default="easyjet_routes.csv")
    ap.add_argument("--airports", help="data/airports_world.json from the repo (distance + duration fallback)")
    ap.add_argument("--cache-dir", default="flightsfrom_cache")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--delay", type=float, default=2.0, help="base seconds between requests (x1-2 jitter)")
    ap.add_argument("--limit", type=int, help="only the first N routes (testing)")
    ap.add_argument("--probe", metavar="DEP-ARR", help="fetch + parse a single route and print the result")
    args = ap.parse_args()

    os.makedirs(args.cache_dir, exist_ok=True)
    os.makedirs(args.out_dir, exist_ok=True)
    if args.probe:
        probe(args.probe, args.cache_dir, args.delay)
        return

    with open(args.routes, newline="", encoding="utf-8") as f:
        routes = list(csv.DictReader(f))
    if args.limit:
        routes = routes[:args.limit]
    airports = load_airports(args.airports)

    rows, missing = [], []
    for i, route in enumerate(routes, 1):
        dep, arr = route["departure_iata"], route["arrival_iata"]
        try:
            html = fetch(dep, arr, args.cache_dir, args.delay)
        except RuntimeError as e:
            print(f"\nSTOPPED at route {i}/{len(routes)}: {e}", file=sys.stderr)
            print("Already-fetched pages are cached; rerun the same command to resume.", file=sys.stderr)
            break
        flights = parse_page(html) if html else []
        status = "ok" if flights else ("no_page" if html is None else "no_easyjet_flights")
        print(f"[{i}/{len(routes)}] {dep}-{arr}: {len(flights)} flight(s) {'' if flights else status}")
        if not flights:
            missing.append({**route, "status": status})
            continue

        dep_ap, arr_ap = airports.get(route["departure_icao"]), airports.get(route["arrival_icao"])
        dist = distance_nm(dep_ap, arr_ap) if dep_ap and arr_ap else None
        for f in flights:
            if f["duration_minutes"] is None and dep_ap and arr_ap:
                f["duration_minutes"] = duration_from_local_times(f["dep_local"], f["arr_local"], dep_ap, arr_ap)
            rows.append({**route, **f, "distance_nm": dist})

    # The app keys routes by flight number, so it must be unique. Same
    # number on two routes happens with multi-sector flights (A-B-C sold as
    # one number): keep the first and report the rest.
    seen, dupes, unique_rows = set(), [], []
    for r in rows:
        if r["flight_number"] in seen:
            dupes.append(r)
            continue
        seen.add(r["flight_number"])
        unique_rows.append(r)

    fields = ["flight_number", "departure_city", "departure_iata", "departure_icao",
              "arrival_city", "arrival_iata", "arrival_icao", "aircraft_type", "aircraft_icao",
              "dep_local", "arr_local", "arrives_next_day", "duration_minutes", "distance_nm",
              "days_operated"]
    with open(os.path.join(args.out_dir, "easyjet_schedule.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in unique_rows:
            w.writerow({**r, "days_operated": "".join(map(str, r["days_operated"] or []))})
    with open(os.path.join(args.out_dir, "easyjet_missing.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(routes[0].keys()) + ["status"])
        w.writeheader()
        w.writerows(missing)

    app_routes = [{
        "flight_number": r["flight_number"],
        "callsign_prefix": None,
        "departure_iata": r["departure_iata"],
        "departure_icao": r["departure_icao"],
        "arrival_iata": r["arrival_iata"],
        "arrival_icao": r["arrival_icao"],
        "aircraft_type": r["aircraft_type"],
        "duration_minutes": r["duration_minutes"],
        "distance_nm": r["distance_nm"],
        "days_operated": r["days_operated"],
        "scheduled_departure_local": r["dep_local"],
        "scheduled_arrival_local": r["arr_local"],
    } for r in unique_rows]
    with open(os.path.join(args.out_dir, "routes.json"), "w", encoding="utf-8") as f:
        json.dump(app_routes, f, indent=2)

    covered = len({(r["departure_iata"], r["arrival_iata"]) for r in unique_rows})
    print(f"\n{len(unique_rows)} flights on {covered}/{len(routes)} routes; "
          f"{len(missing)} routes missing, {len(dupes)} duplicate flight numbers dropped")
    print("aircraft:", dict(Counter(r["aircraft_type"] for r in unique_rows)))
    no_dur = sum(1 for r in unique_rows if r["duration_minutes"] is None)
    if no_dur:
        print(f"{no_dur} flights without duration - pass --airports to fill them")


if __name__ == "__main__":
    main()
