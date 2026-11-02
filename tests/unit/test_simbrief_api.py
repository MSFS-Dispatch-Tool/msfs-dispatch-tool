"""SimBrief's API v2 with the pilot's Navigraph sign-in: the client, and the
admin's download of their own latest plan (no network, no database)."""
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
