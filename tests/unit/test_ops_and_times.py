"""Crew duty, curfews, ATFM slots, schedule times and logbook rows."""
from datetime import date, datetime, timezone

import pytest

import flightstats
import ops
import timeutils


@pytest.mark.parametrize("report, sectors, expected", [
    ("06:00", 2, 13 * 60), ("05:00", 1, 12 * 60), ("05:20", 2, 12 * 60 + 15), ("13:29", 2, 13 * 60),
    ("13:30", 2, 12 * 60 + 45), ("16:59", 2, 11 * 60 + 15), ("17:00", 2, 11 * 60), ("02:00", 2, 11 * 60),
    ("06:00", 4, 12 * 60), ("17:00", 10, 9 * 60),
])
def test_easa_max_fdp_table(report, sectors, expected):
    h, m = map(int, report.split(":"))
    assert ops.max_fdp_minutes(h * 60 + m, sectors) == expected


def test_crew_duty_reports_45_minutes_before_off_block_in_local_time():
    duty = ops.crew_duty(datetime(2026, 7, 1, 5, 0, tzinfo=timezone.utc), "Europe/London", 2)
    assert duty["report_utc"] == "04:15Z" and duty["report_local"] == "05:15"
    assert duty["max_fdp_minutes"] == 12 * 60 + 15


def test_atfm_slot():
    dep = {"icao": "EGKK", "lat": 51.15, "lon": -0.19}
    arr = {"icao": "EGAC", "lat": 54.62, "lon": -5.87}
    slot = ops.atfm_slot({"iata_code": "81"}, dep, arr, "14:52Z")
    assert slot["ctot"] == "14:52Z" and slot["window"] == [-5, 10] and "ACC" in slot["where"]
    assert ops.atfm_slot({"iata_code": "83"}, dep, arr, "14:52Z")["where"] == "EGAC arrivals"
    assert ops.atfm_slot({"iata_code": "17"}, dep, arr, "14:52Z") is None


def test_curfews_in_zulu_for_the_legs_date():
    out = ops.leg_curfews("EGKK", "EGAC", {"EGAC": "Europe/London", "EGKK": "Europe/London"}, date(2026, 7, 1))
    belfast = next(c for c in out if c["icao"] == "EGAC")
    assert belfast["kind"] == "arrival" and belfast["until_utc"] == "22:00Z"   # 23:00 BST


def test_scheduled_arrival_rolls_past_midnight():
    route = {"departure_icao": "EGKK", "arrival_icao": "GCLP", "scheduled_departure_local": "21:00",
             "scheduled_arrival_local": "00:30", "duration_minutes": 270}
    tz = {"EGKK": "Europe/London", "GCLP": "Atlantic/Canary"}
    dep, arr, source = timeutils.resolve_leg_times(route, tz, date(2026, 7, 1))
    assert source == "scraped" and arr > dep
    assert timeutils.format_zulu(arr, dep.date()) == "23:30Z"


def test_turnaround_shift_and_day_suffix():
    a = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
    b = datetime(2026, 7, 1, 10, 20, tzinfo=timezone.utc)
    assert timeutils.turnaround_shift(a, b).total_seconds() == 10 * 60
    assert timeutils.format_zulu(datetime(2026, 7, 2, 0, 10, tzinfo=timezone.utc), date(2026, 7, 1)) == "00:10Z (+1d)"
    assert timeutils.simbrief_date_str(date(2026, 9, 21)) == "21SEP26"


def test_logbook_row_derivations():
    record = {
        "id": "1", "flight_number": "U28001", "callsign": "EZY81A", "departure_icao": "EGKK", "arrival_icao": "LEPA",
        "flight_date": "2026-07-01",
        "leg": {"sobt": "23:50Z", "sibt": "01:55Z (+1d)", "pax_count": 180, "seat_capacity": 186, "carrier": "EZY",
                "delay": {"iata_code": "81", "description": "ATFM"}},
        "ofp": {"weight_unit": "lbs", "block_fuel": "13000"},
        "pirep": {"aobt": "00:05", "atot": "00:15", "aldt": "02:00", "abit": "02:08",
                  "delay_codes": [{"code": "63", "minutes": 5}, {"code": "81", "minutes": 10}]},
    }
    airport = {"EGKK": {"lat": 51.15, "lon": -0.19}, "LEPA": {"lat": 39.55, "lon": 2.74}}.get
    row = flightstats.logbook_row(record, airport)
    assert row["block"] == 123 and row["air"] == 105 and row["dep_delay"] == 15   # across midnight
    assert row["delay_code"] == "81" and row["delay_coded"] is True                 # the pilot's main code
    assert row["block_fuel"] == round(13000 * flightstats.LB_TO_KG)
    assert 690 < row["dist"] < 720
