"""SimBrief's API v2 with the pilot's Navigraph sign-in: the client, and the
admin's download of their own latest plan, and reading the OFP through
the API (no network, no database). The API sample is a real answer with
the pilot's name, IDs and links removed."""
import json
import os

import pytest
import requests

import auth
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


def sign_in(client, app_module, monkeypatch, email):
    monkeypatch.setattr(auth, "auth_available", lambda: True)
    monkeypatch.setattr(auth, "email_allowed", lambda email, extra=(): True)
    monkeypatch.setattr(auth, "ADMIN_EMAILS", {"boss@example.com"})
    with client.session_transaction() as s:
        s["user"] = {"id": "u1", "email": email}
        s["expires_at"] = app_module._now_ts() + 3600


def test_only_an_admin_downloads_their_own_latest_plan(client, app_module, monkeypatch, fake_simbrief):
    calls, replies = fake_simbrief
    monkeypatch.setattr(app_module.db, "db_available", lambda: True)
    tokens_for = []
    monkeypatch.setattr(app_module, "navigraph_access_token", lambda user: tokens_for.append(user) or "acc-1")

    assert client.get("/admin/api/simbrief/latest").status_code == 403          # no accounts: no admin
    sign_in(client, app_module, monkeypatch, "pilot@example.com")
    assert client.get("/admin/api/simbrief/latest").status_code == 403          # not on the admin list
    assert calls == []

    sign_in(client, app_module, monkeypatch, "boss@example.com")
    replies.append(Answer({"origin": {"icao_code": "EGKK"}, "navlog": {"fix": []}}))
    resp = client.get("/admin/api/simbrief/latest")
    assert resp.status_code == 200 and tokens_for == ["u1"]
    assert resp.headers["Content-Disposition"].startswith('attachment; filename="simbrief-latest-')
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.get_json()["origin"]["icao_code"] == "EGKK"

    replies.append(Answer({}, 404))
    resp = client.get("/admin/api/simbrief/latest")
    assert resp.status_code == 502 and "No flight plan" in resp.get_json()["error"]

    monkeypatch.setattr(app_module, "navigraph_access_token", lambda user: (_ for _ in ()).throw(
        navigraph.NavigraphError("Sign in with Navigraph in Account settings first", reconnect=True)))
    resp = client.get("/admin/api/simbrief/latest")
    assert resp.status_code == 409 and "Sign in with Navigraph" in resp.get_json()["error"]


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
    assert got["notams"] and {n["icao"] for n in got["notams"]} <= {"LIRN", "LIML"}
    # The browser asks again every few seconds while it waits: one call to SimBrief
    client.get("/simbrief/ofp?username=someone")
    assert len(calls) == 1


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
