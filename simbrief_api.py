"""SimBrief's API v2, called with the pilot's own Navigraph sign-in.

Each call carries the pilot's access token (navigraph.py and
app.navigraph_access_token keep it current), so it only ever reaches that
pilot's own SimBrief account.

    GET  /v2/flightplans/latest   the pilot's most recent flight plan (OFP)
    GET  /v2/airframes/user/mine  the pilot's saved airframes
    POST /v2/plan/generate        a new flight plan; answers with the OFP

Navigraph's documentation: https://developers.navigraph.com/docs/simbrief
"""
import os

import requests

BASE_URL = (os.environ.get("SIMBRIEF_API_URL") or "https://api.simbrief.com").rstrip("/")
TIMEOUT_SECONDS = 20


class SimBriefError(Exception):
    """SimBrief refused or couldn't be reached. The message is safe to show;
    reconnect means the Navigraph sign-in no longer works for SimBrief."""

    def __init__(self, message, reconnect=False, status=None):
        super().__init__(message)
        self.reconnect = reconnect
        self.status = status


def _get(path, access_token, missing):
    try:
        resp = requests.get(BASE_URL + path, timeout=TIMEOUT_SECONDS,
                            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"})
    except requests.RequestException:
        raise SimBriefError("SimBrief can't be reached right now. Try again in a minute")
    if resp.status_code in (401, 403):
        raise SimBriefError("SimBrief didn't accept your Navigraph sign-in. Sign in with Navigraph again",
                            reconnect=True, status=resp.status_code)
    if resp.status_code == 404:
        raise SimBriefError(missing, status=404)
    if resp.status_code != 200:
        raise SimBriefError(f"SimBrief answered with an error (HTTP {resp.status_code})", status=resp.status_code)
    try:
        return resp.json()
    except ValueError:
        raise SimBriefError("SimBrief sent an answer VirtualDispatch couldn't read", status=resp.status_code)


def latest_plan(access_token):
    """The pilot's most recent flight plan, as SimBrief returns it."""
    return _get("/v2/flightplans/latest", access_token, "No flight plan on your SimBrief account yet")


def user_airframes(access_token):
    """The airframes the pilot has saved on SimBrief, as SimBrief returns them."""
    return _get("/v2/airframes/user/mine", access_token, "No saved airframes on your SimBrief account yet")


def try_generate(access_token, params, as_json=False):
    """Asks SimBrief to generate a plan and reports what came back, as
    {"status", "ok", "data" (the parsed JSON, if any), "text" (the start of
    the raw answer)}, without raising for a refusal: its request format
    isn't published yet, so the admin page uses this to find out which
    format it accepts."""
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    try:
        if as_json:
            resp = requests.post(BASE_URL + "/v2/plan/generate", json=params, headers=headers, timeout=60)
        else:
            resp = requests.post(BASE_URL + "/v2/plan/generate", data=params, headers=headers, timeout=60)
    except requests.RequestException as exc:
        return {"status": None, "ok": False, "data": None, "text": f"SimBrief can't be reached: {type(exc).__name__}"}
    try:
        data = resp.json()
    except ValueError:
        data = None
    return {"status": resp.status_code, "ok": resp.status_code == 200 and isinstance(data, dict),
            "data": data, "text": (getattr(resp, "text", "") or "")[:2000]}
