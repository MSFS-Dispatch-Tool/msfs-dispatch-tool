"""Build static/data/landing_map.json - the map of Europe behind the landing
page's route demo: land as one SVG path, plus the demo's airports placed on
it, all pre-projected so the page draws it with no map library.

    python tools/landing_map/build_landing_map.py

Input: the app's own static/data/basemap.json (Natural Earth, see
tools/basemap) and data/airports_world.json. Mercator, cropped to the
route network; outlines simplified to about 2 units of the map's
1000-unit width, and islets under 6 units across dropped.
"""

import json
import math
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEST = os.path.join(ROOT, "static", "data", "landing_map.json")

WEST, EAST, SOUTH, NORTH = -12.5, 31.0, 34.5, 61.0
WIDTH = 1000
MIN_STEP = 2.2            # drop points closer than this (map units) to the last one kept
MIN_RING = 6.0            # drop islets smaller than this across (map units)

# Bases first (the demo's departure choices), then destinations, with the
# city names the demo shows
AIRPORTS = {
    "EGKK": "London Gatwick", "EIDW": "Dublin", "EGGW": "London Luton", "LHBP": "Budapest",
    "LEMD": "Madrid", "LEBL": "Barcelona", "LEPA": "Palma", "LEMG": "Malaga", "LEAL": "Alicante", "LEIB": "Ibiza",
    "LPPT": "Lisbon", "LPFR": "Faro", "LPPR": "Porto", "LFPG": "Paris", "LFMN": "Nice", "LFLL": "Lyon",
    "LIRF": "Rome", "LIMC": "Milan", "LIPZ": "Venice", "LICC": "Catania", "LIRN": "Naples", "LGAV": "Athens",
    "LGIR": "Heraklion", "LGRP": "Rhodes", "LMML": "Malta", "EDDB": "Berlin", "EDDM": "Munich", "EDDH": "Hamburg",
    "EHAM": "Amsterdam", "EBBR": "Brussels", "LSGG": "Geneva", "LOWW": "Vienna", "LKPR": "Prague", "EPKK": "Krakow",
    "EPWA": "Warsaw", "EKCH": "Copenhagen", "ENGM": "Oslo", "ESSA": "Stockholm", "EFHK": "Helsinki",
    "EGPH": "Edinburgh", "EGAC": "Belfast", "EGNX": "East Midlands", "EINN": "Shannon", "EICK": "Cork",
    "LROP": "Bucharest", "LBSF": "Sofia", "LWSK": "Skopje", "LYBE": "Belgrade", "LDZA": "Zagreb",
    "LTFM": "Istanbul", "GMMX": "Marrakesh", "LHDC": "Debrecen", "LZIB": "Bratislava", "EVRA": "Riga", "EYVI": "Vilnius",
}


def project(lon, lat):
    def y(la):
        return math.log(math.tan(math.pi / 4 + math.radians(la) / 2))
    scale = WIDTH / math.radians(EAST - WEST)
    return (math.radians(lon - WEST) * scale, (y(NORTH) - y(lat)) * scale)


def ring_path(ring):
    pts, last = [], None
    for lon, lat in ring:
        x, y = project(lon, lat)
        if last and math.hypot(x - last[0], y - last[1]) < MIN_STEP:
            continue
        pts.append((x, y))
        last = (x, y)
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    if len(pts) < 3 or max(max(xs) - min(xs), max(ys) - min(ys)) < MIN_RING:
        return ""
    return "M" + "L".join(f"{x:.0f},{y:.0f}" for x, y in pts) + "Z"


def main():
    base = json.load(open(os.path.join(ROOT, "static", "data", "basemap.json")))
    height = project(WEST, SOUTH)[1]
    paths = []
    for feature in base["countries"]["features"]:
        geom = feature["geometry"]
        polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
        for poly in polys:
            ring = poly[0]
            lons, lats = [p[0] for p in ring], [p[1] for p in ring]
            if max(lons) < WEST - 2 or min(lons) > EAST + 2 or max(lats) < SOUTH - 2 or min(lats) > NORTH + 2:
                continue
            paths.append(ring_path(ring))
    by_icao = {a["icao"]: a for a in json.load(open(os.path.join(ROOT, "data", "airports_world.json")))}
    airports = {}
    for icao, city in AIRPORTS.items():
        a = by_icao[icao]
        x, y = project(a["lon"], a["lat"])
        airports[icao] = {"x": round(x, 1), "y": round(y, 1), "lat": round(a["lat"], 2), "lon": round(a["lon"], 2),
                          "city": city, "country": a["country"]}
    out = {"width": WIDTH, "height": round(height, 1), "land": "".join(p for p in paths if p), "airports": airports}
    with open(DEST, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"{DEST}: {os.path.getsize(DEST) // 1024} KB, {len(airports)} airports")


if __name__ == "__main__":
    main()
