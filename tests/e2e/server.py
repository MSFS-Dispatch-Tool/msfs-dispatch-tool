"""The real app for the browser suites, with only what can't be tested
against the live world replaced: weather comes from fixed samples (so runs
are repeatable and need no aviationweather.gov access), Hoppie's ACARS
network is a fake that accepts the logon code TESTLOGON1 and reports every
aircraft as online, and a scenario can pin the random rolls a suite
depends on.

    python tests/e2e/server.py --seed                  # reset the test pilot's data
    python tests/e2e/server.py --scenario mel --port 5055

Scenarios:
  demo    weather samples only
  mel     A320 weather radar, pack and APU deferred, plus a CDL item, with
          thunderstorms forecast everywhere (a NO-GO to swap out of)
  mel738  737 boost pump (rear), centre tank pump and pack items
  ops     every first leg gets an ATFM delay (code 81, 25 min)

Needs DATABASE_URL (a throwaway database: --seed deletes the "local"
pilot's PIREPs and settings) and no Supabase variables, so the app runs
signed in as the single local pilot.
"""
import argparse
import hashlib
import os
import random
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
for var in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY"):
    os.environ.pop(var, None)

import acars  # noqa: E402
import app  # noqa: E402
import db  # noqa: E402
import techlog  # noqa: E402
import wxmap  # noqa: E402

PILOT = "local"


def _h(icao, n):
    return int(hashlib.md5(icao.encode()).hexdigest(), 16) % n


def sample_metar(icao):
    wind = f"{(_h(icao, 36) * 10) or 360:03d}{8 + _h(icao, 14):02d}KT"
    t = 12 + _h(icao, 14)
    variants = [f"9999 FEW0{30 + _h(icao, 20)} {t:02d}/{t - 7:02d} Q10{14 + _h(icao, 9)} NOSIG",
                f"9999 SCT035 BKN080 {t:02d}/{t - 5:02d} Q101{_h(icao, 9)} NOSIG",
                f"CAVOK {t + 4:02d}/{t - 6:02d} Q101{5 + _h(icao, 4)} NOSIG",
                f"6000 -RA BKN022 OVC045 {t - 3:02d}/{t - 5:02d} Q100{6 + _h(icao, 3)} TEMPO 4000 RA",
                f"2500 BR BKN007 {t - 4:02d}/{t - 5:02d} Q100{8 + _h(icao, 2)} BECMG 5000"]
    return f"{icao} 281050Z {wind} {variants[[0, 0, 0, 1, 1, 2, 2, 3, 4][_h(icao, 9)]]}"


def sample_taf(icao):
    return (f"TAF {icao} 281100Z 2812/2918 {(_h(icao, 36) * 10) or 360:03d}10KT 9999 SCT040 "
            f"TEMPO 2814/2818 BKN025 SHRA BECMG 2900/2902 VRB05KT")


def _category(raw):
    if " 2500 " in raw or "BKN007" in raw:
        return "IFR"
    if "BKN022" in raw:
        return "MVFR"
    return "VFR"


SIGMETS = [
    {"id": "A3", "hazard": "TS", "hazard_name": "Thunderstorms", "qualifier": "EMBD", "fir": "BREST FIR",
     "from": "2026-09-28T09:00Z", "to": "2026-09-28T13:00Z", "base": 0, "top": 38000, "dir": "NE", "spd": 15, "chng": "INTSF",
     "raw": "LFRR SIGMET A3 VALID 280900/281300 LFPW- LFRR BREST FIR EMBD TS OBS AT 0850Z TOP FL380 MOV NE 15KT INTSF=",
     "coords": [[47.0, -3.0], [48.5, -1.0], [47.5, 0.5], [46.0, -1.5], [47.0, -3.0]]},
    {"id": "B1", "hazard": "TURB", "hazard_name": "Turbulence", "qualifier": "SEV", "fir": "MILANO FIR",
     "from": "2026-09-28T10:00Z", "to": "2026-09-28T14:00Z", "base": 25000, "top": 38000, "dir": None, "spd": None, "chng": "NC",
     "raw": "LIMM SIGMET B1 VALID 281000/281400 LIMM- LIMM MILANO FIR SEV TURB FCST FL250/380 STNR NC=",
     "coords": [[46.0, 7.0], [47.0, 11.0], [45.5, 12.0], [45.0, 8.0]]},
]


def patch_weather():
    app.fetch_weather_batch = lambda icaos: {i: {"metar": sample_metar(i), "taf": sample_taf(i)} for i in set(icaos)}
    wxmap.metar_categories = lambda icaos, ua: {"updated": "2026-09-28T10:50Z", "stale": False, "stations": {
        i: {"cat": _category(sample_metar(i)), "raw": sample_metar(i), "obs": "2026-09-28T10:50Z"} for i in icaos}}
    wxmap.current_sigmets = lambda ua: {"updated": "2026-09-28T10:52Z", "stale": False, "sigmets": SIGMETS}


