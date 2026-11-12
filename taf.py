"""Destination alternate weather: the alternate's TAF against planning
minima for the time the aircraft would get there.

Planning minima are the EASA ones for a destination alternate with a CAT I
precision approach (AMC1 CAT.OP.MPA.185): decision height + 200 ft and
RVR/visibility + 800 m. With a standard CAT I ILS (200 ft, 550 m) that is
a ceiling of 400 ft and a visibility of 1350 m. The approaches each
airport actually has aren't in the app's data, so every alternate is
planned as having one.

The forecast is read over the period from 1 hour before to 1 hour after
the estimated time at the alternate, as the rule asks:
- the main forecast and FM groups, and both the old and the new
  conditions while a BECMG change is under way;
- TEMPO groups, except deteriorations in showers or thunderstorms, which
  are short-lived and only a caution;
- PROB30/PROB40 groups, which may be disregarded for planning and are
  also only a caution.
"""
import re
from datetime import datetime, timedelta, timezone

CEILING_FT = 400
VISIBILITY_M = 1350
WINDOW = timedelta(hours=1)

_PERIOD = re.compile(r"^(\d{2})(\d{2})/(\d{2})(\d{2})$")
_FM = re.compile(r"^FM(\d{2})(\d{2})(\d{2})$")
_CLOUD = re.compile(r"^(FEW|SCT|BKN|OVC|VV)(\d{3})(?:CB|TCU)?$")
_METRES = re.compile(r"^(\d{4})(?:NDV)?$")
_MILES = re.compile(r"^(P)?(\d+)?(?:\s?(\d)/(\d))?SM$")
_TS = re.compile(r"^(?:[+-]|VC)?TS")
_SHOWERS = re.compile(r"^(?:[+-]|VC)?(?:SH|TS)")
_CB = re.compile(r"\d{3}(?:CB|TCU)$")


def _when(day, hour, minute, ref):
    """The datetime for a TAF day/hour/minute, in the month that puts it
    nearest the reference time (hour 24 is the next day's 00)."""
    best = None
    for months in (-1, 0, 1):
        year, month = ref.year, ref.month + months
        if month < 1:
            year, month = year - 1, 12
        elif month > 12:
            year, month = year + 1, 1
        try:
            t = datetime(year, month, day, 0, minute, tzinfo=timezone.utc) + timedelta(hours=hour)
        except ValueError:
            continue
        if best is None or abs(t - ref) < abs(best - ref):
            best = t
    return best


def _conditions(tokens):
    """What a group of tokens says: {"vis": metres, "ceiling": feet or
    None for none, "ts": bool, "showers": bool}, only the keys it gives."""
    found = {}
    clouds = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "CAVOK":
            found["vis"], found["ceiling"] = 10000, None
        elif tok in ("NSC", "SKC", "CLR", "NCD"):
            found["ceiling"] = None
        elif _METRES.match(tok):
            found["vis"] = 10000 if tok.startswith("9999") else int(tok[:4])
        elif tok.endswith("SM") or (re.match(r"^\d$", tok) and i + 1 < len(tokens) and re.match(r"^\d/\dSM$", tokens[i + 1])):
            text = tok if tok.endswith("SM") else tok + " " + tokens[i + 1]
            if not tok.endswith("SM"):
                i += 1
            m = _MILES.match(text)
            if m:
                miles = float(m.group(2) or 0) + (float(m.group(3)) / float(m.group(4)) if m.group(3) else 0)
                found["vis"] = 10000 if m.group(1) else round(miles * 1609)
        elif _CLOUD.match(tok):
            cover, base = _CLOUD.match(tok).groups()
            if cover in ("BKN", "OVC", "VV"):
                clouds.append(int(base) * 100)
            else:
                clouds.append(None)
            if _CB.search(tok):
                found["ts"] = True
        elif _TS.match(tok):
            found["ts"] = True
        if _SHOWERS.match(tok):
            found["showers"] = True
        i += 1
    if clouds:
        bases = [c for c in clouds if c is not None]
        found["ceiling"] = min(bases) if bases else None
    return found


