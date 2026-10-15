"""Sign in with Navigraph: the pilot's link to their SimBrief account.

SimBrief's API v2 works with the pilot's own Navigraph sign-in instead of
a shared API key: the pilot signs in once on navigraph.com, Navigraph
sends them back to /auth/navigraph/callback with a one-time code, and this
server exchanges it (with the app's client secret, which never leaves the
server) for an access token, valid about an hour, and a refresh token that
keeps the link alive. The flow is OAuth's authorization code with PKCE:

    https://developers.navigraph.com/docs/authentication/authorization-code

Both tokens are stored encrypted (TokenBox) and never reach the browser.
A free Navigraph account is enough; no subscription is needed.

Environment:
    NAVIGRAPH_CLIENT_ID, NAVIGRAPH_CLIENT_SECRET   from Navigraph; without
        them the feature is off and Account settings doesn't show it
    NAVIGRAPH_IDENTITY_URL   optional, Navigraph's sign-in server
"""
import base64
import hashlib
import json
import os
import secrets
from urllib.parse import urlencode

import requests
from cryptography.fernet import Fernet, InvalidToken

IDENTITY_URL = (os.environ.get("NAVIGRAPH_IDENTITY_URL") or "https://identity.api.navigraph.com").rstrip("/")
CLIENT_ID = os.environ.get("NAVIGRAPH_CLIENT_ID") or ""
CLIENT_SECRET = os.environ.get("NAVIGRAPH_CLIENT_SECRET") or ""
# SimBrief's flight plans (simbrief) and plan generation (ofp); a refresh
# token (offline_access) so the pilot signs in once, not every hour
SCOPES = "openid offline_access simbrief ofp"
TIMEOUT_SECONDS = 15


class NavigraphError(Exception):
    """Navigraph refused or couldn't be reached. The message is safe to
    show the pilot; reconnect means the link is gone (revoked, expired) and
    the pilot has to sign in with Navigraph again."""

    def __init__(self, message, reconnect=False):
        super().__init__(message)
        self.reconnect = reconnect


def configured():
    return bool(CLIENT_ID and CLIENT_SECRET)


def new_pkce():
    """A fresh (verifier, challenge) pair: the challenge goes out with the
    sign-in link, the verifier stays here until the code comes back."""
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(redirect_uri, state, challenge):
    return IDENTITY_URL + "/connect/authorize?" + urlencode({
        "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": redirect_uri, "scope": SCOPES,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    })


def _token_request(fields):
    try:
        resp = requests.post(IDENTITY_URL + "/connect/token", timeout=TIMEOUT_SECONDS,
                             data={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, **fields})
    except requests.RequestException:
        raise NavigraphError("Navigraph can't be reached right now. Try again in a minute")
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code == 200 and body.get("access_token"):
        return body
    # invalid_grant: the code or refresh token is used up, expired or revoked
    if body.get("error") == "invalid_grant":
        raise NavigraphError("Your Navigraph sign-in has expired. Sign in with Navigraph again", reconnect=True)
    raise NavigraphError(f"Navigraph refused the sign-in ({body.get('error') or resp.status_code})")


def exchange_code(code, verifier, redirect_uri):
    return _token_request({"grant_type": "authorization_code", "code": code,
                           "code_verifier": verifier, "redirect_uri": redirect_uri})


def refresh(refresh_token):
    """New tokens. Navigraph may hand out a new refresh token each time and
    retire the old one, so the caller always stores what comes back."""
    return _token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})


def revoke(refresh_token):
    """Best effort, when the pilot disconnects: the token is deleted here
    either way."""
    try:
        requests.post(IDENTITY_URL + "/connect/revocation", timeout=TIMEOUT_SECONDS,
                      data={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                            "token": refresh_token, "token_type_hint": "refresh_token"})
    except requests.RequestException:
        pass


def identity(tokens):
    """{"sub", "name"} of the signed-in account, read from the ID token.
    It came straight from Navigraph's token endpoint over HTTPS, so its
    signature needn't be checked again (OpenID Connect Core 3.1.3.7)."""
    try:
        payload = tokens["id_token"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (KeyError, IndexError, ValueError, AttributeError):
        claims = {}
    sub = str(claims.get("sub") or "")
    name = claims.get("preferred_username") or claims.get("name") or claims.get("email") or ""
    return {"sub": sub, "name": str(name)[:60]}


class TokenBox:
    """Encrypts tokens for storage with a key derived from the app's secret
    key. If that key changes, stored tokens can't be read any more and the
    pilot simply signs in with Navigraph again."""

    def __init__(self, secret):
        key = hashlib.sha256(b"virtualdispatch/navigraph-tokens/" + str(secret).encode()).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(key))

    def seal(self, text):
        return self._fernet.encrypt(text.encode()).decode()

    def open(self, sealed):
        """The token, or None if it can't be decrypted."""
        try:
            return self._fernet.decrypt(sealed.encode()).decode()
        except (InvalidToken, AttributeError, ValueError):
            return None
