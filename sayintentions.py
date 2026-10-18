"""SayIntentions.AI: the second way SEND TO AIRCRAFT reaches the aircraft.

Pilots who fly with SayIntentions' AI controllers have their aircraft's
ACARS on SayIntentions' network instead of Hoppie's (an aircraft is on one
network at a time), so for them the loadsheet goes through SayIntentions:
its SAPI "sayAs" endpoint, channel ACARS_IN, as a telex that arrives as a
company message.

Two keys go with every request: the app's developer key (header
X-Developer-Key, from SAYINTENTIONS_DEVELOPER_KEY; without it the option is
off) and the pilot's own API key, from their SayIntentions pilot portal,
which says whose aircraft gets the message. Requests are made only from
this server: a pilot's key never reaches the browser.

SayIntentions caps a message by aircraft: 255 characters on the Fenix A320
and 128 on the PMDG 737 (tested October 2026). It answers HTTP 200 even
when it refuses a message, with {"error": "Message too long: 346 characters.
Maximum is 255 characters."}, so the body is read, not just the status.
"""
import os
import re

import requests

URL = "https://apipri.sayintentions.ai/sapi/sayAs"
DEVELOPER_KEY = os.environ.get("SAYINTENTIONS_DEVELOPER_KEY") or ""
TIMEOUT_SECONDS = 10
# Our station, as the telex's sender
STATION = "VDOPS"

KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{8,128}$")
_MAXIMUM_RE = re.compile(r"maximum is (\d+)", re.I)


class SayIntentionsError(Exception):
    """SayIntentions refused the message or couldn't be reached. The
    message is safe to show the pilot."""


class MessageTooLong(SayIntentionsError):
    """Longer than this aircraft takes: maximum is its limit in characters."""

    def __init__(self, maximum):
        super().__init__(f"Too long for this aircraft: at most {maximum} characters")
        self.maximum = maximum


def configured():
    return bool(DEVELOPER_KEY)


def valid_key(key):
    return bool(KEY_RE.match(key or ""))


def send_telex(api_key, text):
    """Sends text to the pilot's aircraft. Raises MessageTooLong (with the
    aircraft's limit) or SayIntentionsError."""
    try:
        resp = requests.get(URL, timeout=TIMEOUT_SECONDS, headers={"X-Developer-Key": DEVELOPER_KEY}, params={
            "api_key": api_key, "channel": "ACARS_IN", "message_type": "telex", "from": STATION, "message": text})
    except requests.RequestException:
        raise SayIntentionsError("SayIntentions can't be reached right now. Try again in a minute")
    error = None
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("error"):
            error = str(body["error"])
    except ValueError:
        pass
    if error is None and resp.status_code >= 400:
        error = f"HTTP {resp.status_code}"
    if error is None:
        return
    maximum = _MAXIMUM_RE.search(error)
    if maximum and "too long" in error.lower():
        raise MessageTooLong(int(maximum.group(1)))
    if "developer" in error.lower():
        raise SayIntentionsError("SayIntentions didn't accept VirtualDispatch's request. Try again later")
    if "key" in error.lower() or resp.status_code in (401, 403):
        raise SayIntentionsError("SayIntentions doesn't recognise this API key. Check it on your SayIntentions pilot portal")
    raise SayIntentionsError(f"SayIntentions refused the message: {error[:160]}")
