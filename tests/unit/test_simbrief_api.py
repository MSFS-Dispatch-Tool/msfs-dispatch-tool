"""SimBrief's API v2 with the pilot's Navigraph sign-in: the client, reading
the OFP, and SEND TO SIMBRIEF generating the plan with the pilot's saved
airframe (no network, no database). The API sample is a real answer with
the pilot's name, IDs and links removed."""
import json
import os

import pytest
import requests

import navigraph
import simbrief_api


class Answer:
    def __init__(self, body=None, status=200):
        self.body, self.status_code = body, status

    def json(self):
        if self.body is None:
            raise ValueError("not JSON")
        return self.body


@pytest.fixture
def fake_simbrief(monkeypatch):
    calls, replies = [], []

    def get(url, headers=None, timeout=None):
        calls.append((url, headers))
        reply = replies.pop(0) if replies else Answer({}, 200)
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(simbrief_api.requests, "get", get)
    return calls, replies


def test_latest_plan_is_asked_for_with_the_pilots_token(fake_simbrief):
    calls, replies = fake_simbrief
    replies.append(Answer({"params": {"request_id": "123"}, "origin": {"icao_code": "EGKK"}}))
    assert simbrief_api.latest_plan("acc-1")["origin"]["icao_code"] == "EGKK"
    url, headers = calls[0]
    assert url == "https://api.simbrief.com/v2/flightplans/latest"
    assert headers == {"Authorization": "Bearer acc-1", "Accept": "application/json"}


def test_refusals_become_plain_messages(fake_simbrief):
    _, replies = fake_simbrief
    replies += [Answer({}, 401), Answer({}, 404), Answer({}, 500), Answer(None, 200), requests.ConnectionError("down")]
    with pytest.raises(simbrief_api.SimBriefError) as refused:
        simbrief_api.latest_plan("acc-1")
    assert refused.value.reconnect
    for expected in ("No flight plan", "HTTP 500", "couldn't read", "can't be reached"):
        with pytest.raises(simbrief_api.SimBriefError, match=expected) as err:
            simbrief_api.latest_plan("acc-1")
        assert not err.value.reconnect


# ---- Reading the OFP: through the API for pilots signed in with Navigraph ----

API_SAMPLE = json.load(open(os.path.join(os.path.dirname(__file__), "..", "fixtures", "simbrief_api_latest.json")))


@pytest.fixture
def connected(app_module, monkeypatch):
    """A pilot signed in with Navigraph, with a current access token."""
    monkeypatch.setattr(app_module.db, "db_available", lambda: True)
    monkeypatch.setattr(app_module.db, "get_navigraph", lambda user: {"refresh": "sealed"})
    monkeypatch.setattr(navigraph, "CLIENT_ID", "vd-test")
    monkeypatch.setattr(navigraph, "CLIENT_SECRET", "s3cret")
    monkeypatch.setattr(app_module, "navigraph_access_token", lambda user: "acc-1")
    app_module._simbrief_api_cache.clear()


def test_a_connected_pilots_ofp_comes_through_the_api(client, app_module, connected, fake_simbrief):
    calls, replies = fake_simbrief
    replies.append(Answer(API_SAMPLE))
    got = client.get("/simbrief/ofp?username=someone").get_json()
    assert got["source"] == "api" and calls[0][0].endswith("/v2/flightplans/latest")
    assert (got["origin_icao"], got["destination_icao"], got["callsign"]) == ("LIRN", "LIML", "EJU111")
    assert got["icao_type"] == "A320" and got["registration"] == "OE-IDD"     # "icao_code" in the API
    assert got["est_time_enroute_sec"] == str(1 * 3600 + 4 * 60 + 56)        # "01:04:56" in the API
    assert got["weight_unit"] == "kgs" and got["est_zfw"] == "54669" and got["trip_fuel"] == "2553"
    assert len(got["navlog"]) == 25 and got["navlog"][0]["ident"] == "VUNHO"
    assert (got["dep_rwy"], got["arr_rwy"], got["generated_at"]) == ("06", "35", "2026-11-02T18:33:16Z")
    assert got["notams"] and {n["icao"] for n in got["notams"]} <= {"LIRN", "LIML"}
    # The browser asks again every few seconds while it waits: one call to SimBrief
    client.get("/simbrief/ofp?username=someone")
    assert len(calls) == 1


