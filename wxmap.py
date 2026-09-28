"""Weather overlays for the flight-selection map: flight category per
airport (from current METARs) and international SIGMETs, both from the
aviationweather.gov Data API.

Parsing is deliberately defensive - fields the API may omit or format
differently (fltCat, visib as "10+" or "1/2", times as epoch seconds or
ISO strings) are handled rather than assumed, and anything unusable is
dropped instead of guessed.
"""

import threading
import time
from datetime import datetime, timezone

import requests

AWC_BASE = "https://aviationweather.gov/api/data"
FLIGHT_CATEGORIES = ("VFR", "MVFR", "IFR", "LIFR")

# Loose box around the route network (Iceland/Canaries to the Levant).
REGION = (-35.0, 20.0, 50.0, 73.0)   # min lon, min lat, max lon, max lat

HAZARDS = {
    "TS": "Thunderstorms", "TSGR": "Thunderstorms with hail", "TURB": "Turbulence",
    "ICE": "Icing", "MTW": "Mountain waves", "VA": "Volcanic ash",
    "TC": "Tropical cyclone", "DS": "Duststorm", "SS": "Sandstorm",
    "RDOACT": "Radioactive cloud", "CB": "Cumulonimbus",
}


# ---- Flight category ----

def _visibility_sm(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().rstrip("+").replace("SM", "").strip()
    try:
        if " " in text:                       # "1 1/2"
            whole, frac = text.split(" ", 1)
            num, den = frac.split("/")
            return float(whole) + float(num) / float(den)
        if "/" in text:
            num, den = text.split("/")
            return float(num) / float(den)
        return float(text)
    except (ValueError, ZeroDivisionError):
        return None


def _ceiling_ft(clouds):
    bases = [c.get("base") for c in clouds or []
             if c.get("cover") in ("BKN", "OVC", "OVX", "VV") and isinstance(c.get("base"), (int, float))]
    return min(bases) if bases else None


def flight_category(metar):
    """aviationweather.gov's own category when present; otherwise the FAA
    definition from ceiling (BKN/OVC/VV) and visibility. None when neither
    can be determined."""
    reported = metar.get("fltCat")
    if reported in FLIGHT_CATEGORIES:
        return reported
    vis = _visibility_sm(metar.get("visib"))
    ceiling = _ceiling_ft(metar.get("clouds"))
    if vis is None and ceiling is None:
        return None
    if (ceiling is not None and ceiling < 500) or (vis is not None and vis < 1):
        return "LIFR"
    if (ceiling is not None and ceiling < 1000) or (vis is not None and vis < 3):
        return "IFR"
    if (ceiling is not None and ceiling <= 3000) or (vis is not None and vis <= 5):
        return "MVFR"
    return "VFR"


def _obs_epoch(metar):
    t = metar.get("obsTime")
    if isinstance(t, (int, float)):
        return float(t)
    return _parse_time(metar.get("reportTime")) or 0.0


def summarize_metars(items, wanted):
    """Latest METAR per wanted station -> {icao: {cat, raw, obs}}."""
    latest = {}
    for item in items or []:
        icao = item.get("icaoId")
        if icao in wanted and (icao not in latest or _obs_epoch(item) > _obs_epoch(latest[icao])):
            latest[icao] = item
    return {
        icao: {"cat": flight_category(m), "raw": m.get("rawOb"), "obs": _iso(_obs_epoch(m) or None)}
        for icao, m in latest.items()
    }


# ---- SIGMETs ----

def _parse_time(value):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _iso(epoch):
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _in_region(coords):
    lon_min, lat_min, lon_max, lat_max = REGION
    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    return max(lons) >= lon_min and min(lons) <= lon_max and max(lats) >= lat_min and min(lats) <= lat_max


def summarize_sigmets(items, now=None):
    """Current SIGMETs over the region, as drawable polygons."""
    now = now if now is not None else time.time()
    out = []
    for item in items or []:
        coords = [[c.get("lat"), c.get("lon")] for c in item.get("coords") or []
                  if isinstance(c, dict) and isinstance(c.get("lat"), (int, float)) and isinstance(c.get("lon"), (int, float))]
        if len(coords) < 3 or not _in_region(coords):
            continue
        valid_to = _parse_time(item.get("validTimeTo"))
        if valid_to is not None and valid_to < now:
            continue
        hazard = (item.get("hazard") or "").upper()
        out.append({
            "id": item.get("seriesId") or "",
            "hazard": hazard,
            "hazard_name": HAZARDS.get(hazard, hazard.title() or "Hazard"),
            "qualifier": item.get("qualifier") or "",
            "fir": item.get("firName") or item.get("firId") or "",
            "from": _iso(_parse_time(item.get("validTimeFrom"))),
            "to": _iso(valid_to),
            "base": item.get("base"),
            "top": item.get("top"),
            "dir": item.get("dir"),
            "spd": item.get("spd"),
            "chng": item.get("chng"),
            "raw": item.get("rawSigmet") or "",
            "coords": coords,
        })
    return out


# ---- Cached fetching ----

class _Cache:
    """One fetch per TTL per process; on a failed refresh the last good
    data keeps being served (flagged stale) rather than an empty map."""

    def __init__(self, ttl_seconds):
        self.ttl = ttl_seconds
        self.data = None
        self.fetched_at = 0.0
        self.lock = threading.Lock()

    def get(self, loader):
        with self.lock:
            fresh = self.data is not None and time.time() - self.fetched_at < self.ttl
            if not fresh:
                try:
                    self.data = loader()
                    self.fetched_at = time.time()
                except Exception:
                    if self.data is None:
                        raise
                    return self.data, self.fetched_at, True
            return self.data, self.fetched_at, False


_metar_cache = _Cache(ttl_seconds=10 * 60)
_sigmet_cache = _Cache(ttl_seconds=5 * 60)


def _awc_get(path, params, user_agent):
    resp = requests.get(f"{AWC_BASE}/{path}", params=params, headers={"User-Agent": user_agent}, timeout=10)
    resp.raise_for_status()
    return resp.json() if resp.content.strip() else []


def metar_categories(icao_list, user_agent):
    wanted = set(icao_list)

    def load():
        items = _awc_get("metar", {"ids": ",".join(sorted(wanted)), "format": "json"}, user_agent)
        return summarize_metars(items, wanted)

    stations, fetched_at, stale = _metar_cache.get(load)
    return {"updated": _iso(fetched_at), "stale": stale, "stations": stations}


def current_sigmets(user_agent):
    def load():
        return _awc_get("isigmet", {"format": "json"}, user_agent)

    items, fetched_at, stale = _sigmet_cache.get(load)
    return {"updated": _iso(fetched_at), "stale": stale, "sigmets": summarize_sigmets(items)}
