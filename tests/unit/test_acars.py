"""Hoppie ACARS client and routes, against a fake Hoppie (no network)."""
import pytest
import requests

import acars


class Answer:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


@pytest.fixture
def hoppie(monkeypatch):
    """Answers each request with the next queued reply; records the requests."""
    calls, replies = [], []

    def post(url, data=None, timeout=None):
        calls.append(data)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return Answer(reply)
    monkeypatch.setattr(acars.requests, "post", post)
    return calls, replies


def test_ping_checks_the_code_and_lists_aircraft_online(hoppie):
    calls, replies = hoppie
    replies.append("ok {EZY45KP}")
    assert acars.ping("ABC123xyz", ["RYR7K", "EZY45KP"]) == ["EZY45KP"]
    assert calls[0]["logon"] == "ABC123xyz" and calls[0]["type"] == "ping" and calls[0]["to"] == "SERVER"


def test_hoppie_errors_become_plain_messages(hoppie):
    _, replies = hoppie
    replies += ["error {illegal logon code}", "error {callsign not online}", requests.ConnectionError("down")]
    with pytest.raises(acars.AcarsError, match="doesn't recognise this logon code"):
        acars.ping("WRONG1234")
    with pytest.raises(acars.AcarsError, match="callsign not online"):
        acars.send_telex("ABC123xyz", "EZY45KP", "LOADSHEET")
    with pytest.raises(acars.AcarsError, match="can't be reached"):
        acars.ping("ABC123xyz")


def test_telex_goes_to_a_valid_callsign_only(hoppie):
    calls, replies = hoppie
    with pytest.raises(acars.AcarsError):
        acars.send_telex("ABC123xyz", "EZY 45/KP", "x")
    replies.append("ok")
    acars.send_telex("ABC123xyz", "ezy45kp", "LOADSHEET EDNO 01")
    assert calls == [{"logon": "ABC123xyz", "from": acars.STATION, "to": "EZY45KP", "type": "telex", "packet": "LOADSHEET EDNO 01"}]


def test_logon_codes_are_checked_and_masked():
    assert acars.valid_logon("abcDEF123") and not acars.valid_logon("abc") and not acars.valid_logon("abc def 123")
    assert acars.mask("abcDEF123").endswith("123") and "abcDEF" not in acars.mask("abcDEF123")


def test_messages_are_cleaned_to_plain_acars_text():
    text = "Loadsheet final\nZFW  58190 <b>€</b>\n" + "x" * 60 + "\n\n\n"
    assert acars.clean_message(text).split("\n") == ["LOADSHEET FINAL", "ZFW  58190  B   /B", "X" * acars.MAX_LINE]
    assert len(acars.clean_message("A\n" * 100).split("\n")) == acars.MAX_LINES


def test_acars_routes_need_the_database(client):
    assert client.get("/acars/settings").status_code == 502
    assert client.post("/acars/test").status_code == 502
    assert client.post("/acars/send", json={"callsign": "EZY45KP", "text": "X"}).status_code == 502


@pytest.fixture
def saved(app_module, monkeypatch):
    """The pilots' stored ACARS settings, in memory instead of Postgres."""
    store = {}
    monkeypatch.setattr(app_module.db, "db_available", lambda: True)
    monkeypatch.setattr(app_module.db, "get_acars", lambda user: dict(store.get(user, {})))
    monkeypatch.setattr(app_module.db, "save_acars", lambda data, user: store.__setitem__(user, dict(data)))
    return store


def test_logon_codes_are_stored_encrypted(client, app_module, saved):
    status = client.post("/acars/settings", json={"logon": "ABC123xyz"}).get_json()
    assert status["configured"] and status["hint"].endswith("xyz")
    assert "ABC123xyz" not in saved["local"]["logon"] and app_module._hoppie_logon("local") == "ABC123xyz"
    client.post("/acars/settings", json={"logon": ""})
    assert saved["local"] == {} and not client.get("/acars/settings").get_json()["configured"]


def test_a_code_saved_before_encryption_is_encrypted_when_read(app_module, saved):
    saved["local"] = {"logon": "OLDPLAIN99"}
    assert app_module._hoppie_logon("local") == "OLDPLAIN99"
    assert saved["local"]["logon"] != "OLDPLAIN99" and app_module._hoppie_logon("local") == "OLDPLAIN99"


def test_a_code_sealed_with_another_secret_reads_as_not_set(client, saved):
    import sealing
    saved["local"] = {"logon": sealing.SecretBox("an old secret", "hoppie-logon").seal("ABC123xyz")}
    assert client.get("/acars/settings").get_json()["configured"] is False
