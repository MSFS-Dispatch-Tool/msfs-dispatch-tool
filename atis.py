"""Runways in use, from the ATIS of the network the pilot flies on: VATSIM,
IVAO or SayIntentions. They go into the SimBrief plan as the departure and
arrival runways (origrwy/destrwy), so the OFP's SID, STAR and fuel match
what the controllers are using.

- VATSIM: the public data feed (data.vatsim.net/v3/vatsim-data.json, which
  VATSIM refreshes every 15 seconds), "atis" list. A station is ICAO_ATIS,
  or ICAO_D_ATIS / ICAO_A_ATIS where departures and arrivals have their own.
  The text is whatever the controller wrote, so it's read (runways_in_text).
- IVAO: the public tracker (api.ivao.aero/v2/tracker/whazzup), the ATIS of
  an airport's controllers (ICAO_TWR, _APP, _GND, _DEL). Its first line is
  the voice server; the rest is read the same way.
- SayIntentions: SAPI getWX, which already answers with the active runways
  (active_runways_departing / _arriving, "18,07C"), for live and AI ATC.

Nothing is guessed: a network with no ATIS for an airport, or an ATIS with
no runway in it, gives None, and SimBrief picks that runway by wind as
before. Both feeds are fetched at most once a minute for the whole app.
"""
import re
import threading
import time

import requests

import sayintentions

SOURCES = ("vatsim", "ivao", "sayintentions")
VATSIM_URL = "https://data.vatsim.net/v3/vatsim-data.json"
IVAO_URL = "https://api.ivao.aero/v2/tracker/whazzup"
SAYINTENTIONS_URL = "https://apipri.sayintentions.ai/sapi/getWX"
FEED_SECONDS = 60
TIMEOUT_SECONDS = 10
ICAO_RE = re.compile(r"^[A-Z]{4}$")
RUNWAY_RE = re.compile(r"^(0[1-9]|[12]\d|3[0-6])[LRC]?$")

_feeds = {}
_lock = threading.Lock()


class AtisError(Exception):
    """The network couldn't be reached; the message is safe to show."""


# ---- Reading runways out of ATIS text ----
_RWY = r"(?:0[1-9]|[12]\d|3[0-6])[LRC]?(?![\dA-Z])"
# "RWY 26L", "RWYS 07R, 07L", "RUNWAY IN USE 26L", "RWY34L/34R", "RWY 05 AND 34R"
_FOR_WORDS = r"ARRIVALS?|ARRS?|LANDING|LDG|DEPARTURES?|DEPS?|TAKE-?OFF|TKOF"
_MENTION = re.compile(r"\b(?:RWYS?|RUNWAYS?)\]?(?:\s+IN\s+USE)?(?:\s+FOR\s+(?P<for>" + _FOR_WORDS + r"))?(?:\s+IS)?\s*:?\s*"
                      r"(?P<rwys>" + _RWY + r"(?:\s*(?:/|,|&|\bAND\b)\s*" + _RWY + r")*)")
# "RWY 14R FOR ARRIVALS": the use named after the runway
_FOR_AFTER = re.compile(r"^\s*(?:IS\s+)?(?:IN\s+USE\s+)?FOR\s+(" + _FOR_WORDS + r")\b")
_ARR_WORDS = r"ARR|ARRS|ARRIVAL|ARRIVALS|ARRIVING|ARVG|LDG|LAND|LANDING|APCH|APP|APPROACH|ILS|RNP|RNAV|VOR|LOC"
_DEP_WORDS = r"DEP|DEPS|DEPARTURE|DEPARTURES|DEPARTING|DPTG|DEPTG|DEPG|TKOF|TAKEOFF|TAKE-OFF|T/O"
_WORD = re.compile(r"(?<![A-Z])(" + _ARR_WORDS + r"|" + _DEP_WORDS + r")(?![A-Z])")
_ARR = re.compile(r"^(?:" + _ARR_WORDS + r")$")
# A runway mentioned for any other reason: closed, works, light aircraft only...
_NOT_IN_USE = re.compile(r"^\W*(?:IS\s+)?(?:CLSD|CLOSED|NOT\s+AVBL|NOT\s+AVAILABLE|UNAVAILABLE|UNAVBL|U/S|WIP|AVBL\s+FOR|AVAILABLE\s+FOR|ONLY\s+FOR|FOR\s+LIGHT|RESTRICTED)")
_NOT_IN_USE_BEFORE = re.compile(r"(?:VACATE|CROSS|CROSSING|BACKTRACK|BACKTRACKING|HOLD\s+SHORT(?:\s+OF)?)\s*$")


def runways_in_text(text):
    """(departure runways, arrival runways) named in an ATIS, in order.
    A runway counts for whichever of arrivals or departures the words just
    before it name ("ARR RWY 08", "DPTG RWY 16L", "ILS APCH RWY 07"), both
    for "ARVG AND DPTG RWY 08R" or a plain "RWY IN USE 26L"."""
    text = re.sub(r"\s+", " ", (text or "").upper())
    # "06 LEFT", "34 L" -> "06L", "34L"
    text = re.sub(r"\b(\d{2}) ?(LEFT|RIGHT|CENTRE|CENTER|L|R|C)\b", lambda m: m.group(1) + m.group(2)[0], text)
    dep, arr, both = [], [], []
    last_end = 0
    for m in _MENTION.finditer(text):
        before = text[max(last_end, m.start() - 40):m.start()]
        last_end = m.end()
        if _NOT_IN_USE.match(text[m.end():m.end() + 30]) or _NOT_IN_USE_BEFORE.search(before):
            continue
        runways = re.findall(_RWY, m.group("rwys"))
        words = list(_WORD.finditer(before))
        kinds = set()
        named = m.group("for") or (_FOR_AFTER.match(text[m.end():m.end() + 40]) or [None, None])[1]
        if named:
            kinds.add("arr" if named.startswith(("ARR", "LAND", "LDG")) else "dep")
        elif words:
            kinds.add("arr" if _ARR.match(words[-1].group(1)) else "dep")
            # "ARVG AND DPTG", "DEP/ARR": both words name this runway
            if len(words) > 1 and re.fullmatch(r"\s*(?:AND|&|/|,)\s*", before[words[-2].end():words[-1].start()]):
                kinds.add("arr" if _ARR.match(words[-2].group(1)) else "dep")
        target = both if not kinds or kinds == {"arr", "dep"} else arr if kinds == {"arr"} else dep
        for r in runways:
            if r not in target:
                target.append(r)
    deps = dep or both or arr
    arrs = arr or both or dep
    return deps, arrs


