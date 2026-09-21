"""
SimDispatch - Flask app.
"""

from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import json
import os
import re
import secrets
import requests
from datetime import date, timedelta
from urllib.parse import urlencode
import db
from generator import (
    resolve_airport, find_round_trip_pairs, find_itineraries,
    generate_leg_conditions, roll_delay, generate_callsign, generate_loadsheet_extras
)
from timeutils import (
    resolve_leg_times, resolve_leg_schedule, format_zulu, turnaround_minutes,
    turnaround_shift, simbrief_date_str, TAXI_IN_MINUTES
)

app = Flask(__name__)

# Session signing key - set FLASK_SECRET_KEY in the environment so
# sessions (and therefore logins) survive a restart/redeploy. Without
# it, a random key is generated at boot and every existing session is
# invalidated the next time the process restarts.
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(days=30)

# Single shared-password gate for the whole app - not per-user accounts.
# Unset APP_PASSWORD means the gate is off entirely (today's default, so
# an existing deployment doesn't get locked out by this code landing
# before the env var is configured).
APP_PASSWORD = os.environ.get("APP_PASSWORD")


@app.before_request
def require_login():
    if not APP_PASSWORD:
        return
    if request.endpoint in ("login", "static"):
        return
    if not session.get("authenticated"):
        return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        entered = request.form.get("password", "")
        if APP_PASSWORD and secrets.compare_digest(entered, APP_PASSWORD):
            session.clear()
            session["authenticated"] = True
            session.permanent = True
            return redirect(request.args.get("next") or url_for("index"))
        error = "Incorrect password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def load_json(filename):
    path = os.path.join(DATA_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


routes = load_json("routes_enriched.json")
mels = load_json("mel_list.json")
delay_codes = load_json("delay_codes.json")
lmc_events = load_json("lmc_events.json")
dangerous_goods = load_json("dangerous_goods.json")
airports = load_json("airports.json")

routes_by_flight_number = {r["flight_number"]: r for r in routes}
country_by_icao = {a["icao"]: a["country"] for a in airports}
tz_by_icao = {a["icao"]: a["tz"] for a in airports}
airports_by_icao = {a["icao"]: a for a in airports}

rt_pairing = find_round_trip_pairs(routes)

WEATHER_USER_AGENT = "SimDispatch/1.0 (personal MSFS immersion tool; not for real-world ops use)"

# SimBrief dispatch-redirect / OFP fetch-back integration. Both are
# SimBrief's public, no-API-key mechanisms - not the gated "API v1" popup
# flow (that needs an emailed-and-approved key and is out of scope here).
SIMBRIEF_AIRLINE_ICAO = "RYR"  # Ryanair - hardcoded, this tool is RYR-only
SIMBRIEF_AIRCRAFT_TYPE = {"738": "B738"}
SIMBRIEF_AIRCRAFT_REG = "EI-DPN"  # pilot's own airframe - preselected so SimBrief
                                   # doesn't reset other fields after a manual pick


def fetch_weather_batch(icao_list):
    unique = sorted(set(icao_list))
    ids_param = ",".join(unique)
    result = {icao: {"metar": None, "taf": None} for icao in unique}
    try:
        resp = requests.get("https://aviationweather.gov/api/data/metar",
                             params={"ids": ids_param, "format": "json"},
                             headers={"User-Agent": WEATHER_USER_AGENT}, timeout=8)
        resp.raise_for_status()
        for item in resp.json():
            icao = item.get("icaoId")
            if icao in result:
                result[icao]["metar"] = item.get("rawOb")
    except Exception:
        pass
    try:
        resp = requests.get("https://aviationweather.gov/api/data/taf",
                             params={"ids": ids_param, "format": "json"},
                             headers={"User-Agent": WEATHER_USER_AGENT}, timeout=8)
        resp.raise_for_status()
        for item in resp.json():
            icao = item.get("icaoId")
            if icao in result:
                result[icao]["taf"] = item.get("rawTAF")
    except Exception:
        pass
    return result


def airport_info(icao):
    a = airports_by_icao.get(icao, {})
    return {
        "icao": icao,
        "iata": a.get("iata", ""),
        "name": a.get("name", "UNKNOWN"),
        "country": a.get("country", ""),
        "lat": a.get("lat"),
        "lon": a.get("lon"),
    }


def leg_summary_with_times(route, today):
    """Shared by /search (list view) and /select (detail view): airport
    info, cost-agnostic route facts, and Zulu dep/arr times."""
    dep_dt, arr_dt, arr_source = resolve_leg_times(route, tz_by_icao, today)
    # Arrival's "(+Nd)" suffix is relative to THIS leg's own departure
    # date, not "today" - a local-midnight departure at an airport east
    # of UTC legitimately lands on "yesterday" in Zulu (flagged on
    # departure), but an arrival an hour later isn't a further day
    # change and showing the same "-1d" on both just reads as a
    # contradiction.
    arr_reference = dep_dt.date() if dep_dt is not None else today
    return {
        "flight_number": route["flight_number"],
        "departure_info": airport_info(route["departure_icao"]),
        "arrival_info": airport_info(route["arrival_icao"]),
        "duration_minutes": route["duration_minutes"],
        "distance_nm": route["distance_nm"],
        "scheduled_departure_zulu": format_zulu(dep_dt, today),
        "scheduled_arrival_zulu": format_zulu(arr_dt, arr_reference),
        "arrival_time_source": arr_source,
        "_dep_dt": dep_dt, "_arr_dt": arr_dt,  # internal, stripped before jsonify
    }


def strip_internal(d):
    return {k: v for k, v in d.items() if not k.startswith("_")}


@app.route("/")
def index():
    counts = {
        "routes": len(routes), "mels": len(mels), "delay_codes": len(delay_codes),
        "lmc_events": len(lmc_events), "dangerous_goods": len(dangerous_goods),
    }
    return render_template("index.html", counts=counts, auth_enabled=bool(APP_PASSWORD))


@app.route("/airports/search")
def airports_search():
    q = request.args.get("q", "").strip().upper()
    if not q:
        return jsonify([])
    matches = [a for a in airports if a["icao"].startswith(q) or a["iata"].startswith(q) or a["city"].upper().startswith(q)]
    return jsonify(matches[:15])


@app.route("/airports/validate")
def airports_validate():
    code = request.args.get("code", "")
    icao = resolve_airport(airports, code)
    return jsonify({"valid": icao is not None, "icao": icao})


@app.route("/search")
def search():
    origin_raw = request.args.get("origin", default="", type=str)
    destination_raw = request.args.get("destination", default="", type=str)
    trip_type = request.args.get("trip_type", default="random", type=str)

    origin_icao = resolve_airport(airports, origin_raw) if origin_raw else None
    if origin_raw and origin_icao is None:
        return jsonify({"error": f"Unknown airport code: {origin_raw}"}), 400
    destination_icao = resolve_airport(airports, destination_raw) if destination_raw else None
    if destination_raw and destination_icao is None:
        return jsonify({"error": f"Unknown airport code: {destination_raw}"}), 400

    itineraries = find_itineraries(
        routes, rt_pairing,
        origin_icao=origin_icao, destination_icao=destination_icao,
        trip_type=trip_type
    )

    today = date.today()
    summaries = []
    for itin in itineraries:
        legs = itin["legs"]
        leg_data = [leg_summary_with_times(leg, today) for leg in legs]

        # A round-trip pair is matched by flight-number proximity, not by
        # which one actually departs first - real rotations sometimes fly
        # the higher-numbered leg first. Reorder by actual scheduled
        # departure time whenever both are resolvable, so leg[0] is
        # always the one that departs first.
        if itin["trip_type"] == "RT" and len(leg_data) == 2 \
                and leg_data[0]["_dep_dt"] is not None and leg_data[1]["_dep_dt"] is not None \
                and leg_data[1]["_dep_dt"] < leg_data[0]["_dep_dt"]:
            legs = [legs[1], legs[0]]
            leg_data = [leg_data[1], leg_data[0]]

        # Even in the right order, scraped schedule times occasionally
        # leave too little (or a negative) turnaround between the two
        # legs of a rotation. Push the second leg's whole schedule later
        # by whatever's needed for a realistic minimum turnaround.
        if itin["trip_type"] == "RT" and len(leg_data) == 2:
            shift = turnaround_shift(leg_data[0]["_arr_dt"], leg_data[1]["_dep_dt"])
            if shift:
                leg_data[1]["_dep_dt"] += shift
                leg_data[1]["_arr_dt"] += shift
                leg_data[1]["scheduled_departure_zulu"] = format_zulu(leg_data[1]["_dep_dt"], today)
                leg_data[1]["scheduled_arrival_zulu"] = format_zulu(leg_data[1]["_arr_dt"], leg_data[1]["_dep_dt"].date())

        # turnaround between legs (only meaningful for RT, 2 legs)
        for i in range(1, len(leg_data)):
            leg_data[i]["turnaround_minutes"] = turnaround_minutes(leg_data[i - 1]["_arr_dt"], leg_data[i]["_dep_dt"])

        dep_country = country_by_icao.get(legs[0]["departure_icao"], "")
        # "away" airport for RT = leg[0] arrival; for 1W = leg[-1] arrival
        away_country = country_by_icao.get(legs[0]["arrival_icao"], "")
        # For a round trip the rotation's final airport is always back at
        # origin, so "VIA" in the UI shows the away/turnaround airport
        # instead (the outbound leg's arrival). One-way legs fly direct,
        # so there's no via airport at all.
        via_icao = legs[0]["arrival_icao"] if itin["trip_type"] == "RT" else None

        first_dep_dt = leg_data[0]["_dep_dt"]
        last_arr_dt = leg_data[-1]["_arr_dt"]
        ebt_minutes = round((last_arr_dt - first_dep_dt).total_seconds() / 60) \
            if first_dep_dt is not None and last_arr_dt is not None else None

        summaries.append({
            "trip_type": itin["trip_type"],
            "flight_numbers": [l["flight_number"] for l in legs],
            "path": [legs[0]["departure_icao"]] + [l["arrival_icao"] for l in legs],
            "total_minutes": sum(l["duration_minutes"] for l in legs),
            "total_distance_nm": sum(l["distance_nm"] for l in legs),
            "ebt_minutes": ebt_minutes,
            "legs": len(legs),
            "departure_icao": legs[0]["departure_icao"],
            "arrival_icao": legs[-1]["arrival_icao"],
            "via_icao": via_icao,
            "departure_country": dep_country,
            "arrival_country": away_country,
            "domestic": dep_country != "" and dep_country == away_country,
            "leg_details": [strip_internal(l) for l in leg_data],
            "first_departure_zulu": leg_data[0]["scheduled_departure_zulu"],
            "last_arrival_zulu": leg_data[-1]["scheduled_arrival_zulu"],
        })

    return jsonify({"count": len(summaries), "itineraries": summaries})


@app.route("/select")
def select():
    flights_raw = request.args.get("flights", default="", type=str)
    flight_numbers = [f.strip() for f in flights_raw.split(",") if f.strip()]

    itinerary = []
    for fn in flight_numbers:
        leg = routes_by_flight_number.get(fn)
        if leg is None:
            return jsonify({"error": f"Unknown flight number: {fn}"}), 400
        itinerary.append(leg)

    if not itinerary:
        return jsonify({"error": "No flights specified."}), 400

    today = date.today()
    icao_needed = set()
    for leg in itinerary:
        icao_needed.add(leg["departure_icao"])
        icao_needed.add(leg["arrival_icao"])
    weather = fetch_weather_batch(list(icao_needed))

    legs_out = []
    leg_times = []
    for route_leg in itinerary:
        conditions = generate_leg_conditions(duration_minutes=route_leg["duration_minutes"])
        conditions["delay"] = roll_delay(delay_codes)
        conditions["flight_number"] = route_leg["flight_number"]
        conditions["departure_info"] = airport_info(route_leg["departure_icao"])
        conditions["arrival_info"] = airport_info(route_leg["arrival_icao"])
        conditions["weather"] = {
            "departure": weather.get(route_leg["departure_icao"], {"metar": None, "taf": None}),
            "arrival": weather.get(route_leg["arrival_icao"], {"metar": None, "taf": None}),
        }
        taxi_out = conditions["taxi_out_minutes"]
        schedule = resolve_leg_schedule(route_leg, tz_by_icao, today, taxi_out)
        sobt_dt, sibt_dt = schedule["_sobt_dt"], schedule["_sibt_dt"]

        # Enforce a minimum realistic turnaround against the previous leg
        # in this itinerary - scraped schedule times occasionally leave
        # too little (or a negative) turnaround. Shifts this leg's whole
        # schedule later by the deficit; never invents a different flight.
        if sobt_dt is not None and leg_times:
            prev_sibt_dt = leg_times[-1][1]
            shift = turnaround_shift(prev_sibt_dt, sobt_dt)
            if shift:
                sobt_dt += shift
                sibt_dt += shift

        if sobt_dt is not None:
            # STOT/SLDT/SIBT get their "(+Nd)" suffix relative to THIS
            # LEG's own SOBT date, not the external "today" reference.
            # SOBT itself is the one row that says how this leg's Zulu
            # clock relates to today (e.g. a local-midnight departure at
            # an eastern-of-UTC airport is legitimately "yesterday" in
            # Zulu) - repeating that same offset on every other row of
            # the same leg, when nothing actually crosses a further day
            # boundary, just reads as a contradiction ("how can arrival
            # be a different day from departure when they're an hour
            # apart?"), so those rows are only flagged if THEY cross a
            # boundary SOBT didn't already.
            sobt_date = sobt_dt.date()
            stot_dt = sobt_dt + timedelta(minutes=taxi_out)
            sldt_dt = sibt_dt - timedelta(minutes=TAXI_IN_MINUTES)
            conditions["sobt"] = format_zulu(sobt_dt, today)
            conditions["stot"] = format_zulu(stot_dt, sobt_date)
            conditions["sldt"] = format_zulu(sldt_dt, sobt_date)
            conditions["sibt"] = format_zulu(sibt_dt, sobt_date)
        else:
            sobt_date = None
            stot_dt = sldt_dt = None
            conditions["sobt"] = conditions["stot"] = conditions["sldt"] = conditions["sibt"] = None
        conditions["eet_minutes"] = schedule["eet_minutes"]

        # Expected (E-) times = scheduled (S-) times shifted by the leg's
        # own delay, if any - the higher end of the range if it's a
        # range. No delay means expected == scheduled. This is also what
        # /simbrief/redirect-url sends as deph/depm, not the raw SOBT.
        # Same day-suffix anchoring as above: EOBT vs today (it's the
        # same kind of figure as SOBT), ETOT/ELDT/EIBT vs this leg's own
        # SOBT date.
        delay_minutes = max(conditions["delay"]["duration_range_minutes"]) if conditions["delay"] else 0
        conditions["expected_delay_minutes"] = delay_minutes
        if sobt_dt is not None:
            delay_delta = timedelta(minutes=delay_minutes)
            conditions["eobt"] = format_zulu(sobt_dt + delay_delta, today)
            conditions["etot"] = format_zulu(stot_dt + delay_delta, sobt_date)
            conditions["eldt"] = format_zulu(sldt_dt + delay_delta, sobt_date)
            conditions["eibt"] = format_zulu(sibt_dt + delay_delta, sobt_date)
        else:
            conditions["eobt"] = conditions["etot"] = conditions["eldt"] = conditions["eibt"] = None

        leg_times.append((sobt_dt, sibt_dt))
        legs_out.append(conditions)

    for i in range(1, len(legs_out)):
        legs_out[i]["turnaround_minutes"] = turnaround_minutes(leg_times[i - 1][1], leg_times[i][0])

    return jsonify({"itinerary": itinerary, "legs": legs_out})


@app.route("/confirm")
def confirm():
    """
    Called when the person presses CONFIRM after reviewing a flight.
    Generates a callsign PER LEG (only happens here, never earlier -
    each flight number/sector gets its own, since real callsigns are
    per-flight, not per-rotation) and rolls dangerous goods + LMC
    together, as the loadsheet-signing moment.
    """
    flights_raw = request.args.get("flights", default="", type=str)
    flight_numbers = [f.strip() for f in flights_raw.split(",") if f.strip()]
    itinerary = [routes_by_flight_number[fn] for fn in flight_numbers if fn in routes_by_flight_number]
    if not itinerary:
        return jsonify({"error": "No flights specified."}), 400

    callsigns = [generate_callsign() for _ in flight_numbers]
    dg, lmc = generate_loadsheet_extras(dangerous_goods, lmc_events)

    return jsonify({
        "confirmation_id": callsigns[0],  # unique enough for this tool's purposes
        "callsigns": callsigns,           # one per flight_numbers[i], same order
        "flight_numbers": flight_numbers,
        "dangerous_goods": dg,
        "lmc_event": lmc,
        "leg_status": [{"flight_number": fn, "status": "pending"} for fn in flight_numbers],
    })


# ---------------------------------------------------------------------
# PIREP log - persisted to Postgres (db.py), not local disk. Render's
# free-tier disk is ephemeral and doesn't survive a redeploy, so if
# DATABASE_URL isn't configured, these routes degrade explicitly (502)
# rather than silently writing somewhere that will just lose data again.
# ---------------------------------------------------------------------

if db.db_available():
    try:
        db.init_db()
    except Exception as exc:
        print(f"[pireps] DATABASE_URL is set but init failed: {exc}")


def _require_db():
    if not db.db_available():
        return jsonify({"error": "No database configured (DATABASE_URL is unset) - the PIREP log is unavailable."}), 502
    return None


@app.route("/pireps", methods=["GET"])
def list_pireps():
    unavailable = _require_db()
    if unavailable:
        return unavailable
    return jsonify(db.list_pireps())


@app.route("/pireps", methods=["POST"])
def create_pirep():
    unavailable = _require_db()
    if unavailable:
        return unavailable
    payload = request.get_json(silent=True) or {}
    record = db.create_pirep(payload)
    return jsonify(record), 201


@app.route("/pireps/<pirep_id>", methods=["DELETE"])
def delete_pirep(pirep_id):
    unavailable = _require_db()
    if unavailable:
        return unavailable
    if not db.delete_pirep(pirep_id):
        return jsonify({"error": "PIREP not found."}), 404
    return jsonify({"deleted": pirep_id})


@app.route("/simbrief/redirect-url")
def simbrief_redirect_url():
    """
    Builds a SimBrief "dispatch redirect" URL - a plain link to SimBrief's
    own dispatch form, pre-filled via query string. Opening it just shows
    the pilot a pre-filled form on simbrief.com; they still press Generate
    there themselves. No API key involved.

    Aircraft type/reg are preselected to the pilot's own airframe
    (EI-DPN) - leaving them unset made SimBrief reset other fields once
    the pilot picked an aircraft manually. acdata (individual payload/
    performance overrides) and cargo are still left out: cargo's expected
    units aren't confirmed from public docs, and acdata is airframe
    minutiae that belongs to the SimBrief-side fleet entry for EI-DPN,
    not this app. deph/depm/taxiout/taxiin ARE passed - taxi time feeds
    directly into SimBrief's fuel planning, so leaving it out was
    producing a fuel figure the taxi time on our own SOBT/STOT/SLDT/SIBT
    table didn't match.
    """
    flights_raw = request.args.get("flights", default="", type=str)
    flight_numbers = [f.strip() for f in flights_raw.split(",") if f.strip()]
    if not flight_numbers:
        return jsonify({"error": "No flights specified."}), 400

    fn = flight_numbers[0]
    route_leg = routes_by_flight_number.get(fn)
    if route_leg is None:
        return jsonify({"error": f"Unknown flight number: {fn}"}), 400

    civalue = request.args.get("civalue", type=int)
    pax = request.args.get("pax", type=int)
    taxi_out = request.args.get("taxi_out_minutes", type=int)
    if civalue is None or pax is None or taxi_out is None:
        # Only hit when the caller doesn't already have confirmed leg
        # values (e.g. a preview, before CONFIRM has committed a
        # cost_index/pax_count/taxi_out_minutes). An already-confirmed
        # active leg's frontend call always supplies all three, so this
        # never re-rolls numbers the pilot has already seen and confirmed.
        conditions = generate_leg_conditions(duration_minutes=route_leg["duration_minutes"])
        if civalue is None:
            civalue = conditions["cost_index"]
        if pax is None:
            pax = conditions["pax_count"]
        if taxi_out is None:
            taxi_out = conditions["taxi_out_minutes"]

    params = {
        "orig": route_leg["departure_icao"],
        "dest": route_leg["arrival_icao"],
        "airline": SIMBRIEF_AIRLINE_ICAO,
        "fltnum": "".join(ch for ch in fn if ch.isdigit()),
        "date": simbrief_date_str(),
        "civalue": civalue,
        "pax": pax,
        "taxiout": taxi_out,
        "taxiin": TAXI_IN_MINUTES,
        "type": SIMBRIEF_AIRCRAFT_TYPE.get(route_leg.get("aircraft_type", "738"), "B738"),
        "reg": SIMBRIEF_AIRCRAFT_REG,
    }

    # SimBrief gets the EXPECTED (delay-adjusted) off-block time, not the
    # raw scheduled one - a flight scheduled off-block at 12:05Z with a
    # 15min delay allocation should feed SimBrief 12:20Z. The frontend
    # passes through the exact EOBT string /select already computed and
    # showed the pilot (leg.eobt, e.g. "12:20Z") - that figure also
    # accounts for the minimum-turnaround shift a second rotation leg
    # can get, which this route has no way to recompute on its own (it
    # doesn't know about the previous leg). Only if that's missing
    # (e.g. a pre-CONFIRM preview) does it fall back to recomputing SOBT
    # + delay_minutes here.
    eobt_raw = request.args.get("eobt", default="", type=str).strip()
    eobt_match = re.match(r"^(\d{2}):(\d{2})", eobt_raw)
    if eobt_match:
        params["deph"], params["depm"] = eobt_match.group(1), eobt_match.group(2)
    else:
        schedule = resolve_leg_schedule(route_leg, tz_by_icao, date.today(), taxi_out)
        sobt_dt = schedule["_sobt_dt"]
        if sobt_dt is not None:
            delay_minutes = request.args.get("delay_minutes", default=0, type=int)
            expected_dt = sobt_dt + timedelta(minutes=delay_minutes)
            params["deph"] = expected_dt.strftime("%H")
            params["depm"] = expected_dt.strftime("%M")

    # The confirmed callsign is a one-time random roll from /confirm, not
    # reproducible here - the frontend must pass through the exact one
    # already shown/confirmed for this leg. Left unset, SimBrief falls
    # back to airline+fltnum, which is a fine default too.
    callsign = request.args.get("callsign", default="", type=str).strip()
    if callsign:
        params["callsign"] = callsign

    static_id = request.args.get("static_id", default="", type=str).strip()
    if static_id:
        params["static_id"] = static_id

    return jsonify({"url": "https://www.simbrief.com/system/dispatch.php?" + urlencode(params)})


@app.route("/simbrief/ofp")
def simbrief_ofp():
    """
    Reads back the pilot's most recently generated OFP from SimBrief's
    public fetch-back endpoint (json=v2, no API key). This always returns
    whichever OFP is most recent for that SimBrief account - matching
    static_id (echoed back inside the OFP's params block) against the one
    this app sent to dispatch.php is how the frontend confirms it got the
    OFP it just asked for, not a stale one from an earlier session.
    """
    username = request.args.get("username", default="", type=str).strip()
    if not username:
        return jsonify({"error": "SimBrief username is required."}), 400

    try:
        resp = requests.get(
            "https://www.simbrief.com/api/xml.fetcher.php",
            params={"username": username, "json": "v2"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return jsonify({"error": "Could not reach SimBrief, or no OFP is on file for this username."}), 502

    if not isinstance(data, dict):
        return jsonify({"error": "Unexpected response from SimBrief."}), 502
    if str(data.get("fetch", {}).get("status", "")).lower().startswith("error"):
        return jsonify({"error": "SimBrief reported an error for this username - generate an OFP first."}), 502

    origin = data.get("origin", {})
    destination = data.get("destination", {})
    general = data.get("general", {})
    aircraft = data.get("aircraft", {})
    params_block = data.get("params", {})
    fuel = data.get("fuel", {})
    times = data.get("times", {})
    weights = data.get("weights", {})
    # Unit for every weight/fuel figure below - SimBrief reports these in
    # whichever unit the pilot's own profile is set to (not something
    # this app controls), so it's surfaced rather than assumed.
    weight_unit = general.get("units") or params_block.get("units") or "KG"

    return jsonify({
        "static_id": params_block.get("static_id", ""),
        "origin_icao": origin.get("icao_code", ""),
        "destination_icao": destination.get("icao_code", ""),
        "callsign": f"{general.get('icao_airline', '')}{general.get('flight_number', '')}",
        "route": general.get("route", ""),
        "cost_index": general.get("costindex", ""),
        "initial_altitude_ft": general.get("initial_altitude", ""),
        "registration": aircraft.get("reg", ""),
        "icao_type": aircraft.get("icaocode", ""),
        "weight_unit": weight_unit,
        "block_fuel": fuel.get("plan_ramp", ""),
        "est_time_enroute_sec": times.get("est_time_enroute", ""),
        "est_zfw": weights.get("est_zfw", ""),
        "est_tow": weights.get("est_tow", ""),
        # EFOB = Estimated Fuel On Board at block-off, i.e. block/ramp
        # fuel - the planning-stage figure shown alongside EZFW/ETOW.
        # Not to be confused with AFAD (Actual Fuel At Destination),
        # which the pilot enters themselves at PIREP time.
        "efob": fuel.get("plan_ramp", ""),
        "epax": weights.get("pax_count", ""),
    })


if __name__ == "__main__":
    app.run(debug=True, port=5000)
