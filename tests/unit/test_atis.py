"""Runways in use from the pilot's network ATIS (atis.py) and the plan's
RUNWAYS choice (/atis/runways, then origrwy/destrwy to SimBrief). The
fixtures are real answers from VATSIM, IVAO and SayIntentions, cut down to
the ATIS (no names or member IDs); no network is used."""
import json
import os
from urllib.parse import parse_qs, urlparse

import pytest
import requests

import atis

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


# ---- Reading the runways out of ATIS text (real ATIS, 3 Nov 2026) ----
@pytest.mark.parametrize("text, dep, arr", [
    ("EGKK COM ATIS F. 1050Z. RWY IN USE 08R. TRANSITION-LEVEL FL 70.", "08R", "08R"),
    ("HEATHROW INFORMATION INDIA .. TIME 1050 .. ARRIVAL RUNWAY 09L DEPARTURE RUNWAY 09R .. RUNWAY CONDITION REPORT 6 6 6", "09R", "09L"),
    ("THIS IS ZURICH INFORMATION ALPHA AT 1050 .. DEPARTURE RUNWAY 28 .. ARRIVALS EXPECT ILS APPROACH RUNWAY 14 ..", "28", "14"),
    ("KXNA ATIS INFO Q 1053Z. 17011KT 10SM -RA OVC010 18/18 A2975. DEPTG RWY 16L. ARRS EXPT ILS APPROACH RWY 16L.", "16L", "16L"),
    ("ATIS EDHL T METAR 031050 EXPECT RADAR VECTORS ILS APCH RWY 07 WHEN AIRBORNE MONITOR ADVISORY 122.800", "07", "07"),
    ("THIS IS LEBL ATIS INFORMATION E. AT TIME 1100Z. RWY IN USE FOR DEPARTURES 06 RIGHT.. AFTER DEPARTURE CONTACT", "06R", "06R"),
    ("TRANSITION LEVEL 190 RWY 14R FOR ARRIVALS AND RWY 14L FOR DEPARTURES. EXPECT VECTORS", "14L", "14R"),
    ("1100Z OTHH DEP ATIS E. RWY IN USE 34 LEFT. DEPARTURES RWY 34 L , EXP LUSAIL 1. RWY 34 R UNAVAILABLE.", "34L", "34L"),
    ("ATIS YMML S 031112 [APCH] EXP ILS APCH [RWY] 16 +[WIND] 240/5 [WX] CAVOK", "16", "16"),
    ("ROME FIUMICINO, INFO SIERRA. 1050Z. ARVG RWY 34R. DPTG RWY 34L. RWYS 7 AND 25 CLSD.", "34L", "34R"),
    ("LONDON GATWICK ARPT, INFO OSCAR. 1050Z. ARVG AND DPTG RWY 08R. WIND 040 AT 16.", "08R", "08R"),
    ("Schiphol Information FOXTROT recorded at 1109z EHAM 031055Z 05013KT Q1032 ARR RWY 06 DEP RWY 36L TL FL040", "36L", "06"),
    ("Sochi ATIS Information INDIA EGKK 031100Z 25003MPS R02/010070 R06/28003MPS QFE760 ARR RWY 02 DEP RWY 24 TL", "24", "02"),
    ("Lille ARR RWY 08 DEP RWY 08 RMK RWY 01 AVBL FOR LIGHT ACFT / VACATE RWY 26 VIA B", "08", "08"),
])
def test_the_runways_are_read_from_real_atis(text, dep, arr):
    deps, arrs = atis.runways_in_text(text)
    assert (deps[0], arrs[0]) == (dep, arr)


def test_an_atis_without_a_runway_gives_none():
    assert atis.runways_in_text("FACT ATIS INFO B 1000Z. 30009KT 260V360 CAVOK 25/09 Q1014 NOSIG. VMC.") == ([], [])
    assert atis.runways_in_text("") == ([], [])


# ---- The networks ----
@pytest.fixture
def feeds(monkeypatch):
    """VATSIM, IVAO and SayIntentions answering with the fixtures."""
    atis._feeds.clear()
    calls = []
    answers = {atis.VATSIM_URL: fixture("atis_vatsim.json"), atis.IVAO_URL: fixture("atis_ivao.json"),
               atis.SAYINTENTIONS_URL: fixture("atis_sayintentions.json")}

    class Answer:
        status_code = 200

        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            pass

        def json(self):
            return self.body

    def get(url, params=None, headers=None, timeout=None):
        calls.append((url, params, headers))
        if isinstance(answers.get(url), Exception):
            raise answers[url]
        return Answer(answers[url])
    monkeypatch.setattr(atis.requests, "get", get)
    yield calls, answers
    atis._feeds.clear()


def test_vatsim_gives_each_airports_runway_with_its_atis_letter(feeds):
    got = atis.runways("vatsim", "EGKK", "EGLL")
    assert got["dep"] == {"rwy": "08R", "letter": "F", "station": "EGKK_ATIS"}
    assert got["arr"] == {"rwy": "09L", "letter": "I", "station": "EGLL_ATIS"}