# ---- The networks ----
def _feed(name, url):
    """A network's whole feed, fetched at most once a minute."""
    with _lock:
        cached = _feeds.get(name)
        if cached and time.time() - cached[0] < FEED_SECONDS:
            return cached[1]
    try:
        resp = requests.get(url, timeout=TIMEOUT_SECONDS, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        if cached:
            return cached[1]
        raise AtisError(f"{_NAMES[name].upper()} NOT AVAILABLE")
    with _lock:
        _feeds[name] = (time.time(), data)
    return data


def _vatsim(icaos):
    stations = {}
    for entry in _feed("vatsim", VATSIM_URL).get("atis") or []:
        callsign = str(entry.get("callsign") or "").upper()
        m = re.match(r"^([A-Z]{4})_(?:([AD])_)?ATIS$", callsign)
        if m and m.group(1) in icaos and entry.get("text_atis"):
            stations.setdefault(m.group(1), {})[m.group(2) or ""] = {
                "station": callsign, "letter": str(entry.get("atis_code") or "").strip()[:1],
                "text": " ".join(str(t) for t in entry["text_atis"] if t)}
    return stations


_IVAO_POSITIONS = ("TWR", "APP", "GND", "DEL")


def _ivao(icaos):
    stations = {}
    for atc in ((_feed("ivao", IVAO_URL).get("clients") or {}).get("atcs")) or []:
        callsign = str(atc.get("callsign") or "").upper()
        m = re.match(r"^([A-Z]{4})_(?:[A-Z0-9]+_)?([A-Z]+)$", callsign)
        atis = atc.get("atis") or {}
        lines = [str(t) for t in (atis.get("lines") or [])[1:] if t]
        if not (m and m.group(1) in icaos and m.group(2) in _IVAO_POSITIONS and lines):
            continue
        rank = _IVAO_POSITIONS.index(m.group(2))
        held = stations.get(m.group(1), {}).get("")
        if held and held["rank"] <= rank:
            continue
        stations.setdefault(m.group(1), {})[""] = {
            "station": callsign, "letter": str(atis.get("revision") or "").strip()[:1], "text": " ".join(lines), "rank": rank}
    return stations


def _first(value):
    for r in str(value or "").replace(" ", "").split(","):
        if RUNWAY_RE.match(r):
            return r
    return None


def _sayintentions_airports(icaos, api_key):
    """SayIntentions' weather for the airports, as it answers it."""
    params = {"icao": ",".join(sorted(icaos))}
    if api_key:
        params["api_key"] = api_key
    headers = {"Accept": "application/json"}
    if sayintentions.DEVELOPER_KEY:
        headers["X-Developer-Key"] = sayintentions.DEVELOPER_KEY
    try:
        resp = requests.get(SAYINTENTIONS_URL, params=params, headers=headers, timeout=TIMEOUT_SECONDS)
        resp.raise_for_status()
        return resp.json().get("airports") or []
    except (requests.RequestException, ValueError, AttributeError):
        raise AtisError("SAYINTENTIONS NOT AVAILABLE")


def _sayintentions(icaos, api_key):
    out = {}
    for a in _sayintentions_airports(icaos, api_key):
        icao = str(a.get("airport") or "").upper()
        if icao in icaos:
            out[icao] = {"station": "SayIntentions", "letter": str(a.get("current") or "").strip()[:1],
                         "text": str(a.get("atis") or ""), "dep": _first(a.get("active_runways_departing")),
                         "arr": _first(a.get("active_runways_arriving"))}
    return out


_NAMES = {"vatsim": "VATSIM", "ivao": "IVAO", "sayintentions": "SayIntentions"}


def runways(source, dep_icao, arr_icao, api_key=None):
    """{"dep": {...} or None, "arr": {...} or None} for the two airports:
    each {"rwy", "letter", "station"}, None when the network has no ATIS
    there or its ATIS names no runway. Raises AtisError."""
    icaos = {dep_icao, arr_icao}
    if source == "sayintentions":
        found = _sayintentions(icaos, api_key)
        pick = {"dep": found.get(dep_icao), "arr": found.get(arr_icao)}
        return {side: ({"rwy": info[side], "letter": info["letter"], "station": info["station"]}
                       if info and info.get(side) else None) for side, info in pick.items()}
    stations = _vatsim(icaos) if source == "vatsim" else _ivao(icaos)
    out = {}
    for side, icao, own in (("dep", dep_icao, "D"), ("arr", arr_icao, "A")):
        at = stations.get(icao) or {}
        station = at.get(own) or at.get("")
        out[side] = None
        if station:
            deps, arrs = runways_in_text(station["text"])
            rwys = deps if side == "dep" else arrs
            if rwys:
                out[side] = {"rwy": rwys[0], "letter": station["letter"], "station": station["station"]}
    return out
