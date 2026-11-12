"""Destination alternates for the SimBrief plan, offered by the app instead
of left to SimBrief: the airline's own bases (airports it operates from,
so suitable for its aircraft and staffed by the company), within reach of
the destination, nearest first. The pilot can also leave the choice to
SimBrief.

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
SHOWN = 6            # bases offered at most


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
    """The alternates for a destination: up to SHOWN of the airline's bases
    by distance, [{"icao", "name" (the airport's short name, e.g. "Linate"),
    "nm"}]. Empty when the airline serves nothing in reach."""
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
        if icao in company_bases and nm is not None and nm >= MIN_NM:
            found.append({"icao": icao, "name": airport.get("short_name") or airport.get("name") or "", "nm": nm})
    for reach in (MAX_NM, EXTENDED_NM):
        within = [a for a in found if a["nm"] <= reach]
        if within:
            return sorted(within, key=lambda a: a["nm"])[:SHOWN]
    return []
