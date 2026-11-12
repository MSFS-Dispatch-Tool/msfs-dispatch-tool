"""The SimBrief plan's settings: the destination alternate offered from the
airline's own airports (alternates.py), the airline's OFP layout, and the
pilot's OFP options (/simbrief/plan-options), as they reach SimBrief."""
from urllib.parse import parse_qs, urlparse

import requests

import alternates
import simbrief_api

AIRPORTS = {
    "EGAC": {"short_name": "Belfast City", "city": "Belfast", "lat": 54.6181, "lon": -5.8725},
    "EGAA": {"short_name": "Belfast International", "city": "Belfast", "lat": 54.6575, "lon": -6.2158},   # 12 NM
    "EGPF": {"short_name": "Glasgow", "city": "Glasgow", "lat": 55.8719, "lon": -4.4331},                # 90 NM
    "EGAE": {"short_name": "Derry", "city": "Derry, Derry and Strabane", "lat": 55.0428, "lon": -7.1611}, # 51 NM
    "EGKK": {"short_name": "Gatwick", "city": "London", "lat": 51.1481, "lon": -0.1903},                 # 290 NM
}


def test_only_the_airlines_bases_are_offered_nearest_first():
    departures = {"EGAC": 40, "EGAA": 100, "EGPF": 80, "EGAE": 5, "EGKK": 300}     # Derry isn't a base
    got = alternates.alternates("EGAC", departures, AIRPORTS)
    assert [a["icao"] for a in got] == ["EGAA", "EGPF"]
    assert got[0] == {"icao": "EGAA", "name": "Belfast International", "nm": 12}


def test_a_far_destination_looks_further_and_an_isolated_one_gets_none():
    assert [a["icao"] for a in alternates.alternates("EGAC", {"EGKK": 10}, AIRPORTS)] == ["EGKK"]   # 290 NM
    assert alternates.alternates("EGAC", {"EGAC": 10}, AIRPORTS) == []
    assert alternates.alternates("XXXX", {"EGKK": 10}, AIRPORTS) == []


def test_simbrief_layouts_are_read_from_its_list_once_a_day(monkeypatch):
    calls = []

    class Answer:
        def raise_for_status(self):
            pass

        def json(self):
            return {"layouts": {"LIDO": {"id": "LIDO", "name_short": "LIDO"}, "EZY": {"id": "EZY", "name_short": "easyJet"}}}

    monkeypatch.setattr(simbrief_api, "_layouts", {"at": 0.0, "codes": set()})
    monkeypatch.setattr(simbrief_api.requests, "get", lambda url, **kw: calls.append(url) or Answer())
    assert simbrief_api.plan_format("EZY") == "EZY" and simbrief_api.plan_format("RYR") == "LIDO"
    assert calls == [simbrief_api.LAYOUTS_URL]


def test_without_the_layout_list_plans_use_lido(monkeypatch):
    def down(url, **kw):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(simbrief_api, "_layouts", {"at": 0.0, "codes": set()})
    monkeypatch.setattr(simbrief_api.requests, "get", down)
    assert simbrief_api.plan_format("EZY") == "LIDO"


BASE = "/simbrief/redirect-url?civalue=10&pax=150&taxi_out_minutes=10"


def sent(client, query):
    return {k: v[0] for k, v in parse_qs(urlparse(client.get(BASE + query).get_json()["url"]).query).items()}


def test_the_plan_gets_the_airline_layout_an_alternate_and_the_default_ofp_options(client, app_module):
    got = sent(client, "&flights=U2765&aircraft_type=320")
    first = app_module.leg_alternates(app_module.routes_by_flight_number["U2765"])[0]["icao"]
    assert got["planformat"] == "EZY" and got["altn"] == first
    assert got["units"] == "KGS" and got["maps"] == "detail"
    assert (got["stepclimbs"], got["navlog"], got["etops"], got["tlr"], got["notams"], got["firnot"]) == ("0", "1", "1", "1", "1", "1")
    ryanair = next(r["flight_number"] for r in app_module.routes if r["carrier"] == "RYR")
    assert sent(client, f"&flights={ryanair}&aircraft_type=738")["planformat"] == "LIDO"     # no RYR layout in the list


def test_the_pilots_choices_reach_simbrief(client):
    got = sent(client, "&flights=U2765&aircraft_type=320&altn=EGPF&units=LBS&maps=0&stepclimbs=1&notams=0")
    assert (got["altn"], got["units"], got["maps"], got["stepclimbs"], got["notams"]) == ("EGPF", "LBS", "none", "1", "0")
    assert sent(client, "&flights=U2765&aircraft_type=320&altn=EGAC")["altn"] != "EGAC"     # never the destination
    assert "altn" not in sent(client, "&flights=U2765&aircraft_type=320&altn=AUTO")         # SimBrief chooses


def test_plan_options_offer_alternates_and_keep_the_pilots_ofp_options(client, app_module, monkeypatch):
    stored = {}
    monkeypatch.setattr(app_module.db, "db_available", lambda: True)
    monkeypatch.setattr(app_module.db, "get_simbrief", lambda user: dict(stored))
    monkeypatch.setattr(app_module.db, "save_simbrief", lambda p, user: stored.update(p))
    got = client.get("/simbrief/plan-options?flight=U2765").get_json()
    assert got["alternates"][0]["icao"] == "EGAA" and got["options"]["units"] == "KGS" and not got["options"]["stepclimbs"]
    assert got["layout"] == "EZY"
    client.post("/simbrief/plan-options", json={"options": {"units": "LBS", "stepclimbs": True, "maps": False}})
    got = client.get("/simbrief/plan-options?flight=U2765").get_json()["options"]
    assert (got["units"], got["stepclimbs"], got["maps"], got["notams"]) == ("LBS", True, False, True)
    assert client.get("/simbrief/plan-options?flight=XX1").status_code == 400
