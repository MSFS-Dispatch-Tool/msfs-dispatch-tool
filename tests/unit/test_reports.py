"""Report a problem: the route that takes reports and the admin endpoints,
against an in-memory store (no database)."""
import pytest

import auth


@pytest.fixture
def reports(app_module, monkeypatch):
    saved = []
    monkeypatch.setattr(app_module.db, "db_available", lambda: True)

    def create(user_id, email, category, message, context, flight):
        saved.append({"id": f"{len(saved):032x}", "user_id": user_id, "email": email, "category": category,
                      "message": message, "context": context, "flight": flight, "status": "new"})
        return saved[-1]["id"]
    monkeypatch.setattr(app_module.db, "create_report", create)
    app_module._report_times.clear()
    return saved


def send(client, **body):
    body = {"category": "bug", "message": "The loadsheet tab shows nothing", **body}
    return client.post("/reports", json=body, headers={"User-Agent": "TestBrowser/1.0"})


def test_a_report_keeps_the_message_and_known_context_only(client, reports):
    resp = send(client, context={"view": "flight", "path": "/app", "errors": ["TypeError at /static/js/app.js:10:2"] * 12,
                                 "viewport": "1600x950", "theme": "dark", "cookie": "secret", "user_agent": "spoofed"},
                flight={"legs": [{"flight_number": "U2765"}]})
    assert resp.status_code == 200 and resp.get_json()["reference"] == reports[0]["id"][:6].upper()
    r = reports[0]
    assert r["user_id"] == "local" and r["category"] == "bug" and r["flight"] == {"legs": [{"flight_number": "U2765"}]}
    assert set(r["context"]) == {"view", "path", "viewport", "theme", "client_time", "errors", "user_agent"}
    assert len(r["context"]["errors"]) == 10 and r["context"]["user_agent"] == "TestBrowser/1.0"


def test_a_report_needs_a_kind_and_a_real_message(client, reports):
    assert send(client, category="spam").status_code == 400
    assert send(client, message="  broken ").status_code == 400
    assert reports == []


def test_a_huge_flight_is_replaced_by_a_note(client, reports):
    send(client, flight={"blob": "x" * 1_100_000})
    assert reports[0]["flight"] == {"note": "The active flight was too large to attach"}


def test_reports_are_rate_limited(client, reports):
    assert all(send(client).status_code == 200 for _ in range(5))
    assert send(client).status_code == 429 and len(reports) == 5


def test_admin_endpoints_are_for_admins_only(client, app_module, reports, monkeypatch):
    # Without accounts (local mode) there is no admin at all
    assert client.get("/admin/api/reports").status_code == 403
    assert client.post("/admin/api/reports/abc/status", json={"status": "fixed"}).status_code == 403
    assert client.get("/admin/reports").status_code == 302

    # Signed in, but not on the admin list
    monkeypatch.setattr(auth, "auth_available", lambda: True)
    monkeypatch.setattr(auth, "email_allowed", lambda email, extra=(): True)
    monkeypatch.setattr(auth, "ADMIN_EMAILS", {"boss@example.com"})
    with client.session_transaction() as s:
        s["user"] = {"id": "u1", "email": "pilot@example.com"}
        s["expires_at"] = app_module._now_ts() + 3600
    assert client.get("/admin/api/reports").status_code == 403

    # An admin
    monkeypatch.setattr(app_module.db, "list_reports", lambda: [{"id": "r1"}])
    monkeypatch.setattr(app_module.db, "set_report_status", lambda rid, status: rid == "r1")
    with client.session_transaction() as s:
        s["user"] = {"id": "u2", "email": "boss@example.com"}
    assert client.get("/admin/api/reports").get_json() == {"reports": [{"id": "r1"}]}
    assert client.post("/admin/api/reports/r1/status", json={"status": "fixed"}).get_json()["status"] == "fixed"
    assert client.post("/admin/api/reports/r1/status", json={"status": "deleted"}).status_code == 400
    assert client.post("/admin/api/reports/nope/status", json={"status": "seen"}).status_code == 404
