# easyJet route data: handoff summary

## Goal
Add easyJet to the MSFS dispatch app, with the same data Ryanair already has: flight number, aircraft type, local departure and arrival times, block time, distance and days of operation. The starting point was the list of 2,564 easyJet routes (`easyjet_routes.csv`).

## Where it is
- **Repo:** `msfs-dispatch-tool/msfs-dispatch-tool`
- **Branch:** `claude/dreamy-noether-8zaksn`, pushed. **No pull request yet.**
- **Data commit:** `3b018a3`: "Add easyJet route network data; fix flight-number digits for U2"

## Data source: AirLabs Routes API (free plan)
- **Rejected sources:**
  - Flightradar24: excluded by choice.
  - Aviation Edge: $7 for the first month, then renews automatically at $299 with no refund. Cancelling needs 10 days' notice.
  - AeroDataBox (about $5) is only a fallback if aircraft types are ever needed.
- **Free-plan limits:**
  - 1,000 calls per month; the key's period ends **24 October 2026**.
  - 50 rows per call.
  - A query returns at most its first **300 rows**.
- **AirLabs quirks:**
  - `has_more` is calculated from the limit *requested* (500), not the 50 rows actually sent. So the script pages using `total_items` instead.
  - One row per weekday variant of a flight. The script merges them, combining all days.
  - `cs_airline_iata` = EC or DS means the flight is **operated by** easyJet Europe or easyJet Switzerland. These are real easyJet flights, not codeshares with another airline.
  - Aircraft type (`aircraft_icao`) is empty on about 99% of easyJet rows.
- **The API key is stored as a Colab secret** called `AIRLABS_API_KEY`. It is not in the repo.

## What's in the repo
| Path | What it is |
|---|---|
| `tools/easyjet_schedule/fetch_airlabs.py` | Colab fetcher. Queries departures per airport, then arrivals, then single routes. Caches every page in `airlabs_cache/`. `--max-calls` limits spending, `--max-calls 0` rebuilds from the cache only, and it always writes its outputs. Also has `--probe` and `--probe-aircraft`. |
| `tools/easyjet_schedule/easyjet_schedule.csv` | Final data: **4,401 flights on 2,248 routes**, with no gaps apart from aircraft type |
| `tools/easyjet_schedule/easyjet_missing.csv` | 316 routes not operating in September. Almost all winter or seasonal: ski, Lapland, Egypt, Canaries, Morocco, Iceland |
| `tools/easyjet_schedule/build_routes.py` | Converts the CSV into `data/carriers/EZY/routes.json` |
| `tools/easyjet_schedule/scrape_flightsfrom.py` | Earlier scraper for flightsfrom.com. Never run against the real site; not used |
| `data/carriers/EZY/carrier.json` | EZY / U2, callsign prefix EZY. Fleet: `320` (A320, 186 seats), `32N` (A20N, 186), `319` (A319, 156), `32Q` (A21N, 235) |
| `data/carriers/EZY/routes.json` | Same format as Ryanair's. `callsign_prefix` is the operator (EZY, EJU or EZS); `aircraft_type` is null on 99% of flights |

## App code changes
- **`generator.py`:** new `flight_number_digits()`, which skips the 2-character airline code before reading digits. `U28391` now reads as 8391, not 28391. `_numeric_part()` uses it.
- **`app.py`:** the SimBrief `fltnum` uses `flight_number_digits()`.
- **Checked:** Ryanair's round-trip pairing is unchanged (3,496 pairs). `load_carrier("EZY")` loads fine, with 2,126 round-trip pairs.
- **easyJet is not switched on:** `ACTIVE_CARRIER_CODES = ("RYR",)` is unchanged.

## Open items, in suggested order
1. **A320-family MEL set:** `data/aircraft/<type>/mel_list.json`. Only `738` exists today, so easyJet would get no MEL items.
2. **Aircraft assignment:** flights without a type currently fall back to the first fleet entry (A320, 186 seats). Planned: a weighted random pick per base at dispatch time. That's more realistic than a fixed type, because easyJet swaps aircraft daily.
3. **Switching easyJet on:** a carrier picker in the UI plus adding `"EZY"` to `ACTIVE_CARRIER_CODES`. Also check that the app handles route-level `callsign_prefix`; today it only uses the carrier-level one.
4. **Winter routes:** in November, after the AirLabs quota resets, run `fetch_airlabs.py` in Colab again with the same cache folder, copy the new CSV to `tools/easyjet_schedule/`, and run `python tools/easyjet_schedule/build_routes.py`.
5. **Open a PR** for the branch.

## Colab setup, for reruns
1. Load the key:
   ```python
   import os; from google.colab import userdata
   os.environ["AIRLABS_API_KEY"] = userdata.get("AIRLABS_API_KEY")
   ```
2. Put the latest `fetch_airlabs.py` from the branch in place, using `%%writefile` or an upload. Keep `airlabs_cache/`, `easyjet_routes.csv` and `airports_world.json` in the session.
3. Run it:
   ```
   !python fetch_airlabs.py --airports airports_world.json --max-calls 900
   ```
