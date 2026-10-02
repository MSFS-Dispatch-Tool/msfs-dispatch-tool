"""NOTAMs for a leg's departure and destination, from the SimBrief OFP.

SimBrief sends each airport's NOTAMs under origin.notam / destination.notam
(a list, a single object when there is one, missing when NOTAMs are off in
the pilot's SimBrief options), with the ICAO Q-code SimBrief assessed for
each. The format isn't published, so every field is read defensively.

Each NOTAM is graded for the briefing:
  major        the airport or a runway closed, ILS or approach not available,
               fuel not available
  significant  approach or runway lighting, declared distances, part of a
               runway, VOR/DME out of service
  other        everything else (taxiways, obstacles, stands, procedures...)
by its Q-code (ICAO Doc 8126: letters 2-3 the subject, 4-5 the condition),
falling back to the plain text when there is no usable Q-code.
"""
import re
from datetime import datetime, timezone

MAX_PER_AIRPORT = 60
MAX_TEXT = 900

# Q-code subject (letters 2-3) -> grade when its condition makes it unusable
_UNUSABLE = {"LC", "AS", "AU", "AW", "CC", "CN"}   # closed, unserviceable, not available, withdrawn
_MAJOR_SUBJECTS = {
    "FA": "aerodrome", "MR": "runway", "IC": "ILS", "IG": "glide path", "IL": "localizer", "IS": "ILS",
    "IT": "ILS", "IU": "ILS", "IW": "MLS", "PI": "instrument approach", "PA": "approach procedure",
    "FU": "fuel",
}
_SIGNIFICANT_SUBJECTS = {
    "LA": "approach lighting", "LR": "runway lighting", "LH": "high intensity runway lights", "LP": "PAPI",
    "LI": "runway edge lights", "LC": "centreline lights", "LZ": "touchdown zone lights", "MD": "declared distances",
    "NV": "VOR", "ND": "DME", "NB": "NDB", "IM": "ILS marker", "ID": "ILS DME",
}
_TEXT_MAJOR = [
    (re.compile(r"\bAD\s+CLSD\b"), "aerodrome closed"),
    (re.compile(r"\bRWY\s*[0-9]{2}[LCR]?(?:/[0-9]{2}[LCR]?)?\s+CLSD\b"), "runway closed"),
    (re.compile(r"\b(?:ILS|LOC|LLZ|GP)\b[^.]{0,30}\b(?:U/S|UNSERVICEABLE|NOT AVBL|OUT OF SERVICE)\b"), "ILS out of service"),
    (re.compile(r"\b(?:FUEL|JET\s*A-?1|AVTUR)\b[^.]{0,30}\b(?:NOT AVBL|UNAVBL|NOT AVAILABLE)\b"), "fuel not available"),
]
_TEXT_SIGNIFICANT = [
    (re.compile(r"\b(?:ALS|APPROACH LIGHT\w*|PAPI|RCLL|TDZ\w*|REDL|HIRL)\b[^.]{0,30}\b(?:U/S|UNSERVICEABLE|NOT AVBL)\b"), "lighting out of service"),
    (re.compile(r"\b(?:TORA|TODA|ASDA|LDA)\b"), "declared distances changed"),
    (re.compile(r"\b(?:VOR|DME|NDB)\b[^.]{0,30}\b(?:U/S|UNSERVICEABLE|NOT AVBL)\b"), "navaid out of service"),
]


def _as_list(value):
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return [value] if isinstance(value, dict) else []


def _text(value, limit=MAX_TEXT):
    return str(value).strip()[:limit] if isinstance(value, (str, int, float)) else ""


def parse_time(value):
    """Epoch seconds or an ISO/"YYYY-MM-DD HH:MM:SS" string -> aware UTC
    datetime; None when missing or unreadable."""
    if value in (None, "", {}):
        return None
    try:
        n = float(value)
        if n > 10 ** 11:          # milliseconds
            n /= 1000
        return datetime.fromtimestamp(n, tz=timezone.utc)
    except (TypeError, ValueError):
        pass
    text = str(value).strip().replace("Z", "+00:00").replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def grade(qcode, text):
    """("major" | "significant" | "other", short reason)."""
    q = re.sub(r"[^A-Z]", "", (qcode or "").upper())
    if len(q) == 5 and q[0] == "Q":
        subject, condition = q[1:3], q[3:5]
        if subject in _MAJOR_SUBJECTS and (condition in _UNUSABLE or (subject == "FU" and condition in {"AU", "AH"})):
            return "major", f"{_MAJOR_SUBJECTS[subject]} {'closed' if condition in {'LC', 'CC'} else 'not available'}"
        if subject in _SIGNIFICANT_SUBJECTS and condition in _UNUSABLE | {"LT", "CH", "AH"}:
            return "significant", f"{_SIGNIFICANT_SUBJECTS[subject]} {'changed' if condition in {'CH', 'LT', 'AH'} else 'out of service'}"
        if subject == "MR" and condition in {"LT", "CH", "LD", "LP"}:
            return "significant", "runway restricted"
    upper = (text or "").upper()
    for pattern, reason in _TEXT_MAJOR:
        if pattern.search(upper):
            return "major", reason
    for pattern, reason in _TEXT_SIGNIFICANT:
        if pattern.search(upper):
            return "significant", reason
    return "other", ""


def airport_notams(items, icao, now=None):
    """One airport's NOTAMs, graded, live ones only, major first."""
    now = now or datetime.now(timezone.utc)
    out = []
    for n in items:
        text = _text(n.get("notam_text")) or _text(n.get("notam_raw")) or _text(n.get("notam_html"))
        if not text:
            continue
        start, end = parse_time(n.get("date_effective")), parse_time(n.get("date_expire"))
        if (start and start > now) or (end and end < now):
            continue
        level, reason = grade(_text(n.get("notam_qcode"), 8), text)
        out.append({
            "icao": icao, "id": _text(n.get("notam_id"), 20), "level": level, "reason": reason,
            "text": re.sub(r"<[^>]+>", "", text),
            "from": start.strftime("%d%b %H%MZ").upper() if start else "",
            "to": end.strftime("%d%b %H%MZ").upper() if end else ("PERM" if str(n.get("date_expire", "")).upper().startswith("PERM") else ""),
        })
        if len(out) >= MAX_PER_AIRPORT:
            break
    order = {"major": 0, "significant": 1, "other": 2}
    return sorted(out, key=lambda x: order[x["level"]])


def ofp_notams(data, now=None):
    """Departure and destination NOTAMs from a SimBrief OFP, or None when
    the OFP carries none at all (NOTAMs switched off in SimBrief)."""
    found = False
    result = []
    for section in ("origin", "destination"):
        block = data.get(section) if isinstance(data.get(section), dict) else {}
        if "notam" not in block:
            continue
        found = True
        result += airport_notams(_as_list(block.get("notam")), _text(block.get("icao_code"), 4), now)
    return result if found else None