class _HoppieAnswer:
    status_code = 200

    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def patch_hoppie():
    def post(url, data=None, timeout=None):
        if data.get("logon") != "TESTLOGON1":
            return _HoppieAnswer("error {illegal logon code}")
        return _HoppieAnswer("ok {" + data.get("packet", "") + "}" if data.get("type") == "ping" else "ok")
    acars.requests.post = post


def scenario_mel():
    base = app.fetch_weather_batch
    app.fetch_weather_batch = lambda icaos: {
        i: {"metar": v["metar"], "taf": v["taf"].replace("SHRA", "TSRA BKN030CB")} for i, v in base(icaos).items()}

    def forced(mels, cdls, mel_settings=None, cdl_settings=None, exclude_ids=(), rng=random):
        by = {m["system"]: m for m in mels}
        want = [by["Weather radar"], by["Air conditioning pack"], by["APU"]]
        return {"mels": [techlog._resolve(m, rng) for m in want if m["id"] not in exclude_ids],
                "cdl": [techlog._resolve(cdls[1], rng)]}
    techlog.roll_tech_status = forced


def scenario_mel738():
    def forced(mels, cdls, mel_settings=None, cdl_settings=None, exclude_ids=(), rng=random):
        by = {m["id"]: m for m in mels}
        return {"mels": [techlog._resolve(by[i], rng) for i in ("mel-12", "mel-40", "mel-26") if i in by], "cdl": []}
    techlog.roll_tech_status = forced


def scenario_ops():
    def forced_delay(codes, settings=None, dep_metar=None, arr_metar=None, exclude_codes=(), month=None):
        delay = dict(next(c for c in codes if c["iata_code"] == "81"))
        delay["minutes"] = 25
        return delay
    app.roll_delay = forced_delay


SCENARIOS = {"demo": lambda: None, "mel": scenario_mel, "mel738": scenario_mel738, "ops": scenario_ops}


def seed():
    """A fresh test pilot: profile, default generation settings, a short
    logbook over the last months, and no flight in progress."""
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            for table, column in (("pireps", "user_id"), ("app_settings", "id"), ("active_flights", "user_id")):
                cur.execute(f"DELETE FROM {table} WHERE {column} = %s", (PILOT,))
    profile = dict(db.DEFAULT_SETTINGS["profile"], username="skyline_ops", first_name="Alex", last_name="Morgan",
                   nationality="United Kingdom", preferred_base="EGKK", simulator="MSFS 2024",
                   onboarding_complete=True, aircraft_owned=["738", "320"])
    db.save_settings({"profile": profile, "generation": db.DEFAULT_SETTINGS["generation"]}, PILOT)
    trips = [("EGKK", "LEPA", 125, 0), ("LEPA", "EGKK", 130, 0), ("EGKK", "LPPT", 150, 1), ("EGKK", "LIRF", 140, 1),
             ("EGKK", "EIDW", 70, 1), ("EGKK", "LGIR", 230, 2), ("EGKK", "LEMG", 150, 2), ("EGKK", "EDDB", 100, 2),
             ("EGKK", "LFMN", 110, 3), ("EGKK", "LSGG", 90, 3), ("EGKK", "GCLP", 250, 3), ("EGKK", "LHBP", 135, 4)]
    now = datetime.now(timezone.utc)
    for i, (dep, arr, eet, months_ago) in enumerate(trips):
        when = now - timedelta(days=30 * months_ago + 3 + i)
        rec = db.create_pirep({"flight_number": f"U2{8000 + i}", "callsign": f"EZY{80 + i}A",
                               "departure_icao": dep, "arrival_icao": arr,
                               "leg": {"eet_minutes": eet, "flight_number": f"U2{8000 + i}"},
                               "pirep": {"submitted_at": when.isoformat().replace("+00:00", "Z")}}, PILOT)
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE pireps SET flight_date = %s WHERE id = %s", (when.date(), rec["id"]))


def clear_active_flight():
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM active_flights WHERE user_id = %s", (PILOT,))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="demo")
    parser.add_argument("--port", type=int, default=5055)
    parser.add_argument("--seed", action="store_true", help="reset the test pilot's data and exit")
    args = parser.parse_args()
    if not db.db_available():
        sys.exit("DATABASE_URL is not set: point it at a throwaway Postgres database")
    db.init_db()
    if args.seed:
        seed()
        print("seeded")
        return
    clear_active_flight()
    patch_weather()
    patch_hoppie()
    SCENARIOS[args.scenario]()
    app.app.run(port=args.port, debug=False)


if __name__ == "__main__":
    main()