def test_vatsim_uses_the_departure_and_arrival_atis_where_there_are_two(feeds):
    got = atis.runways("vatsim", "LEBL", "LEBL")
    assert (got["dep"]["station"], got["dep"]["rwy"]) == ("LEBL_D_ATIS", "06R")
    assert (got["arr"]["station"], got["arr"]["rwy"]) == ("LEBL_A_ATIS", "06L")


def test_an_airport_without_atis_gives_none_and_the_feed_is_fetched_once_a_minute(feeds):
    calls, _ = feeds
    got = atis.runways("vatsim", "EGKK", "EGAC")
    assert got["arr"] is None and got["dep"]["rwy"] == "08R"
    atis.runways("vatsim", "LSZH", "EGAC")
    assert len(calls) == 1


def test_ivao_reads_the_tower_before_ground_and_skips_the_voice_server_line(feeds):
    got = atis.runways("ivao", "URSS", "EHAM")
    assert got["dep"] == {"rwy": "24", "letter": "I", "station": "URSS_TWR"}
    assert got["arr"] == {"rwy": "06", "letter": "F", "station": "EHAM_A_TWR"}


def test_sayintentions_gives_its_active_runways_with_both_keys(feeds, monkeypatch):
    calls, _ = feeds
    monkeypatch.setattr(atis.sayintentions, "DEVELOPER_KEY", "dev-1")
    got = atis.runways("sayintentions", "EGKK", "EDDF", api_key="pilot-1")
    assert got["dep"]["rwy"] == "08R" and got["arr"]["rwy"] == "07R"      # "07R,07L" arriving at Frankfurt
    url, params, headers = calls[0]
    assert params == {"icao": "EDDF,EGKK", "api_key": "pilot-1"} and headers["X-Developer-Key"] == "dev-1"


def test_a_network_out_of_reach_says_so(feeds):
    _, answers = feeds
    answers[atis.VATSIM_URL] = requests.ConnectionError("down")
    with pytest.raises(atis.AtisError, match="VATSIM NOT AVAILABLE"):
        atis.runways("vatsim", "EGKK", "EGAC")


# ---- The plan's RUNWAYS choice ----
@pytest.fixture
def prefs(app_module, monkeypatch):
    stored = {}
    monkeypatch.setattr(app_module.db, "db_available", lambda: True)
    monkeypatch.setattr(app_module.db, "get_simbrief", lambda user: dict(stored))
    monkeypatch.setattr(app_module.db, "save_simbrief", lambda p, user: stored.update(p))
    monkeypatch.setattr(app_module, "_si_key", lambda user: "")
    return stored


def test_the_chosen_network_is_remembered_for_the_next_flights(client, prefs, feeds):
    got = client.get("/atis/runways?dep=EGKK&arr=EHAM").get_json()
    assert got["source"] == "vatsim" and got["dep"]["rwy"] == "08R"           # VATSIM until the pilot chooses
    got = client.get("/atis/runways?dep=URSS&arr=EHAM&source=ivao").get_json()
    assert got["source"] == "ivao" and got["arr"]["rwy"] == "06" and prefs["runway_source"] == "ivao"
    assert client.get("/atis/runways?dep=URSS&arr=EHAM").get_json()["source"] == "ivao"


def test_without_a_network_simbrief_picks_by_wind(client, prefs, feeds):
    calls, _ = feeds
    assert client.get("/atis/runways?dep=EGKK&arr=EHAM&source=none").get_json() == {"source": "none", "dep": None, "arr": None}
    assert calls == []


def test_unknown_airports_and_networks_are_refused(client, prefs):
    assert client.get("/atis/runways?dep=EGK&arr=EHAM").status_code == 400
    assert client.get("/atis/runways?dep=EGKK&arr=EHAM&source=pilotedge").status_code == 400


def test_a_network_out_of_reach_leaves_the_runways_to_simbrief(client, prefs, feeds):
    _, answers = feeds
    answers[atis.VATSIM_URL] = requests.ConnectionError("down")
    got = client.get("/atis/runways?dep=EGKK&arr=EHAM").get_json()
    assert got["dep"] is None and got["arr"] is None and "NOT AVAILABLE" in got["error"]


def test_the_runways_go_to_simbrief_as_origrwy_and_destrwy(client):
    base = "/simbrief/redirect-url?flights=U2803&civalue=10&pax=150&aircraft_type=320&taxi_out_minutes=10"
    url = client.get(base + "&dep_rwy=08R&arr_rwy=22").get_json()["url"]
    q = parse_qs(urlparse(url).query)
    assert q["origrwy"] == ["08R"] and q["destrwy"] == ["22"]
    url = client.get(base + "&dep_rwy=8R&arr_rwy=37").get_json()["url"]
    assert "origrwy" not in url and "destrwy" not in url
