"""The destination alternate's TAF against planning minima (taf.py):
400 ft and 1350 m from 1 hour before to 1 hour after the time there."""
from datetime import datetime, timedelta, timezone

import pytest

import taf

ETA = datetime(2026, 11, 12, 15, 30, tzinfo=timezone.utc)        # window 1430-1630Z
BASE = "TAF EGAA 121100Z 1212/1312 24010KT 9999 SCT020"


@pytest.mark.parametrize("raw, status, words", [
    (BASE, "ok", "ABOVE PLANNING MINIMA 1430-1630Z"),
    (BASE + " TEMPO 1214/1218 4000 RA BKN008", "ok", "ABOVE"),                     # 800 ft, 4000 m: above
    (BASE + " TEMPO 1214/1218 0800 FG OVC002", "below", "VIS 800 M (TEMPO 1400-1800Z), CEILING 200 FT"),
    (BASE + " PROB30 TEMPO 1214/1218 0800 FG OVC002", "caution", "PROB 1400-1800Z"),   # may be disregarded
    (BASE + " TEMPO 1214/1218 1200 +SHRA BKN009CB", "caution", "SHOWERS 1400-1800Z: VIS 1200 M"),
    (BASE + " BECMG 1215/1217 0300 FG VV001", "below", "VIS 300 M, CEILING 100 FT"),  # both while it changes
    (BASE + " BECMG 1217/1219 0300 FG VV001", "ok", "ABOVE"),                       # after the window
    (BASE + " FM121600 0500 FG OVC002", "below", "VIS 500 M"),
    ("TAF EGAA 121100Z 1212/1312 0300 FG VV001 FM121300 9999 SCT030", "ok", "ABOVE"),  # cleared before
    ("TAF KJFK 121130Z 1212/1318 22012KT P6SM SCT040 FM121500 1 1/2SM BR OVC003", "below", "CEILING 300 FT"),
    ("TAF EGAA 281100Z 2812/2918 9999 SCT040", "unknown", "TAF NOT VALID 1430-1630Z"),
    (None, "unknown", "NO TAF"),
])
def test_the_forecast_around_the_eta_is_checked_against_planning_minima(raw, status, words):
    got = taf.assess(raw, ETA)
    assert got["status"] == status and words in got["text"], got


def test_a_taf_across_the_end_of_the_month():
    got = taf.assess("TAF EGAA 302300Z 3100/0106 9999 SCT040 TEMPO 0102/0104 0400 FG",
                     datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc))
    assert got["status"] == "below" and "VIS 400 M (TEMPO 0200-0400Z)" in got["text"]


def test_plan_options_check_each_alternates_taf_at_its_eta(client, app_module, monkeypatch):
    now = datetime.now(timezone.utc)
    valid = f"{now:%d}00/{now + timedelta(days=1):%d}24"
    tafs = {"EGAA": f"TAF EGAA {now:%d}0000Z {valid} 9999 SCT030 TEMPO {valid} 0400 FG VV002",
            "EGPF": f"TAF EGPF {now:%d}0000Z {valid} 9999 FEW030"}
    monkeypatch.setattr(app_module, "fetch_weather_batch",
                        lambda icaos: {i: {"metar": None, "taf": tafs.get(i)} for i in icaos})
    got = {a["icao"]: a["wx"] for a in client.get("/simbrief/plan-options?flight=U2765&eldt=12:00Z").get_json()["alternates"]}
    assert got["EGAA"]["status"] == "below" and got["EGPF"]["status"] == "ok"
    assert got["EGPH"]["status"] == "unknown"                                     # no TAF
    assert "wx" not in client.get("/simbrief/plan-options?flight=U2765").get_json()["alternates"][0]   # no ETA


def test_leg_times_are_read_as_today_in_utc(app_module):
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    assert app_module._zulu_today("15:49Z") == today + timedelta(hours=15, minutes=49)
    assert app_module._zulu_today("00:20Z (+1d)") == today + timedelta(days=1, minutes=20)
    assert app_module._zulu_today("23:50Z (-1d)") == today - timedelta(minutes=10)
    assert app_module._zulu_today("") is None and app_module._zulu_today("1549") is None
