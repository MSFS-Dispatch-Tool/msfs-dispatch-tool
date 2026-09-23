"""
Collect easyJet flight data from FlightRadar24's live feed (via the
FlightRadarAPI package) and build entries in the SAME schema as
data/routes_enriched.json.

Run repeatedly over time (the easyjet-collect GitHub Actions workflow
runs this every 30 minutes) — each run polls the current live snapshot,
fetches details only for flight numbers not already resolved, and
rebuilds the output JSON from the full accumulated cache.

--- Field mapping (verified against a real probe response) ---
  flight_number              <- flight.number                    (e.g. "U22142")
  departure_iata/icao        <- flight.origin_airport_iata / origin_airport_icao
  arrival_iata/icao          <- flight.destination_airport_iata / destination_airport_icao
  aircraft_type               <- flight.aircraft_code              (e.g. "A21N")
  scheduled_departure_local   <- time_details.scheduled.departure, converted
                                  from UTC epoch to the DEPARTURE airport's
                                  own local time (FR24 gives the timezone
                                  name directly per airport)
  scheduled_arrival_local     <- same, using the ARRIVAL airport's timezone
  duration_minutes            <- (scheduled.arrival - scheduled.departure) / 60
  distance_nm                  <- haversine from FR24's own airport lat/lon
  callsign_prefix, days_operated -> left null (no reliable source)

Run manually:
    pip install FlightRadarAPI
    python3 scripts/easyjet_fr24_collect.py
"""

import json
import math
import os
import threading
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

from FlightRadarAPI import FlightRadar24API

EASYJET_ICAO = "EZY"
CACHE_FILE = "easyjet_fr24_cache.jsonl"
OUTPUT_JSON = "easyjet_routes_enriched.json"

MAX_WORKERS = 4
DETAIL_DELAY = (0.3, 0.6)
MAX_RETRIES = 3

_cache_lock = threading.Lock()


def haversine_nm(lat1, lon1, lat2, lon2):
    R_km = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R_km * math.asin(math.sqrt(a)) / 1.852


def epoch_to_local_hhmm(epoch, tz_name):
    if epoch is None or not tz_name:
        return None
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(ZoneInfo(tz_name))
        return dt.strftime("%H:%M")
    except Exception:
        return None


def load_cache_statuses(path):
    statuses = {}
    if not os.path.exists(path):
        return statuses
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            statuses[rec["flight_number"]] = rec.get("status", "failed")
    return statuses


def iter_cache_records(path):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def append_cache(path, record):
    with _cache_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())


def build_entry_from_flight(fr, flight):
    fn = flight.number
    last_err = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            details = fr.get_flight_details(flight)
            flight.set_flight_details(details)
            break
        except Exception as e:
            last_err = str(e)
            time.sleep(1.5 * attempt)
    else:
        return {"flight_number": fn, "status": "failed", "error": last_err}

    td = flight.time_details or {}
    scheduled = td.get("scheduled", {}) if isinstance(td, dict) else {}
    sched_dep_epoch = scheduled.get("departure")
    sched_arr_epoch = scheduled.get("arrival")

    dep_iata = flight.origin_airport_iata
    arr_iata = flight.destination_airport_iata
    dep_icao = getattr(flight, "origin_airport_icao", None)
    arr_icao = getattr(flight, "destination_airport_icao", None)
    aircraft_type = flight.aircraft_code

    if not all([fn, dep_iata, arr_iata, dep_icao, arr_icao, aircraft_type, sched_dep_epoch, sched_arr_epoch]):
        return {
            "flight_number": fn, "status": "failed",
            "error": "missing required field(s) in details response",
            "raw_had": {
                "dep_iata": dep_iata, "arr_iata": arr_iata,
                "dep_icao": dep_icao, "arr_icao": arr_icao,
                "aircraft_type": aircraft_type,
                "sched_dep_epoch": sched_dep_epoch, "sched_arr_epoch": sched_arr_epoch,
            },
        }

    dep_tz = getattr(flight, "origin_airport_timezone_name", None)
    arr_tz = getattr(flight, "destination_airport_timezone_name", None)
    sched_dep_local = epoch_to_local_hhmm(sched_dep_epoch, dep_tz)
    sched_arr_local = epoch_to_local_hhmm(sched_arr_epoch, arr_tz)

    duration_minutes = round((sched_arr_epoch - sched_dep_epoch) / 60)

    dep_lat = getattr(flight, "origin_airport_latitude", None)
    dep_lon = getattr(flight, "origin_airport_longitude", None)
    arr_lat = getattr(flight, "destination_airport_latitude", None)
    arr_lon = getattr(flight, "destination_airport_longitude", None)
    distance_nm = None
    if None not in (dep_lat, dep_lon, arr_lat, arr_lon):
        distance_nm = round(haversine_nm(dep_lat, dep_lon, arr_lat, arr_lon))

    entry = {
        "flight_number": fn,
        "callsign_prefix": None,
        "departure_iata": dep_iata,
        "departure_icao": dep_icao,
        "arrival_iata": arr_iata,
        "arrival_icao": arr_icao,
        "aircraft_type": aircraft_type,
        "duration_minutes": duration_minutes,
        "distance_nm": distance_nm,
        "days_operated": None,
        "scheduled_departure_local": sched_dep_local,
        "scheduled_arrival_local": sched_arr_local,
    }
    return {"flight_number": fn, "status": "ok", "entry": entry}


def poll_once(fr):
    statuses = load_cache_statuses(CACHE_FILE)
    print(f"Cache so far: {sum(1 for s in statuses.values() if s == 'ok')} resolved, "
          f"{sum(1 for s in statuses.values() if s == 'failed')} pending retry")

    print(f"Polling live easyJet ({EASYJET_ICAO}) flights ...")
    try:
        flights = fr.get_flights(airline=EASYJET_ICAO)
    except Exception as e:
        print(f"[FAILED] poll error: {type(e).__name__}: {e}")
        return

    print(f"Live snapshot: {len(flights)} flights")

    pending = [f for f in flights if statuses.get(f.number) != "ok"]
    seen = set()
    unique_pending = []
    for f in pending:
        if f.number in seen:
            continue
        seen.add(f.number)
        unique_pending.append(f)

    print(f"New/unresolved flight numbers to fetch details for: {len(unique_pending)}")

    if not unique_pending:
        return

    def work(flight):
        result = build_entry_from_flight(fr, flight)
        append_cache(CACHE_FILE, result)
        time.sleep(__import__("random").uniform(*DETAIL_DELAY))
        return result

    resolved_this_poll = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(work, f): f for f in unique_pending}
        for future in as_completed(futures):
            try:
                result = future.result()
                if result["status"] == "ok":
                    resolved_this_poll += 1
            except Exception as e:
                print(f"  [ERROR] {e}")

    print(f"Resolved {resolved_this_poll} new flight numbers this poll")


def build_output():
    latest_by_fn = {}
    for rec in iter_cache_records(CACHE_FILE):
        if rec.get("status") == "ok":
            latest_by_fn[rec["flight_number"]] = rec["entry"]

    entries = list(latest_by_fn.values())
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)

    print(f"\n{OUTPUT_JSON}: {len(entries)} resolved easyJet flights")


def main():
    fr = FlightRadar24API()
    poll_once(fr)
    build_output()


if __name__ == "__main__":
    main()
