"""Error monitoring: what reaches Sentry (through a fake transport, so
nothing leaves the machine) and the browser error endpoint."""
import logging

import pytest
import sentry_sdk
from sentry_sdk.transport import Transport

import monitoring


class Capture(Transport):
    def __init__(self, options=None):
        super().__init__(options)
        self.events = []

    def capture_envelope(self, envelope):
        event = envelope.get_event()
        if event:
            self.events.append(event)


@pytest.fixture
def sentry(app_module):
    transport = Capture()
    assert monitoring.init(dsn="https://key@o0.ingest.sentry.io/0", transport=transport)
    app_module._client_error_times.clear()
    yield transport.events
    sentry_sdk.init()   # off again for the other tests


def test_off_without_a_dsn():
    assert monitoring.init(dsn="") is False


def test_a_server_crash_is_reported_without_bodies_codes_variables_or_cookies(client, app_module, sentry, monkeypatch):
    # Built at run time, so the source lines Sentry quotes don't contain them
    hoppie, sign_in, cookie, bearer = ("".join(p) for p in (("MYHOP", "PIECODE"), ("sign-in", "-code"), ("secret", "-cookie"), ("Bear", "er abc")))

    def broken():
        sentry_sdk.add_breadcrumb(category="httplib", data={"url": f"https://www.simbrief.com/api/x?u={sign_in}",
                                                            "http.query": f"u={sign_in}"})
        logon_code = hoppie.lower()   # a variable in the crashing frame
        raise RuntimeError("boom in a route " + str(len(logon_code)))
    monkeypatch.setitem(app_module.app.view_functions, "acars_settings_save", broken)
    client.set_cookie("session", cookie)
    resp = client.post(f"/acars/settings?code={sign_in}", json={"logon": hoppie},
                       headers={"User-Agent": "TestBrowser/1.0", "Authorization": bearer})
    assert resp.status_code == 500
    sentry_sdk.flush()
    event = next(e for e in sentry if e.get("exception"))
    assert event["exception"]["values"][-1]["value"] == "boom in a route 12"
    raw = repr(event).lower()
    for secret in (hoppie, sign_in, cookie, bearer):
        assert secret.lower() not in raw
    assert event["request"]["headers"] == {"User-Agent": "TestBrowser/1.0"}
    assert event["request"]["url"].endswith("/acars/settings")


def test_browser_errors_are_forwarded_and_grouped(client, sentry):
    body = {"message": "TypeError: x is undefined", "source": "/static/js/app.js:120:7",
            "stack": "TypeError: x is undefined\n    at renderRecap (app.js:120:7)", "page": "/app"}
    assert client.post("/client-errors", json=body).status_code == 204
    sentry_sdk.flush()
    event = sentry[-1]
    assert event["message"] == "Browser: TypeError: x is undefined" and event["level"] == "error"
    assert event["tags"]["origin"] == "browser" and event["fingerprint"] == ["browser", "TypeError: x is undefined", "/static/js/app.js:120"]
    assert "renderRecap" in event["contexts"]["browser_error"]["stack"]


def test_browser_errors_are_capped_per_visitor(client, app_module, sentry):
    for i in range(25):
        client.post("/client-errors", json={"message": f"err {i}", "source": "/static/js/app.js:1:1"})
    sentry_sdk.flush()
    assert len([e for e in sentry if e.get("message", "").startswith("Browser:")]) == app_module.CLIENT_ERROR_MAX_PER_VISITOR


def test_without_sentry_browser_errors_go_to_the_log(client, app_module, caplog):
    app_module._client_error_times.clear()
    with caplog.at_level(logging.WARNING, logger="vd.monitoring"):
        client.post("/client-errors", json={"message": "ReferenceError: y", "source": "/static/js/landing.js:5:1", "page": "/"})
        client.post("/client-errors", data="not json")
    assert len(caplog.records) == 1 and "ReferenceError: y" in caplog.records[0].getMessage()
