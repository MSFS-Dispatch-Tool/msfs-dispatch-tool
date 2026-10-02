# Tools

Scripts that build the app's data. None of them run on the server.

| Path | What it does |
|---|---|
| `easyjet_schedule/fetch_airlabs.py` | Fetches a carrier's timetable from the AirLabs Routes API into a schedule CSV. Run it in Google Colab (see below) |
| `easyjet_schedule/build_routes.py` | Converts a schedule CSV into `data/carriers/<CARRIER>/routes.json` (`--carrier EZY` or `WZZ`) |
| `easyjet_schedule/easyjet_routes.csv` | easyJet route list the fetcher works through |
| `easyjet_schedule/easyjet_schedule.csv` | easyJet timetable, summer 2026 (4,401 flights) |
| `easyjet_schedule/easyjet_missing.csv` | easyJet routes not operating in September: mostly winter and seasonal |
| `wizzair_schedule/wizzair_schedule.csv` | Wizz Air timetable, summer 2026 (2,319 flights) |
| `basemap/build_basemap.py` | Builds `static/data/basemap.json`, the offline world map behind the route maps |
| `landing_map/build_landing_map.py` | Builds `static/data/landing_map.json`, the map of Europe behind the landing page's route demo |
| `check_ui_copy.py` | Checks app UI sentences don't end with a full stop (`--fix` to correct them) |
| `seed_demo_account.py` | Command-line version of the admin page's "Demo account" form |

## Refreshing a schedule (winter season, from November)

1. Open Google Colab. Store the AirLabs key as a Colab secret named `AIRLABS_API_KEY` and load it:
   ```python
   import os; from google.colab import userdata
   os.environ["AIRLABS_API_KEY"] = userdata.get("AIRLABS_API_KEY")
   ```
2. Upload `fetch_airlabs.py`, the carrier's route list (e.g. `easyjet_routes.csv`), `data/airports_world.json`, and the `airlabs_cache/` folder from the previous run if you kept it (cached pages cost no calls).
3. Run it within the monthly quota of the free plan (1,000 calls):
   ```
   !python fetch_airlabs.py --airports airports_world.json --max-calls 900
   ```
   It stops before the quota runs out and resumes from the cache next time.
4. Copy the resulting CSV over the one in this folder, then rebuild and check:
   ```
   python tools/easyjet_schedule/build_routes.py --carrier EZY
   ```
5. Commit the CSV and the new `routes.json`.

AirLabs quirks worth knowing: `has_more` is computed from the requested page size, so the fetcher pages by `total_items`; each weekday variant of a flight is a separate row (merged into one flight with all its days); and the aircraft type is missing on almost every easyJet row, so the app assigns one from the carrier's fleet mix.
