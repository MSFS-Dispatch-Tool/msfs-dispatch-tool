"""Hoppie's ACARS network: the server side of SEND TO AIRCRAFT.

Hoppie (hoppie.nl) relays text messages between ground stations and
aircraft in the simulator. Every request goes to one URL with the sender's
logon code; each pilot uses their own code, the same one they enter in the
aircraft, so nothing here is shared between pilots. Requests are made only
from this server: a logon code never reaches the browser.

    logon   the pilot's Hoppie logon code
    from    the sending station's callsign
    to      the receiving callsign (an aircraft, or SERVER for pings)
    type    telex (free text), ping, ...
    packet  the message

Hoppie answers "ok", "ok {...}" or "error {reason}". The exact wording of
its answers isn't formally documented, so they are parsed leniently.
"""
import re

import requests

URL = "https://www.hoppie.nl/acars/system/connect.html"
TIMEOUT_SECONDS = 10
# Our ground station, as aircraft see it in the "from" of a message
STATION = "VDOPS"

LOGON_RE = re.compile(r"^[A-Za-z0-9]{6,40}$")
CALLSIGN_RE = re.compile(r"^[A-Z0-9]{2,8}$")


class AcarsError(Exception):
    """Hoppie refused the request (bad logon code, unknown callsign...)
    or couldn't be reached. The message is safe to show the pilot."""


def valid_logon(code):
    return bool(LOGON_RE.match(code or ""))


def mask(code):
    """Enough to recognise a saved code, not enough to use it."""
    return "•" * 6 + code[-3:] if code else ""


def _braced(text):
    m = re.search(r"\{(.*)\}", text, re.S)
    return (m.group(1) if m else "").strip()


def _call(logon, to, kind, packet=""):
    try:
        resp = requests.post(URL, data={"logon": logon, "from": STATION, "to": to, "type": kind, "packet": packet},
                             timeout=TIMEOUT_SECONDS)
        resp.raise_for_status()
    except requests.RequestException:
        raise AcarsError("Hoppie's ACARS network can't be reached right now. Try again in a minute")
    text = resp.text.strip()
    if text.lower().startswith("ok"):
        return _braced(text)
    reason = _braced(text) or text[:120] or "no answer"
    if "logon" in reason.lower():
        raise AcarsError("Hoppie doesn't recognise this logon code. Check it on hoppie.nl")
    raise AcarsError(f"Hoppie refused the request: {reason}")


def ping(logon, callsigns=()):
    """Checks the logon code; with callsigns, returns the ones logged on to
    the network right now."""
    online = _call(logon, "SERVER", "ping", " ".join(callsigns))
    found = set(online.upper().split())
    return [c for c in callsigns if c.upper() in found]


def send_telex(logon, callsign, text):
    callsign = (callsign or "").upper()
    if not CALLSIGN_RE.match(callsign):
        raise AcarsError("That isn't a valid callsign for ACARS")
    _call(logon, callsign, "telex", text)
