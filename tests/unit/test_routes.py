"""HTTP endpoints, in-process, without a database or Supabase."""
from urllib.parse import parse_qs, urlparse

import pytest


def test_search_needs_an_airport(client):
    assert client.get("/search").status_code == 400
    assert client.get("/search?origin=ZZZZ").status_code == 400
    data = client.get("/search?origin=EGKK").get_json()
    assert data["count"] > 100
    assert all(i["departure_icao"] == "EGKK" for i in data["itineraries"])


def test_one_aircraft_flies_every_leg_of_a_trip(client):
    """easyJet's schedule data has no aircraft type, so the type is picked
    for the trip: both legs of a round trip must get the same one."""
    trips = [i for i in client.get("/search?origin=EGKK&airline=EZY").get_json()["itineraries"] if i["legs"] == 2][:4]
    assert trips
    for trip in trips:
        for _ in range(10):
            legs = client.get("/select?flights=" + ",".join(trip["flight_numbers"])).get_json()["legs"]
            assert len({leg["aircraft_type"] for leg in legs}) == 1
            assert len({leg["seat_capacity"] for leg in legs}) == 1


def test_select_rolls_a_delay_on_the_first_leg_only(client):
    trip = next(i for i in client.get("/search?origin=EGKK").get_json()["itineraries"] if i["legs"] == 2)
    for _ in range(20):
        legs = client.get("/select?flights=" + ",".join(trip["flight_numbers"])).get_json()["legs"]
        assert legs[1]["delay"] is None and legs[1]["atfm"] is None


def test_confirm_gives_callsigns_only(client):
    data = client.get("/confirm?flights=FR113").get_json()
    assert data["callsigns"][0].startswith("RYR") and "lmc_event" not in data


def test_loadsheet_extras_respect_the_full_flag(client):
    assert client.get("/loadsheet/extras?flight=NOPE").status_code == 400

    def events(full):
        got = (client.get(f"/loadsheet/extras?flight=FR113&full={full}").get_json()["lmc_event"] for _ in range(150))
        return {e["id"] for e in got if e}
    assert "lmc-02" not in events(1)
    assert "lmc-08" not in events(0)


@pytest.mark.parametrize("next_url, safe", [
    ("/app", True), ("/app?from=EGKK", True), ("https://evil.example", False), ("//evil.example", False),
    ("/\\evil.example", False), ("", False), (None, False),
])
def test_login_redirect_stays_on_site(app_module, next_url, safe):
    assert (app_module._safe_next(next_url) is not None) is safe


def test_simbrief_link_carries_the_dispatch_inputs(client):
    resp = client.get("/simbrief/redirect-url?flights=FR113&civalue=8&pax=170&taxi_out_minutes=12&aircraft_type=738"
                      "&eobt=07:55Z&callsign=RYR7K&max_fl=250&extra_fuel_min=15&remarks=MEL 21 PACK <script>")
    url = resp.get_json()["url"]
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q["orig"] == "EGKK" and q["type"] == "B738" and q["deph"] == "07" and q["depm"] == "55"
    assert q["fl"] == "FL250" and q["addedfuel"] == "15" and q["addedfuel_units"] == "min"
    assert q["manualrmk"] == "MEL 21 PACK script"            # only safe characters reach SimBrief
    long = "MEL 24-21-01: ENGINE GENERATOR 2 INOPERATIVE\nCREW ACTIONS: KEEP THE APU RUNNING; EXTRA FUEL (5 MIN)\n" * 60
    q = parse_qs(urlparse(client.get("/simbrief/redirect-url", query_string={
        "flights": "FR113", "civalue": 8, "pax": 170, "taxi_out_minutes": 12, "aircraft_type": "738", "remarks": long}).get_json()["url"]).query)
    rmk = q["manualrmk"][0]
    assert "\n" in rmk and "(5 MIN)" in rmk and len(rmk) <= 2500        # multi-line, capped
    # out-of-range values are dropped, not passed on
    url = client.get("/simbrief/redirect-url?flights=FR113&civalue=8&pax=1&taxi_out_minutes=12&aircraft_type=738"
                     "&max_fl=999&extra_fuel_min=500").get_json()["url"]
    assert "fl=" not in url.replace("fltnum", "") and "addedfuel" not in url


def test_simbrief_ofp_survives_odd_responses(client, app_module, monkeypatch):
    class Resp:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    for body in ({"fetch": "oops", "general": [], "params": None, "navlog": "x"}, ["not", "a", "dict"]):
        monkeypatch.setattr(app_module.requests, "get", lambda *a, _b=body, **k: Resp(_b))
        resp = client.get("/simbrief/ofp?username=someone")
        assert resp.status_code in (200, 502)
        assert resp.is_json


def test_generation_options_and_settings_without_a_database(client):
    opts = client.get("/generation-options").get_json()
    assert {"delay", "lmc", "mel", "cdl", "default_probability"} <= set(opts)
    assert any(d["code"] == "77" for d in opts["delay"])
    assert client.post("/settings", json={}).status_code == 502     # explicit, not a silent no-op


def test_request_size_is_capped(client):
    resp = client.post("/auth/session", data=b"x" * (5 * 1024 * 1024), content_type="application/json")
    assert resp.status_code == 413


def test_responses_are_gzipped_and_static_files_versioned(client):
    resp = client.get("/network", headers={"Accept-Encoding": "gzip"})
    assert resp.headers.get("Content-Encoding") == "gzip"
    page = client.get("/privacy").get_data(as_text=True)
    assert "/static/" in page
    resp = client.get("/static/js/app.js", headers={"Accept-Encoding": "gzip"})
    assert resp.headers.get("Content-Encoding") == "gzip" and "max-age=31536000" in resp.headers.get("Cache-Control", "")
