"""Sign in with Navigraph: the client, token storage and the sign-in
routes, against a fake Navigraph and an in-memory store (no network, no
database)."""
import base64
import contextlib
import hashlib
import json
from urllib.parse import parse_qs, urlparse

import pytest
import requests

import navigraph
import sealing


class Answer:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body


def id_token(claims):
    part = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"e30.{part}.sig"


@pytest.fixture
def fake_navigraph(monkeypatch):
    """Answers each request with the next queued reply; records them."""
    calls, replies = [], []

    def post(url, data=None, timeout=None):
        calls.append((url, data))
        reply = replies.pop(0) if replies else Answer({}, 200)
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(navigraph.requests, "post", post)
    monkeypatch.setattr(navigraph, "CLIENT_ID", "vd-test")
    monkeypatch.setattr(navigraph, "CLIENT_SECRET", "s3cret")
    return calls, replies


@pytest.fixture
def store(app_module, monkeypatch):
    """The pilot's stored Navigraph link, in memory instead of Postgres."""
    links = {}
    monkeypatch.setattr(app_module.db, "db_available", lambda: True)
    monkeypatch.setattr(app_module.db, "get_navigraph", lambda user: dict(links.get(user, {})))
    monkeypatch.setattr(app_module.db, "save_navigraph", lambda link, user: links.__setitem__(user, dict(link)))

    @contextlib.contextmanager
    def locked(user):
        yield dict(links.get(user, {})), lambda link: links.__setitem__(user, dict(link))
    monkeypatch.setattr(app_module.db, "locked_navigraph", locked)
    return links


def test_pkce_challenge_is_the_verifiers_sha256():
    verifier, challenge = navigraph.new_pkce()
    assert len(verifier) == 43 and verifier != navigraph.new_pkce()[0]
    assert challenge == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def test_sign_in_link_asks_for_simbrief_and_a_refresh_token(fake_navigraph):
    url = urlparse(navigraph.authorize_url("https://virtualdispatch.app/auth/navigraph/callback", "st4te", "ch4llenge"))
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.netloc == "identity.api.navigraph.com" and url.path == "/connect/authorize"
    assert q == {"response_type": "code", "client_id": "vd-test", "scope": "openid offline_access simbrief ofp",
                 "redirect_uri": "https://virtualdispatch.app/auth/navigraph/callback", "state": "st4te",
                 "code_challenge": "ch4llenge", "code_challenge_method": "S256"}


def test_token_errors_become_plain_messages(fake_navigraph):
    _, replies = fake_navigraph
    replies += [Answer({"error": "invalid_grant"}, 400), Answer({"error": "invalid_client"}, 401),
                requests.ConnectionError("down")]
    with pytest.raises(navigraph.NavigraphError) as gone:
        navigraph.refresh("old")
    assert gone.value.reconnect
    with pytest.raises(navigraph.NavigraphError, match="invalid_client") as refused:
        navigraph.refresh("old")
    assert not refused.value.reconnect
    with pytest.raises(navigraph.NavigraphError, match="can't be reached"):
        navigraph.refresh("old")


def test_only_the_account_name_comes_from_the_id_token():
    claims = {"sub": "abc-1", "preferred_username": "jdoe", "email": "j@example.com"}
    assert navigraph.account_name({"id_token": id_token(claims)}) == "jdoe"
    assert navigraph.account_name({"id_token": id_token({"sub": "abc-1", "email": "j@example.com"})}) == ""
    assert navigraph.account_name({"id_token": "not a jwt"}) == ""


def test_tokens_are_stored_encrypted():
    box = sealing.SecretBox("key one", "navigraph-tokens")
    sealed = box.seal("refresh-123")
    assert "refresh-123" not in sealed and box.open(sealed) == "refresh-123"
    assert sealing.SecretBox("key two", "navigraph-tokens").open(sealed) is None   # a new app secret: sign in again
    assert sealing.SecretBox("key one", "hoppie-logon").open(sealed) is None       # a key per kind of credential


