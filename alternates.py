"""Destination alternates for the SimBrief plan, offered by the app instead
of left to SimBrief: airports the leg's airline serves (so suitable for its
aircraft and handled by the company), within reach of the destination: the
nearest of the airline's bases, then the nearest of its other airports.
The pilot can also leave the choice to SimBrief.

A base is an airport the airline flies many departures from in the
schedule data: at least BASE_SHARE of its busiest airport's departures,
an approximation of the airline's bases with no list to keep up to date.
"""
import collections

from flightstats import great_circle_nm

MIN_NM = 10          # closer is the same aerodrome area: no alternate
MAX_NM = 200         # usual reach of a short-haul destination alternate
EXTENDED_NM = 350    # only when nothing is within MAX_NM
BASE_SHARE = 0.15
SHOWN = 5            # bases, and other airports, offered at most


def departures_by_airline(routes):
    """{airline: Counter({icao: departures})}, every airport the airline
    serves included (0 for one it only flies into)."""
    out = collections.defaultdict(collections.Counter)
    for r in routes:
        out[r["carrier"]][r["departure_icao"]] += 1
        out[r["carrier"]][r["arrival_icao"]] += 0
    return out


def bases(departures):
    top = max(departures.values(), default=0)
    return {icao for icao, n in departures.items() if top and n >= BASE_SHARE * top}


def alternates(dest_icao, departures, airports_by_icao):
    """The alternates for a destination, best first: up to SHOWN bases by
    distance, then up to SHOWN other airports by distance: [{"icao", "city",
    "nm", "base"}]. Empty when the airline serves nothing in reach."""
    dest = airports_by_icao.get(dest_icao)
    if not dest:
        return []
    company_bases = bases(departures)
    found = []
    for icao in departures:
        airport = airports_by_icao.get(icao)
        if icao == dest_icao or not airport:
            continue
        nm = great_circle_nm(dest, airport)
        if nm is not None and nm >= MIN_NM:
            city = (airport.get("city") or airport.get("name") or "").split(" (")[0].split(",")[0]
            found.append({"icao": icao, "city": city,
                          "nm": nm, "base": icao in company_bases})
    for reach in (MAX_NM, EXTENDED_NM):
        within = [a for a in found if a["nm"] <= reach]
        if within:
            within.sort(key=lambda a: a["nm"])
            return [a for a in within if a["base"]][:SHOWN] + [a for a in within if not a["base"]][:SHOWN]
    return []
