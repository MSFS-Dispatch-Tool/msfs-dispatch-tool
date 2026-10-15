"""Invitation-only access: the admin page's allowed pilots, sign-up for
invited emails, and the website's waitlist (in-memory stores, no database)."""
import pytest

import auth


@pytest.fixture
def lists(app_module, monkeypatch):
    """The allowed pilots and the waitlist, in memory."""
    allowed, waitlist = {}, {}
    monkeypatch.setattr(app_module.db, "db_available", lambda: True)
    monkeypatch.setattr(app_module.db, "allowed_pilot_emails", lambda: set(allowed))
    monkeypatch.setattr(app_module.db, "add_allowed_pilot",
                        lambda email, note, by: (allowed.__setitem__(email, note), waitlist.pop(email, None)))
    monkeypatch.setattr(app_module.db, "remove_allowed_pilot", lambda email: allowed.pop(email, None) is not None)
    monkeypatch.setattr(app_module.db, "join_waitlist", lambda email, consent: waitlist.setdefault(email, consent) == consent)
    monkeypatch.setattr(app_module.db, "list_allowed_pilots", lambda: [{"email": e, "note": n} for e, n in allowed.items()])
    monkeypatch.setattr(app_module, "_allowed_cache", {"at": 0.0, "emails": frozenset()})
    monkeypatch.setattr(auth, "OPEN_ACCESS", False)
    monkeypatch.setattr(auth, "ADMIN_EMAILS", {"boss@example.com"})
    monkeypatch.setattr(auth, "ALLOWED_EMAILS", set())
    app_module._waitlist_times.clear()
    return allowed, waitlist


def test_the_admin_list_adds_to_the_allowed_emails(lists):
    assert not auth.email_allowed("pilot@example.com")
    assert auth.email_allowed("Pilot@Example.com ", {"pilot@example.com"})
    assert auth.email_allowed("boss@example.com")                       # Render's lists still count


def test_an_allowed_pilot_gets_access(app_module, lists):
    allowed, _ = lists
    user = {"id": "u1", "email": "pilot@example.com"}
    assert not app_module._has_access(user)
    allowed["pilot@example.com"] = ""
    assert app_module._has_access(user) is False          # cached for up to ALLOWED_CACHE_SECONDS
    app_module._allowed_pilots(refresh=True)
    assert app_module._has_access(user)


def test_only_invited_emails_can_create_an_account(client, app_module, lists, monkeypatch):
    allowed, _ = lists
    created = []
    monkeypatch.setattr(app_module, "_verify_turnstile_from_form", lambda: True)
    monkeypatch.setattr(app_module.auth, "sign_up", lambda email, password, redirect_to=None: created.append(email) or {"identities": [1]})
    form = {"email": "pilot@example.com", "password": "Str0ng!pass", "password_confirm": "Str0ng!pass"}
    page = client.get("/signup").get_data(as_text=True)
    assert "email address you were invited with" in page and 'name="password"' in page
    refused = client.post("/signup", data=form)
    assert refused.status_code == 403 and "isn&#39;t on the invitation list" in refused.get_data(as_text=True) and not created
    allowed["pilot@example.com"] = ""                     # read fresh at sign-up, no cache wait
    assert client.post("/signup", data=form).status_code == 200 and created == ["pilot@example.com"]


def test_signups_open_to_all_with_open_access(client, app_module, lists, monkeypatch):
    monkeypatch.setattr(auth, "OPEN_ACCESS", True)
    created = []
    monkeypatch.setattr(app_module, "_verify_turnstile_from_form", lambda: True)
    monkeypatch.setattr(app_module.auth, "sign_up", lambda email, password, redirect_to=None: created.append(email) or {"identities": [1]})
    client.post("/signup", data={"email": "anyone@example.com", "password": "Str0ng!pass", "password_confirm": "Str0ng!pass"})
    assert created == ["anyone@example.com"]


def test_joining_the_waitlist(client, app_module, lists):
    _, waitlist = lists
    ok = client.post("/waitlist", json={"email": " Pilot@Example.com", "consent": True})
    assert ok.status_code == 200 and ok.get_json()["ok"] and waitlist == {"pilot@example.com": app_module.WAITLIST_CONSENT}
    again = client.post("/waitlist", json={"email": "pilot@example.com", "consent": True})
    assert again.get_json()["message"] == ok.get_json()["message"]          # no hint that it was already listed
    assert client.post("/waitlist", json={"email": "not-an-email", "consent": True}).status_code == 400
    assert client.post("/waitlist", json={"email": "b@example.com"}).status_code == 400           # no consent, no entry
    assert client.post("/waitlist", json={"email": "bot@example.com", "consent": True, "website": "spam"}).get_json()["ok"]
    assert set(waitlist) == {"pilot@example.com"}


def test_the_waitlist_form_works_without_javascript(client, lists):
    _, waitlist = lists
    resp = client.post("/waitlist", data={"email": "plain@example.com", "consent": "1"})
    assert resp.status_code == 302 and resp.location.endswith("/?waitlist=joined#waitlist") and "plain@example.com" in waitlist
    assert client.post("/waitlist", data={"email": "x"}).location.endswith("/?waitlist=error#waitlist")


def test_the_waitlist_is_rate_limited(client, app_module, lists):
    _, waitlist = lists
    answers = [client.post("/waitlist", json={"email": f"p{i}@example.com", "consent": True}).status_code for i in range(7)]
    assert answers == [200] * app_module.WAITLIST_MAX_PER_VISITOR + [429] * (7 - app_module.WAITLIST_MAX_PER_VISITOR)
    assert len(waitlist) == app_module.WAITLIST_MAX_PER_VISITOR


def test_the_website_shows_the_waitlist_while_signups_are_closed(client, lists, monkeypatch):
    html = client.get("/").get_data(as_text=True)
    assert 'id="waitlistForm"' in html and "Join the waitlist" in html
    monkeypatch.setattr(auth, "OPEN_ACCESS", True)
    assert 'id="waitlistForm"' not in client.get("/").get_data(as_text=True)


def test_admins_manage_the_allowed_pilots(client, app_module, lists, monkeypatch):
    allowed, waitlist = lists
    assert client.post("/admin/api/allowed", json={"email": "a@example.com"}).status_code == 403   # no admin signed in
    monkeypatch.setattr(auth, "auth_available", lambda: True)
    with client.session_transaction() as s:
        s["user"] = {"id": "u0", "email": "boss@example.com"}
        s["expires_at"] = app_module._now_ts() + 3600
    waitlist["new@example.com"] = "consent"
    added = client.post("/admin/api/allowed", json={"email": " New@Example.com ", "note": "Discord"}).get_json()
    assert added["email"] == "new@example.com" and added["signup_url"].endswith("/signup")
    assert allowed == {"new@example.com": "Discord"} and waitlist == {}     # off the waitlist once allowed
    assert app_module._has_access({"id": "u5", "email": "new@example.com"})  # at once on this worker
    assert client.post("/admin/api/allowed", json={"email": "nope"}).status_code == 400
    assert client.delete("/admin/api/allowed/new@example.com").status_code == 200 and allowed == {}
    assert client.delete("/admin/api/allowed/new@example.com").status_code == 404
