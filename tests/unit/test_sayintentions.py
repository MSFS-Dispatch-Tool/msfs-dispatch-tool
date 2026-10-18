"""SayIntentions as the ACARS network: the client, the pilot's settings and
SEND TO AIRCRAFT, against a fake SayIntentions (no network, no database)."""
import contextlib

import pytest
import requests

import sayintentions


class Answer:
    def __init__(self, body=None, text="", status=200):
        self.body, self.text, self.status_code = body, text, status

    def json(self):
        if self.body is None:
            raise ValueError("not JSON")
        return self.body


@pytest.fixture
def si(monkeypatch):
    """A fake sayAs: the aircraft takes up to limit["n"] characters."""
    calls, limit = [], {"n": 255}

    def get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params, "headers": headers})
        if params["api_key"] != "PILOTKEY123":
            return Answer({"error": "Invalid API key"})
        if len(params["message"]) > limit["n"]:
            return Answer({"error": f"Message too long: {len(params['message'])} characters. Maximum is {limit['n']} characters."})
        return Answer(text="OK")
    monkeypatch.setattr(sayintentions.requests, "get", get)
    monkeypatch.setattr(sayintentions, "DEVELOPER_KEY", "DEVKEY")
    return calls, limit


def test_a_telex_goes_to_the_pilots_aircraft_with_both_keys(si):
    calls, _ = si
    sayintentions.send_telex("PILOTKEY123", "LOADSHEET FINAL")
    assert calls[0]["url"] == sayintentions.URL and calls[0]["headers"] == {"X-Developer-Key": "DEVKEY"}
    assert calls[0]["params"] == {"api_key": "PILOTKEY123", "channel": "ACARS_IN", "message_type": "telex",
                                  "from": "VDOPS", "message": "LOADSHEET FINAL"}


def test_refusals_are_read_from_the_body_even_with_status_200(si):
    _, limit = si
    limit["n"] = 128
    with pytest.raises(sayintentions.MessageTooLong) as too_long:
        sayintentions.send_telex("PILOTKEY123", "X" * 200)
    assert too_long.value.maximum == 128
    with pytest.raises(sayintentions.SayIntentionsError, match="doesn't recognise this API key"):
        sayintentions.send_telex("WRONGKEY99", "X")


def test_other_failures_become_plain_messages(monkeypatch):
    replies = [requests.ConnectionError("down"), Answer(status=500, text="oops"),
               Answer({"error": "Developer key revoked"}), Answer({"error": "No active flight"})]

    def get(*args, **kwargs):
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(sayintentions.requests, "get", get)
    for expected in ("can't be reached", "HTTP 500", "didn't accept VirtualDispatch", "No active flight"):
        with pytest.raises(sayintentions.SayIntentionsError, match=expected):
            sayintentions.send_telex("PILOTKEY123", "X")


@pytest.fixture
def pilot(app_module, monkeypatch):
    """The pilot's ACARS settings and active flight, in memory."""
    store = {"acars": {}}
    monkeypatch.setattr(app_module.db, "db_available", lambda: True)
    monkeypatch.setattr(app_module.db, "get_acars", lambda user: dict(store["acars"]))
    monkeypatch.setattr(app_module.db, "save_acars", lambda data, user: store.__setitem__("acars", dict(data)))
    monkeypatch.setattr(app_module.db, "get_active_flight", lambda user: {"flight": {"callsigns": ["EZY45KP"]}, "rev": 1})
    app_module._acars_last_send.clear()
    app_module._acars_last_test.clear()
    return store


FULL = "LOADSHEET FINAL\n" + "ZFW 58190 MAX 62500\n" * 15 + "END"          # longer than 255
V255 = "LOADSHEET FINAL 1432Z\n" + "ZFW 58190 MAX 62500\n" * 8 + "CAPT SIGNED"
V128 = "LOADSHEET FINAL EDNO 01\nEZY45KP EGKK-LEPA\nZFW 58190 TOW 64400\nLAW 61400 TOF 6200\nTIF 3000 UNDLD 5232\nPAX 180 CAPT SIGNED"