def test_the_generation_time_reads_from_either_source(app_module):
    assert app_module._generated_at("1793644396") == "2026-11-02T18:33:16Z"      # Unix time, fetch-back endpoint
    assert app_module._generated_at("2026-11-02T18:33:16Z") == "2026-11-02T18:33:16Z"
    assert app_module._generated_at({}) == "" and app_module._generated_at("soon") == ""


def test_no_username_needed_when_signed_in_with_navigraph(client, connected, fake_simbrief):
    _, replies = fake_simbrief
    replies.append(Answer(API_SAMPLE))
    assert client.get("/simbrief/ofp").get_json()["source"] == "api"


def test_the_username_is_the_fallback_when_the_api_fails(client, app_module, connected, monkeypatch, fake_simbrief):
    _, replies = fake_simbrief
    replies.append(Answer({}, 500))
    monkeypatch.setattr(app_module, "_simbrief_latest", lambda username: {"general": {"route": "SAM L620"}, "aircraft": {"icaocode": "B738"}})
    got = client.get("/simbrief/ofp?username=someone").get_json()
    assert got["source"] == "username" and got["route"] == "SAM L620" and got["icao_type"] == "B738"


def test_pilots_not_signed_in_with_navigraph_keep_the_username_route(client, app_module, monkeypatch, fake_simbrief):
    calls, _ = fake_simbrief
    monkeypatch.setattr(app_module, "_simbrief_latest", lambda username: {"times": {"est_time_enroute": "3896"}})
    got = client.get("/simbrief/ofp?username=someone").get_json()
    assert got["source"] == "username" and got["est_time_enroute_sec"] == "3896" and calls == []
    assert client.get("/simbrief/ofp").status_code == 400


# ---- SEND TO SIMBRIEF through the API, with the pilot's saved airframe ----

class Posted(Answer):
    def __init__(self, body=None, status=200):
        super().__init__(body, status)


@pytest.fixture
def fake_generate(monkeypatch):
    calls, replies = [], []

    def post(url, data=None, headers=None, timeout=None):
        calls.append({"url": url, "data": data, "headers": headers})
        return replies.pop(0) if replies else Posted(API_SAMPLE, 200)
    monkeypatch.setattr(simbrief_api.requests, "post", post)
    return calls, replies


AIRFRAMES = [
    {"airframe_internal_id": "100_1", "airframe_icao": "A320", "airframe_name": "FENIX A320", "airframe_registration": "OE-IDD", "airframe_comments": "EZY - A320 CFM SL"},
    {"airframe_internal_id": "100_2", "airframe_icao": "A320", "airframe_name": "FENIX A320", "airframe_registration": "EI-DTE", "airframe_comments": "ITA - A320 CFM"},
    {"airframe_internal_id": "100_3", "airframe_icao": "B738", "airframe_name": "B737-800", "airframe_registration": "EI-DPN", "airframe_comments": "RYR - B738 HD BW"},
    {"airframe_internal_id": "not an id", "airframe_icao": "A320"},
]
# An easyJet A320 leg, Gatwick to Belfast City, off-block 14:05Z
LEG = {"flights": "U2765", "aircraft_type": "320", "civalue": 12, "pax": 160, "taxi_out_minutes": 12,
       "eobt": "14:05Z", "callsign": "EZY45KP", "static_id": "simdispatch-1-0", "remarks": "MEL 21-01-01 LEFT PACK INOP"}


@pytest.fixture
def generating(app_module, connected, monkeypatch):
    """Generation through the API switched on for everyone, and the pilot's
    SimBrief preferences in memory."""
    prefs = {}
    monkeypatch.setattr(app_module, "SIMBRIEF_API_GENERATE", "all")
    monkeypatch.setattr(app_module.db, "get_simbrief", lambda user: dict(prefs))
    monkeypatch.setattr(app_module.db, "save_simbrief", lambda p, user: prefs.update(p))
    app_module._simbrief_airframes_cache.clear()
    app_module._simbrief_last_generate.clear()
    return prefs


def test_the_departure_time_goes_inside_the_date():
    from datetime import datetime
    assert simbrief_api.departure_date(datetime(2026, 11, 2, 14, 5)) == "2 Nov 2026 - 14:05"


def test_generation_refusals_become_plain_messages(fake_generate):
    _, replies = fake_generate
    replies += [Posted({}, 401), Posted({"error": "Invalid aircraft type"}, 400), Posted(None, 500)]
    with pytest.raises(simbrief_api.SimBriefError) as refused:
        simbrief_api.generate("acc-1", {"orig": "EGKK"})
    assert refused.value.reconnect
    with pytest.raises(simbrief_api.SimBriefError, match="Invalid aircraft type"):
        simbrief_api.generate("acc-1", {"orig": "EGKK"})
    with pytest.raises(simbrief_api.SimBriefError, match="HTTP 500"):
        simbrief_api.generate("acc-1", {"orig": "EGKK"})


