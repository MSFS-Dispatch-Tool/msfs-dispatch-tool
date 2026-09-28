"""Build static/data/basemap.json - the app's own minimal map background
(land, country borders, country names, major cities) drawn by Leaflet
instead of OpenStreetMap's detailed raster tiles.

    python tools/basemap/build_basemap.py

Source: Natural Earth 1:50m admin-0 countries and populated places
(public domain), from the official natural-earth-vector repository.
The region around the route network is kept in detail (outlines
simplified to ~1.5 km, coordinates rounded to ~1 km: sharp up to the maps'
maximum zoom of 8); the rest of the world only coarsely, with its largest
cities, since it only ever appears zoomed out (e.g. a pilot's home base
far from the network on the profile map).
"""

import json
import math
import os
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEST = os.path.join(HERE, "..", "..", "static", "data", "basemap.json")
SOURCE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/{}.geojson"

# Wide enough that the map never shows empty sea at its minimum zoom
# around the network (Iceland to the Canaries to Jordan).
BBOX = (-45.0, 5.0, 80.0, 82.0)          # min lon, min lat, max lon, max lat
TOLERANCE_DEG = 0.015                    # Douglas-Peucker, ~1.5 km
MIN_RING_AREA_DEG2 = 0.0015              # drops islets smaller than ~15 km2
WORLD_TOLERANCE_DEG = 0.25               # outside BBOX
WORLD_MIN_RING_AREA_DEG2 = 0.2
WORLD_MAX_CITY_MIN_ZOOM = 3
DECIMALS = 2
MAX_CITY_MIN_ZOOM = 5                    # Natural Earth's own "show from zoom"; above 5 are towns


def fetch(name):
    with urllib.request.urlopen(SOURCE.format(name)) as resp:
        return json.load(resp)


def perpendicular_distance(p, a, b):
    if a == b:
        return math.dist(p, a)
    (x, y), (x1, y1), (x2, y2) = p, a, b
    return abs((y2 - y1) * x - (x2 - x1) * y + x2 * y1 - y2 * x1) / math.dist(a, b)


def simplify(points, tolerance):
    """Iterative Douglas-Peucker (rings can be long enough to hit the
    recursion limit)."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        best, index = 0.0, None
        for i in range(start + 1, end):
            d = perpendicular_distance(points[i], points[start], points[end])
            if d > best:
                best, index = d, i
        if index is not None and best > tolerance:
            keep[index] = True
            stack.extend([(start, index), (index, end)])
    return [p for p, k in zip(points, keep) if k]


def ring_area(ring):
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:]))) / 2


def clean_ring(ring, tolerance, min_area):
    out = [[round(x, DECIMALS), round(y, DECIMALS)] for x, y in simplify(ring, tolerance)]
    deduped = [p for i, p in enumerate(out) if i == 0 or p != out[i - 1]]
    return deduped if len(deduped) >= 4 and ring_area(deduped) >= min_area else None


def polygons_of(geometry):
    if geometry["type"] == "Polygon":
        return [geometry["coordinates"]]
    return geometry["coordinates"]


def in_bbox(lon, lat):
    return BBOX[0] <= lon <= BBOX[2] and BBOX[1] <= lat <= BBOX[3]


def touches_bbox(polygons):
    return any(in_bbox(x, y) for poly in polygons for x, y in poly[0])


def build_countries(source):
    features = []
    for f in source["features"]:
        props = f["properties"]
        polygons = polygons_of(f["geometry"])
        detail = (TOLERANCE_DEG, MIN_RING_AREA_DEG2) if touches_bbox(polygons) \
            else (WORLD_TOLERANCE_DEG, WORLD_MIN_RING_AREA_DEG2)
        kept = []
        for poly in polygons:
            outer = clean_ring(poly[0], *detail)
            if outer:
                kept.append([outer] + [r for r in (clean_ring(h, *detail) for h in poly[1:]) if r])
        if not kept:
            continue
        features.append({
            "type": "Feature",
            "properties": {
                "name": props["NAME"],
                "label": [round(props["LABEL_X"], 2), round(props["LABEL_Y"], 2)],
                "minz": round(props["MIN_LABEL"], 1),
            },
            "geometry": {"type": "MultiPolygon", "coordinates": kept},
        })
    return features


def build_cities(source):
    cities = []
    for f in source["features"]:
        p = f["properties"]
        lon, lat = p["longitude"], p["latitude"]
        if p["min_zoom"] > (MAX_CITY_MIN_ZOOM if in_bbox(lon, lat) else WORLD_MAX_CITY_MIN_ZOOM):
            continue
        cities.append({
            "name": p["name"],
            "lat": round(lat, 3), "lon": round(lon, 3),
            "cap": 1 if p["adm0cap"] else 0,
            "minz": p["min_zoom"],
        })
    return sorted(cities, key=lambda c: (c["minz"], -c["cap"], c["name"]))


def main():
    countries = build_countries(fetch("ne_50m_admin_0_countries"))
    cities = build_cities(fetch("ne_50m_populated_places_simple"))
    os.makedirs(os.path.dirname(DEST), exist_ok=True)
    with open(DEST, "w", encoding="utf-8") as fh:
        json.dump({
            "source": "Natural Earth 1:50m (public domain)",
            "countries": {"type": "FeatureCollection", "features": countries},
            "cities": cities,
        }, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(countries)} countries, {len(cities)} cities, {os.path.getsize(DEST) // 1024} KB -> {DEST}")


if __name__ == "__main__":
    main()