def test_sign_in_round_trip(client, app_module, fake_navigraph, store):
    calls, replies = fake_navigraph
    assert client.get("/navigraph/status").get_json() == {"available": True, "connected": False, "name": "", "connected_at": None}

    start = client.get("/auth/navigraph/start")
    assert start.status_code == 302
    q = {k: v[0] for k, v in parse_qs(urlparse(start.location).query).items()}
    assert q["redirect_uri"] == "http://localhost/auth/navigraph/callback"

    replies.append(Answer({"access_token": "acc-1", "refresh_token": "ref-1", "expires_in": 3600,
                           "id_token": id_token({"sub": "abc-1", "preferred_username": "jdoe"})}))
    back = client.get(f"/auth/navigraph/callback?code=c0de&state={q['state']}")
    assert back.status_code == 302 and back.location.endswith("/app?navigraph=connected")
    sent = calls[-1][1]
    assert calls[-1][0].endswith("/connect/token") and sent["grant_type"] == "authorization_code" and sent["code"] == "c0de"
    assert sent["client_secret"] == "s3cret" and sent["redirect_uri"] == q["redirect_uri"]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(sent["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert challenge == q["code_challenge"]

    link = store["local"]
    assert set(link) == {"name", "access", "refresh", "expires_at", "connected_at"}
    assert link["name"] == "jdoe" and "ref-1" not in json.dumps(link) and "acc-1" not in json.dumps(link)
    status = client.get("/navigraph/status").get_json()
    assert status["connected"] and status["name"] == "jdoe" and "ref-1" not in json.dumps(status)
    assert app_module.navigraph_access_token("local") == "acc-1"

    # The same code can't be brought back twice
    assert client.get(f"/auth/navigraph/callback?code=c0de&state={q['state']}").location.endswith("navigraph=expired")

    client.post("/navigraph/disconnect")
    assert calls[-1][0].endswith("/connect/revocation") and calls[-1][1]["token"] == "ref-1"
    assert not client.get("/navigraph/status").get_json()["connected"]


def test_callback_needs_the_state_this_browser_started(client, fake_navigraph, store):
    calls, _ = fake_navigraph
    client.get("/auth/navigraph/start")
    assert client.get("/auth/navigraph/callback?code=c0de&state=someone-else").location.endswith("navigraph=expired")
    client.get("/auth/navigraph/start")
    assert client.get("/auth/navigraph/callback?error=access_denied").location.endswith("navigraph=cancelled")
    assert calls == [] and store == {}


def test_off_without_credentials(client, monkeypatch, store):
    monkeypatch.setattr(navigraph, "CLIENT_ID", "")
    assert client.get("/navigraph/status").get_json()["available"] is False
    assert client.get("/auth/navigraph/start").location.endswith("/app?navigraph=unavailable")


def test_access_token_is_refreshed_before_it_expires(app_module, fake_navigraph, store):
    calls, replies = fake_navigraph
    box = app_module._navigraph_box
    store["local"] = {"name": "jdoe", "access": box.seal("acc-old"), "refresh": box.seal("ref-old"),
                      "expires_at": app_module._now_ts() + 30, "connected_at": "2026-10-15T10:00:00Z"}
    replies.append(Answer({"access_token": "acc-new", "refresh_token": "ref-new", "expires_in": 3600}))
    assert app_module.navigraph_access_token("local") == "acc-new"
    assert calls[-1][1]["grant_type"] == "refresh_token" and calls[-1][1]["refresh_token"] == "ref-old"
    link = store["local"]
    assert box.open(link["refresh"]) == "ref-new" and link["name"] == "jdoe" and link["connected_at"] == "2026-10-15T10:00:00Z"
    assert app_module.navigraph_access_token("local") == "acc-new" and len(calls) == 1   # cached until near expiry


def test_a_revoked_sign_in_is_removed(app_module, fake_navigraph, store):
    _, replies = fake_navigraph
    box = app_module._navigraph_box
    store["local"] = {"access": box.seal("a"), "refresh": box.seal("r"), "expires_at": 0}
    replies.append(Answer({"error": "invalid_grant"}, 400))
    with pytest.raises(navigraph.NavigraphError) as gone:
        app_module.navigraph_access_token("local")
    assert gone.value.reconnect and store["local"] == {}
    with pytest.raises(navigraph.NavigraphError, match="Sign in with Navigraph"):
        app_module.navigraph_access_token("local")