def use_sayintentions(client):
    assert client.post("/acars/sayintentions", json={"api_key": "PILOTKEY123"}).status_code == 200
    return client.post("/acars/network", json={"network": "sayintentions"}).get_json()


def send(client):
    return client.post("/acars/send", json={"callsign": "EZY45KP", "text": FULL, "variants": [V255, V128], "check_online": True})


def test_the_key_is_stored_encrypted_and_the_network_remembered(client, app_module, pilot, si):
    status = use_sayintentions(client)
    assert status["network"] == "sayintentions" and status["si_configured"] and status["si_hint"].endswith("123")
    stored = pilot["acars"]
    assert stored["network"] == "sayintentions" and "PILOTKEY123" not in stored["si_key"]
    assert app_module._si_key("local") == "PILOTKEY123"
    client.post("/acars/settings", json={"logon": "HOPPIE123"})            # a Hoppie code alongside
    client.post("/acars/sayintentions", json={"api_key": ""})
    assert "si_key" not in pilot["acars"] and pilot["acars"]["logon"] and pilot["acars"]["network"] == "sayintentions"
    assert client.post("/acars/sayintentions", json={"api_key": "short"}).status_code == 400


def test_sayintentions_is_off_without_the_developer_key(client, app_module, pilot, monkeypatch):
    monkeypatch.setattr(sayintentions, "DEVELOPER_KEY", "")
    assert client.post("/acars/network", json={"network": "sayintentions"}).status_code == 400
    pilot["acars"] = {"network": "sayintentions"}
    status = client.get("/acars/settings").get_json()
    assert status["network"] == "hoppie" and status["si_available"] is False


@pytest.mark.parametrize("aircraft_limit, expected, calls_made", [(500, FULL, 1), (255, V255, 2), (128, V128, 2)])
def test_one_message_the_longest_the_aircraft_takes(client, pilot, si, aircraft_limit, expected, calls_made):
    calls, limit = si
    use_sayintentions(client)
    limit["n"] = aircraft_limit
    resp = send(client).get_json()
    assert resp["ok"] and resp["network"] == "sayintentions" and resp["chars"] == len(expected)
    assert resp["lines"] == expected.count("\n") + 1
    delivered = [c["params"]["message"] for c in calls if len(c["params"]["message"]) <= aircraft_limit]
    assert delivered == [expected] and len(calls) == calls_made          # the refused try never reaches the aircraft


def test_an_aircraft_taking_less_than_every_version_is_reported(client, pilot, si):
    _, limit = si
    use_sayintentions(client)
    limit["n"] = 60
    resp = send(client)
    assert resp.status_code == 502 and "at most 60 characters" in resp.get_json()["error"]


def test_without_a_key_sending_points_to_settings(client, pilot, si):
    client.post("/acars/network", json={"network": "sayintentions"})
    resp = send(client).get_json()
    assert resp["no_logon"] and "SayIntentions API key" in resp["error"]


def test_the_sayintentions_test_sends_a_short_telex(client, pilot, si):
    calls, _ = si
    use_sayintentions(client)
    assert client.post("/acars/test", json={"network": "sayintentions"}).get_json()["ok"]
    assert calls[-1]["params"]["message"].startswith("VIRTUALDISPATCH")


def test_hoppie_pilots_are_unaffected(client, app_module, pilot, si, monkeypatch):
    calls, _ = si
    sent = []
    monkeypatch.setattr(app_module.acars, "ping", lambda code, callsigns=(): list(callsigns))
    monkeypatch.setattr(app_module.acars, "send_telex", lambda code, callsign, text: sent.append((code, callsign, text)))
    client.post("/acars/settings", json={"logon": "HOPPIE123"})
    resp = send(client).get_json()
    assert resp["ok"] and resp["network"] == "hoppie" and sent == [("HOPPIE123", "EZY45KP", FULL)] and calls == []