def test_the_picker_lists_the_pilots_airframes_of_the_legs_type(client, app_module, generating, fake_simbrief):
    calls, replies = fake_simbrief
    replies.append(Answer(AIRFRAMES))
    got = client.get("/simbrief/airframes?flight=U2765&aircraft_type=320").get_json()
    assert calls[-1][0].endswith("/v2/airframes/user/mine")
    assert got["available"] and got["type"] == "A320"
    assert [a["reg"] for a in got["airframes"]] == ["OE-IDD", "EI-DTE"]          # the B738 and the bad entry left out
    assert got["selected"] == "100_1"                                           # its comment starts with EZY
    # A choice made before for easyJet A320s wins, "" (SimBrief's standard one) included
    generating["airframes"] = {"EZY:A320": "100_2"}
    assert client.get("/simbrief/airframes?flight=U2765&aircraft_type=320").get_json()["selected"] == "100_2"
    generating["airframes"] = {"EZY:A320": ""}
    assert client.get("/simbrief/airframes?flight=U2765&aircraft_type=320").get_json()["selected"] == ""
    assert len(calls) == 1                                                      # the list is cached for a few minutes


def test_without_saved_airframes_the_standard_aircraft_is_used(client, app_module, generating, fake_simbrief):
    _, replies = fake_simbrief
    replies.append(Answer({}, 404))
    got = client.get("/simbrief/airframes?flight=U2765&aircraft_type=320").get_json()
    assert got["available"] and got["airframes"] == [] and got["selected"] == ""


def test_generation_is_off_without_navigraph_and_for_pilots_while_admins_try_it(client, app_module, connected, monkeypatch):
    assert client.get("/simbrief/airframes?flight=U2765").get_json() == {"available": False}   # admins only by default
    assert client.post("/simbrief/generate", json=LEG).get_json()["fallback"]
    monkeypatch.setattr(app_module, "SIMBRIEF_API_GENERATE", "all")
    monkeypatch.setattr(app_module.db, "get_navigraph", lambda user: {})                     # not signed in
    assert client.get("/simbrief/airframes?flight=U2765").get_json() == {"available": False}


def test_send_to_simbrief_generates_the_plan_with_the_chosen_airframe(client, app_module, generating, fake_generate, fake_simbrief):
    calls, _ = fake_generate
    got = client.post("/simbrief/generate", json={**LEG, "airframe": "100_2"}).get_json()
    sent = calls[-1]
    assert sent["url"].endswith("/v2/plan/generate") and sent["headers"]["Authorization"] == "Bearer acc-1"
    assert sent["data"]["type"] == "100_2" and sent["data"]["orig"] == "EGKK" and sent["data"]["dest"] == "EGAC"
    assert sent["data"]["date"].endswith(" - 14:05") and "deph" not in sent["data"] and "depm" not in sent["data"]
    assert sent["data"]["static_id"] == "simdispatch-1-0" and sent["data"]["callsign"] == "EZY45KP"
    assert sent["data"]["manualrmk"] == "MEL 21-01-01 LEFT PACK INOP"
    assert got["source"] == "api" and got["origin_icao"] == "LIRN" and len(got["navlog"]) == 25
    assert generating["airframes"] == {"EZY:A320": "100_2"}                     # remembered for easyJet A320s
    # The OFP just made is what a check right after reads
    assert client.get("/simbrief/ofp").get_json()["source"] == "api" and fake_simbrief[0] == []


def test_a_refused_generation_falls_back_to_simbrief_com(client, app_module, generating, fake_generate):
    _, replies = fake_generate
    replies.append(Posted({"error": "Invalid aircraft type"}, 400))
    resp = client.post("/simbrief/generate", json={**LEG, "airframe": "100_9"})
    assert resp.status_code == 502 and resp.get_json()["fallback"] and "Invalid aircraft type" in resp.get_json()["error"]
    assert generating == {}                                                     # nothing remembered
    app_module._simbrief_last_generate.clear()
    assert client.post("/simbrief/generate", json={**LEG, "airframe": "../etc"}).status_code == 400
    # One plan at a time: a second request right away is turned down
    client.post("/simbrief/generate", json=LEG)
    assert client.post("/simbrief/generate", json=LEG).status_code == 429
