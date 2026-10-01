"""Delays, last-minute changes, loads and round-trip pairing."""
import json
import os
import random
from collections import Counter

import pytest

import generator

DATA = os.path.join(os.path.dirname(__file__), "..", "..", "data")
CODES = json.load(open(os.path.join(DATA, "delay_codes.json")))
LMC = json.load(open(os.path.join(DATA, "lmc_events.json")))
DG = json.load(open(os.path.join(DATA, "dangerous_goods.json")))
STORM = "EGKK 011650Z 24012KT 9999 TS SCT030CB 22/16 Q1012"
CALM = "EGKK 011650Z 24012KT CAVOK 22/16 Q1012"


@pytest.fixture(autouse=True)
def seeded():
    random.seed(1234)


def rolls(n=2000, **kwargs):
    return [generator.roll_delay(CODES, **kwargs) for _ in range(n)]


def test_delay_minutes_stay_in_the_codes_range():
    for d in filter(None, rolls(month=7)):
        lo, hi = d["duration_range_minutes"]
        assert lo <= d["minutes"] <= hi


def test_realistic_share_of_departures_over_15_minutes_late():
    """Calibrated to CODA: about a third of departures more than 15 min late
    in an average month (the month factor scales it)."""
    late = sum(1 for d in rolls(4000, dep_metar=CALM, arr_metar=CALM, month=5) if d and d["minutes"] > 15)
    assert 0.28 < late / 4000 < 0.40


def test_pilot_probability_overrides_the_realistic_chance():
    assert all(d is None for d in rolls(300, delay_settings={"probability": 0}, month=7))
    assert all(d is not None for d in rolls(300, delay_settings={"probability": 100}, month=7))


def test_disabled_codes_never_roll():
    disabled = [c["iata_code"] for c in CODES if c["iata_code"] != "93"]
    got = {d["iata_code"] for d in rolls(300, delay_settings={"disabled_codes": disabled, "probability": 100}, month=7) if d}
    assert got == {"93"}


def test_thunderstorm_closes_the_departure_ramp():
    counts = Counter((d or {}).get("iata_code") for d in rolls(dep_metar=STORM, month=7))
    assert 0.53 < counts["77"] / 2000 < 0.67
    assert "77" not in {(d or {}).get("iata_code") for d in rolls(dep_metar=CALM, month=7)}


def test_thunderstorm_at_the_destination_cuts_the_arrival_rate():
    d = generator.roll_delay(CODES, {"probability": 100}, arr_metar="LEMD 011650Z 9999 +TSRA 22/16 Q1012", month=7)
    assert d["iata_code"] == "83" and d["thunderstorm"] == "arrival"


def test_weather_codes_need_the_weather():
    got = {(d or {}).get("iata_code") for d in rolls(1000, dep_metar=CALM, arr_metar=CALM, month=1)}
    assert not got & {"71", "72", "75", "77"}


def test_oversold_only_on_a_full_flight_and_late_joiners_only_with_free_seats():
    def ids(full):
        return {generator.generate_loadsheet_extras(DG, LMC, {"probability": 100}, full_flight=full)[1]["id"]
                for _ in range(400)}
    full, free = ids(True), ids(False)
    assert "lmc-08" in full and "lmc-02" not in full
    assert "lmc-08" not in free and "lmc-02" in free
    assert "lmc-08" not in ids(None)   # load unknown: never oversold


def test_lmc_chance_zero_means_none():
    assert all(generator.generate_loadsheet_extras(DG, LMC, {"probability": 0})[1] is None for _ in range(100))


def test_pax_never_exceed_seats_and_reassignment_keeps_the_load_factor():
    for _ in range(500):
        pax, lf, cargo = generator.pax_and_cargo(189, 150)
        assert 0 < pax <= 189 and 0 < lf <= 1 and cargo > 0
    pax, lf, _ = generator.pax_and_cargo(235, 150, load_factor=0.9)
    assert (pax, lf) == (212, 0.9)


def test_round_trip_pairing():
    routes = [
        {"flight_number": "FR100", "departure_icao": "EGKK", "arrival_icao": "LEPA", "carrier": "RYR"},
        {"flight_number": "FR101", "departure_icao": "LEPA", "arrival_icao": "EGKK", "carrier": "RYR"},
        {"flight_number": "FR109", "departure_icao": "LEPA", "arrival_icao": "EGKK", "carrier": "RYR"},  # too far apart
        {"flight_number": "U2102", "departure_icao": "LEPA", "arrival_icao": "EGKK", "carrier": "EZY"},  # other carrier
    ]
    assert generator.find_round_trip_pairs(routes) == {"FR100": "FR101", "FR101": "FR100"}
    assert generator.flight_number_digits("U28391") == "8391"
