# Carrier route data (easyJet + Wizz Air): handoff summary

## Goal
Give the MSFS dispatch app realistic route networks for new carriers, with the same fields Ryanair has: flight number, aircraft type, local departure and arrival times, block time, distance and days of operation.

## Where it is
- **Repo:** `msfs-dispatch-tool/msfs-dispatch-tool`
- **Branch:** `claude/dreamy-noether-8zaksn`, pushed. **No pull request yet, and not merged**; `main` has moved on since, so expect a merge.
- **Key commits:**
  - `3b018a3`: easyJet data and the flight-number digit fix
  - `517a5f4`: Wizz Air data
  - `185eb55`: latest

## Status per carrier
| | easyJet (EZY) | Wizz Air (WZZ) |
|---|---|---|
| Flights in `routes.json` | 4,401 | 2,319 |
| Routes with flights | 2,248 / 2,564 | 1,902 / 2,531 |
| Operators (`callsign_prefix`) | EZY, EJU, EZS | WZZ 1,123 · WMT 1,060 · WUK 136 |
| Round-trip pairs | 2,126 | 1,148 |
| Aircraft type filled | ~1% | 3 flights |
| Fallback type (first fleet entry) | A320, 186 seats | A321neo, 239 seats |
| Schedule season | Summer (September data) | Summer (September data) |
| Switched on in the app | No | No |

Missing routes are mostly winter or seasonal (ski, Lapland, Egypt, Canaries, Morocco), plus for Wizz Air routes on sale but not yet flying (Arad, Castellón, Santiago, Kaunas, Ciampino) and suspended ones (Gulf, Tel Aviv).

## Files
| Path | What it is |
|---|---|
| `data/carriers/EZY/carrier.json` | EZY / U2. Fleet: `320` A320 186, `32N` A20N 186, `319` A319 156, `32Q` A21N 235 |
| `data/carriers/WZZ/carrier.json` | WZZ / W6. Fleet: `32Q` A21N 239, `321` A321 230, `320` A320 180 |
| `data/carriers/<CODE>/routes.json` | Same format as Ryanair's. `aircraft_type` is null where unknown |
| `tools/easyjet_schedule/fetch_airlabs.py` | **Generic** AirLabs fetcher (`--carrier EZY\|WZZ`, `--codes`), run in Colab |
| `tools/easyjet_schedule/build_routes.py` | Schedule CSV → `data/carriers/<CODE>/routes.json` (`--carrier`) |
| `tools/easyjet_schedule/easyjet_*.csv` | easyJet route list, schedule, missing report |
| `tools/wizzair_schedule/wizzair_*.csv` | Wizz Air route list (2,531, cleaned), schedule, missing report |
| `tools/wizzair_schedule/scrape_timetable.py` | Free Wizz Air timetable scraper: days and departure times only, no flight numbers. Backup, not used |
| `tools/easyjet_schedule/scrape_flightsfrom.py` | Old flightsfrom.com scraper, never run against the real site. Not used |

## App code changes
- **`generator.py`:** `flight_number_digits()` skips the 2-character airline code, so `U28391` → 8391 and `W46488` → 6488. `_numeric_part()` uses it.
- **`app.py`:** the SimBrief `fltnum` uses it.
- **Checked:** Ryanair's pairing is unchanged (3,496 pairs). `load_carrier("EZY")` and `load_carrier("WZZ")` both load.
- **Not switched on:** `ACTIVE_CARRIER_CODES = ("RYR",)` is unchanged.

## Data source: AirLabs Routes API (free plan)
- **Free-plan limits:** 1,000 calls per month, 50 rows per call, and at most 300 rows per query. The fetcher queries per airport, then arrivals, then single routes, and caches every page.
- **Quirks handled in the fetcher:**
  - `has_more` is wrong on the free plan, so it pages using `total_items`.
  - One row per weekday variant; these are merged, combining all days.
  - `cs_airline_iata` names the operating carrier.
- **Airline codes to query:**
  - easyJet: `U2` alone is enough.
  - **Wizz Air needs `--codes W6,W4,W9`.** Wizz Air Malta (W4) and Wizz Air UK (W9) flights are filed under their own codes, and querying W6 alone gave 1,147 flights instead of 2,319.
- **Key:** stored as a Colab secret called `AIRLABS_API_KEY`. It is not in the repo.
- **Rejected alternatives:**
  - Flightradar24: excluded by choice.
  - Aviation Edge: $7, then $299 a month on auto-renewal.
  - **Wizz Air's own flight search:** protected by Kasada anti-bot. Its route map and timetable are open, but they have no flight numbers.

## Open items, in suggested order
1. **A320-family MEL sets:** `data/aircraft/<type>/mel_list.json`. Only `738` exists, so easyJet and Wizz Air would get no MEL items.
2. **Aircraft assignment:** replace the fixed fallback type with a weighted random pick per base at dispatch time. Airlines swap aircraft daily.
3. **Switching them on:** a carrier picker in the UI plus `ACTIVE_CARRIER_CODES`. Also check whether the app uses route-level `callsign_prefix`; today it only uses the carrier-level one.
4. **Winter schedules:** rerun in November, after the winter season starts on 25 October and the quota resets.
5. **Merge `main`** into the branch and open a PR.

## Rerunning (Colab)
```python
import os; from google.colab import userdata
os.environ["AIRLABS_API_KEY"] = userdata.get("AIRLABS_API_KEY")
```
```
!python fetch_airlabs.py --carrier EZY --airports airports_world.json --max-calls 900
!python fetch_airlabs.py --carrier WZZ --codes W6,W4,W9 --airports airports_world.json --max-calls 900
```
Keep `airlabs_cache/`, the carrier's `*_routes.csv` and `airports_world.json` in the session. Copy the new `*_schedule.csv` into its `tools/…` folder, then run:
```
python tools/easyjet_schedule/build_routes.py --carrier EZY   # or WZZ
```