def parse(raw, ref):
    """A TAF as {"from", "to", "groups": [{"kind", "from", "to", "wx"}]};
    kind is BASE, FM, BECMG, TEMPO or PROB. None when it can't be read."""
    tokens = (raw or "").upper().replace("=", " ").split()
    valid = next((i for i, t in enumerate(tokens) if _PERIOD.match(t)), None)
    if valid is None:
        return None
    d1, h1, d2, h2 = (int(x) for x in _PERIOD.match(tokens[valid]).groups())
    start, end = _when(d1, h1, 0, ref), _when(d2, h2, 0, ref)
    if not start or not end:
        return None
    if end <= start:
        end = _when(d2, h2, 0, start + timedelta(days=1))
    groups, current = [], {"kind": "BASE", "from": start, "to": end, "tokens": []}
    i = valid + 1
    while i < len(tokens):
        tok = tokens[i]
        fm = _FM.match(tok)
        kind = None
        if fm:
            kind, t1, t2 = "FM", _when(int(fm.group(1)), int(fm.group(2)), int(fm.group(3)), start), end
        elif tok in ("BECMG", "TEMPO") or tok.startswith("PROB"):
            kind = "PROB" if tok.startswith("PROB") else tok
            if kind == "PROB" and i + 1 < len(tokens) and tokens[i + 1] == "TEMPO":
                i += 1
            if i + 1 < len(tokens) and _PERIOD.match(tokens[i + 1]):
                a, b, c, d = (int(x) for x in _PERIOD.match(tokens[i + 1]).groups())
                t1, t2 = _when(a, b, 0, start), _when(c, d, 0, start)
                i += 1
            else:
                kind = None
        if kind:
            groups.append(current)
            current = {"kind": kind, "from": t1, "to": t2, "tokens": []}
        else:
            current["tokens"].append(tok)
        i += 1
    groups.append(current)
    for g in groups:
        g["wx"] = _conditions(g.pop("tokens"))
    return {"from": start, "to": end, "groups": groups}


def _below(wx):
    reasons = []
    if wx.get("vis") is not None and wx["vis"] < VISIBILITY_M:
        reasons.append(f"VIS {wx['vis']} M")
    if wx.get("ceiling") is not None and wx["ceiling"] < CEILING_FT:
        reasons.append(f"CEILING {wx['ceiling']} FT")
    return reasons


def _hhmm(t):
    return t.strftime("%H%M")


def assess(raw, eta):
    """The alternate's forecast around its ETA (a UTC datetime):
    {"status": "ok" | "caution" | "below" | "unknown", "text"}."""
    if not raw:
        return {"status": "unknown", "text": "NO TAF. NOT USABLE AS ALTERNATE"}
    taf = parse(raw, eta)
    if not taf:
        return {"status": "unknown", "text": "TAF NOT READABLE"}
    w0, w1 = eta - WINDOW, eta + WINDOW
    if w0 >= taf["to"] or w1 <= taf["from"]:
        return {"status": "unknown", "text": f"TAF NOT VALID {_hhmm(w0)}-{_hhmm(w1)}Z"}

    # The prevailing conditions over the window, FM and BECMG applied in turn
    state = dict(taf["groups"][0]["wx"])
    prevailing, below, caution = [], [], []
    for g in taf["groups"][1:]:
        if g["kind"] == "FM":
            if g["from"] <= w0:
                state = dict(g["wx"])
            elif g["from"] < w1:
                prevailing.append(dict(state))
                state = dict(g["wx"])
        elif g["kind"] == "BECMG" and g["from"] < w1:
            if g["to"] > w0:
                prevailing.append(dict(state))
            state = {**state, **g["wx"]}
    prevailing.append(state)
    for wx in prevailing:
        below += _below(wx)
        if wx.get("ts"):
            caution.append("TS")
    # Temporary and probable changes in the window, on the conditions they change
    for g in taf["groups"][1:]:
        if g["kind"] not in ("TEMPO", "PROB") or g["to"] <= w0 or g["from"] >= w1:
            continue
        wx = {**state, **g["wx"]}
        reasons = _below(wx)
        when = f"{_hhmm(g['from'])}-{_hhmm(g['to'])}Z"
        if g["wx"].get("ts"):
            caution.append(f"TS {g['kind']} {when}")
        if reasons and g["kind"] == "TEMPO" and not g["wx"].get("showers"):
            below += [f"{r} (TEMPO {when})" for r in reasons]
        elif reasons:
            caution.append(f"{'SHOWERS' if g['kind'] == 'TEMPO' else 'PROB'} {when}: {', '.join(reasons)}")
    window = f"{_hhmm(w0)}-{_hhmm(w1)}Z"
    if below:
        return {"status": "below", "text": f"BELOW PLANNING MINIMA {window}: {', '.join(dict.fromkeys(below))}"}
    if caution:
        return {"status": "caution", "text": f"ABOVE PLANNING MINIMA {window}. CAUTION: {', '.join(dict.fromkeys(caution))}"}
    return {"status": "ok", "text": f"ABOVE PLANNING MINIMA {window}"}
